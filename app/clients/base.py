"""Client protocol shared by the real and mock implementations."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class VibeClient(Protocol):
    """The slice of the Agent API this service depends on."""

    mode: str

    async def capabilities(self) -> dict[str, Any]:
        """GET /capabilities — the source of truth for models and parameters."""

    async def me(self) -> dict[str, Any]:
        """GET /me — token scopes, balance, daily spend limit."""

    async def balance(self) -> dict[str, Any]:
        """GET /balance — current ruble balance."""

    async def estimate(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /generate/estimate — dry-run validation and pricing, never billed."""

    async def generate(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /generate — billable. Callers must pass strict + idempotency_key."""

    async def generation_status(self, generation_id: int | str) -> dict[str, Any]:
        """GET /generation/{id}/status."""

    async def voiceover_status(self, voiceover_id: int | str) -> dict[str, Any]:
        """GET /voiceover/long/{id} — progress of a long (>5000 chars) voiceover."""

    async def upload_media(
        self, *, filename: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        """POST /upload-media — a stable URL for a file. Free, never billed."""

    async def aclose(self) -> None:
        ...
