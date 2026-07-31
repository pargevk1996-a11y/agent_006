"""Media upload — the missing half of image-to-video planning.

The HTTP mechanics live here (multipart, streaming the file in bounded chunks);
all the policy is in :mod:`app.services.media`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, File, UploadFile

from app.api.deps import get_media_service
from app.api.schemas import MediaKindResponse, MediaLimitsResponse, MediaUploadResponse
from app.services.media import MediaService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/media", tags=["media"])

#: Small enough that an oversized file is refused long before it is buffered.
CHUNK_BYTES = 256 * 1024


@router.post(
    "",
    response_model=MediaUploadResponse,
    status_code=201,
    summary="Загрузить файл и получить стабильный URL для брифа (0 ₽)",
    description=(
        "Проксирует POST /upload-media платформы. Возвращает стабильный URL, который "
        "подставляется в reference_image_urls брифа и разблокирует image-to-video модели "
        "(им нужен обязательный параметр image_urls). Загрузка бесплатна и не трогает "
        "бюджет. Размер и формат проверяются по лимитам из /capabilities до отправки "
        "файла — заведомо неподходящий файл не поедет по сети."
    ),
)
async def upload_media(
    file: UploadFile = File(..., description="Изображение, видео или аудио"),
    service: MediaService = Depends(get_media_service),
) -> MediaUploadResponse:
    uploaded = await service.upload(
        filename=file.filename or "upload.bin",
        content_type=file.content_type or "",
        chunks=_chunks(file),
    )
    return MediaUploadResponse(**uploaded.__dict__)


@router.get(
    "/limits",
    response_model=MediaLimitsResponse,
    summary="Что и какого размера принимает платформа",
    description=(
        "Лимиты читаются из GET /capabilities, а не захардкожены: если платформа поднимет "
        "потолок или добавит формат, сервис последует за ней. source=fallback означает, "
        "что каталог недоступен и действуют консервативные значения по документации."
    ),
)
async def media_limits(
    service: MediaService = Depends(get_media_service),
) -> MediaLimitsResponse:
    limits = await service.limits()
    return MediaLimitsResponse(
        kinds=[
            MediaKindResponse(
                kind=kind.name,
                max_bytes=kind.max_bytes,
                max_megabytes=kind.max_bytes // (1024 * 1024),
                extensions=sorted(kind.extensions),
            )
            for kind in limits.kinds
        ],
        ttl_days=limits.ttl_days,
        source=limits.source,
    )


async def _chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(CHUNK_BYTES):
        yield chunk
