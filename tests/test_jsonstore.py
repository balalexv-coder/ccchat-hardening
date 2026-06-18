"""Tests for backend/jsonstore.py — the shared atomic-write / corruption-safe / locked store helper.

The two behaviours that matter most:
  * a corrupt file must NOT silently read as empty (which would let the next save wipe all data);
  * concurrent read-modify-write under lock_for() must not lose updates.
"""
import json
import os
import stat
import threading

import pytest

from backend import jsonstore


def test_load_missing_returns_default(tmp_path):
    p = tmp_path / "nope.json"
    assert jsonstore.load(p, {}) == {}
    assert jsonstore.load(p, []) == []


def test_save_load_roundtrip_and_mode(tmp_path):
    p = tmp_path / "x.json"
    jsonstore.save(p, {"a": 1, "ключ": "значение"})
    assert jsonstore.load(p, {}) == {"a": 1, "ключ": "значение"}
    # secrets-by-default: 0600
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    # no leftover temp file
    assert not (tmp_path / "x.json.tmp").exists()


def test_corrupt_file_raises_and_is_preserved_not_wiped(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(jsonstore.StoreError):
        jsonstore.load(p, {})
    # the bad file is backed up AND the original is left intact (never silently dropped)
    assert (tmp_path / "c.json.corrupt").read_text(encoding="utf-8") == "{not valid json"
    assert p.read_text(encoding="utf-8") == "{not valid json"


def test_save_is_atomic_on_existing(tmp_path):
    p = tmp_path / "a.json"
    jsonstore.save(p, {"v": 1})
    jsonstore.save(p, {"v": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"v": 2}


def test_lock_for_is_per_path_and_stable(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert jsonstore.lock_for(a) is jsonstore.lock_for(a)
    assert jsonstore.lock_for(a) is not jsonstore.lock_for(b)


def test_concurrent_rmw_under_lock_loses_no_updates(tmp_path):
    """Without the lock this is the classic lost-update race; with it every key survives."""
    p = tmp_path / "race.json"
    jsonstore.save(p, {})
    lock = jsonstore.lock_for(p)
    n = 60

    def writer(i):
        with lock:
            d = jsonstore.load(p, {})
            d[f"k{i}"] = i
            jsonstore.save(p, d)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = jsonstore.load(p, {})
    assert final == {f"k{i}": i for i in range(n)}
