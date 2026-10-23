"""Pure rendering tests for notifier.py — no Telegram I/O."""
from __future__ import annotations

from datetime import datetime, timezone

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


def test_render_shows_kwork_hire_rate_next_to_client() -> None:
    order = _nasty_order()
    order.client = "pasha-project"
    order.client_hire_percent = 85
    order.client_orders_count = 37
    rendered = render_order_card(order, 80, "")
    assert "👤 pasha-project · нанял в 85% из 37 заказов" in rendered


def test_render_shows_zero_hire_rate() -> None:
    order = _nasty_order()
    order.client = "newbuyer"
    order.client_hire_percent = 0
    order.client_orders_count = 1
    rendered = render_order_card(order, 80, "")
    assert "нанял в 0% из 1 заказа" in rendered


def test_render_omits_hire_rate_when_exchange_has_none() -> None:
    rendered = render_order_card(_nasty_order(), 80, "")
    assert "нанял" not in rendered


def test_hire_phrase_genitive() -> None:
    order = _nasty_order()
    order.client_hire_percent = 21
    order.client_orders_count = 21
    assert order.hire_phrase() == "нанял в 21% из 21 заказа"
    order.client_orders_count = 11
    assert order.hire_phrase() == "нанял в 21% из 11 заказов"
    order.client_orders_count = None
    assert order.hire_phrase() == "нанял в 21% заказов"


def test_render_includes_moscow_publication_time() -> None:
    order = _nasty_order()
    order.published_at = datetime(2026, 10, 23, 5, 47, tzinfo=timezone.utc)
    rendered = render_order_card(order, 80, "")
    assert "🕐 23 октября, 08:47 мск" in rendered


def test_render_includes_relevance_marker() -> None:
    rendered = render_order_card(_nasty_order(), 87, "")
    assert "· 87%" in rendered


def test_long_draft_keeps_pre_closed() -> None:
    rendered = render_order_card(_nasty_order(), 87, "X" * 5000)
    assert len(rendered) > 3500
    assert rendered.count("<pre>") == rendered.count("</pre>") == 1


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
