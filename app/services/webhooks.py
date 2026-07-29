"""Webhook signature verification.

Per the Agent API docs::

    expected = HMAC-SHA256(raw_request_body, webhook_secret)
    verify:   constant_time_compare(expected, X-Vibe-Signature)

Tokens issued before 2026-07-09 sign with ``sha256(raw_api_token)`` as the secret
instead of a dedicated webhook secret; that path is opt-in via
``VIBE_WEBHOOK_LEGACY_FALLBACK``.

The signature is computed over the **raw bytes** of the request, before any JSON
parsing — re-serialising the body would change whitespace and break the HMAC.
"""

from __future__ import annotations

import hashlib
import hmac

from app.core.errors import WebhookVerificationError


def legacy_secret(api_token: str) -> str:
    """Legacy scheme: the webhook secret is the hex sha256 of the API token."""
    return hashlib.sha256(api_token.encode("utf-8")).hexdigest()


def compute_signature(raw_body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def signature_matches(raw_body: bytes, signature: str, secret: str) -> bool:
    if not signature or not secret:
        return False
    expected = compute_signature(raw_body, secret)
    return hmac.compare_digest(expected, signature.strip().lower())


def verify_webhook(
    raw_body: bytes,
    signature: str | None,
    *,
    secret: str | None,
    api_token: str | None = None,
    allow_legacy: bool = False,
) -> None:
    """Raise :class:`WebhookVerificationError` unless the payload is authentic.

    Fail-closed by design: a missing header, a missing configured secret, or a
    mismatch are all rejections. An unsigned webhook is never trusted.
    """
    if not signature:
        raise WebhookVerificationError("Отсутствует заголовок X-Vibe-Signature.")

    secrets: list[str] = []
    if secret:
        secrets.append(secret)
    if allow_legacy and api_token:
        secrets.append(legacy_secret(api_token))
    if not secrets:
        raise WebhookVerificationError(
            "VIBE_WEBHOOK_SECRET не сконфигурирован — вебхук не может быть проверен."
        )

    if not any(signature_matches(raw_body, signature, candidate) for candidate in secrets):
        raise WebhookVerificationError("Подпись вебхука не совпадает.")
