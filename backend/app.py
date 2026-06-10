"""ccchat — multi-session, container-per-session pretty chat over interactive claude (subscription).
Identity is the app-native username/password account (userauth); the origin is reachable directly,
so app auth + the admin-approval gate are the only barrier.
"""
import asyncio
import datetime
import json
from contextlib import asynccontextmanager
import os
import re
import time
import traceback

import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import mounts_store, settings_store, userauth
from .manager import Manager, _email_slug, ttyd_credential
from .session import Session

userauth.ensure_default_admin()      # create admin with a random/env password on first start
userauth.assert_no_default_admin()   # refuse to run with the legacy admin/admin on a public origin

TTYD_PORT = 7681
# optional external whisper-stt service for high-quality transcription; the frontend falls back to
# the browser Web Speech API when this is unset or unreachable
STT_UPSTREAM = os.environ.get("STT_UPSTREAM", "").rstrip("/")

# Idle-session reaping: stop containers that have been inactive this long to free RAM/CPU (the
# workspace + transcript persist; the session restarts on the next message). 0 = auto-reaper off
# (the admin "Stop idle now" button still works). Interval = how often the background sweep runs.
IDLE_REAP_HOURS = float(os.environ.get("CCCHAT_IDLE_REAP_HOURS", "0") or 0)
IDLE_REAP_INTERVAL_MIN = float(os.environ.get("CCCHAT_IDLE_REAP_INTERVAL_MIN", "30") or 30)


async def _idle_reaper_loop():
    while True:
        await asyncio.sleep(IDLE_REAP_INTERVAL_MIN * 60)
        try:
            reaped = await _to_thread(mgr.reap_idle, int(IDLE_REAP_HOURS * 3600))
            for r in reaped:
                _teardown_live(r["sid"])
        except Exception:
            pass


@asynccontextmanager
async def _lifespan(_app):
    task = asyncio.create_task(_idle_reaper_loop()) if IDLE_REAP_HOURS > 0 else None
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(title="ccchat", lifespan=_lifespan)
STATIC_DIR = os.environ.get("STATIC_DIR", "/app/static")
mgr = Manager()


# ---- naive per-IP rate limiter (login/register brute-force + spam) ----
_rl: dict = {}


def _client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


def _rate_ok(key: str, limit: int, window: int) -> bool:
    now = time.time()
    q = [t for t in _rl.get(key, []) if now - t < window]
    if len(q) >= limit:
        _rl[key] = q
        return False
    q.append(now)
    _rl[key] = q
    return True


# ---- single security chokepoint: approval gate + CSRF ----
# Everything under /api/ and /term/ requires a logged-in, APPROVED account, EXCEPT the auth endpoints
# (login/register/logout/me/password, which manage the account itself) and the health probe. Putting
# it here means a newly-added endpoint is gated by default. Websockets enforce approval in-handler.
_GATE_EXEMPT = {"/healthz"}


@app.middleware("http")
async def _security_gate(request: Request, call_next):
    path = request.url.path
    if (path.startswith(("/api/", "/term/")) and not path.startswith("/api/auth/")
            and path not in _GATE_EXEMPT):
        user = userauth.parse_token(request.cookies.get(userauth.COOKIE_NAME) or "")
        if not user:
            return JSONResponse({"detail": "not logged in"}, status_code=401)
        if not userauth.is_approved(user):
            return JSONResponse({"detail": "account is pending admin approval"}, status_code=403)
    # CSRF: reject cross-site state-changing requests. Browsers send Sec-Fetch-Site; we only block an
    # explicit "cross-site" (header absent = non-browser client → allowed) — cheap defence-in-depth.
    if (request.method in ("POST", "PUT", "PATCH", "DELETE")
            and path.startswith(("/api/", "/term/"))
            and request.headers.get("sec-fetch-site") == "cross-site"):
        return JSONResponse({"detail": "cross-site request blocked"}, status_code=403)
    return await call_next(request)

