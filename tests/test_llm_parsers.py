"""Tests for LLM helper parsers (no network required)."""
from __future__ import annotations

import pytest

from draft_limits import draft_char_limit
from llm import (
    _build_draft_prompt,
    _clamp_score,
    _hash_key,
    _parse_score_fallback,
    _parse_score_json,
    _smart_truncate,
    fit_reply,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("100% не подходит, 0/100", 100),  # last bounded number wins
        ("Score: 35", 35),
        ("Я бы дал 92.", 92),
        ("не подходит", 0),
        ("0", 0),
        ("100", 100),
    ],
)
def test_parse_score_fallback(text: str, expected: int) -> None:
    assert _parse_score_fallback(text) == expected


@pytest.mark.parametrize(
    "content,expected",
    [
        ('{"score": 87}', 87),
        ('{"score": "73"}', 73),
        ('preamble {"score": 55} suffix', 55),
        ("plain text without anything", None),
        ("42", 42),
    ],
)
def test_parse_score_json(content: str, expected: int | None) -> None:
    assert _parse_score_json(content) == expected


def test_clamp_score() -> None:
    assert _clamp_score(200) == 100
    assert _clamp_score(-5) == 0
    assert _clamp_score(50) == 50


def test_smart_truncate_prefers_sentence_boundary() -> None:
    text = "Первое предложение. Второе предложение! Третье?"
    out = _smart_truncate(text, max_chars=30)
    assert len(out) <= 30
    # Expect cut at the period.
    assert out.endswith(".") or out.endswith("…")


def test_smart_truncate_passthrough_when_short() -> None:
    text = "Короткий текст."
    assert _smart_truncate(text, max_chars=100) == text


def test_draft_prompt_requires_task_approach() -> None:
    prompt = _build_draft_prompt(
        skills="Python, aiogram",
        title="Бот для записи клиентов",
        description="Нужен бот: запись, напоминания, оплата через ЮKassa.",
        price="15000 RUB",
        deadline="7 дней",
        max_chars=900,
        style_hint="",
    )
    assert "Как я подойду к решению именно этой задачи" in prompt
    assert "2-4 конкретных шага" in prompt
    assert "Бот для записи клиентов" in prompt
    assert "ЮKassa" in prompt
    assert "15000 RUB" in prompt
    assert "7 дней" in prompt
    assert "Python, aiogram" in prompt
    assert "максимум 900 символов" in prompt
    assert "нейтральный деловой тон" in prompt
    assert "не придумывай факты" in prompt


def test_draft_prompt_keeps_approach_when_short() -> None:
    prompt = _build_draft_prompt(
        skills="S",
        title="T",
        description="D",
        price="",
        deadline="",
        max_chars=500,
        style_hint="короче обычного, но оставь сжатый план решения из 2 шагов",
    )
    assert "сожми его до 2 шагов" in prompt
    assert "Бюджет: не указан" in prompt
    assert "Срок: не указан" in prompt
    assert "короче обычного" in prompt


def test_fit_reply_never_exceeds_platform_limits() -> None:
    assert draft_char_limit("Kwork") == 2000
    assert draft_char_limit("FL.ru") == 5000
    assert draft_char_limit("FL.ru", short=True) == 700
    blob = "Шаг. " * 4000
    assert len(fit_reply(blob, 2000)) <= 2000
    assert len(fit_reply(blob, 5000)) <= 5000
    assert len(fit_reply("я" * 9000, 2000)) <= 2000
    assert fit_reply("ровно", 10) == "ровно"


def test_draft_prompt_asks_to_fill_long_limit() -> None:
    prompt = _build_draft_prompt(
        skills="S",
        title="T",
        description="D",
        price="",
        deadline="",
        max_chars=2000,
        style_hint="",
    )
    assert "не меньше 1500" in prompt
    assert "github.com/bioxhaust" not in prompt or "репозитор" in prompt.lower()
    assert "репозитор" in prompt.lower()


def test_hash_key_is_stable_and_distinct() -> None:
    a = _hash_key("draft", "S", "T", "D", "700", "")
    b = _hash_key("draft", "S", "T", "D", "700", "")
    c = _hash_key("draft", "S", "T", "D", "700", "friendly")
    assert a == b
    assert a != c
    assert len(a) == 32
