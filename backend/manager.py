"""Session manager: one Docker container per chat session.

Each session = a `claude-term` container with its own host workspace mounted, optionally extra
mounts (e.g. /machines). State is per-user (keyed by CF Access email) in a host-persisted JSON.
ccchat talks to the host docker daemon via the mounted /var/run/docker.sock.

Why a workspace mounted on the HOST: ccchat reads the session JSONL transcript directly from the
host path (no docker exec needed for output), and the session container writes there. Input goes
into the container's interactive claude via `docker exec` into a pty.
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import mounts_store, settings_store, userauth

STATE_FILE = Path(os.environ.get("CCCHAT_STATE", "/state/sessions.json"))
# Host-side root for per-session workspaces. IMPORTANT: this is a HOST path (the docker daemon
# resolves bind mounts against the host fs), passed in via env so it matches the real host dir.
HOST_WORK_ROOT = os.environ.get("HOST_WORK_ROOT", "/srv/vivarium/work")
# Where that same host dir is mounted INSIDE the ccchat container (so we can read JSONL).
LOCAL_WORK_ROOT = Path(os.environ.get("LOCAL_WORK_ROOT", "/work"))
SESSION_IMAGE = os.environ.get("SESSION_IMAGE", "claude-term:local")
# An OAuth access token with less than this left is treated as "stale" everywhere (seed minting,
# session reseeding, pre-launch warm-up) — keep the three in agreement or the 401 race returns.
FRESH_MARGIN_MS = 30 * 60 * 1000
# Session containers join a DEDICATED network (not the shared `web`) so they have internet egress
# but cannot reach the host's internal services (Caddy, Grafana, prod apps) or other users' sessions
# beyond ttyd (which is auth'd). ccchat itself must also be attached to this net to proxy ttyd.
SESSION_NET = os.environ.get("SESSION_NET", "ccchat-net")
# Sessions with "restrict egress" join an INTERNAL network (no direct internet) and reach the outside
# only through the allowlist proxy via HTTPS_PROXY — so they can hit Anthropic/GitHub/PyPI/npm and
# nothing else. ccchat is attached to this net too (for ttyd proxying).
RESTRICTED_NET = os.environ.get("SESSION_RESTRICTED_NET", "ccchat-restricted")
EGRESS_PROXY = os.environ.get("EGRESS_PROXY", "http://ccchat-egress:8888")

# host paths for the bits a session container needs
HOST_SSH = os.environ.get("HOST_SSH", "")
HOST_MACHINES = os.environ.get("HOST_MACHINES", "")
# NOTE: ccchat_ui_mcp.py is intentionally NOT mounted/wired — choices use claude's built-in
# AskUserQuestion widget (parsed from the tmux pane), not an MCP server (review #12).

# admins may attach credential-bearing mounts (host SSH keys, the /machines fleet reference);
# normal users may not (review #3, #4). Comma-separated CF Access emails.
def _is_admin(username: str) -> bool:
    # single source of truth for admin status (includes the default "admin" + CCCHAT_ADMINS)
    return userauth.is_admin(username)


# Optional mounts are now admin-configurable — see backend/mounts_store.py (was a hardcoded dict).
# Each mount carries an admin_only flag enforced in _allowed_mounts (review #3, #4).

_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,60}$")

# Context md files (ccchat's local view; under work/ so they're git-backed):
#   context/<email_slug>/global.md      -> system prompt for ALL the user's sessions
#   context/<email_slug>/<sid>/chat.md  -> system prompt for THIS session
#   context/<email_slug>/<sid>/wiki.md  -> first message on start + re-sent after each compact
CONTEXT_ROOT = LOCAL_WORK_ROOT / "context"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _email_slug(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (email or "anon").lower()).strip("_") or "anon"


def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _docker(*args, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _ttyd_secret() -> str:
    """Server-side secret used to derive per-session ttyd credentials. From CCCHAT_TTYD_SECRET,
    else a persisted random file next to the state file."""
    env = os.environ.get("CCCHAT_TTYD_SECRET")
    if env:
        return env
    f = STATE_FILE.parent / ".ttyd_secret"
    try:
        if f.exists():
            return f.read_text().strip()
        s = uuid.uuid4().hex
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(s)
        f.chmod(0o600)
        return s
    except Exception:
        return "ccchat-ttyd-fallback"


def ttyd_credential(sid: str):
    """Per-session basic-auth credentials for ttyd (review #6). Deterministic from a server-side
    secret + sid, so the proxy and the ttyd launch agree without persisting per session. This stops
    any other container on the shared `web` network from opening an unauthenticated root shell."""
    pwd = hmac.new(_ttyd_secret().encode(), (sid or "").encode(), hashlib.sha256).hexdigest()[:24]
    return "ccchat", pwd


def code_token(sid: str) -> str:
    """Per-session OpenVSCode connection token (same trust model as ttyd_credential). The proxy
    injects it on every upstream request so the browser never sees it; another container on the
    shared net hitting the editor port directly lacks it and is rejected."""
    return hmac.new(_ttyd_secret().encode(), ("code:" + (sid or "")).encode(),
                    hashlib.sha256).hexdigest()[:32]


class Manager:
    def __init__(self):
        self._cli_versions = {}   # image id -> Claude CLI version (exec'd once per image)
        # Per-container locks serialising lifecycle ops (start/stop/recreate). Endpoints run in the
        # FastAPI threadpool and the chat/terminal websockets can call ensure_running concurrently
        # with an in-flight /restart or /recreate — without this, two threads interleave
        # `docker rm -f` + `docker run` on the same --name (one run silently fails, or the rm
        # deletes the container the other just created). RLock: recreate_container/ensure_running
        # nest onto _start_container under the same lock.
        self._oplocks: dict = {}
        self._oplocks_guard = threading.Lock()
        # backfill the per-user id->name index for any sessions that predate this feature
        try:
            for key in _load().keys():
                self._write_session_index(key)
        except Exception:
            pass

    def _oplock(self, sess: dict) -> threading.RLock:
        """The lifecycle lock for this session's container (created on first use)."""
        with self._oplocks_guard:
            return self._oplocks.setdefault(sess["container"], threading.RLock())

    def list(self, email: str) -> list:
        d = _load()
        return d.get(_email_slug(email), [])

    def _find(self, email: str, sid: str):
        for s in self.list(email):
            if s["id"] == sid:
                return s
        return None

    @staticmethod
    def _allowed_mounts(email: str, requested) -> list:
        """Keep only known mounts the requester may use. Admin-only mounts (host SSH keys, the
        /machines fleet reference) are dropped for non-admins so a normal user/LLM session can't
        attach host credentials to its container (review #3, #4)."""
        out = []
        for e in Manager._mount_entries(requested):
            spec = mounts_store.get(e["name"])
            if not spec:
                continue
            if spec.get("admin_only") and not _is_admin(email):
                continue
            out.append({"name": e["name"], "read_only": e["read_only"]})
        return out

    @staticmethod
    def _mount_entries(mounts):
        """Normalise a session's mounts to [{name, read_only}], accepting legacy list-of-names too
        (those default to read_only=True). The per-session read-only flag is chosen at create time."""
        out = []
        for m in (mounts or []):
            if isinstance(m, dict) and m.get("name"):
                out.append({"name": m["name"], "read_only": bool(m.get("read_only", True))})
            elif isinstance(m, str) and m:
                out.append({"name": m, "read_only": True})
        return out

    @staticmethod
    def _slug(sess: dict) -> str:
        """The owner's email slug for this session (stored at create; parsed for legacy sessions)."""
        u = sess.get("user")
        if u:
            return u
        c = sess.get("container", "")
        inner = c[4:] if c.startswith("ccs_") else c
        return inner[:inner.rfind("_")] if "_" in inner else "anon"

    # ---- per-user Claude seed (review #5): each user's sessions are seeded from THEIR refresh
    # token, not one shared token. The user's refresh token bootstraps a per-user seed which then
    # self-rotates (Claude rotates the refresh token on use), so the seed is the live source.
    def _user_seed_local(self, slug: str) -> Path:
        return LOCAL_WORK_ROOT / ".seeds" / slug / ".chome"

    def _user_seed_host(self, slug: str) -> str:
        return f"{HOST_WORK_ROOT}/.seeds/{slug}/.chome"

    def _mint_seed(self, slug: str, only_if_stale: bool = True) -> None:
        """Refresh the per-user seed's access token by running a throwaway `claude -p ok` with that
        seed as HOME (Claude refreshes using the seed's refresh token)."""
        cred = self._user_seed_local(slug) / ".credentials.json"
        oauth = self._oauth_block(cred)
        if (only_if_stale and oauth.get("accessToken")
                and oauth.get("expiresAt", 0) - time.time() * 1000 > FRESH_MARGIN_MS):
            return
        _docker("run", "--rm", "-e", "IS_SANDBOX=1",
                "-v", f"{self._user_seed_host(slug)}:/root/.claude", "-w", "/root", SESSION_IMAGE,
                "claude", "-p", "ok", "--dangerously-skip-permissions", timeout=120)
        for sub in ("projects", "todos"):
            shutil.rmtree(self._user_seed_local(slug) / sub, ignore_errors=True)

    def ensure_user_seed(self, slug: str) -> bool:
        """Bootstrap the per-user seed from the user's stored credentials blob, then keep it fresh.
        The seed is the single owner that refreshes (rotates) the refresh token; sessions get
        access-only copies. Returns True if the seed holds a usable access token. No shared fallback
        — no credentials means no seed (#5).

        We bootstrap from the FULL blob (access+refresh): a refresh token alone can't bootstrap
        claude. The marker tracks the refresh token so a re-entered credential re-seeds."""
        creds = settings_store.get_credentials(slug)
        if not creds:
            return False
        rt = (creds.get("claudeAiOauth") or {}).get("refreshToken") or ""
        if not rt:
            return False
        seed = self._user_seed_local(slug)
        seed.mkdir(parents=True, exist_ok=True)
        cred = seed / ".credentials.json"
        marker = seed / ".src"
        rt_hash = hashlib.sha256(rt.encode()).hexdigest()
        bootstrapped = marker.read_text().strip() if marker.exists() else ""
        if (not cred.exists()) or bootstrapped != rt_hash:
            # (re)bootstrap with the full blob; claude refreshes it natively from here on
            cred.write_text(json.dumps(creds))
            cred.chmod(0o600)
            marker.write_text(rt_hash)
        self._mint_seed(slug)
        try:
            return bool(json.loads(cred.read_text()).get("claudeAiOauth", {}).get("accessToken"))
        except Exception:
            return False

    @staticmethod
    def _norm_cpus(v):
        try:
            return max(0.0, round(float(v or 0), 2))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _norm_mem(v):
        try:
            return max(0, int(float(v or 0)))
        except (TypeError, ValueError):
            return 0

    def create(self, email: str, name: str, mounts: list, cpus=0, mem_mb=0, restrict_egress=False) -> dict:
        d = _load()
        key = _email_slug(email)
        sid = uuid.uuid4().hex[:12]
        cname = f"ccs_{key}_{sid}"
        host_ws = f"{HOST_WORK_ROOT}/{key}/{sid}"
        # create the host workspace dir (via our own mounted view of it)
        (LOCAL_WORK_ROOT / key / sid).mkdir(parents=True, exist_ok=True)

        sess = {
            "id": sid, "name": (name or "Session").strip()[:60],
            "container": cname, "host_ws": host_ws, "user": key,
            "mounts": self._allowed_mounts(email, mounts),
            "cpus": self._norm_cpus(cpus), "mem_mb": self._norm_mem(mem_mb),
            "restrict_egress": bool(restrict_egress),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        # create empty per-session context files (chat.md/wiki.md start empty — wiki.md is only
        # sent as the first message when it's non-empty)
        _, _uc, chat_md, wiki_md = self._ctx_paths(sess)
        for f in (chat_md, wiki_md):
            if not f.exists():
                f.write_text("", encoding="utf-8")
        self._start_container(sess)
        d.setdefault(key, []).append(sess)
        _save(d)
        self._write_session_index(key)
        return sess

    def _start_container(self, sess: dict):
        cname = sess["container"]
        # remove stale container with same name if any
        _docker("rm", "-f", cname)
        slug = self._slug(sess)
        # Auth path A: a long-lived `claude setup-token` (CLAUDE_CODE_OAUTH_TOKEN) — injected as env,
        # no per-user seed / refresh dance. Path B (no token): seed .credentials.json from the user's
        # self-rotating per-user seed (review #5).
        oauth_token = settings_store.get_oauth_token(slug)
        seed = self._user_seed_local(slug)
        home_local = self.local_ws(sess) / ".chome"
        home_local.mkdir(parents=True, exist_ok=True)
        # OpenVSCode extensions + settings, PER-USER (shared across all the user's sessions, isolated
        # between users) so they survive container recreate and follow the user across chats.
        ovsc_local = self.local_ws(sess).parent / ".ovsc"
        ovsc_local.mkdir(parents=True, exist_ok=True)
        if not oauth_token:
            self.ensure_user_seed(slug)
            for fn in (".credentials.json",):
                src = seed / fn
                dst = home_local / fn
                if src.exists() and not dst.exists():
                    _d = json.loads(src.read_text())
                    if "claudeAiOauth" in _d:
                        _d["claudeAiOauth"]["refreshToken"] = ""
                    dst.write_text(json.dumps(_d)); dst.chmod(0o600)
        # .claude.json (account binding) sits next to HOME; seed it as a FILE in the workspace
        cj_src = seed / ".claude.json"
        cj_local = self.local_ws(sess) / ".claude.json"
        if not cj_local.exists():
            try:
                cfg = json.loads(cj_src.read_text(encoding="utf-8")) if cj_src.exists() else {}
            except Exception:
                cfg = {}
            # pre-trust /workspace so claude doesn't show the trust dialog (which eats msg #1)
            cfg.setdefault("projects", {})["/workspace"] = {
                "hasTrustDialogAccepted": True, "projectOnboardingSeenCount": 1, "allowedTools": []}
            # pre-accept the Bypass Permissions warning so its confirm screen doesn't eat msg #1
            cfg["bypassPermissionsModeAccepted"] = True
            # mark onboarding done + pick a theme so the first-run wizard (theme → login → OAuth)
            # never appears (it would eat msg #1 and could push the session into a stuck OAuth flow)
            cfg["hasCompletedOnboarding"] = True
            cfg["theme"] = "dark"
            cj_local.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            cj_local.chmod(0o600)
        host_home = f"{sess['host_ws']}/.chome"
        host_cj = f"{sess['host_ws']}/.claude.json"
        # make sure the editable context files exist before we bind-mount them (mounting a missing
        # file would make docker create a directory in its place). Global.md is NOT mounted.
        _glob, uc, c, w = self._ctx_paths(sess)
        for f in (uc, c, w):
            if not f.exists():
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("", encoding="utf-8")
        # host paths for the editable context files (docker daemon resolves these against host fs)
        rel = sess["host_ws"].replace(HOST_WORK_ROOT, "").lstrip("/")          # <key>/<sid>
        key = rel.split("/")[0]
        host_uc = f"{HOST_WORK_ROOT}/context/{key}/user.md"
        host_chat = f"{HOST_WORK_ROOT}/context/{rel}/chat.md"
        host_wiki = f"{HOST_WORK_ROOT}/context/{rel}/wiki.md"
        host_ovsc = f"{os.path.dirname(sess['host_ws'])}/.ovsc"   # <work>/<user>/.ovsc (per-user)
        vols = [
            "-v", f"{host_home}:/root/.claude",
            "-v", f"{host_cj}:/root/.claude.json",
            "-v", f"{host_ovsc}:/root/.openvscode-server",
            # NOTE: host SSH keys are NO LONGER mounted into every session (review #3). Fleet access
            # is now an admin-only opt-in mount ("ssh"/"machines"), attached below if requested.
            "-v", f"{sess['host_ws']}:/workspace",
            # editable context files: user.md = all your sessions; chat/wiki = this session.
            # (the shared Global.md is intentionally NOT mounted — admin-only)
            "-v", f"{host_uc}:/workspace/.context/user.md",
            "-v", f"{host_chat}:/workspace/.context/chat.md",
            "-v", f"{host_wiki}:/workspace/.context/wiki.md",
        ]
        for e in self._mount_entries(sess.get("mounts")):
            spec = mounts_store.get(e["name"])
            if not spec:
                continue
            ro = ":ro" if e["read_only"] else ""          # per-session choice (default read-only)
            host = mounts_store.expand_path(spec["path"], self._slug(sess), HOST_WORK_ROOT)
            vols += ["-v", f"{host}:{mounts_store.dest_of(spec)}{ro}"]
        # optional resource limits (0 = unlimited)
        limits = []
        cpus = self._norm_cpus(sess.get("cpus"))
        if cpus > 0:
            limits += ["--cpus", str(cpus)]
        mem = self._norm_mem(sess.get("mem_mb"))
        if mem > 0:
            limits += ["--memory", f"{mem}m"]
        # network: restricted sessions go on the internal net and route egress through the allowlist
        # proxy (HTTPS_PROXY); normal sessions go on the dedicated net with direct internet.
        net = self._session_net(sess)
        env = ["-e", "IS_SANDBOX=1", "-e", "HOME=/root"]
        if oauth_token:
            env += ["-e", f"CLAUDE_CODE_OAUTH_TOKEN={oauth_token}"]
        if sess.get("restrict_egress"):
            for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                env += ["-e", f"{k}={EGRESS_PROXY}"]
            env += ["-e", "NO_PROXY=localhost,127.0.0.1", "-e", "no_proxy=localhost,127.0.0.1"]
        # long-lived container; we exec claude into it on demand.
        _docker("run", "-d", "--name", cname, "--restart", "unless-stopped",
                "--network", net, *limits, *env, "-w", "/workspace",
                *vols, SESSION_IMAGE, "sleep", "infinity")

    def status(self, sess: dict) -> str:
        """'running' | 'stopped' | 'missing' — independent of whether we read its transcript."""
        r = _docker("inspect", "-f", "{{.State.Running}}", sess["container"])
        if r.returncode != 0:
            return "missing"
        return "running" if r.stdout.strip() == "true" else "stopped"

    def ensure_running(self, sess: dict) -> bool:
        with self._oplock(sess):
            st = self.status(sess)
            if st == "missing":
                self._start_container(sess)
            elif st == "stopped":
                _docker("start", sess["container"])
            self.ensure_claude(sess)
            self.ensure_ttyd(sess)
            return True

    # xterm themes matching the chat UI (Claude Code palette), set at ttyd launch
    _TTYD_THEME = {
        "dark":  '{"background":"#1f1e1d","foreground":"#e8e6e3","cursor":"#c96442"}',
        "light": '{"background":"#faf9f5","foreground":"#1f1e1d","cursor":"#c96442"}',
    }

    CODE_PORT = 3100

    def ensure_code(self, sess: dict) -> bool:
        """Launch OpenVSCode Server in the session container (lazily), serving /workspace under the
        /code/<sid> base path. Returns True once it answers. Returns False fast if the image predates
        the editor (a session created before this feature — needs a recreate) so the proxy can show
        a helpful page instead of blocking ~24s then 502-ing."""
        c = sess["container"]; sid = sess["id"]
        if "y" not in (_docker("exec", c, "sh", "-c",
                       "test -x /opt/openvscode-server/bin/openvscode-server && echo y || echo n").stdout or ""):
            return False                             # old image, no editor — recreate needed
        # "is it up?" = does the port answer (any HTTP code, incl. 401 for the tokenless probe).
        # Checking the port avoids the pgrep -f self-match trap (the check process matches its own
        # command line). The base path is fixed per container (sid never changes), so no staleness.
        def listening():
            r = _docker("exec", c, "sh", "-c",
                        f"curl -s -o /dev/null -w '%{{http_code}}' "
                        f"http://127.0.0.1:{self.CODE_PORT}/code/{sid}/ 2>/dev/null || true")
            return (r.stdout or "").strip() not in ("", "000")
        if listening():
            return True
        tok = code_token(sid)
        _docker("exec", "-d", c, "sh", "-lc",
                f"/opt/openvscode-server/bin/openvscode-server --host 0.0.0.0 "
                f"--port {self.CODE_PORT} --server-base-path /code/{sid} "
                f"--connection-token {tok} >/tmp/code.log 2>&1")
        for _ in range(40):                          # wait until it's listening (cold boot ~3-8s)
            if listening():
                return True
            time.sleep(0.4)
        return False

    def ensure_ttyd(self, sess: dict, theme: str = "dark"):
        """Run ttyd inside the session container, attached to the SAME tmux 'main' that claude runs
        in. ttyd's xterm theme is baked at launch, so if a different theme is requested we relaunch
        it (the slim image lacks pgrep/ps/pkill — use pidof + kill)."""
        c = sess["container"]
        theme = theme if theme in self._TTYD_THEME else "dark"
        up = "up" in (_docker("exec", c, "sh", "-c",
                              "pidof ttyd >/dev/null 2>&1 && echo up || echo down").stdout or "")
        # remember which theme the running ttyd was started with (marker file)
        cur = (_docker("exec", c, "sh", "-c", "cat /tmp/.ttyd_theme 2>/dev/null").stdout or "").strip()
        if up and cur == theme:
            return
        if up:                                   # wrong theme — kill so we relaunch
            _docker("exec", c, "sh", "-c", "kill $(pidof ttyd) 2>/dev/null; sleep 0.3")
        _docker("exec", c, "sh", "-c", f"echo {theme} > /tmp/.ttyd_theme")
        # -W writable; bind on the web net but require per-session basic auth (review #6) so another
        # container on the shared network can't open an unauthenticated root shell. The proxy
        # (app.term_*) supplies the same credential derived from sid.
        user, pwd = ttyd_credential(sess["id"])
        _docker("exec", "-d", c, "ttyd", "-p", "7681", "-i", "0.0.0.0", "-W",
                "-c", f"{user}:{pwd}",
                "-t", f"theme={self._TTYD_THEME[theme]}",
                "tmux", "attach", "-t", "main")

    @staticmethod
    def _session_net(sess: dict) -> str:
        return RESTRICTED_NET if sess.get("restrict_egress") else SESSION_NET

    def web_ip(self, sess: dict) -> str:
        """The session container's IP on its session network (for ttyd proxying)."""
        r = _docker("inspect", "-f",
                    '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % self._session_net(sess),
                    sess["container"])
        return (r.stdout or "").strip()

    def stop_container(self, sess: dict):
        with self._oplock(sess):
            _docker("stop", sess["container"])

    def version_info(self, sess: dict) -> dict:
        """Claude CLI version of the session container + whether it was created from the current
        SESSION_IMAGE. 'current' compares image IDs, not CLI versions — an image rebuild with the
        same CLI (env/config changes) still counts as outdated. Never raises: this sits on the
        session-open hot path, and a wedged container must not 500 it or pin a worker for long."""
        try:
            r = _docker("inspect", "-f", "{{.Image}} {{.State.Running}}", sess["container"], timeout=10)
            if r.returncode != 0:
                return {"cli": None, "current": None}     # container missing — nothing to compare
            cimg, _, running = r.stdout.strip().partition(" ")
            ri = _docker("image", "inspect", "-f", "{{.Id}}", SESSION_IMAGE, timeout=10)
            cur_img = ri.stdout.strip() if ri.returncode == 0 else None
            cli = self._cli_versions.get(cimg)
            if cli is None and running == "true":
                rv = _docker("exec", sess["container"], "claude", "--version", timeout=10)
                if rv.returncode == 0 and rv.stdout.strip():
                    cli = rv.stdout.strip().split()[0]
                    self._cli_versions[cimg] = cli
            return {"cli": cli, "current": (cimg == cur_img) if cur_img else None}
        except Exception:
            return {"cli": None, "current": None}

    def recreate_container(self, sess: dict):
        """Tear down the container and re-run it from the current SESSION_IMAGE (workspace,
        transcript and credentials persist on the bind mounts). Blocks until claude is ready."""
        with self._oplock(sess):
            self._start_container(sess)   # does rm -f first
            self.ensure_claude(sess)
            self.ensure_ttyd(sess)

    # ---- admin overview + idle reaping --------------------------------------
    def find_any(self, sid: str):
        """Find a session by id across ALL users (admin scope)."""
        for sessions in _load().values():
            for s in sessions:
                if s["id"] == sid:
                    return s
        return None

    def _container_states(self) -> dict:
        """container name -> docker state ('running','exited',…) for ALL containers, one call.
        A name absent from the map means the container doesn't exist ('missing')."""
        r = _docker("ps", "-a", "--format", "{{.Names}}\t{{.State}}", timeout=15)
        out = {}
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                name, _, state = line.partition("\t")
                if name:
                    out[name] = state.strip()
        return out

    @staticmethod
    def _parse_mem_mb(tok: str) -> float:
        m = re.match(r"([0-9.]+)\s*([A-Za-z]+)", tok.strip())
        if not m:
            return 0.0
        mul = {"b": 1 / 1048576, "kib": 1 / 1024, "kb": 1 / 1024, "mib": 1, "mb": 1,
               "gib": 1024, "gb": 1024}.get(m.group(2).lower(), 1)
        return round(float(m.group(1)) * mul, 1)

    def docker_stats(self) -> dict:
        """container name -> {'mem_mb','cpu_pct'} for RUNNING containers, in one `docker stats` call
        (a per-container call would be N docker round-trips on the admin hot path)."""
        r = _docker("stats", "--no-stream", "--no-trunc",
                    "--format", "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}", timeout=25)
        out = {}
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) != 3:
                    continue
                name, mem, cpu = parts
                try:
                    out[name] = {"mem_mb": self._parse_mem_mb(mem.split("/")[0]),
                                 "cpu_pct": round(float(cpu.strip().rstrip("%") or 0), 1)}
                except Exception:
                    pass
        return out

    def _last_activity(self, sess: dict) -> float:
        """Newest transcript mtime (epoch secs) for this session, 0 if none. Claude appends to the
        JSONL as it works (tool calls, results), so this tracks real activity, not just user input."""
        try:
            proj = self.local_ws(sess) / ".chome" / "projects"
            return max((p.stat().st_mtime for p in proj.rglob("*.jsonl")), default=0)
        except Exception:
            return 0

    def _pane_busy(self, sess: dict) -> bool:
        """True if claude is actively working right now (pane shows the interrupt hint). Used as a
        safety guard so reaping never stops a session mid-task even if its transcript looks idle."""
        r = _docker("exec", sess["container"], "tmux", "capture-pane", "-t", "main", "-p", timeout=8)
        return "esc to interrupt" in (r.stdout or "").lower()

    def admin_overview(self) -> list:
        """Every session across all users with live status + resource usage + idle time. Two docker
        calls total (ps -a, stats) plus cheap local mtime reads — independent of session count."""
        states = self.docker_stats_and_states()
        stats, st_map = states["stats"], states["states"]
        now = time.time()
        rows = []
        for slug, sessions in _load().items():
            for s in sessions:
                c = s["container"]
                state = st_map.get(c)
                status = "running" if state == "running" else ("missing" if state is None else "stopped")
                la = self._last_activity(s)
                u = stats.get(c, {})
                rows.append({
                    "sid": s["id"], "name": s.get("name") or s["id"][:8], "user": slug,
                    "status": status,
                    "mem_mb": u.get("mem_mb") if status == "running" else None,
                    "cpu_pct": u.get("cpu_pct") if status == "running" else None,
                    "idle_secs": int(now - la) if la else None,
                })
        rows.sort(key=lambda r: (r["status"] != "running", -(r["mem_mb"] or 0)))
        return rows

    def docker_stats_and_states(self) -> dict:
        return {"stats": self.docker_stats(), "states": self._container_states()}

    def reap_idle(self, idle_secs: int, dry_run: bool = False) -> list:
        """Stop (never delete) RUNNING session containers idle longer than idle_secs and not busy.
        Stopping frees RAM/CPU; the workspace + transcript persist and the session restarts on the
        next message. Returns the affected sessions (with the idle time at reap)."""
        states = self._container_states()
        now = time.time()
        reaped = []
        for slug, sessions in _load().items():
            for s in sessions:
                if states.get(s["container"]) != "running":
                    continue
                la = self._last_activity(s)
                idle = now - la if la else None
                if idle is None or idle < idle_secs:
                    continue
                if self._pane_busy(s):           # never reap a session mid-task
                    continue
                if not dry_run:
                    self.stop_container(s)
                reaped.append({"sid": s["id"], "name": s.get("name") or s["id"][:8],
                               "user": slug, "idle_secs": int(idle)})
        return reaped

    # ---- context (md layers) -------------------------------------------------
    def _ctx_paths(self, sess: dict):
        """(Global.md, user.md, chat.md, wiki.md) for this session.
        - Global.md      : shared by ALL users, injected into every session (admin-only, not mounted)
        - user.md : per-user persona/preferences (mounted, claude-editable)
        - chat.md        : per-session extra system context (mounted, claude-editable)
        - wiki.md        : per-session durable scratchpad, first message (mounted, claude-editable)"""
        rel = sess["host_ws"].replace(HOST_WORK_ROOT, "").lstrip("/")  # <key>/<sid>
        key = rel.split("/")[0]
        cdir = CONTEXT_ROOT / rel
        cdir.mkdir(parents=True, exist_ok=True)
        return (CONTEXT_ROOT / "Global.md", CONTEXT_ROOT / key / "user.md",
                cdir / "chat.md", cdir / "wiki.md")

    # always-on hint so claude prefers the buttons tool when offering choices
    UI_HINT = ("# UI capability\n"
               "When you would offer the user a set of options to pick from, use the built-in "
               "AskUserQuestion tool — this UI renders it as real clickable buttons. Prefer it over "
               "writing a numbered list in prose. For normal prose answers, just reply as usual.")

    # always-on hint so claude knows it can reply with images (the UI auto-embeds /workspace paths)
    IMAGE_HINT = ("# Replying with images\n"
                  "You can show images in your replies. To display an image from this workspace, write "
                  "its BARE absolute path on its own line (e.g. /workspace/chart.png) — the UI embeds it "
                  "inline automatically. Do NOT wrap a /workspace path in Markdown (that double-wraps). "
                  "Supported: png, jpg, gif, webp, svg, bmp. You cannot generate an image from a text "
                  "prompt, but you CAN create one with code — a matplotlib chart, a PIL drawing, or an "
                  ".svg you write directly — then reference its path. For images already on the public "
                  "web, a normal Markdown image works: ![alt](https://...). Only reference files that exist.")

    # note about the editable context files (part of the baked base prompt)
    FILES_NOTE = (
        "# Your context files (editable)\n"
        "These markdown files live at /workspace/.context/ and shape your behaviour — you may "
        "read and edit them directly:\n"
        "- `user.md` — the user's persona/preferences across ALL their chat sessions "
        "(edits affect every session of theirs).\n"
        "- `chat.md` — extra system-prompt context for THIS session only.\n"
        "- `wiki.md` — notes auto-sent as the first message on session start and re-sent after "
        "/clear or a compaction (a durable scratchpad that survives context resets). Keep it "
        "concise. Changes to user.md/chat.md take effect on the next session (re)start.\n"
        "(There is also a shared Global.md above that you cannot edit — it's managed centrally.)")

    def _mount_blurbs(self, sess: dict):
        return [s["description"] for s in
                (mounts_store.get(e["name"]) for e in self._mount_entries(sess.get("mounts")))
                if s and s.get("description")]

    def base_prompt(self, sess: dict) -> str:
        """The static system-baked part of the prompt (UI/image hints + the editable-files note).
        Shown as base.md in the viewer. Mount blurbs live in their own `mounts` view; Global/user/chat
        have their own tabs — so base.md doesn't duplicate any of them."""
        return "\n\n".join([self.UI_HINT, self.IMAGE_HINT, self.FILES_NOTE])

    def mounts_prompt(self, sess: dict) -> str:
        """What this session's attached mounts contribute to the prompt + how they map into the
        container. Shown as mounts.md in the viewer."""
        entries = self._mount_entries(sess.get("mounts"))
        if not entries:
            return "_No optional mounts are attached to this session._"
        lines = ["# Attached mounts",
                 "_The descriptions below are injected into the system prompt; the paths show how "
                 "each mount maps into the container._", ""]
        for e in entries:
            spec = mounts_store.get(e["name"])
            if not spec:
                continue
            dest = mounts_store.dest_of(spec)
            mode = "read-only" if e["read_only"] else "writable"
            lines.append(f"### `{e['name']}` → `{dest}` ({mode})")
            if spec.get("description"):
                lines.append(spec["description"])
            lines.append("")
        return "\n".join(lines).rstrip()

    def system_prompt(self, sess: dict) -> str:
        """Full system prompt (what --append-system-prompt receives): base hints + Global/user/chat."""
        glob, uc, c, _ = self._ctx_paths(sess)
        parts = [self.UI_HINT, self.IMAGE_HINT]
        gt = _read(glob)
        if gt:
            parts.append("# Global (applies to every session)\n" + gt)
        uct = _read(uc)
        if uct:
            parts.append("# User context\n" + uct)
        ct = _read(c)
        if ct:
            parts.append("# Session context\n" + ct)
        blurbs = self._mount_blurbs(sess)
        if blurbs:
            parts.append("# Attached mounts\n" + "\n".join(blurbs))
        parts.append(self.FILES_NOTE)
        return "\n\n".join(parts)

    def wiki_text(self, sess: dict) -> str:
        _, _, _, w = self._ctx_paths(sess)
        return _read(w)

    def context_view(self, sess: dict) -> dict:
        """Read-only view for the in-app viewer: the 4 source layers plus `base` — the system-baked
        part of the prompt (hints + mount blurbs + editable-files note) that the other tabs don't
        cover. The full prompt = base + Global + user + chat."""
        g, u, c, w = self._ctx_paths(sess)
        return {"base": self.base_prompt(sess), "mounts": self.mounts_prompt(sess),
                "global": _read(g), "user": _read(u), "chat": _read(c), "wiki": _read(w)}

    def reseed_creds(self, sess: dict, only_if_stale: bool = False) -> bool:
        """Copy the live seed's .credentials.json into the session's HOME, overwriting the stale
        copy. The seed (agent-bots claude-home) is continuously refreshed; a session's copy is
        short-lived and goes stale → "401 / Please run /login". claude re-reads this file per
        request, so refreshing it (even mid-session) fixes auth without a restart.
        With only_if_stale=True, skips the write when the session token is still comfortably valid.
        Returns True if it (re)wrote the file."""
        try:
            slug = self._slug(sess)
            # setup-token sessions authenticate via CLAUDE_CODE_OAUTH_TOKEN env — nothing to reseed.
            if settings_store.get_oauth_token(slug):
                return False
            # self-managed session keeps its own independent OAuth login — never reseed from the
            # shared seed (one token across many processes -> prone to rotation-race death).
            if (self.local_ws(sess) / ".chome" / ".self_managed").exists():
                return False
            self.ensure_user_seed(slug)            # keep the user's self-rotating seed fresh
            seed_cred = self._user_seed_local(slug) / ".credentials.json"
            if not seed_cred.exists():
                return False
            dst = self.local_ws(sess) / ".chome" / ".credentials.json"
            if only_if_stale and dst.exists():
                # still fresh -> leave it (session is topped up from the seed; its own copy has no
                # refresh token so it cannot refresh)
                if self._oauth_block(dst).get("expiresAt", 0) - time.time() * 1000 > FRESH_MARGIN_MS:
                    return False
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Strip the refresh token from the session copy: a session must NEVER refresh on its own
            # (that would rotate the user's seed token out from under their other sessions). Sessions
            # live purely on access tokens topped up from the per-user seed (see ensure_user_seed).
            data = json.loads(seed_cred.read_text())
            if "claudeAiOauth" in data:
                data["claudeAiOauth"]["refreshToken"] = ""
            dst.write_text(json.dumps(data)); dst.chmod(0o600)
            return True
        except Exception:
            return False

    @staticmethod
    def _oauth_block(path) -> dict:
        """The claudeAiOauth block of a .credentials.json ({} on any error)."""
        try:
            return json.loads(Path(path).read_text()).get("claudeAiOauth", {}) or {}
        except Exception:
            return {}

    def _warm_auth(self, sess: dict):
        """Force any pending OAuth access-token refresh BEFORE the interactive claude launches and
        the user sends msg #1 (a fresh container can race the auto-refresh and 401 the first
        request, tempting a needless /login). One cheap NON-persisted print-mode call (no session
        is written, so the interactive `-c` continue is unaffected). Only useful for credentials
        that can actually self-refresh — i.e. a .credentials.json that KEPT its refreshToken
        (.self_managed sessions / an in-container /login). Seed-managed copies have the refresh
        token stripped (see reseed_creds) and are topped up by the seed instead, and env-token
        sessions have no cred file: both skip. Runs while no interactive claude is up yet, so
        there's no refresh-rotation race. Best-effort: never blocks or fails the launch."""
        try:
            cred = self.local_ws(sess) / ".chome" / ".credentials.json"
            o = self._oauth_block(cred)
            if not o.get("refreshToken"):
                return                              # cannot self-refresh — a warm call can't help
            if o.get("expiresAt", 0) - time.time() * 1000 > FRESH_MARGIN_MS:
                return                              # still fresh: no refresh needed yet
            _docker("exec", "-e", "IS_SANDBOX=1", sess["container"],
                    "claude", "--dangerously-skip-permissions", "-p", "ok",
                    "--no-session-persistence", "--model", "claude-haiku-4-5", timeout=30)
        except Exception:
            pass

    def ensure_claude(self, sess: dict):
        """Ensure an interactive claude is running inside a tmux session in the container.
        tmux gives claude a real TTY; we send input via `tmux send-keys` (no pty-in-pty).
        Also auto-dismisses the one-time Bypass-Permissions confirm screen so it doesn't eat msg #1.
        """
        c = sess["container"]
        has = _docker("exec", c, "tmux", "has-session", "-t", "main")
        if has.returncode == 0:
            _docker("exec", c, "tmux", "set", "-g", "mouse", "on")   # idempotent: also fix existing sessions
            return
        # Re-seed FRESH OAuth creds from the live seed before launch (see reseed_creds for why).
        self.reseed_creds(sess)
        # Guarantee the bypass-accepted + trust flags are in .claude.json BEFORE every launch
        # (claude rewrites this file on each run; setting it only at create-time can get lost).
        cj = self.local_ws(sess) / ".claude.json"
        try:
            cfg = json.loads(cj.read_text(encoding="utf-8")) if cj.exists() else {}
        except Exception:
            cfg = {}
        cfg["bypassPermissionsModeAccepted"] = True
        cfg.setdefault("projects", {}).setdefault("/workspace", {})["hasTrustDialogAccepted"] = True
        # skip the first-run wizard (theme → login → OAuth) which otherwise blocks msg #1
        cfg["hasCompletedOnboarding"] = True
        cfg.setdefault("theme", "dark")
        cj.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        # warm up auth now (no interactive claude running yet) so msg #1 never races the token refresh
        self._warm_auth(sess)
        # write the combined system prompt (global.md + chat.md) to a file the container can read,
        # then pass it via --append-system-prompt. File path is /workspace/.sysprompt (mounted).
        sp = self.system_prompt(sess)
        sp_arg = ""
        if sp:
            (self.local_ws(sess) / ".sysprompt").write_text(sp, encoding="utf-8")
            sp_arg = ' --append-system-prompt "$(cat /workspace/.sysprompt)"'
        # choice buttons use claude's BUILT-IN AskUserQuestion widget (parsed from JSONL, driven
        # via tmux) — no MCP server, so no --mcp-config and no new-server approval dialog.
        launch = (f"claude --dangerously-skip-permissions{sp_arg} -c 2>/dev/null || "
                  f"claude --dangerously-skip-permissions{sp_arg}")
        _docker("exec", "-e", "IS_SANDBOX=1", c, "tmux", "new-session", "-d", "-s", "main",
                "-x", "200", "-y", "50", "bash", "-lc", f"cd /workspace && {launch}")
        # enable mouse-wheel scrolling in terminal mode (forwards wheel to the TUI / enters copy-mode
        # in the shell) and keep a deep scrollback. Without this the wheel does nothing (mouse off).
        _docker("exec", c, "tmux", "set", "-g", "mouse", "on")
        _docker("exec", c, "tmux", "set", "-g", "history-limit", "50000")
        # The interactive bypass-permissions screen ALWAYS appears and blocks until 2+Enter; it
        # never clears itself. Deterministic dismissal: keep pressing 2+Enter WHILE the screen is
        # shown; only stop once it's actually gone (don't exit early on a 'ready' heuristic that can
        # fire before the screen even renders, which used to let the first message hit the dialog).
        def _pane():
            return (_docker("exec", c, "tmux", "capture-pane", "-t", "main", "-p").stdout or "")
        for _ in range(40):                 # ~40s safety cap
            txt = _pane()
            if "Yes, I accept" in txt:
                _docker("exec", c, "tmux", "send-keys", "-t", "main", "2")
                time.sleep(0.3)
                _docker("exec", c, "tmux", "send-keys", "-t", "main", "Enter")
                time.sleep(1.2)
                continue
            # screen gone — but make sure claude actually reached its prompt before we proceed
            if "esc to interrupt" in txt.lower() or 'Try "' in txt or "shortcuts" in txt:
                break
            time.sleep(0.8)
        # auto-send wiki as the very first message (messages zone => prompt-cached, survives compact).
        # Not shown in the chat; the UI just renders a "wiki loaded" chip.
        wiki = self.wiki_text(sess)
        if wiki:
            time.sleep(1.0)
            _docker("exec", c, "tmux", "send-keys", "-t", "main", "-l", wiki.replace("\n", " "))
            time.sleep(0.2)
            _docker("exec", c, "tmux", "send-keys", "-t", "main", "Enter")

    def rename(self, email: str, sid: str, name: str):
        return self.update(email, sid, name=name)

    def update(self, email: str, sid: str, name=None, mounts=None, cpus=None, mem_mb=None, restrict_egress=None):
        """Edit a session's name, mounts and/or resource limits. Changing mounts or limits recreates
        the container (they're set at `docker run`); the workspace + transcript persist on the host,
        the claude process restarts. Returns the session, or None if not found/invalid."""
        d = _load(); key = _email_slug(email)
        for s in d.get(key, []):
            if s["id"] != sid:
                continue
            if name is not None:
                if not _NAME_RE.match(name):
                    return None
                s["name"] = name.strip()[:60]
            recreate = False
            if mounts is not None:
                new_mounts = self._allowed_mounts(email, mounts)
                if new_mounts != s.get("mounts"):
                    s["mounts"] = new_mounts
                    recreate = True
            if cpus is not None:
                nc = self._norm_cpus(cpus)
                if nc != self._norm_cpus(s.get("cpus")):
                    s["cpus"] = nc
                    recreate = True
            if mem_mb is not None:
                nm = self._norm_mem(mem_mb)
                if nm != self._norm_mem(s.get("mem_mb")):
                    s["mem_mb"] = nm
                    recreate = True
            if restrict_egress is not None:
                re_ = bool(restrict_egress)
                if re_ != bool(s.get("restrict_egress")):
                    s["restrict_egress"] = re_
                    recreate = True
            _save(d)
            if name is not None:
                self._write_session_index(key)
            if recreate:
                self.recreate_container(s)   # rm -f + run with the new -v mounts/limits
            return s
        return None

    def delete(self, email: str, sid: str):
        d = _load(); key = _email_slug(email)
        lst = d.get(key, [])
        s = next((x for x in lst if x["id"] == sid), None)
        if not s:
            return False
        _docker("rm", "-f", s["container"])
        d[key] = [x for x in lst if x["id"] != sid]
        _save(d)
        self._write_session_index(key)
        return True

    def _write_session_index(self, key: str):
        """Drop a human-readable id->name map next to the per-user session folders, so a session
        that mounts its workspace (myfiles=@workspace/@user, allfiles=@workspace) can tell which
        `<sid>` folder is which chat. The folders themselves stay id-named (containers + transcript
        paths are keyed on the sid); this index is regenerated on every create/rename/delete."""
        try:
            sessions = _load().get(key, [])
            base = LOCAL_WORK_ROOT / key
            base.mkdir(parents=True, exist_ok=True)
            lines = [
                "# Sessions in this workspace",
                "",
                "Each subfolder here is one chat session, named by its short id. This file is",
                "auto-generated by ccchat — do not edit. Folder id → chat name:",
                "",
            ]
            for s in sessions:
                created = s.get("created", "")
                suffix = f"  ({created})" if created else ""
                lines.append(f"- `{s['id']}` — {s.get('name', 'Session')}{suffix}")
            if not sessions:
                lines.append("_(no sessions)_")
            (base / "SESSIONS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass  # the index is a convenience, never fail a session op over it

    def local_ws(self, sess: dict) -> Path:
        """Our (ccchat-container) view of the session workspace, to read JSONL."""
        key = sess["container"].split("_")[1] if "_" in sess["container"] else "anon"
        # host_ws = HOST_WORK_ROOT/key/sid ; local mirror = LOCAL_WORK_ROOT/key/sid
        rel = sess["host_ws"].replace(HOST_WORK_ROOT, "").lstrip("/")
        return LOCAL_WORK_ROOT / rel