# Lightweight debug log to a writable, host-visible file (./state is mounted rw). Read it on the
# host with: tail -f <env-dir>/state/upload-debug.log  — avoids touching the app's docker log.
DBG_LOG = os.environ.get("CCCHAT_DBG", "/state/upload-debug.log")
def _dbg(tag, **kw):
    try:
        line = datetime.datetime.now().isoformat(timespec="seconds") + " " + tag + " " + \
            json.dumps(kw, ensure_ascii=False, default=str)
        with open(DBG_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass

LIVE: dict[str, Session] = {}        # sid -> live Session (pty)
PUMP: dict[str, asyncio.Task] = {}

# READ-ONLY info commands: draw a TUI overlay (not in JSONL). We scrape it from the pane, show it
# in chat, and dismiss with Esc. Safe because they don't change state.
TUI_OVERLAY_COMMANDS = {"/help", "/cost", "/status", "/keybindings-help"}

# INTERACTIVE commands: open a dialog that needs a choice / changes state (/model Sonnet, etc.).
# Scrape+Esc would either cancel them or leave a half-entered dialog — so we don't run them in the
# pretty UI; we tell the user to use the terminal mode instead.
INTERACTIVE_COMMANDS = {"/model", "/config", "/agents", "/mcp",
                        "/permissions", "/resume", "/login", "/logout"}


def _cookie(header_val: str, name: str):
    """Pull one cookie value out of a Cookie header (no Request.cookies on raw ws)."""
    for part in (header_val or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


def current_user(request: Request) -> str:
    """The logged-in username from the signed auth cookie. 401 if not logged in."""
    token = request.cookies.get(userauth.COOKIE_NAME)
    user = userauth.parse_token(token or "")
    if not user:
        raise HTTPException(401, "not logged in")
    return user


def current_user_ws(ws: WebSocket):
    """Username from the auth cookie on the ws handshake, or None (handlers close the socket)."""
    token = _cookie(ws.headers.get("cookie"), userauth.COOKIE_NAME)
    return userauth.parse_token(token or "")


def _is_admin(username: str) -> bool:
    return userauth.is_admin(username)


def _require_approved(request: Request) -> str:
    """current_user(), but also 403 if the account hasn't been approved by an admin yet.
    Used to gate everything past the registration screen (session list/create, terminals)."""
    username = current_user(request)
    if not userauth.is_approved(username):
        raise HTTPException(403, "account is pending admin approval")
    return username


async def _to_thread(fn, *args, **kwargs):
    """Run a blocking (subprocess/docker/tmux-backed) call off the event loop, so one session's
    docker call doesn't stall every other session's websocket (review #7)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# ----- auth (app-native username/password; CF Access stays as the outer gate) -----
def _set_auth_cookie(resp: JSONResponse, username: str):
    resp.set_cookie(userauth.COOKIE_NAME, userauth.issue_token(username),
                    max_age=userauth.TOKEN_TTL, httponly=True, samesite="lax", secure=True, path="/")


@app.post("/api/auth/register")
async def auth_register(request: Request):
    if not _rate_ok("reg:" + _client_ip(request), 5, 3600):
        raise HTTPException(429, "too many registrations from this address — try again later")
    body = await request.json()
    try:
        username = userauth.register(body.get("username", ""), body.get("password", ""))
    except userauth.AuthError as e:
        raise HTTPException(400, str(e))
    resp = JSONResponse({"username": username, "is_admin": _is_admin(username),
                         "approved": userauth.is_approved(username)})
    _set_auth_cookie(resp, username)
    return resp


@app.post("/api/auth/login")
async def auth_login(request: Request):
    if not _rate_ok("login:" + _client_ip(request), 8, 60):
        raise HTTPException(429, "too many attempts — slow down")
    body = await request.json()
    username = (body.get("username") or "").strip()
    if not userauth.verify(username, body.get("password") or ""):
        raise HTTPException(401, "invalid username or password")
    resp = JSONResponse({"username": username, "is_admin": _is_admin(username),
                         "approved": userauth.is_approved(username)})
    _set_auth_cookie(resp, username)
    return resp


@app.post("/api/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(userauth.COOKIE_NAME, path="/")
    return resp


@app.post("/api/auth/password")
async def auth_password(request: Request):
    username = current_user(request)   # 401 if not logged in
    body = await request.json()
    try:
        userauth.set_password(username, body.get("old_password") or "", body.get("new_password") or "")
    except userauth.AuthError as e:
        raise HTTPException(400, str(e))
    # re-issue the cookie (token is unaffected by the password change, but refresh its TTL)
    resp = JSONResponse({"ok": True})
    _set_auth_cookie(resp, username)
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request):
    username = current_user(request)
    return {"username": username, "is_admin": _is_admin(username),
            "approved": userauth.is_approved(username)}


# ----- admin: user approval (gate the registration → app transition)
@app.get("/api/admin/users")
def admin_list_users(request: Request):
    if not _is_admin(current_user(request)):
        raise HTTPException(403, "admin only")
    return {"users": userauth.list_users()}


@app.post("/api/admin/users/{username}/approve")
async def admin_approve_user(username: str, request: Request):
    if not _is_admin(current_user(request)):
        raise HTTPException(403, "admin only")
    body = await request.json()
    try:
        approved = userauth.set_approved(username, bool(body.get("approved", True)))
    except userauth.AuthError as e:
        raise HTTPException(404, str(e))
    return {"username": username, "approved": approved}


# ----- admin: session overview + idle reaping (resource visibility / cleanup)
@app.get("/api/admin/sessions")
async def admin_sessions(request: Request):
    if not _is_admin(current_user(request)):
        raise HTTPException(403, "admin only")
    rows = await _to_thread(mgr.admin_overview)
    return {"sessions": rows,
            "reap": {"auto_hours": IDLE_REAP_HOURS, "interval_min": IDLE_REAP_INTERVAL_MIN}}


@app.post("/api/admin/sessions/{sid}/stop")
async def admin_stop_session(sid: str, request: Request):
    if not _is_admin(current_user(request)):
        raise HTTPException(403, "admin only")
    sess = mgr.find_any(sid)
    if not sess:
        raise HTTPException(404, "not found")
    _teardown_live(sid)
    await _to_thread(mgr.stop_container, sess)
    return {"status": await _to_thread(mgr.status, sess)}


@app.post("/api/admin/reap")
async def admin_reap(request: Request):
    if not _is_admin(current_user(request)):
        raise HTTPException(403, "admin only")
    body = await request.json()
    hours = float(body.get("idle_hours") or 0)
    if hours <= 0:
        raise HTTPException(400, "idle_hours must be > 0")
    dry = bool(body.get("dry_run"))
    reaped = await _to_thread(mgr.reap_idle, int(hours * 3600), dry)
    if not dry:
        for r in reaped:
            _teardown_live(r["sid"])   # kick any open ws so it reflects the now-stopped container
    return {"reaped": reaped, "dry_run": dry}


# ----- session CRUD (per user)
@app.get("/api/mounts")
def mounts(request: Request):
    # mounts a user may select at session creation: admin-only ones are hidden from non-admins (#3/#4)
    return {"available": mounts_store.visible_for(_is_admin(current_user(request)))}


@app.get("/api/admin/mounts")
def admin_get_mounts(request: Request):
    if not _is_admin(current_user(request)):
        raise HTTPException(403, "admin only")
    return {"mounts": mounts_store.all_mounts()}


@app.put("/api/admin/mounts")
async def admin_put_mounts(request: Request):
    if not _is_admin(current_user(request)):
        raise HTTPException(403, "admin only")
    body = await request.json()
    return {"mounts": mounts_store.replace(body.get("mounts") or [])}


@app.get("/api/settings")
def get_settings(request: Request):
    email = current_user(request)
    slug = _email_slug(email)
    view = settings_store.public_view(slug, _is_admin(email))
    # the owner may see/edit their own stored credentials (their own secret), pretty-printed
    creds = settings_store.get_credentials(slug)
    view["credentials_raw"] = json.dumps(creds, indent=2) if creds else ""
    return view


@app.put("/api/settings")
async def put_settings(request: Request):
    email = current_user(request)
    slug = _email_slug(email)
    body = await request.json()
    # setup-token path (preferred): a long-lived `claude setup-token`. Present (even empty to clear)
    # → store it and skip the credentials-blob path entirely.
    if "oauth_token" in body:
        try:
            settings_store.set_oauth_token(slug, body.get("oauth_token"))
        except settings_store.CredentialError as e:
            raise HTTPException(400, f"invalid token: {e}")
        return settings_store.public_view(slug, _is_admin(email))
    raw = body.get("credentials")
    if not raw:
        raise HTTPException(400, "credentials required (paste ~/.claude/.credentials.json)")
    try:
        settings_store.set_credentials(slug, raw)
    except settings_store.CredentialError as e:
        raise HTTPException(400, f"invalid credentials: {e}")
    # validate by minting/refreshing the per-user seed — immediate feedback for the user
    token_valid = await _to_thread(mgr.ensure_user_seed, slug)
    view = settings_store.public_view(slug, _is_admin(email))
    view["token_valid"] = bool(token_valid)
    return view


@app.post("/api/stt")
async def stt(request: Request):
    """Push-to-talk audio → laptop whisper-stt → transcribed text (best Russian quality). The
    frontend falls back to the browser Web Speech API when this returns an error."""
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty audio")
    ctype = request.headers.get("content-type", "") or "audio/webm"
    # whisper-stt expects multipart/form-data with an `audio` file field (not a raw body)
    ext = "webm" if "webm" in ctype else ("wav" if "wav" in ctype else
          "ogg" if "ogg" in ctype else "mp4" if "mp4" in ctype else "webm")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as cl:
            r = await cl.post(f"{STT_UPSTREAM}/transcribe",
                              files={"audio": (f"clip.{ext}", body, ctype)})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"stt upstream unreachable: {e.__class__.__name__}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/api/stt/health")
async def stt_health():
    try:
        async with httpx.AsyncClient(timeout=3.0) as cl:
            r = await cl.get(f"{STT_UPSTREAM}/health")
        return JSONResponse(r.json(), status_code=r.status_code)
    except httpx.HTTPError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.get("/api/vscode-host")
def vscode_host():
    # SSH host the user's local VS Code Remote-SSH should connect to (the VPS). The frontend builds
    # vscode://vscode-remote/ssh-remote+<host>/<host_ws> so it opens the session workspace directly.
    return {"host": os.environ.get("VSCODE_SSH_HOST", "")}


@app.get("/api/sessions")
def list_sessions(request: Request):
    return mgr.list(_require_approved(request))


@app.post("/api/sessions")
async def create_session(request: Request):
    body = await request.json()
    name = (body.get("name") or "Session").strip()
    mounts = body.get("mounts") or []
    email = _require_approved(request)
    if not settings_store.has_auth(_email_slug(email)):
        raise HTTPException(400, "Set your Claude credentials in Settings first")
    return await _to_thread(mgr.create, email, name, mounts,
                            body.get("cpus", 0), body.get("mem_mb", 0),
                            bool(body.get("restrict_egress", False)))


@app.patch("/api/sessions/{sid}")
async def edit_session(sid: str, request: Request):
    body = await request.json()
    email = current_user(request)
    name = body.get("name")
    mounts = body.get("mounts")   # None = leave mounts unchanged; recreates container if changed
    OPS_IN_FLIGHT.add(sid)        # update() may recreate the container (mounts/limits change)
    try:
        s = await _to_thread(mgr.update, email, sid, name, mounts,
                             body.get("cpus"), body.get("mem_mb"), body.get("restrict_egress"))
    finally:
        OPS_IN_FLIGHT.discard(sid)
    if not s:
        raise HTTPException(400, "bad name or not found")
    return s


# sids with a restart/recreate in flight. The terminal ws uses this to pick a close code for a
# container that is briefly "missing": op in flight -> 1012 (retry, it'll be back), otherwise ->
# 1000 (genuinely stopped; retrying would only loop docker work).
OPS_IN_FLIGHT: set = set()


def _teardown_live(sid: str):
    """Tear down the live pump for a session AND kick its open chat websockets.
    Called from sync endpoints (threadpool), so everything loop-bound goes through
    call_soon_threadsafe. Poisoning the subscriber queues with None makes each ws
    writer() exit -> the handler closes the socket -> the frontend's onclose
    auto-reconnect builds a fresh Session; without this, an open chat ws stays
    subscribed to the dead Session forever and never renders another event."""
    s = LIVE.pop(sid, None)
    if s:
        s.stop()
    t = PUMP.pop(sid, None)
    loop = t.get_loop() if t else None
    if t and loop:
        loop.call_soon_threadsafe(t.cancel)
    if s and loop:
        for q in list(getattr(s, "_subscribers", [])):
            try:
                loop.call_soon_threadsafe(q.put_nowait, None)   # writer() exits on None
            except Exception:
                pass


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str, request: Request):
    email = current_user(request)
    _teardown_live(sid)
    ok = mgr.delete(email, sid)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


def _live(email: str, sid: str) -> Session:
    """Get/create the Session wrapper WITHOUT starting the container — pump only tails the
    transcript from disk, so viewing history works even when the container is stopped."""
    # Verify ownership on EVERY call, including cache hits: the global LIVE cache is keyed by sid
    # alone, so without this a user who knows/guesses another user's sid could attach to their
    # live session straight from the cache (review #2).
    sess = mgr._find(email, sid)
    if not sess:
        return None
    s = LIVE.get(sid)
    if s is None:
        s = Session(sess, mgr.local_ws(sess), wiki=mgr.wiki_text(sess))
        s.start()
        # after a compaction, re-send the wiki so it survives the summary (Firefly-style)
        async def _resend_wiki():
            wiki = mgr.wiki_text(sess)
            if wiki:
                await asyncio.sleep(2)  # let the compaction settle
                await s.send_wiki(wiki)
        s.on_compact = _resend_wiki
        LIVE[sid] = s
    # Start the transcript pump only when there's a running event loop. _live() is called from
    # BOTH sync endpoints (/commands, /status) and the async ws handler; creating the task in a
    # sync context silently fails ("coroutine never awaited") and leaves the session with no
    # watchdog — so the "thinking" indicator never gets its done. Start/restart it lazily here.
    try:
        loop = asyncio.get_running_loop()
        t = PUMP.get(sid)
        if t is None or t.done():
            PUMP[sid] = loop.create_task(s.pump())
    except RuntimeError:
        pass  # no running loop (sync call) — the ws handler will start the pump
    return s


async def _ensure_up(email: str, sid: str):
    """Bring the session container + claude up. Wiki is auto-sent by ensure_claude on (re)start,
    not here — so it lands immediately when the session boots, before any user message."""
    sess = mgr._find(email, sid)
    if sess:
        await asyncio.get_event_loop().run_in_executor(None, mgr.ensure_running, sess)


@app.websocket("/ws/{sid}")
async def ws(websocket: WebSocket, sid: str):
    await websocket.accept()
    email = current_user_ws(websocket)
    if email is None or not userauth.is_approved(email):
        await websocket.send_json({"kind": "error", "text": "unauthenticated"})
        await websocket.close()
        return
    s = _live(email, sid)
    if s is None:
        await websocket.send_json({"kind": "error", "text": "session not found"})
        await websocket.close()
        return
    for ev in s.history():
        await websocket.send_json(ev)
    _ctx = s.last_context_tokens()   # persist the context-size indicator across reconnects/tab switches
    if _ctx:
        await websocket.send_json({"kind": "context", "tokens": _ctx})
    await websocket.send_json({"kind": "ready"})

    # reflect the CURRENT state on (re)connect: if claude is busy right now (e.g. you switched away
    # while it was working and came back), show the indicator immediately instead of waiting for the
    # next idle→busy transition.
    try:
        if await _to_thread(mgr.status, s.sess) == "running":
            pane = await asyncio.get_event_loop().run_in_executor(None, s._pane)
            if s._is_busy(pane):
                # already busy before we connected — we don't know when it started, so the UI
                # shows a plain "…" (no fake timer) until the next event arrives.
                await websocket.send_json({"kind": "busy", "ongoing": True})
            # replay a pending AskUserQuestion widget (not in the JSONL) so the choice buttons
            # survive reconnects / tab switches instead of being lost after pump emitted them once.
            chev = s._parse_choice(pane)
            if chev and chev["id"] not in s._answered_choices:
                await websocket.send_json(chev)
                s._seen_choices.add(chev["id"])
            # reflect a current blocking state (auth / rate-limit / trust) on connect
            br = s._classify_block(pane)
            if br:
                s._block_reason = br
                await websocket.send_json({"kind": "blocked", "reason": br})
    except Exception:
        pass

    q = s.subscribe()

    # bring the session up on open so the wiki is auto-loaded immediately (in the background,
    # so the websocket stays responsive while the container + claude boot)
    if await _to_thread(mgr.status, s.sess) != "running" or s._current_jsonl() is None:
        asyncio.create_task(_ensure_up(email, sid))

    async def reader():
        try:
            while True:
                msg = await websocket.receive_json()
                if msg.get("type") == "input":
                    text = (msg.get("text") or "").strip()
                    if text:
                        cmd = text.split()[0] if text.startswith("/") else ""
                        # bring the container up if it was stopped, then send
                        if await _to_thread(mgr.status, s.sess) != "running":
                            await websocket.send_json({"kind": "busy", "what": "Starting container…"})
                            await _ensure_up(email, sid)
                            await websocket.send_json({"kind": "container", "status": "running"})
                            await s.send_text(text)
                        elif cmd in TUI_OVERLAY_COMMANDS:
                            # read-only overlay (not in JSONL) — scrape from pane, show, dismiss.
                            await _to_thread(mgr.reseed_creds, s.sess, only_if_stale=True)
                            await websocket.send_json({"kind": "busy", "what": "Opening…"})
                            body = await s.run_tui_command(text)
                            await websocket.send_json({"kind": "assistant",
                                "text": f"```\n{body}\n```" if body else f"{cmd}: (no output)"})
                            await websocket.send_json({"kind": "done"})
                        elif cmd in INTERACTIVE_COMMANDS:
                            # interactive/state-changing — can't be driven from the pretty UI safely
                            await s.interrupt()   # ensure no half-open dialog eats the next message
                            await websocket.send_json({"kind": "info",
                                "text": f"{cmd} — interactive command, available in console mode (⌨ button in the header)."})
                            await websocket.send_json({"kind": "done"})
                        else:
                            # refresh the session's OAuth token from the live seed if it went stale
                            # mid-session (claude re-reads the file per request → no restart needed)
                            await _to_thread(mgr.reseed_creds, s.sess, only_if_stale=True)
                            await websocket.send_json({"kind": "busy"})
                            await s.send_text(text)
                elif msg.get("type") == "stop":
                    await s.interrupt()
                    await websocket.send_json({"kind": "done"})
                elif msg.get("type") == "choice_answer":
                    if msg.get("custom"):
                        await _to_thread(s.answer_custom, msg.get("id", ""), msg.get("custom"))
                    else:
                        await _to_thread(s.answer_choice, msg.get("id", ""), msg.get("answer", ""),
                                         multi=bool(msg.get("multi")))
                    await websocket.send_json({"kind": "busy"})  # tool unblocks, claude continues
        except WebSocketDisconnect:
            pass

    async def writer():
        # forward every event from the pump, INCLUDING kind:user — the JSONL transcript is the
        # single source of truth (the frontend no longer renders the user bubble optimistically),
        # so messages typed in terminal mode show up too, with no duplicates.
        try:
            while True:
                ev = await q.get()
                if ev is None:        # poison pill from _teardown_live: session replaced — drop the
                    break             # socket so the client reconnects to the fresh Session
                await websocket.send_json(ev)
        except Exception:
            pass

    rt = asyncio.create_task(reader())
    wt = asyncio.create_task(writer())
    _, pending = await asyncio.wait({rt, wt}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    s.unsubscribe(q)


@app.get("/api/sessions/{sid}/status")
def session_status(sid: str, request: Request):
    sess = mgr._find(current_user(request), sid)
    if not sess:
        raise HTTPException(404, "not found")
    return {"status": mgr.status(sess)}


@app.get("/api/sessions/{sid}/context")
def session_context(sid: str, request: Request):
    """Read-only view of the 4 context layers (Global/user/chat/wiki) for this session."""
    sess = mgr._find(current_user(request), sid)
    if not sess:
        raise HTTPException(404, "not found")
    return mgr.context_view(sess)


@app.post("/api/clog")
async def client_log(request: Request):
    """Receive a client-side debug event (from the browser) and append it to the debug log."""
    try:
        body = await request.json()
    except Exception:
        body = {"raw": (await request.body()).decode("utf-8", "replace")[:2000]}
    try:
        email = current_user(request)
    except Exception:
        email = "?"
    _dbg("client", email=email, body=body)
    return {"ok": True}


@app.post("/api/sessions/{sid}/upload")
async def upload_file(sid: str, request: Request, file: UploadFile = File(...)):
    """Save an uploaded file into the session workspace (mounted at /workspace in the container),
    so the user can attach it to a message and claude can Read it by path."""
    sess = mgr._find(current_user(request), sid)
    _dbg("upload.in", sid=sid, filename=getattr(file, "filename", None),
         content_type=getattr(file, "content_type", None), found=bool(sess))
    if not sess:
        _dbg("upload.404", sid=sid)
        raise HTTPException(404, "not found")
    try:
        updir = mgr.local_ws(sess) / "uploads"
        updir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-]", "_", os.path.basename(file.filename or "file"))[:120] or "file"
        dest = updir / safe
        stem, ext = os.path.splitext(safe)
        n = 1
        while dest.exists():
            dest = updir / f"{stem}_{n}{ext}"
            n += 1
        data = await file.read()
        dest.write_bytes(data)
        _dbg("upload.ok", sid=sid, dest=str(dest), name=dest.name, size=len(data))
        return {"path": f"/workspace/uploads/{dest.name}", "name": dest.name, "size": len(data)}
    except Exception as e:
        _dbg("upload.err", sid=sid, err=str(e), tb=traceback.format_exc())
        raise


@app.get("/api/sessions/{sid}/file")
def get_file(sid: str, p: str, request: Request):
    """Serve a file from the session's /workspace by path (for inline image previews etc).
    `p` is the in-container path like /workspace/uploads/x.png. Confined to the workspace tree."""
    sess = mgr._find(current_user(request), sid)
    if not sess:
        raise HTTPException(404, "not found")
    rel = p.replace("/workspace/", "", 1).lstrip("/")
    base = mgr.local_ws(sess).resolve()
    fp = (base / rel).resolve()
    if base not in fp.parents or not fp.is_file():     # block path traversal
        raise HTTPException(404, "not found")
    return FileResponse(str(fp))


@app.post("/api/sessions/{sid}/start")
def session_start(sid: str, request: Request):
    email = current_user(request)
    sess = mgr._find(email, sid)
    if not sess:
        raise HTTPException(404, "not found")
    if not settings_store.has_auth(_email_slug(email)):
        raise HTTPException(400, "Set your Claude credentials in Settings first")
    mgr.ensure_running(sess)
    return {"status": mgr.status(sess)}


@app.post("/api/sessions/{sid}/restart")
def session_restart(sid: str, request: Request):
    email = current_user(request)
    sess = mgr._find(email, sid)
    if not sess:
        raise HTTPException(404, "not found")
    if not settings_store.has_auth(_email_slug(email)):
        raise HTTPException(400, "Set your Claude credentials in Settings first")
    # tear down the live pump + container, then bring it back up and wait until claude is ready
    OPS_IN_FLIGHT.add(sid)
    try:
        _teardown_live(sid)
        mgr.stop_container(sess)
        mgr.ensure_running(sess)   # start + ensure_claude + ensure_ttyd — blocks until the prompt is ready
    finally:
        OPS_IN_FLIGHT.discard(sid)
    return {"status": mgr.status(sess)}


@app.get("/api/sessions/{sid}/version")
def session_version(sid: str, request: Request):
    sess = mgr._find(current_user(request), sid)
    if not sess:
        raise HTTPException(404, "not found")
    return mgr.version_info(sess)


@app.post("/api/sessions/{sid}/recreate")
def session_recreate(sid: str, request: Request):
    """Like /restart, but rm-f + run so the container picks up the current SESSION_IMAGE."""
    email = current_user(request)
    sess = mgr._find(email, sid)
    if not sess:
        raise HTTPException(404, "not found")
    if not settings_store.has_auth(_email_slug(email)):
        raise HTTPException(400, "Set your Claude credentials in Settings first")
    OPS_IN_FLIGHT.add(sid)
    try:
        _teardown_live(sid)
        mgr.recreate_container(sess)   # blocks until claude is ready in the new container
    finally:
        OPS_IN_FLIGHT.discard(sid)
    return {"status": mgr.status(sess)}


@app.post("/api/sessions/{sid}/stop")
def session_stop(sid: str, request: Request):
    sess = mgr._find(current_user(request), sid)
    if not sess:
        raise HTTPException(404, "not found")
    # stopping the container does NOT delete the transcript — viewing stays available
    _teardown_live(sid)
    mgr.stop_container(sess)
    return {"status": mgr.status(sess)}


@app.get("/api/sessions/{sid}/commands")
def session_commands(sid: str, request: Request):
    try:
        s = _live(current_user(request), sid)
        if s is None:
            return {"commands": []}
        return {"commands": s.commands()}
    except Exception:
        from .session import Session
        return {"commands": Session.BUILTIN}


# ---- terminal mode: proxy the session container's in-container ttyd (attached to tmux main) ----
async def _ttyd_ip(email: str, sid: str, theme: str = "dark") -> str | None:
    sess = mgr._find(email, sid)
    if not sess:
        return None
    loop = asyncio.get_event_loop()
    # The terminal path must NEVER power the container on: ttyd's client auto-reconnects (we close
    # 1001 on drops), so if this auto-started stopped containers, powering a session off while a
    # terminal iframe exists would just turn it back on in a loop. Lifecycle belongs to the chat
    # ws + the power/start endpoints; here we only ensure claude/ttyd inside a RUNNING container.
    if await loop.run_in_executor(None, mgr.status, sess) != "running":
        return None
    await loop.run_in_executor(None, mgr.ensure_running, sess)
    await loop.run_in_executor(None, lambda: mgr.ensure_ttyd(sess, theme))  # match UI theme
    return mgr.web_ip(sess) or None


@app.websocket("/term/{sid}/ws")
async def term_ws(ws: WebSocket, sid: str):
    await ws.accept(subprotocol="tty")           # ttyd speaks the "tty" subprotocol
    em = current_user_ws(ws)
    if em is None or not userauth.is_approved(em):
        await ws.close(); return
    ip = await _ttyd_ip(em, sid)
    if not ip:
        # restart/recreate in flight -> 1012 ("service restart"): the ttyd client keeps retrying
        # and re-attaches once the container is back. Genuinely stopped/missing -> clean 1000 stop.
        await ws.close(code=1012 if sid in OPS_IN_FLIGHT else 1000)
        return
    user, pwd = ttyd_credential(sid)   # ttyd now requires per-session basic auth (#6)
    connected = False
    try:
        async with websockets.connect(f"ws://{user}:{pwd}@{ip}:{TTYD_PORT}/ws",
                                      subprotocols=["tty"], open_timeout=8,
                                      max_size=None) as up:
            connected = True
            async def c2u():
                try:
                    while True:
                        m = await ws.receive_bytes()
                        await up.send(m)
                except Exception:
                    pass
            async def u2c():
                try:
                    async for m in up:
                        await ws.send_bytes(m if isinstance(m, bytes) else m.encode())
                except Exception:
                    pass
            t1 = asyncio.create_task(c2u()); t2 = asyncio.create_task(u2c())
            _, pend = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for t in pend:
                t.cancel()
    except Exception:
        pass
    try:
        # Close-code drives ttyd's client policy: non-1000 -> silent auto-reconnect, clean 1000 ->
        # it stops and shows "Press ⏎ to Reconnect". An ESTABLISHED link that dropped (container
        # restart/recreate, backend redeploy) is transient -> 1001 so the client re-attaches by
        # itself. A link that never came up (upstream connect failed, container down) gets 1000 —
        # retrying can't help and would loop docker work via _ttyd_ip forever.
        await ws.close(code=1001 if connected else 1000)
    except Exception:
        pass


@app.get("/term/{sid}")
@app.get("/term/{sid}/")
async def term_index(sid: str, request: Request):
    theme = "light" if request.query_params.get("theme") == "light" else "dark"
    ip = await _ttyd_ip(_require_approved(request), sid, theme)
    if not ip:
        raise HTTPException(404, "not found")
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.get(f"http://{ip}:{TTYD_PORT}/", auth=ttyd_credential(sid))
    # ttyd's index uses relative asset + ws URLs — inject <base> so they resolve under our prefix
    html = r.text.replace("<head>", f'<head><base href="/term/{sid}/">', 1)
    return Response(html, media_type="text/html")


@app.get("/term/{sid}/{path:path}")
async def term_asset(sid: str, path: str, request: Request):
    ip = await _ttyd_ip(_require_approved(request), sid)
    if not ip:
        raise HTTPException(404, "not found")
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.get(f"http://{ip}:{TTYD_PORT}/{path}", auth=ttyd_credential(sid))
    return Response(r.content, media_type=r.headers.get("content-type", "application/octet-stream"),
                    status_code=r.status_code)


@app.get("/healthz")
def healthz():
    return {"ok": True}


def _index():
    return FileResponse(f"{STATIC_DIR}/index.html",
                        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# client-side routes — serve the SPA shell so a refresh on these paths works
@app.get("/")
@app.get("/chats")
@app.get("/settings")
@app.get("/settings/{tab}")
def index(tab: str = ""):
    return _index()


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
