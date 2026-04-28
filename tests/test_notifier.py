"""Pure rendering tests for notifier.py — no Telegram I/O."""
from __future__ import annotations

from exchanges import Order
from notifier import (
    build_actions_keyboard,
    build_finalised_keyboard,
    build_tone_keyboard,
    render_order_card,
)


def _nasty_order() -> Order:
    return Order(
        title="<script>alert(1)</script> & R&D",
        description="A & B " + "lorem " * 200,
        price="не указан",
        deadline="не указан",
        client="не указан",
        source="Kwork",
        url="https://kwork.ru/projects/12345/view?ref=<x>&y=1",
    )


def test_render_escapes_script_in_title() -> None:
    rendered = render_order_card(_nasty_order(), 80, "")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_render_hides_placeholder_fields() -> None:
    rendered = render_order_card(_nasty_order(), 80, "")
    assert "Цена" not in rendered
    assert "Срок" not in rendered
    assert "Клиент" not in rendered


def test_render_escapes_url_in_href() -> None:
    rendered = render_order_card(_nasty_order(), 80, "")
    # & must become &amp; in href; < must become &lt;.
    assert "&lt;x&gt;&amp;y=1" in rendered


def test_render_includes_relevance_marker() -> None:
    rendered = render_order_card(_nasty_order(), 87, "")
    assert "· 87%" in rendered


def test_render_card_under_telegram_limit() -> None:
    rendered = render_order_card(_nasty_order(), 87, "X" * 5000)
    assert len(rendered) <= 4000  # leaves headroom under 4096


def test_actions_keyboard_no_draft_layout() -> None:
    kb = build_actions_keyboard("https://x", has_draft=False)
    assert len(kb.inline_keyboard) == 3
    assert kb.inline_keyboard[0][0].text.startswith("✏️ Сгенерировать")
    # Bottom rows should still expose Why/Open and Skip/Archive.
    callbacks = {b.callback_data for r in kb.inline_keyboard for b in r}
    assert {"why", "skip", "archive"} <= callbacks


def test_actions_keyboard_with_draft_has_send_and_tone() -> None:
    kb = build_actions_keyboard("https://x", has_draft=True)
    callbacks = {b.callback_data for r in kb.inline_keyboard for b in r if b.callback_data}
    assert {"send", "draft", "short", "tone", "why", "skip", "archive"} <= callbacks


def test_tone_keyboard_has_three_tones_plus_back() -> None:
    kb = build_tone_keyboard("https://x")
    callbacks = {b.callback_data for r in kb.inline_keyboard for b in r}
    assert {"tone_neutral", "tone_friendly", "tone_hard", "tone_back"} <= callbacks


def test_finalised_keyboard_one_url_button() -> None:
    kb = build_finalised_keyboard("https://x")
    assert len(kb.inline_keyboard) == 1
    btn = kb.inline_keyboard[0][0]
    assert btn.url == "https://x"
    assert btn.callback_data is None
