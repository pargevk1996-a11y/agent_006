#!/usr/bin/env python
"""Verify webhook signature handling — locally, and end-to-end against the platform.

Two modes, both free:

``--local`` (default)
    Signs a payload with the **real** ``VIBE_WEBHOOK_SECRET`` from ``.env`` and posts
    it to our own endpoint through an in-process ASGI transport: no network, no
    tunnel. Proves that the configured secret works as an HMAC key exactly as our
    verification expects, that a tampered body is rejected, and that an unsigned
    request is rejected.

``--public https://<host>``
    Asks the platform to deliver a genuinely signed test event
    (``POST /api/agent/webhook-test``, free) to ``<host>/api/v1/webhooks/vibe``, then
    watches our own ``webhook_events`` table until it arrives. Requires the service to
    be running behind a public URL (``ngrok http 8000`` / ``cloudflared tunnel``).

    uv run python scripts/webhook_check.py
    uv run python scripts/webhook_check.py --public https://abc123.ngrok.app
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients.exceptions import VibeAPIError
from app.clients.vibe import HttpVibeClient
from app.core.config import AppMode, Settings
from app.repositories.db import Database
from app.services.webhooks import compute_signature, legacy_secret

RULER = "─" * 78
WEBHOOK_PATH = "/api/v1/webhooks/vibe"

SAMPLE_EVENT = {
    "event": "generation.complete",
    "generation_id": 999_001,
    "task_id": "task_webhook_check",
    "type": "image",
    "model": "nano-banana-pro-2k",
    "status": "complete",
    "cost": 16.5,
    "refunded": False,
    "attempt": 1,
}


def hr(title: str) -> None:
    print(f"\n{RULER}\n {title}\n{RULER}")


async def check_local(settings: Settings) -> int:
    """Post signed / tampered / unsigned payloads to our own endpoint."""
    from app.main import create_app

    secret = settings.webhook_secret_value
    if not secret:
        print("❌ VIBE_WEBHOOK_SECRET не задан — проверять нечем.")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        # Mock mode + throwaway DB: the check never touches the paid API or real data.
        local = settings.model_copy(
            update={
                "app_mode": AppMode.MOCK,
                "db_path": Path(tmp) / "webhook_check.db",
                "log_level": "ERROR",  # проверка сама печатает результат, логи только мешают
            }
        )
        app = create_app(local)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://check") as client:
                raw = json.dumps(SAMPLE_EVENT, ensure_ascii=False).encode()
                cases = [
                    (
                        "подписанный настоящим VIBE_WEBHOOK_SECRET",
                        raw,
                        compute_signature(raw, secret),
                        200,
                    ),
                    (
                        "подпись от чужого секрета",
                        raw,
                        compute_signature(raw, "не-тот-секрет"),
                        401,
                    ),
                    (
                        "тело подменено после подписи",
                        raw.replace(b'"cost": 16.5', b'"cost": 0.0'),
                        compute_signature(raw, secret),
                        401,
                    ),
                    ("без заголовка подписи", raw, None, 401),
                ]
                if settings.token_value:
                    cases.append(
                        (
                            "legacy-схема sha256(token) при выключенном флаге",
                            raw,
                            compute_signature(raw, legacy_secret(settings.token_value)),
                            401,
                        )
                    )

                failures = 0
                for name, body, signature, expected in cases:
                    headers = {"Content-Type": "application/json"}
                    if signature:
                        headers["X-Vibe-Signature"] = signature
                    response = await client.post(WEBHOOK_PATH, content=body, headers=headers)
                    ok = response.status_code == expected
                    failures += 0 if ok else 1
                    mark = "✓" if ok else "✗"
                    print(f"  {mark} {name:<52} → {response.status_code} (ожидали {expected})")

    if failures:
        print(f"\n❌ Провалов: {failures}")
        return 1
    print("\n✅ Проверка подписи проходит с вашим настоящим секретом.")
    print("   Полный сквозной тест — с публичным URL: см. --public.")
    return 0


async def check_public(settings: Settings, base_url: str, wait_seconds: float) -> int:
    """Ask the platform to send a real signed event and wait for it to land."""
    callback_url = base_url.rstrip("/") + WEBHOOK_PATH
    print(f"callback_url: {callback_url}")

    database = Database(settings.db_path)
    await database.connect()
    before = await database.fetch_one("SELECT COUNT(*) AS n FROM webhook_events")
    seen_before = before["n"] if before else 0
    print(f"событий в базе до проверки: {seen_before}")

    client = HttpVibeClient(base_url=settings.vibe_base_url, token=settings.token_value)
    try:
        payload = await client.request_raw(
            "POST", "/webhook-test", json_body={"callback_url": callback_url}
        )
        print(f"платформа приняла запрос: {json.dumps(payload, ensure_ascii=False)[:300]}")
    except VibeAPIError as exc:
        print(f"❌ Платформа отклонила запрос: {exc.status_code} {exc.code} — {exc.message}")
        if exc.details:
            print(f"   details: {exc.details}")
        print("   Публичный URL должен быть доступен из интернета (не localhost).")
        await client.aclose()
        await database.close()
        return 1
    await client.aclose()

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        row = await database.fetch_one(
            "SELECT event, received_at, generation_id FROM webhook_events "
            "ORDER BY id DESC LIMIT 1"
        )
        current = await database.fetch_one("SELECT COUNT(*) AS n FROM webhook_events")
        if current and current["n"] > seen_before and row:
            print(
                f"\n✅ Подписанный вебхук получен и принят: event={row['event']}, "
                f"generation_id={row['generation_id']}, время {row['received_at']}"
            )
            print("   Запись в webhook_events появляется только после успешной проверки HMAC.")
            await database.close()
            return 0
        await asyncio.sleep(1.0)

    print(f"\n❌ За {wait_seconds:.0f} с вебхук не дошёл.")
    print("   Проверьте: сервис запущен, туннель жив, VIBE_WEBHOOK_SECRET совпадает с кабинетом.")
    print("   Отклонённые подписи в базу не пишутся — смотрите логи на invalid_signature.")
    await database.close()
    return 1


async def main(args: argparse.Namespace) -> int:
    settings = Settings()
    hr("Проверка вебхуков (списаний нет)")
    print(f"режим приложения: {settings.app_mode.value}")
    print(f"webhook secret  : {'задан' if settings.webhook_secret_value else 'НЕ ЗАДАН'}")
    legacy = "включена" if settings.vibe_webhook_legacy_fallback else "выключена"
    print(f"legacy-схема    : {legacy}")

    if args.public:
        hr("Сквозная проверка через платформу")
        return await check_public(settings, args.public, args.wait)
    hr("Локальная проверка подписи (без сети и туннеля)")
    return await check_local(settings)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", help="публичный базовый URL сервиса, напр. https://x.ngrok.app")
    parser.add_argument("--wait", type=float, default=60.0, help="сколько ждать вебхук, секунд")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
