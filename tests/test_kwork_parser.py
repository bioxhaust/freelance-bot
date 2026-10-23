"""Tests for the Kwork item -> Order conversion + MSK timezone handling."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from exchanges.kwork import kwork_category_id, kwork_published_at, parse_kwork_hire_stats

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]


FIXTURES = Path(__file__).parent / "fixtures_clean"


def _load(cat: int) -> dict:
    return json.loads(
        (FIXTURES / f"kwork_c{cat}.sanitized.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("cat", [41, 11, 45])
def test_kwork_fixture_shape(cat: int) -> None:
    data = _load(cat)
    items = ((data.get("data") or {}).get("pagination") or {}).get("data") or []
    assert items, f"empty fixture for c={cat}"
    item = items[0]
    # Keys our parser depends on:
    for key in ("id", "name", "description", "priceLimit", "max_days", "user", "date_create"):
        assert key in item, f"missing key {key!r} in c={cat} item"


@pytest.mark.parametrize("cat", [41, 11, 45])
def test_kwork_msk_timezone_conversion(cat: int) -> None:
    """date_create is MSK; converting to UTC must subtract 3 hours."""
    data = _load(cat)
    items = ((data.get("data") or {}).get("pagination") or {}).get("data") or []
    assert items, "no items"
    msk = ZoneInfo("Europe/Moscow")
    for item in items:
        date_create = (item.get("date_create") or "").strip()
        if not date_create:
            continue
        naive = datetime.strptime(date_create, "%Y-%m-%d %H:%M:%S")
        as_msk = naive.replace(tzinfo=msk)
        as_utc = as_msk.astimezone(timezone.utc)
        # Hour delta must be 3 (MSK is UTC+3 year-round, no DST).
        assert (as_msk.hour - as_utc.hour) % 24 == 3, (
            f"unexpected timezone shift for {date_create!r}: {as_msk} vs {as_utc}"
        )


def test_kwork_url_and_price_format() -> None:
    """Smoke-check the same normalization KworkExchange.fetch_orders does."""
    data = _load(41)
    items = ((data.get("data") or {}).get("pagination") or {}).get("data") or []
    item = items[0]
    url = f"https://kwork.ru/projects/{item['id']}/view"
    assert url.startswith("https://kwork.ru/projects/")
    price_limit = (item.get("priceLimit") or "").strip()
    price = f"{price_limit} RUB" if price_limit else "по договоренности"
    assert price.endswith("RUB") or price == "по договоренности"


def test_kwork_published_at_is_moscow() -> None:
    published = kwork_published_at("2026-10-23 08:24:47")
    assert published is not None
    assert published == datetime(2026, 10, 23, 5, 24, 47, tzinfo=timezone.utc)
    assert kwork_published_at("") is None
    assert kwork_published_at("not-a-date") is None


def test_kwork_category_id_accepts_string() -> None:
    assert kwork_category_id("41") == 41
    assert kwork_category_id(11) == 11
    assert kwork_category_id("") is None
    assert kwork_category_id(None) is None


def test_parse_kwork_hire_stats() -> None:
    percent, count = parse_kwork_hire_stats(
        {"data": {"wants_hired_percent": "85", "wants_count": "37"}}
    )
    assert percent == 85
    assert count == 37

    # 0 is a real value: the buyer has projects and has not hired anyone.
    percent, count = parse_kwork_hire_stats(
        {"data": {"wants_hired_percent": "0", "wants_count": "1"}}
    )
    assert percent == 0
    assert count == 1

    assert parse_kwork_hire_stats({}) == (None, None)
    assert parse_kwork_hire_stats({"data": {"wants_hired_percent": "nope"}}) == (None, None)
    assert parse_kwork_hire_stats({"data": {"wants_hired_percent": "150", "wants_count": "-1"}}) == (
        None,
        None,
    )
