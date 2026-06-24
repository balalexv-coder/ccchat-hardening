"""Tests for admin-configurable optional mounts (mounts_store)."""
from backend import mounts_store as ms


def test_empty_on_fresh_install(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "MOUNTS_FILE", tmp_path / "nope.json")
    assert ms.all_mounts() == []        # no hardcoded defaults


def test_validate_sanitizes_and_drops_invalid():
    out = ms.validate([
        {"name": "My Data", "path": "/srv/data", "description": "d", "admin_only": False},
        {"name": "bad", "path": "relative/path"},          # non-absolute, no alias -> dropped
        {"name": "mine", "path": "@workspace/@user"},      # alias path -> allowed
        {"name": "machines", "path": "/root/machines"},
        {"name": "machines", "path": "/dup"},              # duplicate name -> dropped
        "not-a-dict",
    ])
    names = [m["name"] for m in out]
    assert "mydata" in names and "mine" in names
    assert "bad" not in names
    assert names.count("machines") == 1
    md = next(m for m in out if m["name"] == "mydata")
    assert md["read_only"] is True and md["admin_only"] is False and md["dest"] == "/mydata"


def test_validate_blocks_docker_socket_even_with_leading_double_slash():
    # The denylist must catch the docker socket even when POSIX preserves a leading "//"
    # (os.path.normpath does NOT collapse it) — otherwise it would slip through as a writable,
    # non-admin mount = host escape. Regression test for that bypass.
    out = ms.validate([
        {"name": "ok", "path": "/srv/data"},
        {"name": "sock1", "path": "/var/run/docker.sock", "admin_only": False, "read_only": False},
        {"name": "sock2", "path": "//var/run/docker.sock", "admin_only": False, "read_only": False},
        {"name": "sock3", "path": "/run/docker.sock"},
        {"name": "rootfs", "path": "/"},
    ])
    assert [m["name"] for m in out] == ["ok"]   # every docker.sock / root variant dropped


def test_expand_path_aliases():
    wr = "/root/ccchat/work"
    assert ms.expand_path("@workspace", "alice", wr) == wr
    assert ms.expand_path("@workspace/@user", "alice", wr) == "/root/ccchat/work/alice"
    assert ms.expand_path("/plain/abs", "alice", wr) == "/plain/abs"


def test_visible_for_filters_admin_only_and_hides_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "MOUNTS_FILE", tmp_path / "m.json")
    ms.replace([
        {"name": "pub", "path": "/p", "description": "public", "admin_only": False},
        {"name": "sec", "path": "/s", "description": "secret", "admin_only": True},
    ])
    assert [m["name"] for m in ms.visible_for(False)] == ["pub"]
    assert {m["name"] for m in ms.visible_for(True)} == {"pub", "sec"}
    # the create-modal payload exposes only name + description — never the host path
    assert set(ms.visible_for(True)[0].keys()) == {"name", "description"}


def test_replace_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "MOUNTS_FILE", tmp_path / "m.json")
    ms.replace([{"name": "x", "path": "/x", "description": "", "admin_only": True}])
    assert ms.get("x")["path"] == "/x"
    assert ms.dest_of(ms.get("x")) == "/x"
