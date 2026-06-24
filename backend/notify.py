"""Web Push sender (VAPID) — notifies a user's devices when their session finishes / needs input.

Uses the standard W3C Web Push protocol, which iOS Safari supports for Home-Screen PWAs since
16.4 (no Apple Developer account / APNs cert needed). The VAPID keypair is generated once and
persisted under /state (NOT git-backed); the public key (applicationServerKey) is handed to the
browser via /api/notifications/vapid-key. Payload encryption (RFC 8291 aes128gcm) + the VAPID JWT
are handled by pywebpush.
"""
import base64
import json
import logging
import os
import threading
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import push_store

try:
    from pywebpush import WebPushException, webpush
except Exception:                                   # dependency missing → degrade gracefully
    webpush = None

    class WebPushException(Exception):
        pass


_LOG = logging.getLogger("uvicorn.error")
VAPID_DIR = Path(os.environ.get("CCCHAT_VAPID_DIR", "/state/vapid"))
_PRIV = VAPID_DIR / "private.pem"
# contact address embedded in the VAPID JWT (push services may use it to reach the operator).
# MUST be a routable mailto:/https: — Apple's push service rejects non-real domains (e.g. a
# `.local` TLD) with 403 BadJwtToken, which silently kills delivery to iOS.
_SUB = os.environ.get("CCCHAT_VAPID_SUB", "mailto:admin@example.com")

_lock = threading.Lock()
_cache: dict = {}


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _ensure() -> dict:
    """Load (or generate-and-persist) the VAPID keypair. Returns {'key': applicationServerKey}."""
    with _lock:
        if _cache.get("key"):
            return _cache
        priv = None
        if _PRIV.exists():
            try:
                priv = serialization.load_pem_private_key(_PRIV.read_bytes(), password=None)
            except Exception:
                priv = None
        if priv is None:
            priv = ec.generate_private_key(ec.SECP256R1())
            VAPID_DIR.mkdir(parents=True, exist_ok=True)
            pem = priv.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption())
            tmp = _PRIV.with_suffix(".tmp")
            tmp.write_bytes(pem)
            try:
                tmp.chmod(0o600)
            except Exception:
                pass
            tmp.replace(_PRIV)
        raw = priv.public_key().public_bytes(serialization.Encoding.X962,
                                             serialization.PublicFormat.UncompressedPoint)
        _cache["key"] = _b64url(raw)
        return _cache


def public_key() -> str:
    """The applicationServerKey (base64url) the browser passes to pushManager.subscribe()."""
    return _ensure()["key"]


def _send_one(sub: dict, data: str) -> None:
    webpush(subscription_info=sub, data=data,
            vapid_private_key=str(_PRIV), vapid_claims={"sub": _SUB})


def send_to_user(slug: str, payload: dict, skip_endpoints=(), only_endpoints=None) -> int:
    """Push `payload` (dict → JSON) to the user's registered devices. Skips any endpoint in
    `skip_endpoints` (foreground-watching → they chime in-tab instead). If `only_endpoints` is not
    None, sends ONLY to endpoints in it (to target just the device that sent the last message).
    Prunes subscriptions the push service reports as gone (404/410). Returns how many were sent."""
    _ensure()
    if webpush is None:
        _LOG.warning("[notify] pywebpush not installed — push disabled")
        return 0
    subs = push_store.get(slug)
    if not subs:
        return 0
    skip = set(skip_endpoints or ())
    only = None if only_endpoints is None else set(only_endpoints)
    data = json.dumps(payload)
    sent = 0
    dead = []
    for sub in subs:
        ep = sub.get("endpoint")
        if ep in skip:
            continue
        if only is not None and ep not in only:
            continue
        try:
            _send_one(sub, data)
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):                  # subscription expired/unsubscribed
                dead.append(sub.get("endpoint"))
            else:
                _LOG.warning("[notify] push failed (%s): %s", code, e)
        except Exception as e:
            _LOG.warning("[notify] push error: %s", e)
    for ep in dead:
        if ep:
            push_store.remove(slug, ep)
    return sent
