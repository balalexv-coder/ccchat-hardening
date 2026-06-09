"""Tests for the public-exposure hardening: mount denylist, admin approval gate, task snapshot."""
import json

from backend import mounts_store as ms
from backend import userauth


# ---- mount host-path denylist ----
def test_mount_denylist_rejects_and_forces():
    out = ms.validate([
        {"name": "sock", "path": "/var/run/docker.sock"},               # rejected (host root)
        {"name": "root", "path": "/"},                                  # rejected
        {"name": "traverse", "path": "/home/x/../../root"},             # rejected (..)
        {"name": "optinj", "path": "/data:rw"},                         # rejected (mount-opt inject)
        {"name": "machines", "path": "/root/machines",
         "admin_only": False, "read_only": False},                      # forced admin_only + read_only
        {"name": "shared", "path": "/srv/shared",
         "admin_only": False, "read_only": True},                       # ordinary path, kept as-is
        {"name": "ws", "path": "@workspace/@user",
         "admin_only": False, "read_only": False},                      # alias, kept as-is
    ])
    by = {m["name"]: m for m in out}
    assert set(by) == {"machines", "shared", "ws"}
    assert by["machines"]["admin_only"] is True and by["machines"]["read_only"] is True
    assert by["shared"]["admin_only"] is False
    assert by["ws"]["read_only"] is False


# ---- admin approval gate ----
def test_approval_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(userauth, "USERS_FILE", tmp_path / "users.json")
    userauth.register("bob", "password1")                     # new accounts start unapproved
    assert userauth.is_approved("bob") is False
    assert userauth.set_approved("bob", True) is True
    assert userauth.is_approved("bob") is True
    userauth.set_approved("bob", False)
    assert userauth.is_approved("bob") is False
    monkeypatch.setenv("CCCHAT_ADMIN_PASSWORD", "adminpass12")
    userauth.ensure_default_admin()
    assert userauth.is_approved("admin") is True               # admins always approved
    rows = {r["username"]: r for r in userauth.list_users()}
    assert rows["bob"]["approved"] is False and rows["admin"]["is_admin"] is True


def test_approval_grandfathers_legacy_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(userauth, "USERS_FILE", tmp_path / "users.json")
    (tmp_path / "users.json").write_text(json.dumps({          # no 'approved' key = pre-feature
        "legacy": {"salt": "00", "hash": "00", "created": 1}}))
    assert userauth.is_approved("legacy") is True


# ---- task snapshot reader ----
def test_read_tasks_snapshot(tmp_path):
    from session import Session
    ws = tmp_path
    proj = ws / ".chome" / "projects" / "-workspace"
    proj.mkdir(parents=True)
    uuid = "sess-uuid-1"
    jl = proj / (uuid + ".jsonl")
    jl.write_text("")
    tdir = ws / ".chome" / "tasks" / uuid
    tdir.mkdir(parents=True)
    (tdir / "2.json").write_text(json.dumps(
        {"id": "2", "subject": "second", "activeForm": "doing second", "status": "in_progress"}))
    (tdir / "1.json").write_text(json.dumps(
        {"id": "1", "subject": "first", "activeForm": "doing first", "status": "completed"}))
    (tdir / "3.json").write_text(json.dumps({"id": "3", "subject": "gone", "status": "deleted"}))
    (tdir / "4.json").write_text(json.dumps({"id": "4", "subject": "x", "status": 'evil"><img>'}))
    s = Session(sess={"container": "c"}, local_ws=ws)
    s.jsonl_path = str(jl)
    items = s._read_tasks()
    assert [i["id"] for i in items] == ["1", "2", "4"]         # sorted by id, 'deleted' dropped
    assert items[0]["status"] == "completed" and items[1]["status"] == "in_progress"
    assert items[2]["status"] == "pending"                    # unknown status sanitised to enum
    assert items[1]["content"] == "second" and items[1]["activeForm"] == "doing second"


# ---- AskUserQuestion choice: no stale re-emit after answering ----
def test_choice_not_reemitted_after_answer(tmp_path, monkeypatch):
    """An answered widget lingers in the tmux pane for a few polls; it must NOT be re-surfaced
    (that produced a duplicate choice pinned to the bottom of the chat, detached from its question)."""
    from session import Session
    s = Session(sess={"container": "c"}, local_ws=tmp_path)
    ev = {"kind": "choice", "id": "q1", "question": "Pick", "options": ["a", "b"]}
    # the live widget is on screen -> emitted exactly once
    monkeypatch.setattr(s, "_parse_choice", lambda pane: dict(ev))
    assert s._choice_from_pane("PANE")["id"] == "q1"
    assert s._choice_from_pane("PANE") is None            # same widget, already seen -> suppressed

    # user answers via web; the widget still shows for a couple of polls
    s._answered_choices.add("q1")
    s._seen_choices.discard("q1")
    assert s._choice_from_pane("PANE") is None            # answered -> NOT re-emitted (no stale dup)

    # widget finally dismisses (parse returns None) -> answered-state clears
    monkeypatch.setattr(s, "_parse_choice", lambda pane: None)
    assert s._choice_from_pane("") is None
    assert s._answered_choices == set()                   # a genuine re-ask can surface again

    # the SAME question asked again later is a real new ask -> emitted
    monkeypatch.setattr(s, "_parse_choice", lambda pane: dict(ev))
    assert s._choice_from_pane("PANE")["id"] == "q1"
