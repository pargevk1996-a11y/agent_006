"""Webhook HMAC verification — an unsigned callback is never trusted."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.core.errors import WebhookVerificationError
from app.services.webhooks import (
    compute_signature,
    legacy_secret,
    signature_matches,
    verify_webhook,
)

SECRET = "whsec_test_value"
BODY = json.dumps(
    {"event": "generation.complete", "generation_id": 5811, "cost": 196.0}, ensure_ascii=False
).encode()


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestSignature:
    def test_valid_signature_passes(self):
        verify_webhook(BODY, sign(BODY, SECRET), secret=SECRET)

    def test_signature_matches_documented_scheme(self):
        assert compute_signature(BODY, SECRET) == sign(BODY, SECRET)

    def test_wrong_signature_rejected(self):
        with pytest.raises(WebhookVerificationError, match="не совпадает"):
            verify_webhook(BODY, sign(BODY, "another-secret"), secret=SECRET)

    def test_missing_signature_rejected(self):
        with pytest.raises(WebhookVerificationError, match="Отсутствует"):
            verify_webhook(BODY, None, secret=SECRET)

    def test_empty_signature_rejected(self):
        with pytest.raises(WebhookVerificationError):
            verify_webhook(BODY, "", secret=SECRET)

    def test_tampered_body_rejected(self):
        signature = sign(BODY, SECRET)
        tampered = BODY.replace(b'"cost": 196.0', b'"cost": 0.0')
        with pytest.raises(WebhookVerificationError):
            verify_webhook(tampered, signature, secret=SECRET)

    def test_unconfigured_secret_rejects_everything(self):
        with pytest.raises(WebhookVerificationError, match="VIBE_WEBHOOK_SECRET"):
            verify_webhook(BODY, sign(BODY, SECRET), secret=None)

    def test_signature_is_case_insensitive_hex(self):
        verify_webhook(BODY, sign(BODY, SECRET).upper(), secret=SECRET)

    def test_signature_is_computed_over_raw_bytes(self):
        """Re-serialising the JSON changes whitespace and must break verification."""
        signature = sign(BODY, SECRET)
        reserialised = json.dumps(json.loads(BODY), indent=2).encode()
        assert not signature_matches(reserialised, signature, SECRET)


class TestLegacyScheme:
    def test_legacy_secret_is_sha256_of_token(self):
        assert legacy_secret("oc_token") == hashlib.sha256(b"oc_token").hexdigest()

    def test_legacy_signature_accepted_when_enabled(self):
        token = "oc_legacy_token"
        signature = sign(BODY, legacy_secret(token))
        verify_webhook(BODY, signature, secret=None, api_token=token, allow_legacy=True)

    def test_legacy_signature_rejected_when_disabled(self):
        token = "oc_legacy_token"
        signature = sign(BODY, legacy_secret(token))
        with pytest.raises(WebhookVerificationError):
            verify_webhook(BODY, signature, secret=SECRET, api_token=token, allow_legacy=False)


class TestWebhookEndpoint:
    async def test_unsigned_webhook_changes_nothing(self, api_client):
        response = await api_client.post("/api/v1/webhooks/vibe", content=BODY)
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_signature"

    async def test_signed_webhook_updates_the_matching_step(self, api_client, webhook_secret):
        plan = (
            await api_client.post(
                "/api/v1/plans",
                json={
                    "product_name": "P",
                    "product_description": "D",
                    "target_audience": "A",
                    "offer": "O",
                    "formats": ["image"],
                    "budget_rub": 500,
                },
            )
        ).json()
        job = (
            await api_client.post(
                f"/api/v1/plans/{plan['plan_id']}/execute", json={"confirmed": True}
            )
        ).json()
        generation_id = job["steps"][0]["generation_id"]

        body = json.dumps(
            {
                "event": "generation.error",
                "generation_id": generation_id,
                "status": "error",
                "error_message": "провайдер недоступен",
                "refunded": True,
                "cost": 0,
            }
        ).encode()
        response = await api_client.post(
            "/api/v1/webhooks/vibe",
            content=body,
            headers={
                "X-Vibe-Signature": sign(body, webhook_secret),
                "X-Vibe-Event": "generation.error",
            },
        )
        assert response.status_code == 200
        assert response.json()["matched_step"] == "image-1"

    async def test_signed_webhook_for_unknown_generation_is_accepted_but_matches_nothing(
        self, api_client, webhook_secret
    ):
        body = json.dumps({"event": "generation.complete", "generation_id": 999999}).encode()
        response = await api_client.post(
            "/api/v1/webhooks/vibe",
            content=body,
            headers={"X-Vibe-Signature": sign(body, webhook_secret)},
        )
        assert response.status_code == 200
        assert response.json()["matched_step"] is None


class TestWebhookCheckScript:
    """The diagnostic script must fail loudly rather than pass on a broken setup."""

    def _module(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "scripts" / "webhook_check.py"
        spec = importlib.util.spec_from_file_location("webhook_check", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def test_local_check_passes_with_a_configured_secret(self, tmp_path):
        from tests.conftest import make_settings

        module = self._module()
        settings = make_settings(tmp_path, vibe_webhook_secret="whsec_check_value")
        assert await module.check_local(settings) == 0

    async def test_local_check_fails_without_a_secret(self, tmp_path):
        from tests.conftest import make_settings

        module = self._module()
        settings = make_settings(tmp_path)
        assert await module.check_local(settings) == 1

    def test_sample_event_matches_the_documented_payload(self):
        module = self._module()
        for field in ("event", "generation_id", "status", "cost", "refunded"):
            assert field in module.SAMPLE_EVENT
