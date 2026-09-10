"""Session-container lifecycle guards.

Two failure modes are covered here, both seen in production:
  * a slow `docker rm -f` raising TimeoutExpired between the rm and the run in _start_container,
    which left the session with no container at all;
  * `claude -c` falling through to a fresh session, whose empty transcript then shadowed the real
    history because it was the newest.
"""
import subprocess

import pytest

from backend import manager as M
from backend.manager import Manager


def _raise_timeout(*a, **k):
    raise subprocess.TimeoutExpired(cmd="docker", timeout=1)


def test_docker_timeout_is_tolerated_when_asked(monkeypatch):
    monkeypatch.setattr(M.subprocess, "run", _raise_timeout)
    r = M._docker("rm", "-f", "c", timeout=1, tolerate_timeout=True)
    assert r.returncode == 124
    assert "timed out" in r.stderr


def test_docker_timeout_still_raises_by_default(monkeypatch):
    monkeypatch.setattr(M.subprocess, "run", _raise_timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        M._docker("inspect", "c", timeout=1)


def _mgr(tmp_path, monkeypatch):
    m = Manager.__new__(Manager)
    monkeypatch.setattr(Manager, "local_ws", lambda self, sess: tmp_path)
    return m


def _proj(tmp_path):
    p = tmp_path / ".chome" / "projects" / "-workspace"
    p.mkdir(parents=True)
    return p


BIG = "aebd60e0-8e1a-4a4f-8462-8b46dfd9e840"
SMALL = "77c20ee4-11e2-4dab-a1cd-f3ac443e6cef"


def test_adopts_largest_transcript_and_ignores_non_conversations(tmp_path, monkeypatch):
    p = _proj(tmp_path)
    (p / (BIG + ".jsonl")).write_text("x" * 5000)
    (p / (SMALL + ".jsonl")).write_text("x" * 10)            # stray empty session, newest+smallest
    (p / "journal.jsonl").write_text("x" * 99999)            # not a conversation
    (p / "agent-abc123def456.jsonl").write_text("x" * 99999) # subagent transcript
    m = _mgr(tmp_path, monkeypatch)
    assert m._pinned_session_id({}) == BIG


def test_adoption_is_pinned_to_disk(tmp_path, monkeypatch):
    p = _proj(tmp_path)
    (p / (BIG + ".jsonl")).write_text("x" * 5000)
    m = _mgr(tmp_path, monkeypatch)
    m._pinned_session_id({})
    assert (tmp_path / ".chome" / ".ccchat-session-id").read_text().strip() == BIG


def test_pin_wins_even_over_a_bigger_transcript(tmp_path, monkeypatch):
    p = _proj(tmp_path)
    (p / (BIG + ".jsonl")).write_text("x" * 5000)
    (p / (SMALL + ".jsonl")).write_text("x" * 10)
    (tmp_path / ".chome" / ".ccchat-session-id").write_text(SMALL)
    m = _mgr(tmp_path, monkeypatch)
    assert m._pinned_session_id({}) == SMALL


def test_stale_pin_falls_back_to_adoption(tmp_path, monkeypatch):
    p = _proj(tmp_path)
    (p / (BIG + ".jsonl")).write_text("x" * 5000)
    (tmp_path / ".chome" / ".ccchat-session-id").write_text("11111111-2222-3333-4444-555555555555")
    m = _mgr(tmp_path, monkeypatch)
    assert m._pinned_session_id({}) == BIG


def test_never_ran_yields_empty_so_a_fresh_start_is_used(tmp_path, monkeypatch):
    _proj(tmp_path)
    m = _mgr(tmp_path, monkeypatch)
    assert m._pinned_session_id({}) == ""
