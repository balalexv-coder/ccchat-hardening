"""Per-user Web Push subscriptions, persisted in /state/push-subscriptions.json.

Keyed by the same email-slug used for sessions (`sess["user"]`), so the pump can look up a
session owner's devices directly. Each value is a list of W3C PushSubscription objects
({endpoint, keys:{p256dh, auth}}). De-duped by endpoint so re-subscribing the same device
(which happens on every "enable") doesn't pile up. Follows the same atomic-write pattern as
appconfig / mounts_store.
"""
import json
import os
from pathlib import Path

PUSH_FILE = Path(os.environ.get("CCCHAT_PUSH", "/state/push-subscriptions.json"))


def _load() -> dict:
    try:
        return json.loads(PUSH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    PUSH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PUSH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    tmp.replace(PUSH_FILE)


def get(slug: str) -> list:
    """All push subscriptions registered for this user (email-slug)."""
    return _load().get(slug or "", [])


def add(slug: str, sub: dict) -> None:
    """Store a subscription for this user, replacing any existing one with the same endpoint."""
    ep = (sub or {}).get("endpoint")
    if not slug or not ep:
        return
    d = _load()
    lst = [s for s in d.get(slug, []) if s.get("endpoint") != ep]
    lst.append(sub)
    d[slug] = lst
    _save(d)


def remove(slug: str, endpoint: str) -> None:
    """Drop a subscription by endpoint (on unsubscribe, or when the push service reports it gone)."""
    if not slug or not endpoint:
        return
    d = _load()
    if slug in d:
        d[slug] = [s for s in d[slug] if s.get("endpoint") != endpoint]
        if not d[slug]:
            d.pop(slug, None)
        _save(d)
