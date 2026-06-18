"""Shared helpers for the small JSON-backed state stores (settings/ideas/push/mounts/appconfig and
the session state in manager.py).

Two guarantees the stores need but used to each re-implement (inconsistently):

1. **Atomic, durable writes** — `save()` writes a temp file, fsyncs it, then `os.replace()`s it into
   place. A crash mid-write can never leave a torn/half file; the old file stays intact.

2. **Corruption never causes silent data loss** — the old pattern was `except Exception: return {}`,
   so ANY read error (corrupt JSON, disk glitch) returned empty, and the next write then overwrote the
   file with that empty dict — destroying every other user's data. `load()` instead distinguishes a
   missing file (→ the default) from an unreadable/corrupt one: it backs the bad file up to
   `<name>.corrupt` and raises `StoreError`, so the operation fails loudly instead of wiping data.

3. **Read-modify-write races** — `lock_for(path)` hands out a process-wide re-entrant lock keyed by
   file path. Mutators wrap their `load → mutate → save` sequence in it so two concurrent writers
   (two browser tabs, a request racing the reaper/push sender) can't clobber each other's change.
   uvicorn is single-process, so an in-process lock is sufficient.
"""
import json
import os
import threading
from pathlib import Path

_locks: dict = {}
_locks_guard = threading.Lock()


class StoreError(Exception):
    """A store file exists but could not be read (corrupt/unreadable). Raised instead of silently
    returning empty, which would let the next save overwrite (wipe) the data."""


def lock_for(path) -> threading.RLock:
    """A process-wide re-entrant lock for this file path (created on first use)."""
    key = str(path)
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.RLock()
        return lk


def load(path, default):
    """Parse the JSON file. A MISSING file returns `default` (pass a fresh literal each call — it may
    be mutated by the caller). A present-but-unreadable file is backed up to `<name>.corrupt` and
    raises StoreError, so corruption can never feed a wiping overwrite."""
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, ValueError, OSError, UnicodeDecodeError) as e:
        try:
            (path.parent / (path.name + ".corrupt")).write_bytes(path.read_bytes())
        except OSError:
            pass
        raise StoreError(
            f"{path.name} is unreadable/corrupt (preserved as {path.name}.corrupt): {e}"
        ) from e


def save(path, data, mode: int = 0o600) -> None:
    """Atomically + durably write `data` as pretty JSON: temp file → fsync → os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2))
        fh.flush()
        os.fsync(fh.fileno())
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)
