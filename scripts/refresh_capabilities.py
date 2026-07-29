#!/usr/bin/env python
"""Refresh the bundled ``GET /capabilities`` snapshot and report what changed.

The snapshot in ``app/clients/fixtures/capabilities.json`` is what mock mode and the
whole test suite run against. The live catalog moves — during this project's own
development a new model type (``text``) appeared — so the snapshot has to be
refreshed deliberately, with a visible diff rather than a silent overwrite.

The endpoint is public: no token, no cost.

    uv run python scripts/refresh_capabilities.py            # показать изменения
    uv run python scripts/refresh_capabilities.py --write    # применить
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients.mock import FIXTURE_PATH
from app.clients.vibe import HttpVibeClient
from app.core.config import Settings
from app.domain.capabilities import Capabilities

RULER = "─" * 78


def _models(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for type_, entries in (payload.get("models") or {}).items():
        if isinstance(entries, dict):
            for key, spec in entries.items():
                if isinstance(spec, dict):
                    result[(str(type_), str(key))] = spec
    return result


def _price_of(spec: dict[str, Any]) -> str:
    if isinstance(spec.get("price"), (int, float)):
        return f"{spec['price']}₽"
    if spec.get("per_second"):
        return f"{spec['per_second']}₽/с"
    return "по токенам/оценке"


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Human-readable changes between two catalog payloads."""
    old_models, new_models = _models(old), _models(new)
    lines: list[str] = []

    for key in sorted(set(new_models) - set(old_models)):
        lines.append(f"  + {key[0]}/{key[1]} — {_price_of(new_models[key])}")
    for key in sorted(set(old_models) - set(new_models)):
        lines.append(f"  - {key[0]}/{key[1]} — удалена из каталога")
    for key in sorted(set(old_models) & set(new_models)):
        before, after = old_models[key], new_models[key]
        if before.get("price") != after.get("price"):
            lines.append(
                f"  ~ {key[0]}/{key[1]} — цена {_price_of(before)} → {_price_of(after)}"
            )
        for field in ("required", "optional"):
            if before.get(field) != after.get(field):
                lines.append(
                    f"  ~ {key[0]}/{key[1]} — {field}: {before.get(field)} → {after.get(field)}"
                )

    old_types = set(old.get("types") or [])
    new_types = set(new.get("types") or [])
    if old_types != new_types:
        lines.append(f"  ~ types: {sorted(old_types)} → {sorted(new_types)}")
    return lines


async def fetch() -> dict[str, Any]:
    settings = Settings(_env_file=None)
    client = HttpVibeClient(base_url=settings.vibe_base_url, token=None)
    try:
        return await client.capabilities()
    finally:
        await client.aclose()


async def main(write: bool) -> int:
    print(f"{RULER}\n Обновление слепка {FIXTURE_PATH.relative_to(Path.cwd())}\n{RULER}")
    current = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    live = await fetch()

    caps_now, caps_live = Capabilities.parse(current), Capabilities.parse(live)
    print(f"сейчас в слепке : {sum(len(m) for m in caps_now.models_by_type.values())} моделей "
          f"{ {t: len(m) for t, m in caps_now.models_by_type.items()} }")
    print(f"в живом каталоге: {sum(len(m) for m in caps_live.models_by_type.values())} моделей "
          f"{ {t: len(m) for t, m in caps_live.models_by_type.items()} }")

    changes = diff(current, live)
    if not changes:
        print("\n✅ Изменений нет — слепок актуален.")
        return 0

    print(f"\nИзменений: {len(changes)}")
    for line in changes:
        print(line)

    if not write:
        print("\nНичего не записано. Повторите с --write, чтобы применить,")
        print("затем прогоните `uv run pytest -q` — тесты идут против этого слепка.")
        return 1

    FIXTURE_PATH.write_text(
        json.dumps(live, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("\n✅ Слепок обновлён. Обязательно прогоните `uv run pytest -q`: "
          "планировщик и моки работают именно по нему.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="записать новый слепок")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.write)))
