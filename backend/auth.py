"""Identity resolution for ccchat (review #1).

Cloudflare Access sits in front of ccchat and, on every request that passed SSO, injects
`Cf-Access-Authenticated-User-Email` plus a signed `Cf-Access-Jwt-Assertion` JWT. The ORIGIN
is reachable directly by IP, so trusting a plain request header for identity let anyone bypass
per-user isolation. Hardening here:

  * The legacy `x-user-email` fallback is REMOVED — it was a trivial impersonation vector.
  * When CF_ACCESS_TEAM_DOMAIN + CF_ACCESS_AUD are configured, the signed CF Access JWT is
    verified (signature against the team JWKS + audience) and identity comes from its claims,
    so a forged header alone is not enough.
  * Without that config we fall back to the CF-injected email header (previous behaviour),
    which is only safe if the origin is locked to Cloudflare. Set CCCHAT_REQUIRE_AUTH=1 to
    reject unidentifiable requests instead of bucketing them as "anon".
"""
import os

CF_TEAM_DOMAIN = os.environ.get("CF_ACCESS_TEAM_DOMAIN", "").strip().rstrip("/")
CF_AUD = os.environ.get("CF_ACCESS_AUD", "").strip()
REQUIRE_AUTH = os.environ.get("CCCHAT_REQUIRE_AUTH", "").lower() in ("1", "true", "yes")

EMAIL_HEADER = "cf-access-authenticated-user-email"
JWT_HEADER = "cf-access-jwt-assertion"

_jwks_client = None


def _verify_jwt(token: str):
    """Verify a CF Access JWT and return its email claim, or None. PyJWT is imported lazily so
    the header path keeps working even if the optional dependency is absent."""
    if not (CF_TEAM_DOMAIN and CF_AUD and token):
        return None
    global _jwks_client
    try:
        import jwt
        from jwt import PyJWKClient
        if _jwks_client is None:
            _jwks_client = PyJWKClient(f"https://{CF_TEAM_DOMAIN}/cdn-cgi/access/certs")
        signing_key = _jwks_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, signing_key, algorithms=["RS256"], audience=CF_AUD)
        return claims.get("email") or claims.get("identity") or None
    except Exception:
        return None


def resolve_email(get_header):
    """Resolve the authenticated user's email from request headers.

    `get_header(name)` returns a header value (case-insensitive) or None. Returns a lowercased
    email, or None when the request cannot be trusted/identified. Never honours `x-user-email`.
    """
    if CF_TEAM_DOMAIN and CF_AUD:
        # strict mode: identity MUST come from a verified JWT
        email = _verify_jwt(get_header(JWT_HEADER) or "")
        return email.lower() if email else None
    # compat mode: trust ONLY the CF-injected email header (not arbitrary client headers)
    email = get_header(EMAIL_HEADER)
    return email.lower() if email else None
