"""Per-user settings (review #5 — per-user Claude credentials instead of one shared token).

Stored as JSON keyed by CF Access email slug in /state (gitignored, never committed). The
credentials blob is a secret: it is NEVER returned to the client in full — only a masked refresh
tail, an access-token-present flag, and the access expiry.

Why the FULL credentials blob (not just a refresh token): a refresh token alone cannot bootstrap
claude (verified — it reports "Not logged in"); claude needs an existing access token to refresh
from. So we store the whole `~/.claude/.credentials.json` blob (access + refresh) and let the
per-user seed refresh it natively.

Schema is intentionally open so admin-only fields can be added later without a migration:
    { "<slug>": { "credentials": {claudeAiOauth: {...}}, ... } }
"""
import json
import os
from pathlib import Path

SETTINGS_FILE = Path(os.environ.get("CCCHAT_USER_SETTINGS", "/state/user-settings.json"))

USER_FIELDS = ("credentials", "oauth_token")
ADMIN_FIELDS = ()  # reserved (e.g. limits, forced rotation) — architected, none yet


def _load() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    tmp.replace(SETTINGS_FILE)


def get(slug: str) -> dict:
    return _load().get(slug, {})


def get_credentials(slug: str):
    """The stored full credentials blob ({claudeAiOauth: {...}}) for this user, or None."""
    return (get(slug) or {}).get("credentials") or None


def get_refresh_token(slug: str):
    c = get_credentials(slug) or {}
    return (c.get("claudeAiOauth") or {}).get("refreshToken") or None


def get_oauth_token(slug: str):
    """The long-lived `claude setup-token` (CLAUDE_CODE_OAUTH_TOKEN), or None. When set, sessions
    authenticate with this env var directly — no per-user seed / refresh dance."""
    return (get(slug) or {}).get("oauth_token") or None


def set_oauth_token(slug: str, token) -> str:
    """Validate + store a setup-token. Pass an empty string to clear it. Returns the stored value."""
    token = (token or "").strip()
    if token and not token.startswith("sk-ant-oat"):
        raise CredentialError("expected a `claude setup-token` value starting with sk-ant-oat…")
    update(slug, {"oauth_token": token})
    return token


def has_auth(slug: str) -> bool:
    """True if the user can run sessions — either a setup-token or a credentials blob is set."""
    return bool(get_oauth_token(slug) or get_credentials(slug))


class CredentialError(ValueError):
    pass


def parse_credentials(raw):
    """Accept the pasted `~/.claude/.credentials.json` (a JSON string or already-parsed dict),
    validate it holds an OAuth access+refresh pair, and return the normalised blob.
    Raises CredentialError on anything malformed."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception as e:
            raise CredentialError(f"not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise CredentialError("expected a JSON object")
    oauth = raw.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise CredentialError("missing claudeAiOauth")
    if not oauth.get("accessToken") or not oauth.get("refreshToken"):
        raise CredentialError("claudeAiOauth needs both accessToken and refreshToken")
    return {"claudeAiOauth": oauth}


def update(slug: str, fields: dict, allow_admin: bool = False) -> dict:
    """Merge allowed fields into a user's settings. Unknown/disallowed keys are ignored."""
    allowed = set(USER_FIELDS) | (set(ADMIN_FIELDS) if allow_admin else set())
    d = _load()
    cur = d.get(slug, {})
    for k, v in (fields or {}).items():
        if k in allowed and v is not None:
            cur[k] = v
    d[slug] = cur
    _save(d)
    return cur


def set_credentials(slug: str, raw) -> dict:
    """Validate + store the full credentials blob. Returns the parsed blob."""
    blob = parse_credentials(raw)
    update(slug, {"credentials": blob})
    return blob


def mask(token) -> str | None:
    if not token:
        return None
    return ("…" + token[-4:]) if len(token) > 4 else "…set"


def public_view(slug: str, is_admin: bool) -> dict:
    """What the settings form may see — masked secrets only, never the raw blob."""
    oauth = (get_credentials(slug) or {}).get("claudeAiOauth") or {}
    tok = get_oauth_token(slug)
    return {
        "is_admin": is_admin,
        "credentials_set": bool(oauth.get("refreshToken")),
        "refresh_token_masked": mask(oauth.get("refreshToken")),
        "expires_at": oauth.get("expiresAt"),
        "subscription": oauth.get("subscriptionType"),
        "oauth_token_set": bool(tok),
        "oauth_token_masked": mask(tok),
        "fields": list(USER_FIELDS) + (list(ADMIN_FIELDS) if is_admin else []),
    }
