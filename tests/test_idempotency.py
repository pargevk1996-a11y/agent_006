"""Idempotency keys: stable per step, unique across steps, never reused for a new payload."""

from __future__ import annotations

import uuid

import pytest

from app.repositories.jobs import JobRepository
from app.services.planner import make_idempotency_key
from tests.conftest import make_brief


class TestKeyDerivation:
    def test_key_is_deterministic(self):
        first = make_idempotency_key("plan-1", "image-1", "z-image", {"prompt": "a"})
        second = make_idempotency_key("plan-1", "image-1", "z-image", {"prompt": "a"})
        assert first == second
        assert uuid.UUID(first)

    def test_key_differs_per_step(self):
        a = make_idempotency_key("plan-1", "image-1", "z-image", {"prompt": "a"})
        b = make_idempotency_key("plan-1", "voice-1", "z-image", {"prompt": "a"})
        assert a != b

    def test_key_differs_per_plan(self):
        a = make_idempotency_key("plan-1", "image-1", "z-image", {"prompt": "a"})
        b = make_idempotency_key("plan-2", "image-1", "z-image", {"prompt": "a"})
        assert a != b

    def test_key_differs_when_payload_changes(self):
        a = make_idempotency_key("plan-1", "image-1", "z-image", {"prompt": "a"})
        b = make_idempotency_key("plan-1", "image-1", "z-image", {"prompt": "b"})
        assert a != b

    def test_key_is_order_insensitive_for_same_params(self):
        a = make_idempotency_key("p", "s", "m", {"prompt": "x", "aspect_ratio": "9:16"})
        b = make_idempotency_key("p", "s", "m", {"aspect_ratio": "9:16", "prompt": "x"})
        assert a == b


class TestGenerateRequests:
    async def test_every_generate_call_is_strict_and_keyed(self, service, client):
        plan = await service.create_plan(
            make_brief(formats=["image", "voice"], budget_rub=500.0)
        )
        await service.execute_plan(plan.plan_id, confirmed=True)

        bodies = client.calls_of("generate")
        assert len(bodies) == 2
        keys = set()
        for body in bodies:
            assert body["strict"] is True
            assert body["idempotency_key"]
            keys.add(body["idempotency_key"])
        assert len(keys) == 2, "у каждого шага должен быть собственный ключ"

    async def test_plan_steps_expose_their_keys(self, service):
        plan = await service.create_plan(
            make_brief(formats=["image", "voice"], budget_rub=500.0)
        )
        keys = [s.idempotency_key for s in plan.steps]
        assert all(keys)
        assert len(set(keys)) == len(keys)

    async def test_replay_uses_the_same_key_and_does_not_recharge(self, service, client):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        await service.execute_plan(plan.plan_id, confirmed=True)
        key = client.calls_of("generate")[0]["idempotency_key"]

        # Directly replay the identical request: the platform must return the
        # original response and not charge again.
        balance = client.balance_rub
        replay = await client.generate(client.calls_of("generate")[0])
        assert replay["generation_id"] == 5811
        assert client.balance_rub == balance
        assert key == client.calls_of("generate")[0]["idempotency_key"]

    async def test_client_refuses_generate_without_key(self):
        from app.clients.vibe import HttpVibeClient

        http_client = HttpVibeClient(base_url="https://example.invalid", token="t")
        with pytest.raises(ValueError, match="idempotency_key"):
            await http_client.generate({"type": "image", "model": "z-image", "strict": True})
        with pytest.raises(ValueError, match="strict"):
            await http_client.generate(
                {"type": "image", "model": "z-image", "idempotency_key": "k"}
            )
        await http_client.aclose()


class TestLedger:
    async def test_step_claim_is_reused(self, database):
        repo = JobRepository(database)
        body = {"type": "image", "model": "z-image", "prompt": "x"}
        first = await repo.claim_step(
            plan_id="p1", step_id="image-1", job_id="j1", idempotency_key="k1", body=body
        )
        second = await repo.claim_step(
            plan_id="p1", step_id="image-1", job_id="j2", idempotency_key="k1", body=body
        )
        assert first.idempotency_key == second.idempotency_key == "k1"

    async def test_same_step_with_different_key_is_refused(self, database):
        repo = JobRepository(database)
        body = {"type": "image", "model": "z-image", "prompt": "x"}
        await repo.claim_step(
            plan_id="p1", step_id="image-1", job_id="j1", idempotency_key="k1", body=body
        )
        with pytest.raises(ValueError, match="different"):
            await repo.claim_step(
                plan_id="p1", step_id="image-1", job_id="j1", idempotency_key="k2", body=body
            )

    async def test_same_key_with_different_payload_is_refused(self, database):
        repo = JobRepository(database)
        await repo.claim_step(
            plan_id="p1",
            step_id="image-1",
            job_id="j1",
            idempotency_key="k1",
            body={"type": "image", "model": "z-image", "prompt": "x"},
        )
        with pytest.raises(ValueError, match="identical payload"):
            await repo.claim_step(
                plan_id="p1",
                step_id="image-1",
                job_id="j1",
                idempotency_key="k1",
                body={"type": "image", "model": "z-image", "prompt": "CHANGED"},
            )

    async def test_callback_url_does_not_affect_request_hash(self, database):
        repo = JobRepository(database)
        base = {"type": "image", "model": "z-image", "prompt": "x"}
        await repo.claim_step(
            plan_id="p1", step_id="s", job_id="j", idempotency_key="k", body=base
        )
        record = await repo.claim_step(
            plan_id="p1",
            step_id="s",
            job_id="j",
            idempotency_key="k",
            body={**base, "callback_url": "https://example.test/hook"},
        )
        assert record.idempotency_key == "k"
