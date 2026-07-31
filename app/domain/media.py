"""Upload limits, read from the catalog rather than hard-coded.

``/capabilities`` describes ``upload_endpoint`` in prose::

    "limits": {"image": "30 MB (jpeg/png/webp/gif)", "video": "50 MB (mp4/mov)", ...}

Parsing that keeps the service honest when the platform raises a limit or adds a
format: rejecting a file the platform would have accepted is our bug, not the
user's. When the shape is unrecognisable we fall back to the values observed at
the time of writing — conservative, and never larger than what is documented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Used only when the catalog cannot be parsed; mirrors the documented limits.
FALLBACK_LIMITS: dict[str, tuple[int, tuple[str, ...]]] = {
    "image": (30, ("jpeg", "jpg", "png", "webp", "gif")),
    "video": (50, ("mp4", "mov")),
    "audio": (15, ("mp3", "wav")),
}

#: ``jpg`` and ``jpeg`` are the same format; the catalog only spells one of them.
EXTENSION_ALIASES = {"jpeg": ("jpg",), "jpg": ("jpeg",)}

_LIMIT_RE = re.compile(r"(?P<mb>\d+)\s*MB\s*\((?P<formats>[^)]*)\)", re.IGNORECASE)


@dataclass(frozen=True)
class MediaKind:
    """One row of the upload limits table: a kind, its ceiling and its formats."""

    name: str
    max_bytes: int
    extensions: tuple[str, ...]

    def accepts(self, extension: str) -> bool:
        return extension.lower().lstrip(".") in self.extensions


@dataclass(frozen=True)
class MediaLimits:
    kinds: tuple[MediaKind, ...]
    ttl_days: int = 7
    source: str = "fallback"

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> MediaLimits:
        endpoint = payload.get("upload_endpoint")
        if not isinstance(endpoint, dict):
            return cls.fallback()
        limits = endpoint.get("limits")
        if not isinstance(limits, dict):
            return cls.fallback()

        kinds: list[MediaKind] = []
        for name, description in limits.items():
            parsed = _parse_limit(str(name), str(description))
            if parsed is not None:
                kinds.append(parsed)
        if not kinds:
            return cls.fallback()

        ttl = endpoint.get("ttl_days")
        return cls(
            kinds=tuple(kinds),
            ttl_days=int(ttl) if isinstance(ttl, int) and ttl > 0 else 7,
            source="capabilities",
        )

    @classmethod
    def fallback(cls) -> MediaLimits:
        return cls(
            kinds=tuple(
                MediaKind(name, mb * 1024 * 1024, _with_aliases(exts))
                for name, (mb, exts) in FALLBACK_LIMITS.items()
            ),
            source="fallback",
        )

    def kind_for(self, extension: str) -> MediaKind | None:
        return next((k for k in self.kinds if k.accepts(extension)), None)

    @property
    def max_bytes(self) -> int:
        """Largest file any kind allows — the cheap first check before the specific one."""
        return max((k.max_bytes for k in self.kinds), default=0)

    def describe(self) -> str:
        return "; ".join(
            f"{k.name}: до {k.max_bytes // (1024 * 1024)} МБ ({', '.join(sorted(k.extensions))})"
            for k in self.kinds
        )


def _parse_limit(name: str, description: str) -> MediaKind | None:
    match = _LIMIT_RE.search(description)
    if match is None:
        return None
    megabytes = int(match.group("mb"))
    extensions = tuple(
        part.strip().lower().lstrip(".")
        for part in match.group("formats").split("/")
        if part.strip()
    )
    if not megabytes or not extensions:
        return None
    return MediaKind(name, megabytes * 1024 * 1024, _with_aliases(extensions))


def _with_aliases(extensions: tuple[str, ...]) -> tuple[str, ...]:
    expanded = list(extensions)
    for ext in extensions:
        expanded.extend(a for a in EXTENSION_ALIASES.get(ext, ()) if a not in expanded)
    return tuple(expanded)
