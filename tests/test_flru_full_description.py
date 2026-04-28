"""Tests for full-description extraction from an FL.ru order detail page.

Two-stage extraction strategy must work:
  1. JSON-LD (schema.org Product.description) — primary, most stable.
  2. div.fl-project-content__description-text — CSS fallback.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from exchanges.flru import (
    _normalize_paragraphs,
    parse_flru_full_description,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures_clean"
    / "flru_order_page.sanitized.html"
)


@pytest.fixture(scope="module")
def fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_returns_substantial_description(fixture_html: str) -> None:
    desc = parse_flru_full_description(fixture_html)
    assert desc is not None
    # Listing-preview is ~150 chars; full must be much longer.
    assert len(desc) >= 400, f"only {len(desc)} chars: {desc[:200]!r}"


def test_description_starts_with_real_text(fixture_html: str) -> None:
    desc = parse_flru_full_description(fixture_html)
    assert desc is not None
    # First sentence must be content, not a "Найти работу..." nav scrap.
    head = desc[:200].lower()
    assert "найти работу" not in head
    assert "тестирован" in head or "сервис" in head, head[:120]


def test_returns_none_on_unrelated_html() -> None:
    assert parse_flru_full_description("<html><body>Hello</body></html>") is None


def test_returns_none_on_empty_string() -> None:
    assert parse_flru_full_description("") is None


def test_returns_none_when_description_too_short() -> None:
    html = (
        '<html><body>'
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","description":"короткое"}'
        '</script>'
        '</body></html>'
    )
    assert parse_flru_full_description(html) is None


def test_falls_back_to_css_selector_when_no_jsonld() -> None:
    big_text = "Полное описание заказа. " * 30
    html = (
        '<html><body>'
        f'<div class="fl-project-content__description-text">{big_text}</div>'
        '</body></html>'
    )
    desc = parse_flru_full_description(html)
    assert desc is not None
    assert len(desc) >= 400


def test_jsonld_wins_over_css_when_both_present() -> None:
    html = (
        '<html><body>'
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product",'
        '"description":"' + ("JSON-LD content. " * 30) + '"}'
        '</script>'
        '<div class="fl-project-content__description-text">'
        + ("CSS fallback content. " * 30)
        + "</div></body></html>"
    )
    desc = parse_flru_full_description(html)
    assert desc is not None
    assert "JSON-LD content." in desc
    assert "CSS fallback content." not in desc


@pytest.mark.parametrize(
    "raw,expected_substr,must_not_contain",
    [
        ("a\r\nb\r\nc", "a\nb\nc", "\r"),
        ("x   y\t\tz", "x y z", "\t"),
        ("p1\n\n\n\np2", "p1\n\np2", "\n\n\n"),
    ],
)
def test_normalize_paragraphs(raw: str, expected_substr: str, must_not_contain: str) -> None:
    out = _normalize_paragraphs(raw)
    assert expected_substr in out
    assert must_not_contain not in out
