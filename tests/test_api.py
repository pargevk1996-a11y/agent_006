"""HTTP surface: the contract another service integrates against."""

from __future__ import annotations

BRIEF = {
    "product_name": "CRM для мастеров маникюра",
    "product_description": "Онлайн-запись, напоминания и учёт расходников.",
    "target_audience": "Мастера маникюра, 22–40 лет",
    "offer": "Первый месяц бесплатно",
    "formats": ["text", "image", "voice"],
    "budget_rub": 300,
    "style": "дружелюбный",
}


class TestHealth:
    async def test_health_reports_mode_and_spending_flag(self, api_client):
        response = await api_client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["mode"] == "mock"
        assert body["live_spending_enabled"] is False
        assert body["database"] == "ok"

    async def test_correlation_id_is_echoed(self, api_client):
        response = await api_client.get("/health", headers={"X-Correlation-Id": "abc-123"})
        assert response.headers["X-Correlation-Id"] == "abc-123"


class TestPlanEndpoint:
    async def test_plan_returns_full_explanation(self, api_client):
        response = await api_client.post("/api/v1/plans", json=BRIEF)
        assert response.status_code == 201
        plan = response.json()

        assert plan["plan_id"]
        assert plan["currency"] == "RUB"
        assert plan["total_estimated_rub"] <= plan["budget_rub"]
        assert plan["budget_remaining_rub"] >= 0
        assert plan["account"]["balance_rub"] is not None
        for step in plan["steps"]:
            assert step["reason"]
            assert step["cost_basis"]
            assert step["idempotency_key"]
            assert step["estimated_cost_rub"] >= 0
        assert any(s["cost_source"] == "estimate" for s in plan["steps"])

    async def test_plan_does_not_spend(self, api_client):
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        # Balance in the snapshot equals the mock starting balance: nothing was charged.
        assert plan["account"]["balance_rub"] == 5000.0

    async def test_plan_is_retrievable(self, api_client):
        plan_id = (await api_client.post("/api/v1/plans", json=BRIEF)).json()["plan_id"]
        again = await api_client.get(f"/api/v1/plans/{plan_id}")
        assert again.status_code == 200
        assert again.json()["plan_id"] == plan_id

    async def test_invalid_brief_is_422(self, api_client):
        response = await api_client.post("/api/v1/plans", json={**BRIEF, "budget_rub": -5})
        assert response.status_code == 422
        assert response.json()["error"] == "validation_failed"

    async def test_budget_over_service_ceiling_is_rejected(self, api_client):
        response = await api_client.post("/api/v1/plans", json={**BRIEF, "budget_rub": 999_999})
        assert response.status_code == 422
        assert "MAX_BUDGET_RUB" in response.json()["message"]

    async def test_unknown_plan_is_404(self, api_client):
        response = await api_client.get("/api/v1/plans/does-not-exist")
        assert response.status_code == 404


class TestExecuteEndpoint:
    async def test_execute_requires_explicit_confirmation(self, api_client):
        plan_id = (await api_client.post("/api/v1/plans", json=BRIEF)).json()["plan_id"]
        response = await api_client.post(
            f"/api/v1/plans/{plan_id}/execute", json={"confirmed": False}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "confirmation_required"

    async def test_execute_with_empty_body_is_refused(self, api_client):
        plan_id = (await api_client.post("/api/v1/plans", json=BRIEF)).json()["plan_id"]
        response = await api_client.post(f"/api/v1/plans/{plan_id}/execute", json={})
        assert response.status_code == 400

    async def test_confirmed_execution_returns_job_report(self, api_client):
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        response = await api_client.post(
            f"/api/v1/plans/{plan['plan_id']}/execute", json={"confirmed": True}
        )
        assert response.status_code == 200
        job = response.json()
        assert job["status"] == "succeeded"
        assert job["actual_cost_rub"] <= job["budget_rub"]
        assert job["budget_remaining_rub"] >= 0
        assert job["duration_seconds"] is not None
        for step in job["steps"]:
            assert step["status"] == "succeeded"
            # Every finished step delivers something: a link, generated copy, or the
            # locally composed fallback text.
            assert step["display_url"] or step["text_output"] or step["local_output"]


class TestJobEndpoint:
    async def test_job_report_contains_costs_and_links(self, api_client):
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        job_id = (
            await api_client.post(
                f"/api/v1/plans/{plan['plan_id']}/execute", json={"confirmed": True}
            )
        ).json()["job_id"]

        response = await api_client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        assert job["job_id"] == job_id
        assert job["plan_id"] == plan["plan_id"]
        assert {s["step_id"] for s in job["steps"]} == {s["step_id"] for s in plan["steps"]}
        assert job["errors"] == []

    async def test_unknown_job_is_404(self, api_client):
        response = await api_client.get("/api/v1/jobs/nope")
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"


class TestOpenAPI:
    async def test_schema_is_generated(self, api_client):
        response = await api_client.get("/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        assert "/api/v1/plans" in paths
        assert "/api/v1/plans/{plan_id}/execute" in paths
        assert "/api/v1/jobs/{job_id}" in paths
        assert "/api/v1/webhooks/vibe" in paths
        assert "/health" in paths
