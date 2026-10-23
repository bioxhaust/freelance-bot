"""Golden-fixture tests for FL.ru selectolax parser.

These guard against silent regressions when fl.ru tweaks its DOM. If
selectors break, parse_flru_listing returns < 3 cards and tests fail
loudly instead of the bot quietly returning empty lists for days.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from exchanges.flru import _parse_age_snippet, parse_flru_listing, parse_flru_published_at

FIXTURE = (
    Path(__file__).parent
    / "fixtures_clean"
    / "flru_programming_p1.sanitized.html"
)


@pytest.fixture(scope="module")
def fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_parse_returns_cards(fixture_html: str) -> None:
    parsed = parse_flru_listing(fixture_html)
    # The page contains 30 cards; we accept ≥ 25 to allow for sanitization
    # accidentally dropping a few while still catching real regressions.
    assert len(parsed) >= 25, f"Only {len(parsed)} cards parsed"


def test_listing_reads_reply_counts(fixture_html: str) -> None:
    parsed = parse_flru_listing(fixture_html)
    counts = [order.offers_count for order, _ in parsed if order.offers_count is not None]
    assert counts, "expected at least one reply count"
    assert 4 in counts


def test_every_card_has_url_and_title(fixture_html: str) -> None:
    parsed = parse_flru_listing(fixture_html)
    for order, _ in parsed:
        assert order.url.startswith("https://www.fl.ru/projects/"), order.url
        assert order.title, "empty title"
        assert order.description, "empty description"
        # Source is hardcoded in the parser, but check anyway.
        assert order.source == "FL.ru"


def test_no_duplicate_urls(fixture_html: str) -> None:
    parsed = parse_flru_listing(fixture_html)
    urls = [o.url for o, _ in parsed]
    assert len(urls) == len(set(urls)), "duplicate URLs in parsed list"


def test_age_extraction_coverage(fixture_html: str) -> None:
    """At least 50% of cards on a normal page should have a parseable age."""
    parsed = parse_flru_listing(fixture_html)
    ages = [a for _, a in parsed if a is not None]
    coverage = len(ages) / max(1, len(parsed))
    assert coverage >= 0.5, f"age coverage {coverage:.0%} < 50%"


@pytest.mark.parametrize(
    "snippet,expected",
    [
        ("только что", 0),
        ("сейчас", 0),
        ("5 минут назад", 5),
        ("1 час назад", 60),
        ("6 часов 39 минут назад", 6 * 60 + 39),
        ("15 часов 22 минуты назад", 15 * 60 + 22),
        ("12 часов назад", 12 * 60),
        ("1 день назад", 60 * 24),
        ("3 суток назад", 60 * 24 * 3),
        ("вчера в 18:00", 60 * 24),
        ("сегодня", 60),
        ("30 секунд назад", 0),
        ("", None),
    ],
)
def test_parse_age_snippet(snippet: str, expected: int | None) -> None:
    assert _parse_age_snippet(snippet) == expected


def test_absolute_moscow_date_is_not_treated_as_fresh() -> None:
    now = datetime(2026, 10, 23, 5, 50, tzinfo=timezone.utc)  # 08:50 MSK
    published = parse_flru_published_at("22 октября, 08:01", now)
    assert published is not None
    assert published.astimezone(timezone.utc).day == 22
    # A "назад" inside the task text must not override the calendar date.
    html = """
    <div class="b-page__lenta_item">
      <h2 class="b-post__title"><a href="/projects/1/old.html">Старый заказ</a></h2>
      <div class="b-post__txt">Нужно было сделать час назад</div>
      <span class="text-gray-opacity-4">22 октября, 08:01</span>
    </div>
    """
    parsed = parse_flru_listing(html)
    assert len(parsed) == 1
    order, _age = parsed[0]
    assert order.published_at is not None
    assert order.published_at.astimezone(timezone.utc).day == 22
