"""``Idempotency-Key`` на нашем API.

Внутри всё уже защищено от повторов — шаг захватывается в ledger, задание в своей
строке. Не защищён ровно клиент: тот, у кого оборвалось соединение после
`/execute`, не знает, приняли план или нет, и повторяет вслепую. Здесь
проверяется, что повтор с тем же ключом отдаёт сохранённый ответ первой попытки,
а не делает работу заново.
"""

from __future__ import annotations

import asyncio

from tests.conftest import wait_for_job

BRIEF = {
    "product_name": "CRM для мастеров маникюра",
    "product_description": "Онлайн-запись, напоминания и учёт расходников.",
    "target_audience": "Мастера маникюра, 22–40 лет",
    "offer": "Первый месяц бесплатно",
    "formats": ["image"],
    "budget_rub": 300,
}
KEY = {"Idempotency-Key": "req-7f3a-0001"}


class TestPlanCreation:
    async def test_repeat_returns_the_stored_answer(self, api_client):
        first = await api_client.post("/api/v1/plans", json=BRIEF, headers=KEY)
        second = await api_client.post("/api/v1/plans", json=BRIEF, headers=KEY)

        assert first.status_code == 201
        assert second.status_code == 201
        assert second.json() == first.json(), "повтор обязан вернуть тот же ответ побайтно"
        assert first.headers["Idempotency-Replayed"] == "false"
        assert second.headers["Idempotency-Replayed"] == "true"

    async def test_repeat_does_not_create_a_second_plan(self, api_client):
        first = (await api_client.post("/api/v1/plans", json=BRIEF, headers=KEY)).json()
        await api_client.post("/api/v1/plans", json=BRIEF, headers=KEY)

        second_plan = await api_client.get(f"/api/v1/plans/{first['plan_id']}")
        assert second_plan.status_code == 200
        assert second_plan.json()["plan_id"] == first["plan_id"]

    async def test_same_key_with_a_different_body_is_refused(self, api_client):
        await api_client.post("/api/v1/plans", json=BRIEF, headers=KEY)
        response = await api_client.post(
            "/api/v1/plans", json={**BRIEF, "budget_rub": 900}, headers=KEY
        )

        assert response.status_code == 409
        assert response.json()["error"] == "idempotency_key_reused"

    async def test_different_keys_build_different_plans(self, api_client):
        one = await api_client.post("/api/v1/plans", json=BRIEF, headers={"Idempotency-Key": "a"})
        two = await api_client.post("/api/v1/plans", json=BRIEF, headers={"Idempotency-Key": "b"})

        assert one.json()["plan_id"] != two.json()["plan_id"]

    async def test_without_a_key_nothing_is_deduplicated(self, api_client):
        one = await api_client.post("/api/v1/plans", json=BRIEF)
        two = await api_client.post("/api/v1/plans", json=BRIEF)

        assert one.json()["plan_id"] != two.json()["plan_id"]
        assert "Idempotency-Replayed" not in one.headers

    async def test_a_refused_request_does_not_burn_the_key(self, api_client):
        """Иначе одна опечатка в брифе навсегда сжигает ключ у клиента."""
        rejected = await api_client.post(
            "/api/v1/plans", json={**BRIEF, "budget_rub": 999_999}, headers=KEY
        )
        assert rejected.status_code == 422

        retried = await api_client.post("/api/v1/plans", json=BRIEF, headers=KEY)
        assert retried.status_code == 201
        assert retried.headers["Idempotency-Replayed"] == "false"


class TestExecute:
    async def test_repeat_replays_the_same_job(self, api_client):
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        url = f"/api/v1/plans/{plan['plan_id']}/execute"

        first = await api_client.post(url, json={"confirmed": True}, headers=KEY)
        second = await api_client.post(url, json={"confirmed": True}, headers=KEY)

        assert first.status_code == 202
        assert second.status_code == 202
        assert second.json() == first.json()
        assert second.headers["Idempotency-Replayed"] == "true"
        assert second.headers["Location"] == first.headers["Location"], "Location тоже повторён"

        finished = await wait_for_job(api_client, first.json()["job_id"])
        assert finished["actual_cost_rub"] <= finished["budget_rub"]

    async def test_the_key_is_scoped_to_the_plan(self, api_client):
        """Тот же ключ на другом плане — другой запрос, а не повтор."""
        one = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        two = (await api_client.post("/api/v1/plans", json=BRIEF)).json()

        await api_client.post(
            f"/api/v1/plans/{one['plan_id']}/execute", json={"confirmed": True}, headers=KEY
        )
        response = await api_client.post(
            f"/api/v1/plans/{two['plan_id']}/execute", json={"confirmed": True}, headers=KEY
        )

        assert response.status_code == 409
        assert response.json()["error"] == "idempotency_key_reused"

    async def test_unconfirmed_attempt_does_not_burn_the_key(self, api_client):
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        url = f"/api/v1/plans/{plan['plan_id']}/execute"

        refused = await api_client.post(url, json={"confirmed": False}, headers=KEY)
        assert refused.status_code == 400

        confirmed = await api_client.post(url, json={"confirmed": True}, headers=KEY)
        assert confirmed.status_code == 202

    async def test_wait_true_is_replayed_with_its_own_status(self, api_client):
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        url = f"/api/v1/plans/{plan['plan_id']}/execute"
        body = {"confirmed": True, "wait": True}

        first = await api_client.post(url, json=body, headers=KEY)
        second = await api_client.post(url, json=body, headers=KEY)

        assert first.status_code == 200, "wait=true отдаёт готовый отчёт"
        assert second.status_code == 200, "повтор сохраняет код первой попытки"
        assert second.json() == first.json()


class TestConcurrentRetry:
    async def test_two_simultaneous_retries_do_not_both_run(self, api_client):
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        url = f"/api/v1/plans/{plan['plan_id']}/execute"

        first, second = await asyncio.gather(
            api_client.post(url, json={"confirmed": True}, headers=KEY),
            api_client.post(url, json={"confirmed": True}, headers=KEY),
        )

        codes = sorted([first.status_code, second.status_code])
        # Проигравший либо получает сохранённый ответ, либо честное «ещё выполняется».
        assert codes in ([202, 202], [202, 409]), codes
        loser = next((r for r in (first, second) if r.status_code == 409), None)
        if loser is not None:
            assert loser.json()["error"] == "idempotency_key_in_flight"

    async def test_in_flight_claim_is_reported_as_conflict(self, api_client, tmp_path):
        """Ключ занят, ответа ещё нет — это 409, а не тихий второй запуск."""
        from app.repositories.db import Database
        from app.repositories.idempotency import IdempotencyRepository

        database = Database(tmp_path / "claim.db")
        await database.connect()
        store = IdempotencyRepository(database)

        assert await store.claim(key="k", endpoint="e", request_hash="h") is None
        existing = await store.claim(key="k", endpoint="e", request_hash="h")
        assert existing is not None
        assert existing.is_complete is False

        await store.complete("k", status_code=201, headers={}, body={"ok": True})
        done = await store.claim(key="k", endpoint="e", request_hash="h")
        assert done is not None and done.is_complete
        assert done.response_body == {"ok": True}

        # Сохранённый ответ не должен исчезать при попытке освободить ключ.
        await store.release("k")
        assert (await store.get("k")) is not None
        await database.close()
