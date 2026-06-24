"""Transactional email via Resend (password reset / email verification).

Send-only: uses the restricted RESEND_API_KEY from the environment. No extra deps — just urllib.
All sends are best-effort; a failure is logged and returned as (False, error) so callers can decide
what to surface (we never leak whether an account exists, so the API responds the same either way).
"""
import json
import logging
import os
import urllib.request

log = logging.getLogger("uvicorn.error")

API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
BASE_URL = os.environ.get("CCCHAT_BASE_URL", "").rstrip("/")
ENDPOINT = "https://api.resend.com/emails"


def enabled() -> bool:
    return bool(API_KEY)


def send(to: str, subject: str, html: str) -> tuple:
    """Send one email. Returns (ok, info)."""
    if not API_KEY:
        return False, "email not configured (RESEND_API_KEY unset)"
    payload = json.dumps({"from": FROM, "to": [to], "subject": subject, "html": html}).encode()
    req = urllib.request.Request(
        ENDPOINT, data=payload, method="POST",
        # A real User-Agent is required: Resend is behind Cloudflare, which blocks the default
        # "Python-urllib/x" signature with HTTP 403 "error code: 1010".
        headers={"Authorization": "Bearer " + API_KEY, "Content-Type": "application/json",
                 "User-Agent": "vivarium/1.0 (+https://github.com/balalexv-coder/ccchat-hardening)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return True, data.get("id", "")
    except Exception as e:  # noqa: BLE001 — never let an email failure break the request
        log.warning("[mailer] send to %s failed: %s", to, e)
        return False, str(e)


def _wrap(title: str, body_html: str) -> str:
    return (
        '<div style="font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;color:#222">'
        f'<h2 style="color:#3b6fd4">{title}</h2>{body_html}'
        '<hr style="border:none;border-top:1px solid #eee;margin:24px 0">'
        '<p style="color:#999;font-size:12px">ccchat · если вы не запрашивали это письмо, просто проигнорируйте его.</p>'
        '</div>'
    )


def send_reset(to: str, username: str, token: str) -> tuple:
    link = f"{BASE_URL}/reset?token={token}"
    html = _wrap("Сброс пароля", (
        f'<p>Запрошен сброс пароля для аккаунта <b>{username}</b>.</p>'
        f'<p><a href="{link}" style="display:inline-block;background:#3b6fd4;color:#fff;'
        'text-decoration:none;padding:11px 20px;border-radius:8px">Задать новый пароль</a></p>'
        f'<p style="color:#999;font-size:12px">Ссылка действует 30 минут. Или открой: {link}</p>'
    ))
    return send(to, "ccchat — сброс пароля", html)


def send_verify(to: str, username: str, token: str) -> tuple:
    link = f"{BASE_URL}/verify-email?token={token}"
    html = _wrap("Подтверждение email", (
        f'<p>Подтвердите адрес для аккаунта <b>{username}</b> — он нужен для восстановления пароля.</p>'
        f'<p><a href="{link}" style="display:inline-block;background:#3b6fd4;color:#fff;'
        'text-decoration:none;padding:11px 20px;border-radius:8px">Подтвердить email</a></p>'
        f'<p style="color:#999;font-size:12px">Или открой: {link}</p>'
    ))
    return send(to, "ccchat — подтверждение email", html)
