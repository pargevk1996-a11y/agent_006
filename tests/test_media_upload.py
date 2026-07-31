"""Загрузка медиа — то, без чего image-to-video модели недостижимы.

`veo3.1`, `kling-3.0`, `grok-itv` требуют `image_urls` со стабильным URL. Пока
файл некуда положить, планировщик честно отклоняет их с «нет обязательных
параметров», и пользователю остаётся искать хостинг самому. Здесь проверяется,
что круг замыкается внутри API и что заведомо неподходящий файл не уезжает по
сети.
"""

from __future__ import annotations

import pytest

from app.clients.exceptions import VibeAPIError, VibeError
from app.core.errors import ValidationError
from app.domain.media import MediaLimits
from app.services.media import MediaService, _url_of

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 512

BRIEF = {
    "product_name": "CRM для мастеров маникюра",
    "product_description": "Онлайн-запись, напоминания и учёт расходников.",
    "target_audience": "Мастера маникюра, 22–40 лет",
    "offer": "Первый месяц бесплатно",
    "formats": ["video"],
    "budget_rub": 400,
}


async def chunks_of(data: bytes, size: int = 64):
    for start in range(0, len(data), size):
        yield data[start : start + size]


class TestLimitsComeFromTheCatalog:
    def test_parsed_from_the_real_snapshot(self, capabilities):
        limits = MediaLimits.parse(capabilities.raw)

        assert limits.source == "capabilities"
        assert {k.name for k in limits.kinds} == {"image", "video", "audio"}
        assert limits.kind_for("png").max_bytes == 30 * 1024 * 1024
        assert limits.kind_for("mp4").max_bytes == 50 * 1024 * 1024
        assert limits.kind_for("mp3").max_bytes == 15 * 1024 * 1024
        assert limits.ttl_days == 7

    def test_jpg_and_jpeg_are_the_same_format(self, capabilities):
        limits = MediaLimits.parse(capabilities.raw)
        assert limits.kind_for("jpg") is not None
        assert limits.kind_for("jpeg") is not None

    def test_unparsable_catalog_falls_back_conservatively(self):
        limits = MediaLimits.parse({"upload_endpoint": {"limits": {"image": "как получится"}}})

        assert limits.source == "fallback"
        assert limits.kind_for("png").max_bytes == 30 * 1024 * 1024
        assert limits.kind_for("exe") is None

    def test_a_raised_platform_limit_is_followed(self):
        limits = MediaLimits.parse(
            {"upload_endpoint": {"limits": {"image": "80 MB (png/avif)"}, "ttl_days": 30}}
        )

        assert limits.kind_for("avif").max_bytes == 80 * 1024 * 1024
        assert limits.ttl_days == 30


class TestUploadEndpoint:
    async def test_file_becomes_a_stable_url(self, api_client):
        response = await api_client.post(
            "/api/v1/media", files={"file": ("logo.png", PNG, "image/png")}
        )

        assert response.status_code == 201
        body = response.json()
        assert body["url"].startswith("http")
        assert body["kind"] == "image"
        assert body["size_bytes"] == len(PNG)
        assert body["expires_in_days"] == 7
        assert "reference_image_urls" in body["usage"]

    async def test_the_url_unlocks_image_to_video_models(self, api_client):
        """Полный круг: загрузили файл — и модель, которую раньше отклоняли, доступна."""
        without = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        rejected = [
            alt["reason"]
            for step in without["steps"]
            for alt in step["rejected_alternatives"]
        ]
        assert any("image_urls" in reason for reason in rejected), (
            "без картинок image-to-video модели должны отклоняться"
        )

        url = (
            await api_client.post(
                "/api/v1/media", files={"file": ("frame.png", PNG, "image/png")}
            )
        ).json()["url"]
        with_image = (
            await api_client.post(
                "/api/v1/plans", json={**BRIEF, "reference_image_urls": [url]}
            )
        ).json()

        video_step = next(s for s in with_image["steps"] if s["format"] == "video")
        assert video_step["params"].get("image_urls") == [url]
        assert with_image["total_estimated_rub"] <= with_image["budget_rub"]

    async def test_unsupported_format_is_refused(self, api_client):
        response = await api_client.post(
            "/api/v1/media", files={"file": ("payload.exe", b"MZ\x00\x00", "application/exe")}
        )

        assert response.status_code == 422
        assert "не принимается платформой" in response.json()["message"]

    async def test_empty_file_is_refused(self, api_client):
        response = await api_client.post(
            "/api/v1/media", files={"file": ("empty.png", b"", "image/png")}
        )
        assert response.status_code == 422

    async def test_upload_spends_nothing(self, api_client):
        await api_client.post("/api/v1/media", files={"file": ("logo.png", PNG, "image/png")})
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()

        assert plan["account"]["balance_rub"] == 5000.0

    async def test_limits_endpoint_describes_the_rules(self, api_client):
        body = (await api_client.get("/api/v1/media/limits")).json()

        assert body["source"] == "capabilities"
        assert body["ttl_days"] == 7
        image = next(k for k in body["kinds"] if k["kind"] == "image")
        assert image["max_megabytes"] == 30
        assert "png" in image["extensions"]


class TestOversizedFilesAreStopped:
    async def test_reading_stops_at_the_ceiling(self, client):
        """Дочитывать гигабайт, чтобы потом его отклонить, — это способ съесть память."""
        service = MediaService(client=client)
        limits = await service.limits()
        kind = limits.kind_for("png")
        oversized = kind.max_bytes + 1024

        consumed = 0

        async def endless():
            nonlocal consumed
            while True:
                consumed += 65536
                yield b"\x00" * 65536

        with pytest.raises(ValidationError, match="больше допустимых"):
            await service.upload(
                filename="huge.png", content_type="image/png", chunks=endless()
            )

        assert consumed <= oversized + 65536, "чтение должно оборваться на лимите"
        assert client.calls_of("upload_media") == [], "оверсайз не должен уезжать по сети"


class TestUpstreamAnswers:
    def test_url_is_found_wherever_the_platform_puts_it(self):
        assert _url_of({"url": "https://a"}) == "https://a"
        assert _url_of({"file_url": "https://b"}) == "https://b"
        assert _url_of({"data": {"stable_url": "https://c"}}) == "https://c"
        assert _url_of({"status": "ok"}) is None
        assert _url_of({"url": "/relative/path"}) is None

    async def test_missing_url_is_an_upstream_failure(self, client):
        service = MediaService(client=client)

        async def no_url(**kwargs):
            return {"status": "ok"}

        client.upload_media = no_url
        with pytest.raises(VibeError, match="не вернул ссылку"):
            await service.upload(
                filename="logo.png", content_type="image/png", chunks=chunks_of(PNG)
            )

    async def test_unreachable_catalog_does_not_block_uploads(self, client):
        """Каталог недоступен — работаем по консервативным лимитам, а не падаем."""
        service = MediaService(client=client)

        async def broken():
            raise VibeAPIError(503, code="upstream_unavailable", message="каталог лежит")

        client.capabilities = broken
        limits = await service.limits()
        assert limits.source == "fallback"

        uploaded = await service.upload(
            filename="logo.png", content_type="image/png", chunks=chunks_of(PNG)
        )
        assert uploaded.url.startswith("http")
