"""Tests for LLM helper parsers (no network required)."""
from __future__ import annotations

import pytest

from llm import (
    _clamp_score,
    _hash_key,
    _parse_score_fallback,
    _parse_score_json,
    _smart_truncate,
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


def test_hash_key_is_stable_and_distinct() -> None:
    a = _hash_key("draft", "S", "T", "D", "700", "")
    b = _hash_key("draft", "S", "T", "D", "700", "")
    c = _hash_key("draft", "S", "T", "D", "700", "friendly")
    assert a == b
    assert a != c
    assert len(a) == 32
