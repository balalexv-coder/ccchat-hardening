"""Per-session "ideas" — messages the user parked to revisit later, stored server-side so they
sync across devices (jot a few on a laptop, act on them from a phone). Keyed by session id (sid);
each value is a list of idea strings. Same atomic-write pattern as push_store / mounts_store.

Corrections ("tweaks") are NOT stored here — they auto-send at the next opening and are ephemeral.
Only the manually-held ideas need to survive a reload and travel between devices.
"""
import os
from pathlib import Path

from . import jsonstore

IDEAS_FILE = Path(os.environ.get("CCCHAT_IDEAS", "/state/ideas.json"))
_LOCK = jsonstore.lock_for(IDEAS_FILE)


def _load() -> dict:
    return jsonstore.load(IDEAS_FILE, {})


def _save(d: dict) -> None:
    jsonstore.save(IDEAS_FILE, d)


def get(sid: str) -> list:
    """All parked ideas for this session (list of strings)."""
    v = _load().get(sid or "", [])
    return v if isinstance(v, list) else []


def replace(sid: str, ideas: list) -> list:
    """Replace the whole idea list for a session (the client owns the list and PUTs it on each
    change — last write wins, which is fine for the jot-here / act-there workflow). Sanitises and
    returns the stored list."""
    if not sid:
        return []
    clean = []
    for x in (ideas or []):
        if isinstance(x, str):
            x = x.strip()
            if x:
                clean.append(x[:8000])
    clean = clean[:200]
    with _LOCK:
        d = _load()
        if clean:
            d[sid] = clean
        else:
            d.pop(sid, None)
        _save(d)
    return clean
