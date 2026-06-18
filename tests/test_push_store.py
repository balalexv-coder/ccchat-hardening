"""Unit tests for the Web Push subscription store and VAPID key generation."""
import importlib

import pytest


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("CCCHAT_PUSH", str(tmp_path / "push.json"))
    import backend.push_store as ps
    return importlib.reload(ps)


def _sub(ep):
    return {"endpoint": ep, "keys": {"p256dh": "x", "auth": "y"}}


def test_add_get_remove(tmp_path, monkeypatch):
    ps = _fresh(tmp_path, monkeypatch)
    assert ps.get("alice") == []
    ps.add("alice", _sub("https://push/1"))
    ps.add("alice", _sub("https://push/2"))
    eps = {s["endpoint"] for s in ps.get("alice")}
    assert eps == {"https://push/1", "https://push/2"}
    ps.remove("alice", "https://push/1")
    assert {s["endpoint"] for s in ps.get("alice")} == {"https://push/2"}


def test_add_dedupes_by_endpoint(tmp_path, monkeypatch):
    ps = _fresh(tmp_path, monkeypatch)
    ps.add("bob", _sub("https://push/same"))
    ps.add("bob", _sub("https://push/same"))   # re-subscribe same device
    assert len(ps.get("bob")) == 1


def test_remove_last_drops_user(tmp_path, monkeypatch):
    ps = _fresh(tmp_path, monkeypatch)
    ps.add("carol", _sub("https://push/only"))
    ps.remove("carol", "https://push/only")
    assert ps.get("carol") == []
    assert "carol" not in ps._load()


def test_add_ignores_blank(tmp_path, monkeypatch):
    ps = _fresh(tmp_path, monkeypatch)
    ps.add("", _sub("https://push/x"))
    ps.add("dan", {"keys": {}})                # no endpoint
    assert ps.get("dan") == []


def test_corrupt_file_does_not_silently_wipe(tmp_path, monkeypatch):
    """A corrupt store must raise, not read as {} — otherwise the next write would wipe everyone's
    subscriptions. The good data stays on disk for recovery."""
    from backend import jsonstore
    ps = _fresh(tmp_path, monkeypatch)
    ps.add("erin", _sub("https://push/keep"))
    (tmp_path / "push.json").write_text("{ broken", encoding="utf-8")
    with pytest.raises(jsonstore.StoreError):
        ps.get("erin")            # _load() raises instead of returning {}
    with pytest.raises(jsonstore.StoreError):
        ps.add("erin", _sub("https://push/new"))   # the wiping write never happens
    assert (tmp_path / "push.json").read_text(encoding="utf-8") == "{ broken"


def test_vapid_key_stable(tmp_path, monkeypatch):
    monkeypatch.setenv("CCCHAT_VAPID_DIR", str(tmp_path / "vapid"))
    monkeypatch.setenv("CCCHAT_PUSH", str(tmp_path / "push.json"))
    import backend.notify as nf
    nf = importlib.reload(nf)
    k1 = nf.public_key()
    assert isinstance(k1, str) and len(k1) > 80   # base64url uncompressed P-256 point
    # cached + persisted: a reload re-reads the same private key → same applicationServerKey
    nf2 = importlib.reload(nf)
    assert nf2.public_key() == k1
