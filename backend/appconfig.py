"""Global, admin-tunable app config persisted in /state/app-config.json.

Currently just the idle-session reaper. Env vars seed the DEFAULTS (so an operator can pre-set them
at deploy time); the admin UI overrides and persists them, and the background reaper re-reads this on
every cycle — so toggling it in Settings → Sessions takes effect with no restart.
"""
import os
from pathlib import Path

from . import jsonstore

CONFIG_FILE = Path(os.environ.get("CCCHAT_APP_CONFIG", "/state/app-config.json"))
_LOCK = jsonstore.lock_for(CONFIG_FILE)

_ENV_HOURS = float(os.environ.get("CCCHAT_IDLE_REAP_HOURS", "0") or 0)
_ENV_INTERVAL = float(os.environ.get("CCCHAT_IDLE_REAP_INTERVAL_MIN", "30") or 30)
_REAP_DEFAULTS = {
    "enabled": _ENV_HOURS > 0,
    "hours": _ENV_HOURS or 6.0,
    "interval_min": _ENV_INTERVAL or 30.0,
}


def _load() -> dict:
    return jsonstore.load(CONFIG_FILE, {})


def _save(d: dict) -> None:
    jsonstore.save(CONFIG_FILE, d)


def get_reap() -> dict:
    """Current reaper config (defaults merged with the persisted overrides), sanitised."""
    saved = _load().get("reap") or {}
    r = dict(_REAP_DEFAULTS)
    for k in ("enabled", "hours", "interval_min"):
        if k in saved:
            r[k] = saved[k]
    return {
        "enabled": bool(r["enabled"]),
        "hours": max(0.25, float(r["hours"] or 6)),
        "interval_min": max(1.0, float(r["interval_min"] or 30)),
    }


def set_reap(enabled=None, hours=None, interval_min=None) -> dict:
    """Persist a partial update to the reaper config; returns the new sanitised config."""
    with _LOCK:
        r = get_reap()
        if enabled is not None:
            r["enabled"] = bool(enabled)
        if hours is not None:
            r["hours"] = max(0.25, float(hours))
        if interval_min is not None:
            r["interval_min"] = max(1.0, float(interval_min))
        d = _load()
        d["reap"] = r
        _save(d)
        return r
