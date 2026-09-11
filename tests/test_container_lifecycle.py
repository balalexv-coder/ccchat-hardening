"""Session-container lifecycle guards.

Two failure modes are covered here, both seen in production:
  * a slow `docker rm -f` raising TimeoutExpired between the rm and the run in _start_container,
    which left the session with no container at all;
  * `claude -c` falling through to a fresh session, whose empty transcript then shadowed the real
    history because it was the newest.
"""
import datetime
import json
import os
import subprocess
import time

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


def _fake_docker(rc, out=""):
    return lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=rc, stdout=out, stderr="")


def test_pane_busy_fails_closed_when_the_probe_fails(monkeypatch):
    """A probe that times out must NOT read as idle -- that would green-light a mid-task stop."""
    monkeypatch.setattr(M, "_docker", _fake_docker(124))
    assert Manager.__new__(Manager)._pane_busy({"container": "c"}) is True


def test_pane_busy_true_on_the_interrupt_hint(monkeypatch):
    monkeypatch.setattr(M, "_docker", _fake_docker(0, "  building... (esc to interrupt)"))
    assert Manager.__new__(Manager)._pane_busy({"container": "c"}) is True


def test_pane_busy_false_only_on_a_clean_idle_pane(monkeypatch):
    monkeypatch.setattr(M, "_docker", _fake_docker(0, "> "))
    assert Manager.__new__(Manager)._pane_busy({"container": "c"}) is False


def test_container_states_survives_a_timeout(monkeypatch):
    """A slow `docker ps -a` costs a sweep, not an exception: reap_idle then skips every session."""
    monkeypatch.setattr(M, "_docker", _fake_docker(124))
    assert Manager.__new__(Manager)._container_states() == {}


# ---- ttyd liveness ------------------------------------------------------------------------------

def _ttyd_docker(probe_results, calls):
    """Fake _docker: theme marker reads back as dark, curl probes follow probe_results."""
    it = iter(probe_results)

    def fake(*a, **k):
        calls.append(" ".join(str(x) for x in a))
        cmd = " ".join(str(x) for x in a)
        if "cat /tmp/.ttyd_theme" in cmd:
            return subprocess.CompletedProcess(a, 0, "dark", "")
        if "curl -sf" in cmd:
            return subprocess.CompletedProcess(a, 0, next(it), "")
        return subprocess.CompletedProcess(a, 0, "", "")
    return fake


def _sess():
    return {"container": "c", "id": "sid"}


def test_ensure_ttyd_leaves_a_healthy_one_alone(monkeypatch):
    calls = []
    monkeypatch.setattr(M, "_docker", _ttyd_docker(["ok"], calls))
    assert Manager.__new__(Manager).ensure_ttyd(_sess(), "dark") is True
    assert not any("-d c ttyd" in c for c in calls), "must not relaunch a working ttyd"


def test_ensure_ttyd_replaces_one_that_rejects_our_credential(monkeypatch):
    """A stale ttyd on a different credential satisfies pidof but 401s every request."""
    calls = []
    monkeypatch.setattr(M, "_docker", _ttyd_docker(["no", "ok"], calls))
    assert Manager.__new__(Manager).ensure_ttyd(_sess(), "dark") is True
    assert any("kill $(pidof ttyd)" in c for c in calls), "stale instance must be killed"
    assert any("ttyd -p 7681" in c for c in calls), "a fresh ttyd must be launched"


# ---- idle metric --------------------------------------------------------------------------------

def _jsonl(path, stamp):
    path.write_text(json.dumps({"type": "assistant", "timestamp": stamp}) + "\n", encoding="utf-8")


def test_last_activity_reads_the_transcript_not_the_mtime(tmp_path, monkeypatch):
    """The hourly backup touches these files; mtime then reports every session as just-active."""
    proj = tmp_path / ".chome" / "projects" / "-workspace"
    proj.mkdir(parents=True)
    p = proj / "aebd60e0-8e1a-4a4f-8462-8b46dfd9e840.jsonl"
    _jsonl(p, "2026-09-01T00:00:00.000Z")
    os.utime(p, (time.time(), time.time()))          # backup touches it -> mtime is now
    monkeypatch.setattr(Manager, "local_ws", lambda self, sess: tmp_path)
    got = Manager.__new__(Manager)._last_activity({})
    expected = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc).timestamp()
    assert abs(got - expected) < 2, "must report the entry time, not the touched mtime"


def test_last_activity_falls_back_to_mtime_when_unreadable(tmp_path, monkeypatch):
    """No parsable timestamp -> overstate activity rather than reap a session we cannot read."""
    proj = tmp_path / ".chome" / "projects" / "-workspace"
    proj.mkdir(parents=True)
    p = proj / "junk.jsonl"
    p.write_text("not json at all\n", encoding="utf-8")
    monkeypatch.setattr(Manager, "local_ws", lambda self, sess: tmp_path)
    assert abs(Manager.__new__(Manager)._last_activity({}) - p.stat().st_mtime) < 2
