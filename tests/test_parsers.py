"""Unit tests for the pure parsing logic in session.py.

These cover string/dict transforms that need no Docker/tmux runtime:
_clean_overlay, _parse_choice (+ _choice_from_pane dedup), and _parse_line.
"""
from pathlib import Path

from session import Session


def make_session(wiki: str = "") -> Session:
    # Session.__init__ does no I/O; the parser methods under test never touch the container.
    return Session({"container": "test-container"}, Path("/tmp/ws"), wiki=wiki)


# ---------- _clean_overlay ----------

def test_clean_overlay_keeps_body_drops_chrome():
    pane = "\n".join([
        "old chat scrollback",
        "❯ /help",
        "Help overlay title",
        "  some help body line",
        "──────────────",
        "esc to interrupt",
    ])
    out = Session._clean_overlay(pane, "/help")
    assert "Help overlay title" in out
    assert "some help body line" in out
    assert "esc to interrupt" not in out      # chrome dropped
    assert "─" not in out                       # separator row dropped
    assert "old chat scrollback" not in out     # everything above the ❯ prompt dropped


# ---------- _parse_choice ----------

def test_parse_choice_single_question_with_custom():
    pane = "\n".join([
        "scrollback with no digits",
        "Which option do you want?",
        "❯ 1. Option A",
        "  2. Option B",
        "  3. Type something",
        "Enter to select · ↑↓ to navigate · Esc to cancel",
    ])
    ev = make_session()._parse_choice(pane)
    assert ev is not None
    assert ev["kind"] == "choice"
    assert ev["question"] == "Which option do you want?"
    assert ev["options"] == ["Option A", "Option B"]   # "Type something" -> allow_custom, not an option
    assert ev["allow_custom"] is True
    assert ev["multi"] is False


def test_parse_choice_multiselect_checkboxes():
    pane = "\n".join([
        "Pick some?",
        "  1. [ ] Alpha",
        "  2. [✔] Beta",
        "Enter to select · to navigate",
    ])
    ev = make_session()._parse_choice(pane)
    assert ev is not None
    assert ev["options"] == ["Alpha", "Beta"]
    assert ev["multi"] is True


def test_parse_choice_returns_none_without_widget():
    assert make_session()._parse_choice("just some normal pane text") is None


def test_choice_from_pane_dedups():
    s = make_session()
    pane = "\n".join([
        "Question?",
        "❯ 1. Yes",
        "  2. No",
        "Enter to select · to navigate",
    ])
    first = s._choice_from_pane(pane)
    second = s._choice_from_pane(pane)
    assert first is not None
    assert second is None        # same signature -> surfaced only once


# ---------- _parse_line ----------

def test_parse_line_turn_duration_is_done():
    s = make_session()
    assert s._parse_line({"type": "system", "subtype": "turn_duration"}) == {"kind": "done"}


def test_parse_line_user_text():
    s = make_session()
    d = {"type": "user", "message": {"role": "user", "content": "hello there"}}
    assert s._parse_line(d) == {"kind": "user", "text": "hello there"}


def test_parse_line_hides_xml_tagged_user_content():
    s = make_session()
    d = {"type": "user", "message": {"role": "user", "content": "<system-reminder>noise</system-reminder>"}}
    assert s._parse_line(d) is None


def test_parse_line_assistant_text():
    s = make_session()
    d = {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "hi"}]}}
    assert s._parse_line(d) == {"kind": "assistant", "text": "hi"}


def test_parse_line_compact_summary_is_wiki_chip():
    s = make_session()
    assert s._parse_line({"isCompactSummary": True}) == {"kind": "wiki", "tokens": None, "compacted": True}


def test_parse_line_hides_wiki_and_its_answer():
    s = make_session(wiki="my wiki text")
    user = {"type": "user", "message": {"role": "user", "content": "my wiki text"}}
    assert s._parse_line(user) == {"kind": "wiki", "loaded": True}
    assert s._hide_next_assistant is True
    # the assistant turn answering the wiki is hidden, but emits the token chip once
    asst = {"type": "assistant", "message": {"role": "assistant", "usage": {"input_tokens": 10},
                                             "content": [{"type": "text", "text": "ack"}]}}
    ev = s._parse_line(asst)
    assert ev["kind"] == "wiki" and ev["tokens"] == 10


def test_parse_line_hides_askuserquestion_tool_use():
    s = make_session()
    d = {"type": "assistant", "message": {"role": "assistant",
         "content": [{"type": "tool_use", "name": "AskUserQuestion", "id": "tool-1", "input": {}}]}}
    assert s._parse_line(d) is None
    assert "tool-1" in s._hidden_tool_ids


# ---------- _fire_compact (#9) ----------

def test_fire_compact_keeps_ref_and_dedupes():
    import asyncio

    s = make_session()
    calls = []

    async def cb():
        calls.append(1)
        await asyncio.sleep(0.01)

    s.on_compact = cb

    async def run():
        s._fire_compact()
        s._fire_compact()                 # one already in flight -> suppressed
        assert len(s._bg_tasks) == 1      # strong reference held (not GC'd)
        await asyncio.gather(*list(s._bg_tasks))

    asyncio.run(run())
    assert calls == [1]                   # wiki re-sent exactly once, not twice
    assert s._bg_tasks == set()           # ref cleaned up after completion


# ---------- _ctx_tokens (#12) ----------

def test_ctx_tokens_sums_input_and_cache_tiers():
    usage = {"input_tokens": 100, "cache_read_input_tokens": 20,
             "cache_creation_input_tokens": 5, "output_tokens": 999}
    assert Session._ctx_tokens(usage) == 125     # output_tokens excluded
    assert Session._ctx_tokens({}) == 0
    assert Session._ctx_tokens(None) == 0


# ---------- _switch_jsonl (#8) ----------

def test_switch_jsonl_resumes_instead_of_replaying():
    s = make_session()
    assert s._switch_jsonl("/a.jsonl") == 0      # first file, start at 0
    s._jsonl_pos = 500                            # tailed 500 bytes of a
    assert s._switch_jsonl("/b.jsonl") == 0      # new session (/clear) -> fresh
    s._jsonl_pos = 200
    # resume the OLD file (claude -c bumps its mtime) -> resume at 500, NOT replay from 0
    assert s._switch_jsonl("/a.jsonl") == 500
    assert s._switch_jsonl("/b.jsonl") == 200    # b remembered too


# ---------- _classify_block (blocking-state detection, idea from claude-p) ----------

def test_classify_block():
    c = Session._classify_block
    # auth errors are intentionally NOT surfaced as a banner (they show in the chat itself, and a
    # stale 401 lingering in the pane history would keep the banner up after recovery)
    assert c("Failed to authenticate (please run /login)") is None
    assert c("Invalid API key · Please run /login") is None
    assert c("You've hit your limit for today") == "rate_limit"
    assert c("Claude usage limit reached") == "rate_limit"
    assert c("Do you trust the files in this folder?") == "workspace_trust"
    assert c("normal pane — esc to interrupt") is None
    assert c("") is None
