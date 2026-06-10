"""Unit tests for the per-session parked-ideas store."""
import importlib


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("CCCHAT_IDEAS", str(tmp_path / "ideas.json"))
    import backend.ideas_store as ist
    return importlib.reload(ist)


def test_replace_and_get(tmp_path, monkeypatch):
    ist = _fresh(tmp_path, monkeypatch)
    assert ist.get("s1") == []
    out = ist.replace("s1", ["look into caching", "rename the module"])
    assert out == ["look into caching", "rename the module"]
    assert ist.get("s1") == ["look into caching", "rename the module"]


def test_replace_is_per_session(tmp_path, monkeypatch):
    ist = _fresh(tmp_path, monkeypatch)
    ist.replace("s1", ["a"])
    ist.replace("s2", ["b"])
    assert ist.get("s1") == ["a"]
    assert ist.get("s2") == ["b"]


def test_empty_list_drops_session(tmp_path, monkeypatch):
    ist = _fresh(tmp_path, monkeypatch)
    ist.replace("s1", ["a"])
    ist.replace("s1", [])
    assert ist.get("s1") == []
    assert "s1" not in ist._load()


def test_sanitises_blanks_and_nonstrings(tmp_path, monkeypatch):
    ist = _fresh(tmp_path, monkeypatch)
    out = ist.replace("s1", ["  keep  ", "", "   ", 5, None, {"x": 1}])
    assert out == ["keep"]


def test_blank_sid_is_noop(tmp_path, monkeypatch):
    ist = _fresh(tmp_path, monkeypatch)
    assert ist.replace("", ["a"]) == []
    assert ist.get("") == []
