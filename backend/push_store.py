"""Per-user Web Push subscriptions, persisted in /state/push-subscriptions.json.

Keyed by the same email-slug used for sessions (`sess["user"]`), so the pump can look up a
session owner's devices directly. Each value is a list of W3C PushSubscription objects
({endpoint, keys:{p256dh, auth}}). De-duped by endpoint so re-subscribing the same device
(which happens on every "enable") doesn't pile up. Follows the same atomic-write pattern as
appconfig / mounts_store.
"""
import os
from pathlib import Path

from . import jsonstore

PUSH_FILE = Path(os.environ.get("CCCHAT_PUSH", "/state/push-subscriptions.json"))
_LOCK = jsonstore.lock_for(PUSH_FILE)


def _load() -> dict:
    return jsonstore.load(PUSH_FILE, {})


def _save(d: dict) -> None:
    jsonstore.save(PUSH_FILE, d)


def get(slug: str) -> list:
    """All push subscriptions registered for this user (email-slug)."""
    return _load().get(slug or "", [])


def add(slug: str, sub: dict) -> None:
    """Store a subscription for this user, replacing any existing one with the same endpoint."""
    ep = (sub or {}).get("endpoint")
    if not slug or not ep:
        return
    with _LOCK:
        d = _load()
        lst = [s for s in d.get(slug, []) if s.get("endpoint") != ep]
        lst.append(sub)
        d[slug] = lst
        _save(d)


def clear(slug: str) -> int:
    """Drop ALL subscriptions for this user (a global "disable on every device"). Returns how many."""
    with _LOCK:
        d = _load()
        n = len(d.get(slug, []))
        if slug in d:
            d.pop(slug, None)
            _save(d)
    return n


def remove(slug: str, endpoint: str) -> None:
    """Drop a subscription by endpoint (on unsubscribe, or when the push service reports it gone)."""
    if not slug or not endpoint:
        return
    with _LOCK:
        d = _load()
        if slug in d:
            d[slug] = [s for s in d[slug] if s.get("endpoint") != endpoint]
            if not d[slug]:
                d.pop(slug, None)
            _save(d)
