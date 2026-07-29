"""Deterministic prompt composition from a brief.

Same brief in, same prompts out — planning must be reproducible, and identical
prompts are what make the derived idempotency keys meaningful.
"""

from __future__ import annotations

from app.domain.brief import Brief, ContentFormat


def _style(brief: Brief) -> str:
    return brief.style.strip() or "деловой, современный, без кликбейта"


def image_prompt(brief: Brief) -> str:
    return (
        f"Рекламный визуал для продукта «{brief.product_name}». "
        f"Суть продукта: {brief.product_description} "
        f"Целевая аудитория: {brief.target_audience}. "
        f"Ключевой оффер, который должен читаться в кадре: {brief.offer}. "
        f"Стиль: {_style(brief)}. "
        "Композиция под рекламный пост, крупный акцент на продукте, "
        "чистый фон, место под заголовок."
    )


def video_prompt(brief: Brief) -> str:
    return (
        f"Короткий рекламный ролик продукта «{brief.product_name}». "
        f"Описание: {brief.product_description} "
        f"Аудитория: {brief.target_audience}. "
        f"Оффер в финальном кадре: {brief.offer}. "
        f"Стиль съёмки: {_style(brief)}. "
        "Динамичный монтаж, крупные планы продукта, финальный кадр с призывом к действию."
    )


def voice_script(brief: Brief) -> str:
    """Voiceover text — cost here scales with length, so keep it tight."""
    if brief.voiceover_script:
        return brief.voiceover_script.strip()
    return (
        f"{brief.product_name}. {brief.product_description} "
        f"{brief.offer} Успейте воспользоваться предложением."
    )


def music_prompt(brief: Brief) -> str:
    return (
        f"Фоновый трек для рекламного ролика продукта «{brief.product_name}». "
        f"Настроение: {_style(brief)}. Аудитория: {brief.target_audience}. "
        "Без вокала на переднем плане, ровный ритм, подходит под закадровый голос."
    )


def marketing_copy(brief: Brief) -> str:
    """Locally composed ad copy.

    Used when ``/capabilities`` exposes no text models: the plan still delivers a
    text asset, at zero cost and with an explicit warning, instead of silently
    dropping the requested format or inventing a model name.
    """
    lines = [
        f"# {brief.product_name}",
        "",
        f"**Для кого:** {brief.target_audience}",
        f"**Что это:** {brief.product_description}",
        f"**Оффер:** {brief.offer}",
        "",
        "## Варианты заголовков",
        f"1. {brief.product_name}: {brief.offer}",
        f"2. {brief.target_audience} — {brief.offer.rstrip('.')}.",
        f"3. {brief.product_name} — коротко о главном: {brief.product_description[:80].rstrip()}…",
        "",
        "## Основной текст",
        f"{brief.product_description} {brief.offer}",
        "",
        "## Призыв к действию",
        "Оставьте заявку сегодня — предложение ограничено.",
        "",
        f"_Тон: {_style(brief)}._",
    ]
    return "\n".join(lines)


PROMPT_BUILDERS = {
    ContentFormat.IMAGE: image_prompt,
    ContentFormat.VIDEO: video_prompt,
    ContentFormat.VOICE: voice_script,
    ContentFormat.MUSIC: music_prompt,
    ContentFormat.TEXT: marketing_copy,
}


def prompt_for(brief: Brief, fmt: ContentFormat) -> str:
    return PROMPT_BUILDERS[fmt](brief)
