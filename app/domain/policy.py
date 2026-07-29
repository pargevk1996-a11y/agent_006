"""Model-selection policy.

Prices, required/optional parameters, enums and limits are **always** read from
``/capabilities``. This module only holds what the API cannot tell us: a small,
overridable opinion about *quality ordering* between models, plus which
parameters this agent is able to supply on its own.

Models that match no tier pattern land in a neutral fallback tier — a brand-new
model appearing in the catalog is therefore selectable immediately (ranked by
price), it just does not jump ahead of curated favourites without an operator
updating the table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

DEFAULT_POLICY: dict[str, Any] = {
    # Order in which formats claim budget when not everything fits.
    "format_priority": ["text", "image", "voice", "video", "music"],
    # Quality tiers, best first. Patterns are fnmatch-style on the model key.
    "tiers": {
        "image": [
            ["seedream-5-pro", "gpt-image-2", "nano-banana-pro-2k", "nano-banana-pro-4k"],
            ["nano-banana-pro-1k", "nano-banana-2-2k", "nano-banana-2-4k", "seedream-4.5",
             "seedream-5-lite", "gpt-image-1.5", "grok-image"],
            ["nano-banana-2-lite", "nano-banana-2-1k", "z-image"],
        ],
        "video": [
            ["veo3.1", "veo3_fast", "kling-3.0-pro"],
            ["kling-3.0-std", "grok-ttv", "gemini-omni-video", "veo3"],
            ["seedance-2-mini", "seedance-2-fast", "seedance-2", "grok-itv"],
        ],
        "voice": [
            ["el-tts-multilingual-v2", "gemini-pro-tts"],
            ["el-tts-turbo", "gemini-flash-tts"],
            ["el-dialogue-v3"],
        ],
        "music": [
            ["suno-v5.5"],
            ["suno-v5"],
            ["*-instrumental"],
        ],
        # Text models are billed per actual tokens: /capabilities gives no price, the
        # ceiling comes from /generate/estimate (reserve for max_tokens).
        "text": [
            ["claude-opus-5", "claude-*"],
            ["gpt-5.6-sol", "gpt-*"],
        ],
    },
    # Never auto-selected: chained/utility models that only make sense as a manual
    # follow-up to an existing generation, or that need assets we cannot produce.
    "exclude": ["grok-extend", "grok-upscale", "*-edit", "veed-avatar", "gemini-omni-character"],
    # Types billed per started 1000 characters rather than per request. Used only
    # for pre-flight upper-bound estimation; /generate/estimate always wins.
    "per_1000_chars_types": ["voice"],
    # Parameter values this agent supplies by default when a model accepts them.
    "defaults": {
        "aspect_ratio": "9:16",
        "video_duration_seconds": 8,
        "resolution": "720p",
        "voice_id": "Brian",
        "voice_name": "Puck",
        "language_code": "ru",
        "vocal_gender": "f",
        # Only lever that bounds the price of token-billed text models.
        "text_max_tokens": 900,
        "text_system": "Ты пишешь рекламные тексты на русском языке кратко и по делу.",
    },
    # Parameters the agent can fill from a brief. Anything a model requires that is
    # not here (avatar_id, ref_task_id, reference_video_url, ...) makes it ineligible.
    "suppliable_params": [
        "prompt", "aspect_ratio", "duration", "resolution", "generation_type",
        "generate_audio", "negative_prompt", "seed", "sound", "quality", "output_format",
        "voice_id", "voice_name", "language_code", "style", "stability", "similarity_boost",
        "speed", "temperature", "scene", "lyrics", "music_style", "style_tags",
        "vocal_gender", "lang", "callback_url", "strict", "idempotency_key",
        "system", "max_tokens",
    ],
}

FALLBACK_TIER = 99


@dataclass(frozen=True)
class Policy:
    format_priority: tuple[str, ...]
    tiers: dict[str, tuple[tuple[str, ...], ...]]
    exclude: tuple[str, ...]
    per_1000_chars_types: frozenset[str]
    defaults: dict[str, Any]
    suppliable_params: frozenset[str]
    source: str = "builtin"
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, source: str = "builtin") -> Policy:
        merged = {**DEFAULT_POLICY, **payload}
        tiers = {
            str(type_): tuple(tuple(str(p) for p in tier) for tier in tier_list)
            for type_, tier_list in (merged.get("tiers") or {}).items()
        }
        return cls(
            format_priority=tuple(str(f) for f in merged["format_priority"]),
            tiers=tiers,
            exclude=tuple(str(p) for p in merged.get("exclude", ())),
            per_1000_chars_types=frozenset(str(t) for t in merged.get("per_1000_chars_types", ())),
            defaults=dict(merged.get("defaults") or {}),
            suppliable_params=frozenset(str(p) for p in merged.get("suppliable_params", ())),
            source=source,
            raw=merged,
        )

    @classmethod
    def load(cls, path: Path | None = None) -> Policy:
        """Load the built-in table, or a JSON override from ``POLICY_FILE``."""
        if path is None:
            return cls.from_dict({})
        file = Path(path)
        if not file.is_file():
            raise ValueError(
                f"POLICY_FILE={file} не найден или не является файлом. "
                "Оставьте переменную пустой, чтобы использовать встроенную policy-таблицу."
            )
        payload = json.loads(file.read_text(encoding="utf-8"))
        return cls.from_dict(payload, source=str(file))

    def tier_of(self, type_: str, model_key: str) -> int:
        for index, patterns in enumerate(self.tiers.get(type_, ())):
            if any(fnmatch(model_key, pattern) for pattern in patterns):
                return index
        return FALLBACK_TIER

    def is_excluded(self, model_key: str) -> bool:
        return any(fnmatch(model_key, pattern) for pattern in self.exclude)

    def priority_of(self, format_: str) -> int:
        try:
            return self.format_priority.index(format_)
        except ValueError:
            return len(self.format_priority)
