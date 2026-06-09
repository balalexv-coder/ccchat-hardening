"""Tests for app-native username/password auth and the LIVE ownership recheck (#2)."""
import pytest
from pathlib import Path

from backend import userauth


# ---------- username/password auth ----------

def test_register_verify_and_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(userauth, "USERS_FILE", tmp_path / "users.json")
    userauth.register("alice", "hunter22")
    assert userauth.verify("alice", "hunter22") is True
    assert userauth.verify("alice", "wrong") is False
    assert userauth.user_exists("alice") and not userauth.user_exists("bob")
    # usernames are canonicalised (case-insensitive)
    assert userauth.verify("ALICE", "hunter22") is True
    assert userauth.user_exists("Alice")
    with pytest.raises(userauth.AuthError):
        userauth.register("alice", "another1")       # duplicate username
    with pytest.raises(userauth.AuthError):
        userauth.register("Alice", "another1")       # duplicate (case-insensitive)
    with pytest.raises(userauth.AuthError):
        userauth.register("x", "short123")           # username too short
    with pytest.raises(userauth.AuthError):
        userauth.register("bob", "1234567")          # password too short (< 8)


def test_token_roundtrip_and_tamper(tmp_path, monkeypatch):
    monkeypatch.setattr(userauth, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(userauth, "_secret", lambda: b"test-secret")
    userauth.register("alice", "hunter22")
    tok = userauth.issue_token("alice")
    assert userauth.parse_token(tok) == "alice"
    assert userauth.parse_token(tok + "x") is None    # tampered signature
    assert userauth.parse_token("garbage") is None
    # token for a user that no longer exists is rejected
    monkeypatch.setattr(userauth, "USERS_FILE", tmp_path / "empty.json")
    assert userauth.parse_token(tok) is None


def test_token_revoked_on_password_change(tmp_path, monkeypatch):
    monkeypatch.setattr(userauth, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(userauth, "_secret", lambda: b"test-secret")
    userauth.register("alice", "hunter22")
    old = userauth.issue_token("alice")
    assert userauth.parse_token(old) == "alice"
    import time as _t
    _t.sleep(1.1)                                     # ensure password_changed > token issued-at
    userauth.set_password("alice", "hunter22", "newpass12")
    assert userauth.parse_token(old) is None          # old token revoked
    assert userauth.parse_token(userauth.issue_token("alice")) == "alice"  # fresh one works


def test_admin_seed_no_default_password(tmp_path, monkeypatch):
    monkeypatch.setattr(userauth, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setenv("CCCHAT_ADMIN_PASSWORD", "s3cretAdminPw")
    userauth.ensure_default_admin()
    assert userauth.is_admin("admin") is True
    assert userauth.is_admin("alice") is False
    assert userauth.verify("admin", "s3cretAdminPw") is True
    assert userauth.verify("admin", "admin") is False     # NEVER admin/admin


def test_assert_no_default_admin_blocks_legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(userauth, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.delenv("CCCHAT_ALLOW_DEFAULT_ADMIN", raising=False)
    userauth._seed_user("admin", "admin")             # simulate a legacy admin/admin instance
    with pytest.raises(RuntimeError):
        userauth.assert_no_default_admin()
    monkeypatch.setenv("CCCHAT_ALLOW_DEFAULT_ADMIN", "1")
    userauth.assert_no_default_admin()                # explicit override -> allowed


# ---------- #2 _live ownership recheck ----------

class _FakeMgr:
    def __init__(self):
        self._db = {("owner@x", "s1"): {"id": "s1", "container": "c1", "host_ws": "/h/owner/s1"}}

    def _find(self, email, sid):
        return self._db.get((email, sid))

    def local_ws(self, sess):
        return Path("/tmp")

    def wiki_text(self, sess):
        return ""


def test_live_rechecks_ownership_on_cache_hit(monkeypatch):
    from backend import app as appmod

    monkeypatch.setattr(appmod, "mgr", _FakeMgr())
    appmod.LIVE.clear()
    appmod.PUMP.clear()

    owner = appmod._live("owner@x", "s1")
    assert owner is not None
    assert "s1" in appmod.LIVE                       # now cached

    # a different user supplying the same (cached) sid must be rejected, not handed the session
    assert appmod._live("attacker@x", "s1") is None
    # and the owner still gets it
    assert appmod._live("owner@x", "s1") is owner


# ---------- #3/#4 admin-only credential mounts ----------

def _seed_mounts(tmp_path, monkeypatch):
    from backend import mounts_store as ms
    monkeypatch.setattr(ms, "MOUNTS_FILE", tmp_path / "m.json")
    ms.replace([
        {"name": "machines", "path": "/root/machines", "admin_only": True},
        {"name": "ssh", "path": "/root/ssh", "admin_only": True},
    ])


def test_allowed_mounts_drops_admin_only_for_non_admin(tmp_path, monkeypatch):
    from backend import manager as m
    _seed_mounts(tmp_path, monkeypatch)
    monkeypatch.setattr(userauth, "ADMINS", set())
    # host SSH keys + fleet reference must NOT be attachable by a normal user
    assert m.Manager._allowed_mounts("normaluser", ["machines", "ssh", "bogus"]) == []


def test_allowed_mounts_keeps_for_admin_with_read_only(tmp_path, monkeypatch):
    from backend import manager as m
    _seed_mounts(tmp_path, monkeypatch)
    monkeypatch.setattr(userauth, "ADMINS", {"admin"})
    # per-session read-only choice is preserved; legacy name strings default to read_only=True
    got = m.Manager._allowed_mounts("admin", [{"name": "machines", "read_only": False}, "ssh"])
    assert got == [{"name": "machines", "read_only": False}, {"name": "ssh", "read_only": True}]


def test_mount_entries_normalises_legacy():
    from backend import manager as m
    assert m.Manager._mount_entries(["a", {"name": "b", "read_only": False}]) == \
        [{"name": "a", "read_only": True}, {"name": "b", "read_only": False}]


# ---------- #6 per-session ttyd credentials ----------

def test_ttyd_credential_deterministic_and_per_session(monkeypatch):
    from backend import manager as m
    monkeypatch.setattr(m, "_ttyd_secret", lambda: "test-secret")
    u1, p1 = m.ttyd_credential("sid-A")
    _, p1b = m.ttyd_credential("sid-A")
    _, p2 = m.ttyd_credential("sid-B")
    assert u1 == "ccchat"
    assert p1 == p1b            # same sid -> same credential (proxy & launch agree)
    assert p1 != p2             # different session -> different credential
    assert len(p1) == 24 and p1.isalnum()
