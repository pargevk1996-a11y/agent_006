"""Secret masking helpers.

Two independent layers protect credentials:

1. :func:`mask_secret` — used at call sites that deliberately want to show a
   token fingerprint (e.g. ``sk_l***c3f2``) without revealing it.
2. :func:`redact` — a defensive sweep applied to every log record, scrubbing
   registered secret values and anything that looks like an ``Authorization``
   header, even when a developer logs something they should not have.
"""

from __future__ import annotations

import re

_SECRETS: set[str] = set()

_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization|x-vibe-signature|api[_-]?token|webhook[_-]?secret)"
    r"(\"?\s*[:=]\s*\"?)(bearer\s+)?([^\s,;\"'}\]]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{6,}")
_PLACEHOLDER = "***REDACTED***"


def register_secret(value: str | None) -> None:
    """Register a literal secret so :func:`redact` can scrub it anywhere."""
    if value and len(value) >= 4:
        _SECRETS.add(value)


def clear_secrets() -> None:
    _SECRETS.clear()


def mask_secret(value: str | None, *, keep_prefix: int = 4, keep_suffix: int = 4) -> str:
    """Return a short, non-reversible fingerprint of a secret.

    Short secrets are masked completely rather than partially exposed.
    """
    if not value:
        return "<unset>"
    if len(value) <= keep_prefix + keep_suffix + 2:
        return "*" * 8
    return f"{value[:keep_prefix]}***{value[-keep_suffix:]}(len={len(value)})"


def redact(text: str) -> str:
    """Scrub registered secrets and credential-looking substrings from ``text``."""
    if not text:
        return text
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, _PLACEHOLDER)
    text = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_PLACEHOLDER}", text)
    return _BEARER_RE.sub(f"Bearer {_PLACEHOLDER}", text)


def safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Header dict safe for logging: credential headers are replaced, not truncated."""
    sensitive = {"authorization", "x-vibe-signature", "cookie", "set-cookie", "proxy-authorization"}
    return {k: (_PLACEHOLDER if k.lower() in sensitive else v) for k, v in headers.items()}
