"""The marketing brief: what the user wants, in what formats, for how much."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


class ContentFormat(StrEnum):
    """Formats the user can request.

    These map onto ``type`` values of the Agent API (``text``/``image``/``voice``/
    ``video``/``music``). ``text`` is requestable, but the live ``/capabilities``
    catalog currently exposes no text models — the planner degrades that step to a
    free, locally composed copy instead of inventing an endpoint.
    """

    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    MUSIC = "music"


class SelectionStrategy(StrEnum):
    CHEAPEST = "cheapest"
    BALANCED = "balanced"
    QUALITY = "quality"


class Brief(BaseModel):
    """Marketing brief plus the hard money ceiling for the whole plan."""

    product_name: str = Field(min_length=1, max_length=200)
    product_description: str = Field(min_length=1, max_length=4000)
    target_audience: str = Field(min_length=1, max_length=1000)
    offer: str = Field(min_length=1, max_length=1000)
    formats: list[ContentFormat] = Field(min_length=1)
    budget_rub: float = Field(gt=0, description="Hard ceiling in rubles for the entire plan")
    style: str = Field(default="", max_length=1000, description="Tone-of-voice / visual style")

    language: str = Field(default="ru", max_length=8)
    strategy: SelectionStrategy = SelectionStrategy.BALANCED
    aspect_ratio: str | None = Field(default=None, description="Preferred aspect ratio, e.g. 9:16")
    video_duration_seconds: int | None = Field(default=None, ge=1, le=30)
    voiceover_script: str | None = Field(
        default=None,
        max_length=20_000,
        description="Exact voiceover text; composed from the brief when omitted",
    )
    reference_image_urls: list[str] = Field(
        default_factory=list,
        max_length=7,
        description="Stable URLs (ideally from POST /upload-media) unlocking image-to-* models",
    )
    landing_url: str | None = Field(default=None, description="Unlocks URL-driven video models")

    @field_validator("formats")
    @classmethod
    def _dedupe_formats(cls, value: list[ContentFormat]) -> list[ContentFormat]:
        seen: list[ContentFormat] = []
        for fmt in value:
            if fmt not in seen:
                seen.append(fmt)
        return seen

    @model_validator(mode="after")
    def _strip(self) -> Brief:
        self.product_name = self.product_name.strip()
        self.offer = self.offer.strip()
        return self

    def fingerprint_source(self) -> str:
        """Stable representation used to derive deterministic idempotency keys."""
        return self.model_dump_json(exclude_none=False)
