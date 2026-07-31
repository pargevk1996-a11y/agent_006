"""The page a non-developer opens.

Swagger describes the API; this describes the *decision*: what will be generated,
what it costs, and one deliberate button that spends money. Served from the
service itself — a single self-contained file, no build step, no CDN, so it works
on a laptop with no internet exactly like the rest of the demo.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["ui"])

INDEX = Path(__file__).resolve().parent.parent.parent / "static" / "index.html"


@router.get(
    "/",
    response_class=HTMLResponse,
    include_in_schema=False,
    summary="Веб-интерфейс: бриф → цена → подтверждение → результат",
)
async def index() -> HTMLResponse:
    # Read per request: the page is tiny, and editing it live needs no restart.
    return HTMLResponse(INDEX.read_text(encoding="utf-8"))
