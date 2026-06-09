"""Username/password auth for ccchat — app-native identity, decoupled from Cloudflare Access.

CF Access (if present) is an optional coarse outer gate, but the origin is now reachable directly,
so WHO you are inside the app is determined solely by a username/password account here. Open
registration: anyone can create an account, but new accounts are unapproved until an admin approves
them (Settings → Users). No email, so there is no password recovery.

Storage: /state/users.json  -> { "<username>": {"salt": hex, "hash": hex, "created": ts,
                                                 "approved": bool, "password_changed": ts} }
Usernames are canonicalised to lowercase. Passwords are hashed with scrypt (stdlib). Login issues an
HMAC-signed cookie token `username.issuedAt.sig` — stateless, but revoked when the password changes
(tokens issued before `password_changed` are rejected).
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

USERS_FILE = Path(os.environ.get("CCCHAT_USERS", "/state/users.json"))
COOKIE_NAME = "ccchat_auth"
TOKEN_TTL = int(os.environ.get("CCCHAT_AUTH_TTL_DAYS", "30")) * 86400

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{2,32}$")
DEFAULT_ADMIN = "admin"


def _canon(username: str) -> str:
    """Canonical account key: trimmed + lowercased. Used for EVERY lookup/auth/approval/admin check
    so behaviour is consistent and case-insensitive (fixes the is_admin-lowercases-but-store-doesn't
    class of bugs)."""
    return (username or "").strip().lower()


# "admin" is always an admin; extra admins via CCCHAT_ADMINS=name1,name2 (USERNAMES, not emails).
_raw_admins = [u.strip().lower() for u in os.environ.get("CCCHAT_ADMINS", "").split(",") if u.strip()]
_bad_admins = [u for u in _raw_admins if not USERNAME_RE.match(u)]
if _bad_admins:
    print(f"[userauth] WARNING: ignoring invalid CCCHAT_ADMINS entries (must be usernames matching "
          f"{USERNAME_RE.pattern}, NOT emails): {_bad_admins}", file=sys.stderr)
ADMINS = {DEFAULT_ADMIN} | {u for u in _raw_admins if USERNAME_RE.match(u)}


def is_admin(username: str) -> bool:
    return _canon(username) in ADMINS


def is_approved(username: str) -> bool:
    """Whether the account may pass the registration screen into the app. Admins are always approved.
    Accounts created before this feature (no 'approved' key) are grandfathered in; freshly registered
    accounts start unapproved until an admin approves them in Settings → Users."""
    if is_admin(username):
        return True
    u = _load().get(_canon(username))
    if not u:
        return False
    return bool(u.get("approved", True))


def set_approved(username: str, approved: bool) -> bool:
    """Admin action: flip a user's approval. Raises AuthError if the user doesn't exist."""
    d = _load()
    u = d.get(_canon(username))
    if not u:
        raise AuthError("no such user")
    u["approved"] = bool(approved)
    u["approved_at"] = int(time.time()) if approved else None
    _save(d)
    return bool(approved)


def list_users() -> list:
    """All accounts with their approval/admin status, for the admin Users panel. Newest first."""
    d = _load()
    out = [{
        "username": name,
        "approved": is_approved(name),
        "is_admin": is_admin(name),
        "created": u.get("created", 0),
    } for name, u in d.items()]
    out.sort(key=lambda r: r.get("created", 0), reverse=True)
    return out


# ---- secret for signing tokens (persisted; fail CLOSED — never a hardcoded fallback) ----
_secret_cache = None


def _secret() -> bytes:
    """HMAC secret for cookie tokens. Order: CCCHAT_AUTH_SECRET env → persisted random file. If
    neither can be obtained we RAISE — a hardcoded fallback would let anyone forge admin tokens."""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    env = os.environ.get("CCCHAT_AUTH_SECRET")
    if env:
        _secret_cache = env.encode()
        return _secret_cache
    f = USERS_FILE.parent / ".auth_secret"
    if f.exists():
        _secret_cache = f.read_bytes()
        return _secret_cache
    s = os.urandom(32)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(s)
    try:
        f.chmod(0o600)
    except OSError as e:
        print(f"[userauth] WARNING: could not chmod 0600 {f}: {e}", file=sys.stderr)
    _secret_cache = s
    return _secret_cache


# ---- user store ----
def _load() -> dict:
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError as e:
        print(f"[userauth] WARNING: could not chmod 0600 {tmp}: {e}", file=sys.stderr)
    tmp.replace(USERS_FILE)


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32).hex()


class AuthError(ValueError):
    pass


def register(username: str, password: str) -> str:
    key = _canon(username)
    if not USERNAME_RE.match(key):
        raise AuthError("username must be 2–32 chars: letters, digits, _ or -")
    if not password or len(password) < 8:
        raise AuthError("password must be at least 8 characters")
    d = _load()
    if key in d:
        raise AuthError("username already taken")
    salt = os.urandom(16)
    d[key] = {"salt": salt.hex(), "hash": _hash(password, salt), "created": int(time.time()),
              "approved": False}
    _save(d)
    return key


def verify(username: str, password: str) -> bool:
    u = _load().get(_canon(username))
    if not u:
        return False
    try:
        expected = u["hash"]
        actual = _hash(password, bytes.fromhex(u["salt"]))
    except Exception:
        return False
    return hmac.compare_digest(expected, actual)


def set_password(username: str, old_password: str, new_password: str) -> None:
    """Change a user's password, requiring the current one. Bumps `password_changed`, which revokes
    every previously-issued token (see parse_token). Raises AuthError on a bad current/too-short pw."""
    d = _load()
    key = _canon(username)
    u = d.get(key)
    if not u or not verify(key, old_password or ""):
        raise AuthError("current password is incorrect")
    if not new_password or len(new_password) < 8:
        raise AuthError("new password must be at least 8 characters")
    salt = os.urandom(16)
    u["salt"] = salt.hex()
    u["hash"] = _hash(new_password, salt)
    u["password_changed"] = int(time.time())
    _save(d)


def user_exists(username: str) -> bool:
    return _canon(username) in _load()


def _seed_user(username: str, password: str, approved: bool = True) -> None:
    """Create a user directly, bypassing public-registration validation. No-op if it already exists."""
    key = _canon(username)
    d = _load()
    if key in d:
        return
    salt = os.urandom(16)
    d[key] = {"salt": salt.hex(), "hash": _hash(password, salt), "created": int(time.time()),
              "approved": approved}
    _save(d)


def ensure_default_admin() -> None:
    """On first start, create the admin account WITHOUT a known default password. The password comes
    from CCCHAT_ADMIN_PASSWORD if set, else a random one written to <state>/.admin-initial-password
    (0600) and logged once. No more admin/admin (the origin is public)."""
    if DEFAULT_ADMIN in _load():
        return
    env_pw = os.environ.get("CCCHAT_ADMIN_PASSWORD")
    pw = env_pw or secrets.token_urlsafe(18)
    _seed_user(DEFAULT_ADMIN, pw, approved=True)
    if not env_pw:
        f = USERS_FILE.parent / ".admin-initial-password"
        try:
            f.write_text(pw + "\n", encoding="utf-8")
            f.chmod(0o600)
        except OSError:
            pass
        print(f"[userauth] seeded 'admin' with a RANDOM password — see {f} (or set "
              f"CCCHAT_ADMIN_PASSWORD). Change it after first login.", file=sys.stderr)


def assert_no_default_admin() -> None:
    """Refuse to run with the legacy admin/admin (a public origin makes it a trivial full compromise).
    Override only for a deliberately-isolated instance with CCCHAT_ALLOW_DEFAULT_ADMIN=1."""
    if os.environ.get("CCCHAT_ALLOW_DEFAULT_ADMIN") == "1":
        return
    if verify(DEFAULT_ADMIN, "admin"):
        raise RuntimeError(
            "Refusing to start: 'admin' still has the default password 'admin'. Change it (Settings → "
            "Change password) or set CCCHAT_ALLOW_DEFAULT_ADMIN=1 for an isolated instance.")


# ---- signed cookie tokens ----
def issue_token(username: str) -> str:
    username = _canon(username)
    issued = str(int(time.time()))
    body = f"{username}.{issued}"
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{body}.{sig_b64}"


def parse_token(token: str):
    """Return the username from a valid, unexpired, unrevoked token, else None."""
    if not token or token.count(".") != 2:
        return None
    username, issued, sig_b64 = token.split(".")
    body = f"{username}.{issued}"
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).digest()
    try:
        got = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
    except Exception:
        return None
    if not hmac.compare_digest(expected, got):
        return None
    try:
        issued_i = int(issued)
    except ValueError:
        return None
    if issued_i + TOKEN_TTL < time.time():
        return None
    u = _load().get(_canon(username))
    if not u:                       # account deleted since issue
        return None
    if issued_i < int(u.get("password_changed", 0)):   # revoked by a password change
        return None
    return _canon(username)
