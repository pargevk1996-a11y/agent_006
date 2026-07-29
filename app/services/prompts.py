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


def text_prompt(brief: Brief) -> str:
    """Instruction for a paid text model (``type: text``).

    Note the difference from :func:`marketing_copy`: this is a *task* for the model,
    while ``marketing_copy`` is the finished local fallback text.
    """
    return (
        f"Ты — маркетолог. Напиши рекламный текст для продукта «{brief.product_name}».\n"
        f"Что за продукт: {brief.product_description}\n"
        f"Целевая аудитория: {brief.target_audience}\n"
        f"Оффер: {brief.offer}\n"
        f"Тон и стиль: {_style(brief)}\n"
        f"Язык ответа: {brief.language}.\n"
        "Формат ответа: 3 варианта заголовка, основной текст на 400–600 знаков, "
        "один призыв к действию. Без вымышленных фактов, гарантий и обещаний результата."
    )


def marketing_copy(brief: Brief) -> str:
    """Locally composed ad copy — the free fallback.

    Used when no paid text model fits (нет в каталоге, несовместима, не влезла в
    бюджет): план всё равно отдаёт текстовый результат за 0 ₽ и с явным
    предупреждением, вместо того чтобы молча потерять формат или выдумать модель.
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
    ContentFormat.TEXT: text_prompt,
}


def prompt_for(brief: Brief, fmt: ContentFormat) -> str:
    return PROMPT_BUILDERS[fmt](brief)
