"""Per-session "ideas" — messages the user parked to revisit later, stored server-side so they
sync across devices (jot a few on a laptop, act on them from a phone). Keyed by session id (sid);
each value is a list of idea strings. Same atomic-write pattern as push_store / mounts_store.

Corrections ("поправки") are NOT stored here — they auto-send at the next opening and are ephemeral.
Only the manually-held ideas need to survive a reload and travel between devices.
"""
import json
import os
from pathlib import Path

IDEAS_FILE = Path(os.environ.get("CCCHAT_IDEAS", "/state/ideas.json"))


def _load() -> dict:
    try:
        return json.loads(IDEAS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    IDEAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = IDEAS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    tmp.replace(IDEAS_FILE)


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
    d = _load()
    if clean:
        d[sid] = clean
    else:
        d.pop(sid, None)
    _save(d)
    return clean
