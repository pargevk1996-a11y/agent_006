"""Liveness / readiness."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_client, get_database, get_settings
from app.api.schemas import HealthResponse
from app.clients.base import VibeClient
from app.core.config import AppMode, Settings
from app.repositories.db import Database

router = APIRouter(tags=["ops"])

VERSION = "0.1.0"


@router.get("/health", response_model=HealthResponse, summary="Состояние приложения")
async def health(
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
    client: VibeClient = Depends(get_client),
) -> HealthResponse:
    try:
        await database.fetch_one("SELECT 1 AS ok")
        db_status = "ok"
    except Exception as exc:  # pragma: no cover - defensive
        db_status = f"error: {type(exc).__name__}"

    upstream = (
        "mock (сеть не используется)"
        if settings.app_mode is AppMode.MOCK
        else settings.vibe_base_url
    )
    return HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        mode=settings.app_mode.value,
        version=VERSION,
        database=db_status,
        upstream=upstream,
        live_spending_enabled=settings.app_mode.allows_spending,
    )
