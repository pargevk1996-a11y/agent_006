"""Media upload: turning a local file into a URL the planner can use.

Several of the most interesting video models are image-to-video — ``veo3.1``,
``kling-3.0``, ``grok-itv`` — and every one of them requires ``image_urls`` with
a *stable* URL. Without a place to put files, those models are permanently
unreachable: the planner rejects them with "нет обязательных параметров:
image_urls", and the user is told to go find hosting on their own.

This service proxies ``POST /upload-media`` so the round trip stays inside the
API: upload a file, get a URL, put it in ``reference_image_urls`` of the brief.
Nothing here is billable — uploading is free, and the money path is untouched.

Validation happens **before** the bytes leave this process: the size ceiling and
the accepted formats are read from ``/capabilities`` (see
:mod:`app.domain.media`), so a file the platform would reject is refused without
shipping it across the network first.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.clients.base import VibeClient
from app.clients.exceptions import VibeError
from app.core.errors import ValidationError
from app.domain.media import MediaKind, MediaLimits
from app.repositories.media import MediaRepository

logger = logging.getLogger(__name__)

#: Keys the platform might carry the stable URL under, in order of preference.
URL_KEYS = ("url", "file_url", "media_url", "stable_url", "public_url", "link")
NESTED_KEYS = ("data", "file", "media", "result")


@dataclass(frozen=True)
class UploadedMedia:
    url: str
    filename: str
    content_type: str
    size_bytes: int
    kind: str
    expires_in_days: int


class MediaService:
    def __init__(self, *, client: VibeClient, uploads: MediaRepository | None = None) -> None:
        self.client = client
        #: Remembering an upload is what lets the planner warn before the link dies.
        self.uploads = uploads

    async def limits(self) -> MediaLimits:
        """Upload rules from the catalog, with a conservative fallback."""
        try:
            return MediaLimits.parse(await self.client.capabilities())
        except VibeError as exc:
            logger.warning("upload_limits_unavailable", extra={"detail": str(exc)})
            return MediaLimits.fallback()

    async def upload(
        self,
        *,
        filename: str,
        content_type: str,
        chunks: AsyncIterator[bytes],
    ) -> UploadedMedia:
        limits = await self.limits()
        kind = self._kind_for(filename, limits)
        content = await self._read_bounded(chunks, kind)

        payload = await self.client.upload_media(
            filename=filename,
            content=content,
            content_type=content_type or f"application/{kind.name}",
        )
        url = _url_of(payload)
        if url is None:
            raise VibeError(
                "POST /upload-media не вернул ссылку на файл — использовать его в брифе "
                f"нельзя (получены поля: {sorted(payload)})."
            )

        ttl_days = _ttl_of(payload, limits)
        if self.uploads is not None:
            await self.uploads.record(
                url=url,
                filename=filename,
                kind=kind.name,
                size_bytes=len(content),
                ttl_days=ttl_days,
            )

        logger.info(
            "media_uploaded",
            extra={
                # `filename` is reserved by LogRecord — using it raises at log time.
                "media_filename": filename,
                "kind": kind.name,
                "size_bytes": len(content),
                "limits_source": limits.source,
                "expires_in_days": ttl_days,
            },
        )
        return UploadedMedia(
            url=url,
            filename=filename,
            content_type=content_type,
            size_bytes=len(content),
            kind=kind.name,
            expires_in_days=ttl_days,
        )

    def _kind_for(self, filename: str, limits: MediaLimits) -> MediaKind:
        extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
        kind = limits.kind_for(extension) if extension else None
        if kind is None:
            raise ValidationError(
                f"Формат «{extension or filename}» не принимается платформой. "
                f"Допустимо — {limits.describe()}.",
                details={"limits_source": limits.source},
            )
        return kind

    async def _read_bounded(self, chunks: AsyncIterator[bytes], kind: MediaKind) -> bytes:
        """Read the file, stopping the moment it exceeds its ceiling.

        Reading past the limit only to reject the result would let any caller make
        the process buffer an arbitrary amount of memory.
        """
        buffer = bytearray()
        async for chunk in chunks:
            buffer.extend(chunk)
            if len(buffer) > kind.max_bytes:
                raise ValidationError(
                    f"Файл больше допустимых {kind.max_bytes // (1024 * 1024)} МБ "
                    f"для типа «{kind.name}» — загрузка прервана."
                )
        if not buffer:
            raise ValidationError("Файл пустой — загружать нечего.")
        return bytes(buffer)


def _url_of(payload: dict[str, Any]) -> str | None:
    """The stable URL, wherever the platform put it.

    The catalog documents only "JSON with stable URL", so the key is not
    guaranteed; a tolerant search beats guessing one name and failing.
    """
    for key in URL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    for key in NESTED_KEYS:
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = _url_of(nested)
            if found is not None:
                return found
    return None


def _ttl_of(payload: dict[str, Any], limits: MediaLimits) -> int:
    for key in ("expires_in_days", "ttl_days"):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return limits.ttl_days
