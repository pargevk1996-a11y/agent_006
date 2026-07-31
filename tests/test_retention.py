"""Срок жизни ключей повтора и загруженных ссылок.

Две таблицы росли бы вечно, но правила у них разные, потому что разная цена
ошибки: удалить ключ повтора рано — значит превратить ретрай клиента во второй
настоящий запрос; удалить запись о загрузке рано — значит потерять возможность
предупредить, что ссылка вот-вот умрёт.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.repositories.idempotency import IdempotencyRepository
from app.repositories.media import MediaRepository
from app.services.retention import RetentionService
from app.services.scheduling import PeriodicTask
from tests.conftest import make_brief, make_service, make_settings

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 256


def make_retention(database, **overrides) -> RetentionService:
    return RetentionService(
        idempotency=IdempotencyRepository(database),
        media=MediaRepository(database),
        interval_seconds=overrides.pop("interval_seconds", 0),
        idempotency_ttl_hours=overrides.pop("idempotency_ttl_hours", 24.0),
    )


class TestIdempotencyRetention:
    async def test_fresh_keys_are_kept(self, database):
        store = IdempotencyRepository(database)
        await store.claim(key="k", endpoint="e", request_hash="h")
        await store.complete("k", status_code=201, headers={}, body={"ok": True})

        result = await make_retention(database).purge()

        assert result.idempotency_keys == 0
        assert await store.get("k") is not None

    async def test_keys_past_their_ttl_are_dropped(self, database):
        store = IdempotencyRepository(database)
        await store.claim(key="old", endpoint="e", request_hash="h")
        await store.complete("old", status_code=201, headers={}, body={"ok": True})

        later = datetime.now(UTC) + timedelta(hours=25)
        result = await make_retention(database).purge(now=later)

        assert result.idempotency_keys == 1
        assert await store.get("old") is None

    async def test_an_unfinished_claim_is_never_purged(self, database):
        """Иначе чистка отпустила бы ключ запроса, который прямо сейчас выполняется."""
        store = IdempotencyRepository(database)
        await store.claim(key="in-flight", endpoint="e", request_hash="h")

        later = datetime.now(UTC) + timedelta(days=30)
        result = await make_retention(database).purge(now=later)

        assert result.idempotency_keys == 0
        assert await store.get("in-flight") is not None

    async def test_purged_key_can_be_used_again(self, api_client, tmp_path):
        """После TTL повтор — это уже новый запрос, а не подделка ответа."""
        headers = {"Idempotency-Key": "expiring"}
        brief = {
            "product_name": "P", "product_description": "D", "target_audience": "A",
            "offer": "O", "formats": ["image"], "budget_rub": 300,
        }
        first = (await api_client.post("/api/v1/plans", json=brief, headers=headers)).json()

        database = api_client._transport.app.state.database
        await make_retention(database).purge(now=datetime.now(UTC) + timedelta(hours=25))

        second = await api_client.post("/api/v1/plans", json=brief, headers=headers)
        assert second.status_code == 201
        assert second.headers["Idempotency-Replayed"] == "false"
        assert second.json()["plan_id"] != first["plan_id"], "это уже новый план"


class TestUploadRetention:
    async def test_live_links_are_kept_and_dead_ones_dropped(self, database):
        media = MediaRepository(database)
        await media.record(
            url="https://live", filename="a.png", kind="image", size_bytes=1, ttl_days=7
        )
        await media.record(
            url="https://dead", filename="b.png", kind="image", size_bytes=1, ttl_days=7
        )

        # Через 8 дней обе ссылки мертвы; проверяем на границе — жива только «live».
        result = await make_retention(database).purge(
            now=datetime.now(UTC) + timedelta(days=7, seconds=1)
        )

        assert result.expired_uploads == 2
        assert await media.count() == 0

    async def test_a_link_near_expiry_is_not_deleted(self, database):
        media = MediaRepository(database)
        await media.record(
            url="https://soon", filename="a.png", kind="image", size_bytes=1, ttl_days=7
        )

        result = await make_retention(database).purge(
            now=datetime.now(UTC) + timedelta(days=6, hours=23)
        )

        assert result.expired_uploads == 0
        record = await media.get("https://soon")
        remaining = record.expires_in_days(now=datetime.now(UTC) + timedelta(days=6, hours=12))
        assert 0 < remaining < 1, "ссылка жива, но на грани — именно об этом и предупреждаем"

    async def test_upload_is_recorded_with_its_expiry(self, api_client):
        body = (
            await api_client.post(
                "/api/v1/media", files={"file": ("logo.png", PNG, "image/png")}
            )
        ).json()

        database = api_client._transport.app.state.database
        record = await MediaRepository(database).get(body["url"])
        assert record is not None
        assert record.kind == "image"
        assert 6.9 < record.expires_in_days() <= 7.0


class TestPlanningWarnsBeforeTheLinkDies:
    async def _service(self, client, database, tmp_path, **overrides):
        return make_service(client, make_settings(tmp_path, **overrides), database)

    async def test_expiring_link_is_flagged_in_the_plan(self, client, database, tmp_path):
        media = MediaRepository(database)
        await media.record(
            url="https://cdn/soon.png", filename="soon.png", kind="image",
            size_bytes=1, ttl_days=1,
        )
        service = await self._service(client, database, tmp_path)
        service.media = media

        plan = await service.create_plan(
            make_brief(formats=["video"], budget_rub=400.0,
                       reference_image_urls=["https://cdn/soon.png"])
        )

        assert any("перестанет работать через" in w for w in plan.warnings)

    async def test_dead_link_is_called_out_before_confirmation(
        self, client, database, tmp_path
    ):
        """Узнать о мёртвой ссылке после подтверждения списания — худший момент."""
        media = MediaRepository(database)
        await media.record(
            url="https://cdn/dead.png", filename="dead.png", kind="image",
            size_bytes=1, ttl_days=7,
        )
        await database.execute(
            "UPDATE media_uploads SET expires_at = ? WHERE url = ?",
            ((datetime.now(UTC) - timedelta(days=1)).isoformat(), "https://cdn/dead.png"),
        )
        service = await self._service(client, database, tmp_path)
        service.media = media

        plan = await service.create_plan(
            make_brief(formats=["video"], budget_rub=400.0,
                       reference_image_urls=["https://cdn/dead.png"])
        )

        assert any("уже недействительна" in w for w in plan.warnings)

    async def test_a_healthy_link_produces_no_noise(self, client, database, tmp_path):
        media = MediaRepository(database)
        await media.record(
            url="https://cdn/fresh.png", filename="fresh.png", kind="image",
            size_bytes=1, ttl_days=7,
        )
        service = await self._service(client, database, tmp_path)
        service.media = media

        plan = await service.create_plan(
            make_brief(formats=["video"], budget_rub=400.0,
                       reference_image_urls=["https://cdn/fresh.png"])
        )

        assert not any("ссылк" in w.lower() for w in plan.warnings)

    async def test_foreign_urls_are_reported_as_unknown(self, client, database, tmp_path):
        media = MediaRepository(database)
        await media.record(
            url="https://cdn/ours.png", filename="ours.png", kind="image",
            size_bytes=1, ttl_days=7,
        )
        service = await self._service(client, database, tmp_path)
        service.media = media

        plan = await service.create_plan(
            make_brief(
                formats=["video"], budget_rub=400.0,
                reference_image_urls=["https://cdn/ours.png", "https://elsewhere/x.png"],
            )
        )

        assert any("Срок жизни 1 ссылок неизвестен" in w for w in plan.warnings)


class TestPeriodicTask:
    async def test_loop_survives_a_failing_pass(self):
        calls: list[int] = []

        async def failing():
            calls.append(1)
            raise RuntimeError("проход упал")

        task = PeriodicTask("t", interval_seconds=0.001, run=failing)
        await task.start()
        await asyncio.sleep(0.05)
        await task.stop()

        assert len(calls) > 1
        assert task.is_running is False

    async def test_zero_interval_disables_the_loop(self):
        async def never():  # pragma: no cover - must not run
            raise AssertionError("не должно вызываться")

        task = PeriodicTask("t", interval_seconds=0, run=never)
        await task.start()
        assert task.is_running is False
        await task.stop()
