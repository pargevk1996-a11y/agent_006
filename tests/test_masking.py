"""Secrets must never reach the logs — in any field, at any level."""

from __future__ import annotations

import io
import json
import logging

import httpx
import pytest

from app.clients.retry import RetryPolicy
from app.clients.vibe import HttpVibeClient
from app.core.logging import JsonFormatter, RedactionFilter, set_correlation_id
from app.core.security import (
    clear_secrets,
    mask_secret,
    redact,
    register_secret,
    safe_headers,
)

TOKEN = "oc_live_9f8e7d6c5b4a3210deadbeefcafe"
WEBHOOK_SECRET = "whsec_super_secret_value_123456"


@pytest.fixture(autouse=True)
def _clean_secrets():
    clear_secrets()
    yield
    clear_secrets()


class TestMaskSecret:
    def test_only_a_fingerprint_survives(self):
        masked = mask_secret(TOKEN)
        assert TOKEN not in masked
        assert masked.startswith("oc_l")
        assert masked.endswith(f"(len={len(TOKEN)})")
        assert masked.count("*") >= 3

    def test_short_secrets_are_fully_masked(self):
        assert mask_secret("abc123") == "*" * 8

    def test_unset_token_is_labelled(self):
        assert mask_secret(None) == "<unset>"


class TestRedact:
    def test_registered_secret_is_scrubbed_anywhere(self):
        register_secret(TOKEN)
        text = f"calling api with token={TOKEN} and more"
        assert TOKEN not in redact(text)
        assert "REDACTED" in redact(text)

    def test_bearer_header_is_scrubbed_even_if_unregistered(self):
        text = "Authorization: Bearer oc_unknown_token_value_abcdef"
        assert "oc_unknown_token_value_abcdef" not in redact(text)

    def test_signature_header_is_scrubbed(self):
        text = 'X-Vibe-Signature: 9a8b7c6d5e4f3a2b1c0d'
        assert "9a8b7c6d5e4f3a2b1c0d" not in redact(text)

    def test_json_style_token_field_is_scrubbed(self):
        text = '{"api_token": "oc_secret_value_here_1234"}'
        assert "oc_secret_value_here_1234" not in redact(text)

    def test_safe_headers_replaces_credentials(self):
        headers = safe_headers(
            {"Authorization": f"Bearer {TOKEN}", "X-Vibe-Signature": "abc", "Accept": "json"}
        )
        assert TOKEN not in json.dumps(headers)
        assert headers["Accept"] == "json"


class TestLogPipeline:
    def _capture(self) -> tuple[logging.Logger, io.StringIO]:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(RedactionFilter())
        logger = logging.getLogger("test.masking")
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        return logger, stream

    def test_secret_in_message_is_redacted(self):
        register_secret(TOKEN)
        logger, stream = self._capture()
        logger.info("token is %s", TOKEN)
        assert TOKEN not in stream.getvalue()

    def test_secret_in_extra_field_is_redacted(self):
        register_secret(WEBHOOK_SECRET)
        logger, stream = self._capture()
        logger.info("webhook", extra={"secret": WEBHOOK_SECRET, "nested": {"s": WEBHOOK_SECRET}})
        output = stream.getvalue()
        assert WEBHOOK_SECRET not in output
        assert "REDACTED" in output

    def test_correlation_id_is_present(self):
        logger, stream = self._capture()
        set_correlation_id("cid-12345")
        logger.info("hello")
        assert json.loads(stream.getvalue())["correlation_id"] == "cid-12345"


class TestClientLogging:
    async def test_client_logs_masked_token_only(self, caplog):
        register_secret(TOKEN)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"  # really sent
            return httpx.Response(200, json={"status": "ok"})

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport, base_url="https://api.test")
        client = HttpVibeClient(
            base_url="https://api.test",
            token=TOKEN,
            client=http,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        with caplog.at_level(logging.INFO):
            for record in caplog.records:
                RedactionFilter().filter(record)
            await client.capabilities()

        for record in caplog.records:
            RedactionFilter().filter(record)
            assert TOKEN not in record.getMessage()
            assert TOKEN not in json.dumps(record.__dict__, default=str)
        await client.aclose()
