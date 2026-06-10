"""Talk to the interactive claude running in a tmux session inside the session container.

Input  -> `docker exec <c> tmux send-keys -t main "<text>" Enter`  (real TTY via tmux, no pty-in-pty)
Output -> tail the JSONL transcript from the mounted workspace (.chome/projects/-workspace).
Interactive => subscription billing.
"""
import asyncio
import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path


def _docker(*args, timeout=30):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


class Session:
    def __init__(self, sess: dict, local_ws: Path, wiki: str = ""):
        self.sess = sess
        self.container = sess["container"]
        self.local_ws = local_ws
        self.proj_dir = local_ws / ".chome" / "projects" / "-workspace"
        self.jsonl_path = None
        self._jsonl_pos = 0
        self._pos_by_file = {}     # path -> bytes already consumed, so a resumed session is not replayed (#8)
        self._block_reason = None  # last surfaced blocking state (auth / rate_limit / workspace_trust)
        self._subscribers = set()
        self._alive = True
        self.on_compact = None     # async callback set by app: re-send wiki after a compaction
        self.on_notify = None      # sync callback set by app: fire a push notification on
                                   # done / blocked / choice (server-side, so it works when the
                                   # browser/phone is closed). Called as on_notify(self.sess, ev).
        self._bg_tasks = set()     # strong refs to fire-and-forget tasks so they aren't GC'd
        self._wiki_sent = False
        # wiki: auto-sent on start, must NOT appear in chat — shown as a "wiki loaded (N tokens)" chip
        self._wiki_norm = (wiki or "").replace("\n", " ").strip()
        self._hide_next_assistant = False   # the assistant turn answering the wiki is hidden too
        self._wiki_tokens_sent = False      # emit the token-count chip only once per wiki load
        self._seen_choices = set()          # ask_user_choice ids already surfaced to the UI
        self._answered_choices = set()      # ids answered (via web) but whose widget still lingers in
                                            # the pane — suppress re-emit until it clears (no stale dup)
        self._await_done = False            # a turn was sent; watchdog should emit done when idle
        self._hidden_tool_ids = set()       # tool_use ids whose tool_result must be hidden too
        self._tasks_mtime = 0               # newest mtime in the tasks/ snapshot dir we've emitted
                                            # (ToolSearch + ask_user_choice: rendered as buttons / noise)

    def start(self):
        # claude is started in tmux by Manager.ensure_claude(); nothing to do here.
        self._alive = True

    def _is_ready(self, pane: str) -> bool:
        # claude's input bar is rendered once the bypass footer / interrupt hint shows; before
        # that it's still painting the splash screen and send-keys would be dropped into the void.
        low = pane.lower()
        return ("bypass permissions on" in low) or ("esc to interrupt" in low) or ("? for shortcuts" in low)

    async def _wait_ready(self, timeout: float = 40.0):
        """Block until claude's TUI input bar is up, so the FIRST message after a fresh start
        isn't swallowed by the still-loading splash screen. If the Bypass-Permissions confirm
        screen is showing (it appears non-deterministically on fresh starts), dismiss it here
        too — sending into that dialog is the other way the first message gets lost."""
        loop = asyncio.get_event_loop()
        end = asyncio.get_event_loop().time() + timeout
        async def _press(key):
            await loop.run_in_executor(None, lambda: _docker(
                "exec", self.container, "tmux", "send-keys", "-t", "main", key))
        while asyncio.get_event_loop().time() < end:
            pane = await loop.run_in_executor(None, self._pane)
            # only treat it as a blocking dialog if the confirm prompt is actually on screen —
            # otherwise a stale substring would make us type "2" into the normal input
            is_dialog = ("Enter to confirm" in pane) and ("❯ 1." in pane or "1. No" in pane
                                                          or "1. Use this" in pane)
            if is_dialog and ("Yes, I accept" in pane or "MCP server" in pane):
                await _press("2"); await asyncio.sleep(0.3); await _press("Enter")
                await asyncio.sleep(1.2); continue
            if self._is_ready(pane):
                return
            await asyncio.sleep(0.4)

    async def send_text(self, text: str):
        # tmux send-keys: send the literal text, then a separate Enter so it submits.
        await self._wait_ready()
        loop = asyncio.get_event_loop()
        # if an AskUserQuestion / select widget is open, a typed message would be eaten by it
        # (digits toggle options, Enter submits). Cancel it with Esc first so the text goes to the
        # normal prompt — the user chose to type instead of clicking a button.
        pane = await loop.run_in_executor(None, self._pane)
        if "Enter to select" in pane and "to navigate" in pane:
            await loop.run_in_executor(None, lambda: _docker(
                "exec", self.container, "tmux", "send-keys", "-t", "main", "Escape"))
            await asyncio.sleep(0.4)
        clean = text.replace("\n", " ")
        await loop.run_in_executor(None, lambda: _docker(
            "exec", self.container, "tmux", "send-keys", "-t", "main", "-l", clean))
        # A slash command (e.g. /compact, /clear) pops Claude Code's autocomplete menu. Sending
        # Enter while that menu is open can just *complete* the command into the input (leaving
        # e.g. "/compact <args…>" stuck there awaiting an argument) instead of running it — the
        # "stuck command" bug. Esc closes the menu but keeps the typed text, so the next Enter
        # reliably submits. Guard with a regex so message-like text starting with "/" (e.g. a
        # "/workspace/..." path) is left alone.
        if re.fullmatch(r"/[\w-]+", (clean.split() or [""])[0]):
            await asyncio.sleep(0.4)            # let the autocomplete menu render
            await loop.run_in_executor(None, lambda: _docker(
                "exec", self.container, "tmux", "send-keys", "-t", "main", "Escape"))
            await asyncio.sleep(0.2)
        else:
            await asyncio.sleep(0.15)
        await loop.run_in_executor(None, lambda: _docker(
            "exec", self.container, "tmux", "send-keys", "-t", "main", "Enter"))
        self._await_done = True   # expect this turn to finish → watchdog closes the indicator

    async def send_wiki(self, text: str):
        """Send the wiki as a normal user message (messages-zone, so it's prompt-cached and
        survives compaction). Used on session start and re-sent after each compact/clear.
        Keep _wiki_norm in sync with the CURRENT wiki text so _parse_line recognises it and hides
        it from the chat (shows the 'wiki loaded' chip instead) — the file may have changed since
        the session started."""
        if text and text.strip():
            self._wiki_norm = text.replace("\n", " ").strip()
            self._hide_next_assistant = False
            self._wiki_tokens_sent = False
            await self.send_text(text)

    async def run_tui_command(self, cmd: str) -> str:
        """Run a slash-command that draws a full-screen TUI overlay (e.g. /help, /cost, /status)
        and is NOT written to the JSONL transcript. We send it, scrape the overlay text from the
        tmux pane, then press Esc to dismiss it. Returns the cleaned overlay text for the UI."""
        loop = asyncio.get_event_loop()
        await self._wait_ready()
        await loop.run_in_executor(None, lambda: _docker(
            "exec", self.container, "tmux", "send-keys", "-t", "main", "-l", cmd))
        await asyncio.sleep(0.15)
        await loop.run_in_executor(None, lambda: _docker(
            "exec", self.container, "tmux", "send-keys", "-t", "main", "Enter"))
        await asyncio.sleep(1.3)                       # let the overlay render
        pane = await loop.run_in_executor(None, self._pane)
        # dismiss the overlay so the next message isn't eaten by it
        await loop.run_in_executor(None, lambda: _docker(
            "exec", self.container, "tmux", "send-keys", "-t", "main", "Escape"))
        return self._clean_overlay(pane, cmd)

    @staticmethod
    def _clean_overlay(pane: str, cmd: str) -> str:
        """Extract just the overlay body from a captured pane. The overlay is drawn at the BOTTOM,
        with prior chat history scrolled above it; so we anchor on the echoed `❯ <cmd>` prompt line
        (the overlay renders right after it) and keep everything below, minus chrome lines."""
        lines = pane.splitlines()
        # find the LAST line echoing this command prompt — the overlay starts just after it
        start = 0
        for i in range(len(lines) - 1, -1, -1):
            t = lines[i].strip()
            if t.startswith("❯") and cmd in t:
                start = i + 1
                break
        out = []
        for ln in lines[start:]:
            s = ln.rstrip()
            if not s.strip():
                continue
            st = s.strip()
            if st.startswith("❯") or st.startswith(">"):
                continue
            if set(st) <= set("─—-│ "):                 # pure separator rows
                continue
            if ("bypass permissions on" in st or "esc to interrupt" in st.lower()
                    or "Auto-updating" in st or ("effort" in st and st.startswith("●"))
                    or "Share Claude Code" in st or "/passes" in st
                    or st in ("Esc to cancel", "Enter to confirm")
                    or st.startswith("⎿")):            # tmux "dialog dismissed" notices
                continue
            out.append(s)
        return "\n".join(out).strip()

    async def interrupt(self):
        """Stop claude's current work: Escape interrupts the running turn in the TUI."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _docker(
            "exec", self.container, "tmux", "send-keys", "-t", "main", "Escape"))

    def stop(self):
        self._alive = False

    def _fire_compact(self):
        """Re-send the wiki after a compaction/clear. A bare asyncio.create_task can be
        garbage-collected mid-flight, so keep a strong reference; and never run two in parallel —
        the new-file detector and the compact_boundary detector can both trip for a single event,
        which would re-send the wiki twice."""
        if not self.on_compact:
            return
        if any(not t.done() for t in self._bg_tasks):
            return
        t = asyncio.create_task(self.on_compact())
        self._bg_tasks.add(t)
        t.add_done_callback(self._bg_tasks.discard)

    def _current_jsonl(self):
        files = sorted(glob.glob(str(self.proj_dir / "*.jsonl")),
                       key=lambda p: os.path.getmtime(p))
        return files[-1] if files else None

    _TASK_STATUSES = {"pending", "in_progress", "completed"}

    def _tasks_dir(self):
        """<ws>/.chome/tasks/<session-uuid> — where Claude Code writes one json per task. The uuid is
        the transcript basename (Claude names the JSONL after the session uuid, so they match)."""
        uuid = Path(self.jsonl_path).stem if self.jsonl_path else None
        return (self.proj_dir.parent.parent / "tasks" / uuid) if uuid else None

    def _read_tasks(self):
        """Current task list from the on-disk snapshot (full {subject,activeForm,status} per task).
        Source of truth for the TaskCreate/TaskUpdate family. Returns [] on any problem."""
        try:
            tdir = self._tasks_dir()
            if not tdir:
                return []
            items = []
            for f in tdir.glob("*.json"):
                try:
                    t = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(t, dict) or t.get("status") == "deleted":
                    continue
                status = t.get("status", "pending")
                if status not in self._TASK_STATUSES:   # never trust into the UI's class attr
                    status = "pending"
                items.append({
                    "id": str(t.get("id", "")),
                    "content": str(t.get("subject", ""))[:200],
                    "activeForm": str(t.get("activeForm", ""))[:200],
                    "status": status,
                })
            items.sort(key=lambda x: int(x["id"]) if x["id"].isdigit() else 0)
            return items
        except Exception:
            return []

    @staticmethod
    def _ctx_tokens(usage: dict) -> int:
        """Context-window size from a usage block = prompt input + both cache tiers (review #12)."""
        u = usage or {}
        return (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                + u.get("cache_creation_input_tokens", 0))

    @staticmethod
    def _classify_block(pane: str):
        """Detect a state where claude is stuck waiting on the user/environment rather than working,
        so the UI can show a clear banner instead of a silent hang. Patterns adapted from claude-p's
        terminal classifier. Returns 'auth' | 'rate_limit' | 'workspace_trust' | None."""
        low = (pane or "").lower()
        compact = re.sub(r"\s+", "", low)
        # auth errors are intentionally NOT surfaced as a banner — they show up in the chat itself,
        # and a stale 401 line lingering in the pane history would keep a banner up after recovery.
        if ("hit your limit" in low or "usage limit" in low or "rate limit" in low
                or "limit reached" in low):
            return "rate_limit"
        if ("do you trust" in low and "folder" in low) or "workspacetrust" in compact:
            return "workspace_trust"
        return None

    def _switch_jsonl(self, path: str) -> int:
        """Switch the tailed transcript to `path` and return the byte offset to resume from. Saves
        the leaving file's position and restores `path`'s previous one (0 if never seen). This is
        the #8 fix: selecting the active file by mtime is fine, but a file we've tailed before
        (a session resumed via `claude -c`, which bumps its mtime) must resume where we left off —
        NOT reset to 0, which replayed the whole old transcript as if it were live."""
        if self.jsonl_path is not None and self.jsonl_path != path:
            self._pos_by_file[self.jsonl_path] = self._jsonl_pos
        self.jsonl_path = path
        self._jsonl_pos = self._pos_by_file.get(path, 0)
        return self._jsonl_pos

    def _parse_line(self, d):
        if not isinstance(d, dict):
            return None
        # compaction summary: claude injects a huge "This session is being continued…" user message
        # when the context is compacted. It's machinery, not a real message — show a chip instead.
        if d.get("isCompactSummary"):
            return {"kind": "wiki", "tokens": None, "compacted": True}
        t = d.get("type")
        msg = d.get("message") or {}
        role = msg.get("role")
        cont = msg.get("content")
        if t == "system" and d.get("subtype") == "turn_duration":
            if self._hide_next_assistant:
                self._hide_next_assistant = False   # end of the hidden wiki-answer turn
                return None
            return {"kind": "done"}        # claude finished this turn
        if t == "user" and role == "user":
            if isinstance(cont, str):
                s = cont.strip()
                if s.startswith("<") or not s:
                    return None
                # isMeta records are command/system output (e.g. /context prints a markdown table),
                # NOT the user's own message — render as an assistant-style markdown block, not a
                # blue user bubble.
                if d.get("isMeta"):
                    return {"kind": "assistant", "text": s}
                # is this the auto-sent wiki message? hide it; show a chip instead, and hide its answer
                if self._wiki_norm and s.replace("\n", " ").strip() == self._wiki_norm:
                    self._hide_next_assistant = True
                    return {"kind": "wiki", "loaded": True}
                return {"kind": "user", "text": s}
            if isinstance(cont, list):
                for c in cont:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        # results of hidden tools (ToolSearch / ask_user_choice) are noise:
                        # the button widget already shows the choice, the search is internal
                        if c.get("tool_use_id") in self._hidden_tool_ids:
                            return None
                        out = c.get("content")
                        if isinstance(out, list):
                            out = " ".join(x.get("text", "") for x in out if isinstance(x, dict))
                        return {"kind": "tool_result", "text": (str(out) or "")[:20000]}
            return None
        if t == "assistant" and role == "assistant" and isinstance(cont, list):
            # assistant turns answering the wiki are hidden; emit token count once (first one),
            # keep hiding the rest until turn_duration ends the hidden turn
            if self._hide_next_assistant:
                if not self._wiki_tokens_sent:
                    self._wiki_tokens_sent = True
                    tok = self._ctx_tokens(msg.get("usage"))
                    return {"kind": "wiki", "tokens": tok}
                return None
            for c in cont:
                if not isinstance(c, dict):
                    continue
                ct = c.get("type")
                if ct == "text":
                    txt = c.get("text", "").strip()
                    # claude emits this placeholder when a turn is only a tool call — pure noise
                    if not txt or txt == "No response requested.":
                        continue
                    return {"kind": "assistant", "text": c["text"]}
                if ct == "thinking" and c.get("thinking", "").strip():
                    return {"kind": "thinking", "text": c["thinking"]}
                if ct == "tool_use":
                    name = c.get("name") or ""
                    # AskUserQuestion (built-in choice widget) is surfaced as buttons from the live
                    # tmux pane while pending; its resolved tool_use/result in JSONL is just noise.
                    # ToolSearch is internal deferred-tool lookup — also hidden.
                    if name in ("AskUserQuestion", "ToolSearch"):
                        self._hidden_tool_ids.add(c.get("id"))
                        continue
                    inp = c.get("input") or {}
                    # The task tools carry the live to-do list — surface it as a dedicated event so the
                    # UI renders a checklist + drives the activity phrase from the in-progress item's
                    # activeForm, instead of a noisy/empty task tool bubble.
                    #  - Claude Code 2.1.x uses the TaskCreate/TaskUpdate/TaskList family; the full
                    #    snapshot lives on disk (see _read_tasks), so we read that.
                    #  - older builds use TodoWrite, which passes the whole list inline as input.todos.
                    if name in ("TaskCreate", "TaskUpdate", "TaskList"):
                        self._hidden_tool_ids.add(c.get("id"))   # hide the tool bubble + its result echo
                        return {"kind": "todos", "items": self._read_tasks()}
                    if name == "TodoWrite":
                        self._hidden_tool_ids.add(c.get("id"))
                        items = [{
                            "content": str(t.get("content", ""))[:200],
                            "activeForm": str(t.get("activeForm", ""))[:200],
                            "status": t.get("status", "pending"),
                        } for t in (inp.get("todos") or [])[:50] if isinstance(t, dict)]
                        return {"kind": "todos", "items": items}
                    summary = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                    return {"kind": "tool_use", "name": name, "text": str(summary)[:400]}
        return None

    _OPT_RE = re.compile(r"^\s*[❯>]?\s*(\d+)\.\s*(?:\[([ ✔xX])\]\s*)?(.+?)\s*$")

    def _parse_choice(self, pane: str):
        """Pure parse of claude's live AskUserQuestion widget from the tmux pane -> {kind:choice}
        event (or None). No dedup state: pump dedups via _choice_from_pane; attach replays this
        unconditionally so a pending widget survives reconnects / tab switches (it is NOT in JSONL)."""
        if "to navigate" not in pane or "Enter to select" not in pane:
            return None
        all_lines = pane.splitlines()
        # widget is bounded BELOW by the footer ("Enter to select…"). Above it there may be a tab
        # bar ("…✔ Submit…") for MULTI-question widgets; a single-question widget has none. Start
        # right after the tab bar if present, otherwise a few lines above the footer (just enough to
        # capture the question + options, but not unrelated numbered lines from earlier chat).
        foot_i = next((i for i, ln in enumerate(all_lines) if "Enter to select" in ln), None)
        if foot_i is None:
            return None
        # the widget body is bounded ABOVE by the header chip line: a multi-question widget has a tab
        # bar ("←  ☐ Size  ☐ Toppings  ✔ Submit  →"), a single-question one a lone chip ("☐ Fruit").
        # Both carry ☐/☒, so the nearest such line above the footer is a robust upper bound (replaces
        # the old fixed foot_i-16 window, which broke when options had long/wrapping descriptions).
        chip_i = next((i for i in range(foot_i - 1, -1, -1)
                       if "☐" in all_lines[i] or "☒" in all_lines[i]), None)
        if chip_i is not None:
            chipline = all_lines[chip_i]
            if "✔ Submit" in chipline:             # multi-question tab bar
                group_total = (chipline.count("☐") + chipline.count("☒")) or 1
                group_done = chipline.count("☒")
            else:                                   # single-question header chip
                group_total, group_done = 1, 0
            start_i = chip_i + 1
        else:
            group_total, group_done = 1, 0
            start_i = max(0, foot_i - 16)         # fallback: grab the block above the footer
        lines = all_lines[start_i:foot_i]      # widget body only
        question, options, descriptions, has_checkbox, allow_custom = "", [], [], False, False
        first_opt_line = None
        for idx, ln in enumerate(lines):
            m = self._OPT_RE.match(ln)
            if m and m.group(3):
                first_opt_line = idx; break
        if first_opt_line is None:
            return None
        for j in range(first_opt_line - 1, -1, -1):
            if lines[j].strip():
                question = lines[j].strip(); break
        # each option is followed by an indented description line (sometimes wrapped over several);
        # collect them so the UI can show what each choice MEANS, not just its bare label. `collecting`
        # is the index of the option currently accumulating description text, or None to drop it.
        collecting = None
        for ln in lines:
            m = self._OPT_RE.match(ln)
            if m and m.group(3):
                label = m.group(3).strip()
                # skip the synthetic trailing entries the widget always appends; "Type something" is
                # the free-text option -> expose it as allow_custom so the UI renders a text input
                if label.rstrip(".") == "Type something":
                    allow_custom = True; collecting = None; continue
                if (label.rstrip(".") in ("Submit", "Chat about this")
                        or label.startswith("Submit")):
                    collecting = None; continue
                if m.group(2) is not None:
                    has_checkbox = True
                options.append(label); descriptions.append("")
                collecting = len(options) - 1
                continue
            # non-option line: a description continuation, separator, or blank
            s = ln.strip()
            if not s or set(s) <= set("─-——"):     # blank or a separator rule -> ignore
                continue
            if collecting is not None:
                descriptions[collecting] = (descriptions[collecting] + " " + s).strip()
        if not options:
            return None
        # drop descriptions that just echo the label (claude sometimes sets description == label)
        descriptions = ["" if d == lbl else d for d, lbl in zip(descriptions, options)]
        # signature includes which group we're on, so each group of a multi-group widget is distinct
        sig = f"{group_done}/{group_total}|" + question + "|" + "|".join(options)
        return {"kind": "choice", "id": sig,
                "question": question, "options": options, "descriptions": descriptions,
                "multi": has_checkbox, "allow_custom": allow_custom,
                "group_idx": group_done, "group_total": group_total}

    def _choice_from_pane(self, pane: str):
        """pump's view: parse + dedup so each distinct group is streamed to subscribers only once."""
        ev = self._parse_choice(pane)
        if not ev:
            # no widget on screen — the next identical question is a genuine new ask, so forget
            # what we've answered (otherwise a re-asked question would be suppressed forever)
            self._answered_choices.clear()
            return None
        # an answered widget can linger in the pane for a few polls before it dismisses; never
        # re-surface it (that produced a stale duplicate pinned to the bottom of the chat)
        if ev["id"] in self._seen_choices or ev["id"] in self._answered_choices:
            return None
        self._seen_choices.add(ev["id"])
        return ev

    def answer_choice(self, qid: str, indices, multi=False):
        """Answer ONE group of claude's native AskUserQuestion widget via tmux.
        Verified mechanics: pressing an option's 1-based number key toggles it. A single-select
        group AUTO-ADVANCES to the next group on the keypress; a multi-select group does not, so
        we press Tab to advance. When the last group is answered the widget shows a Review screen;
        pump() watches for it and auto-submits. `indices` = selected 0-based option indices."""
        try:
            if isinstance(indices, str):
                indices = [int(x) for x in indices.split(",") if x.strip().isdigit()]
            for i in indices:
                _docker("exec", self.container, "tmux", "send-keys", "-t", "main", str(int(i) + 1))
                time.sleep(0.25)
            if multi:
                # multi-select doesn't auto-advance — Tab moves to the next group (or to Submit)
                time.sleep(0.2)
                _docker("exec", self.container, "tmux", "send-keys", "-t", "main", "Tab")
            self._answered_choices.add(qid)
            self._seen_choices.discard(qid)
        except Exception:
            pass

    def answer_custom(self, qid: str, text: str):
        """Answer claude's AskUserQuestion via its free-text "Type something" option: focus that
        option by its number, type the text literally, submit with Enter (verified tmux mechanic)."""
        try:
            pane = self._pane()
            num = None
            for ln in pane.splitlines():
                m = self._OPT_RE.match(ln)
                if m and m.group(3).strip().rstrip(".") == "Type something":
                    num = m.group(1); break
            if not num:
                return
            c = self.container
            _docker("exec", c, "tmux", "send-keys", "-t", "main", num)
            time.sleep(0.4)
            _docker("exec", c, "tmux", "send-keys", "-t", "main", "-l", text)
            time.sleep(0.3)
            _docker("exec", c, "tmux", "send-keys", "-t", "main", "Enter")
            self._answered_choices.add(qid)
            self._seen_choices.discard(qid)
        except Exception:
            pass

    def _pane(self) -> str:
        try:
            r = _docker("exec", self.container, "tmux", "capture-pane", "-t", "main", "-p", timeout=8)
            return r.stdout or ""
        except Exception:
            return ""

    def _is_busy(self, pane: str) -> bool:
        # claude shows an interrupt hint / spinner while working; absence => idle prompt
        return ("esc to interrupt" in pane.lower()) or ("(esc to" in pane.lower())

    async def pump(self):
        import logging
        tick = 0
        prev_busy = False
        idle_streak = 0
        try:
            while self._alive:
                # tmux watchdog: keep the "thinking" indicator in sync with claude's REAL state
                # across all clients. On busy → indicator on. Once a turn was sent (_await_done) and
                # claude is genuinely idle for ~2 polls → indicator off. This also closes commands
                # that never enter a busy state nor write turn_duration (e.g. /clear, /context).
                tick += 1
                if tick % 5 == 0:  # ~every 2s
                    pane = await asyncio.get_event_loop().run_in_executor(None, self._pane)
                    busy = self._is_busy(pane)
                    if busy:
                        idle_streak = 0
                        self._await_done = True
                        if not prev_busy:
                            for q in list(self._subscribers):
                                q.put_nowait({"kind": "busy"})
                    else:
                        idle_streak += 1
                        if idle_streak >= 2 and self._await_done:
                            self._await_done = False
                            for q in list(self._subscribers):
                                q.put_nowait({"kind": "done"})
                            self._fire_notify({"kind": "done"})   # "task finished" push
                    prev_busy = busy
                    # surface a blocking state (auth / rate-limit / workspace-trust) so the UI shows
                    # a clear banner instead of a silent hang (idea from claude-p). Emit on change;
                    # reason=None clears it.
                    reason = self._classify_block(pane)
                    if reason != self._block_reason:
                        self._block_reason = reason
                        for q in list(self._subscribers):
                            q.put_nowait({"kind": "blocked", "reason": reason})
                        if reason:                              # entered a blocking state → push
                            self._fire_notify({"kind": "blocked", "reason": reason})
                    # surface a live AskUserQuestion widget as choice buttons (pane is the only
                    # source while it's pending — claude doesn't write it to JSONL until answered)
                    chev = self._choice_from_pane(pane)
                    if chev:
                        for q in list(self._subscribers):
                            q.put_nowait(chev)
                        self._fire_notify(chev)                 # "needs your answer" push
                    # after the last group is answered the widget shows a Review screen — auto-submit
                    elif "Submit answers" in pane and "Review your answers" in pane:
                        await asyncio.get_event_loop().run_in_executor(None, lambda: _docker(
                            "exec", self.container, "tmux", "send-keys", "-t", "main", "1"))
                    # re-emit the task checklist when its snapshot dir changes — covers the race where a
                    # TaskCreate/Update JSONL line is tailed before Claude flushes the tasks/*.json, and
                    # surfaces the current list on (re)attach.
                    tdir = self._tasks_dir()
                    if tdir:
                        try:
                            mt = max((f.stat().st_mtime for f in tdir.glob("*.json")), default=0)
                        except Exception:
                            mt = 0
                        if mt and mt != self._tasks_mtime:
                            self._tasks_mtime = mt
                            items = self._read_tasks()
                            for q in list(self._subscribers):
                                q.put_nowait({"kind": "todos", "items": items})
                path = self._current_jsonl()
                if path:
                    if path != self.jsonl_path:
                        # the active transcript changed. On /clear claude starts a fresh JSONL — re-send
                        # the wiki so it survives the wipe (like after a compaction). _switch_jsonl
                        # resumes a previously-tailed file where we left off instead of replaying it (#8).
                        had_prev = self.jsonl_path is not None
                        self._switch_jsonl(path)
                        if had_prev:
                            self._fire_compact()
                    try:
                        with open(path, "rb") as f:
                            f.seek(self._jsonl_pos)
                            while True:
                                raw = f.readline()
                                if not raw.endswith(b"\n"):
                                    break
                                self._jsonl_pos = f.tell()
                                try:
                                    d = json.loads(raw.decode("utf-8", "ignore"))
                                except json.JSONDecodeError:
                                    continue
                                # detect a compaction boundary -> re-send wiki
                                if (d.get("type") == "system"
                                        and d.get("subtype") in ("compact_boundary", "compaction")):
                                    self._fire_compact()
                                # emit the current context size from each assistant turn's usage
                                # (input + cache read + cache create = what's in the context window)
                                um = (d.get("message") or {})
                                if um.get("role") == "assistant" and um.get("usage"):
                                    ctx = self._ctx_tokens(um["usage"])
                                    if ctx:
                                        for q in list(self._subscribers):
                                            q.put_nowait({"kind": "context", "tokens": ctx})
                                ev = self._parse_line(d)
                                if ev:
                                    for q in list(self._subscribers):
                                        q.put_nowait(ev)
                    except OSError:
                        pass
                await asyncio.sleep(0.4)
        except Exception as e:
            logging.getLogger("uvicorn.error").exception("[pump %s] %s", self.container, e)

    def _fire_notify(self, ev):
        """Hand a noteworthy event (done / blocked / choice) to the app-level push hook, if set.
        Best-effort: a push failure must never break the pump."""
        cb = self.on_notify
        if not cb:
            return
        try:
            cb(self.sess, ev)
        except Exception:
            pass

    def subscribe(self):
        q = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        self._subscribers.discard(q)

    # built-in slash commands (always available) + skills parsed from the transcript
    # only slash-commands that work in the pretty web chat are offered in autocomplete:
    #  - /clear, /compact, /context: do real work or write markdown to the transcript
    #  - /help, /cost, /status: read-only TUI overlays we scrape into the chat
    # interactive/terminal-only ones (/model, /config, /agents, /memory, /resume, /exit, /login…)
    # are intentionally omitted — use terminal mode (⌨) for those.
    BUILTIN = ["/clear", "/compact", "/context", "/help", "/cost", "/status"]

    def commands(self):
        """Return slash commands available in this session: built-ins + skills/commands found in
        the JSONL transcript (skill_listing / agent_listing attachments claude writes at startup)."""
        cmds = list(self.BUILTIN)
        seen = set(cmds)
        path = self._current_jsonl()
        if path:
            try:
                for raw in open(path, "rb"):
                    try:
                        d = json.loads(raw.decode("utf-8", "ignore"))
                    except json.JSONDecodeError:
                        continue
                    att = d.get("attachment") or {}
                    if att.get("type") == "skill_listing":
                        for n in att.get("names", []):
                            c = "/" + n
                            if c not in seen:
                                seen.add(c); cmds.append(c)
            except OSError:
                pass
        return cmds

    def history(self):
        evs = []
        path = self._current_jsonl()
        if path:
            try:
                with open(path, "rb") as f:
                    for raw in f:
                        if not raw.endswith(b"\n"):
                            break
                        try:
                            d = json.loads(raw.decode("utf-8", "ignore"))
                        except json.JSONDecodeError:
                            continue
                        ev = self._parse_line(d)
                        if ev:
                            evs.append(ev)
                    self._jsonl_pos = f.tell()
                self.jsonl_path = path
                self._pos_by_file[path] = self._jsonl_pos   # remember position for resume (#8)
            except OSError:
                pass
        return evs

    def last_context_tokens(self):
        """Latest assistant turn's context size (input + cache read + cache create) from the
        transcript, so the UI context indicator persists across reconnects / tab switches."""
        path = self._current_jsonl()
        if not path:
            return None
        ctx = None
        try:
            with open(path, "rb") as f:
                for raw in f:
                    if not raw.endswith(b"\n"):
                        break
                    try:
                        d = json.loads(raw.decode("utf-8", "ignore"))
                    except json.JSONDecodeError:
                        continue
                    um = d.get("message") or {}
                    if um.get("role") == "assistant" and um.get("usage"):
                        c = self._ctx_tokens(um["usage"])
                        if c:
                            ctx = c
        except OSError:
            pass
        return ctx
