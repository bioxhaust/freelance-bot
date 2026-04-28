"""Async DB tests against a temp SQLite file."""
from __future__ import annotations

import os
import tempfile

import pytest
import pytest_asyncio

from db import Database
from exchanges import Order


@pytest_asyncio.fixture
async def db():
    tmp_dir = tempfile.mkdtemp(prefix="bot_test_")
    path = os.path.join(tmp_dir, "test.db")
    instance = Database(path)
    await instance.init()
    try:
        yield instance
    finally:
        await instance.close()


def _order(i: int, *, source: str = "Kwork") -> Order:
    return Order(
        title=f"Order {i}",
        description=f"Description {i}",
        price="100 RUB",
        deadline="—",
        client="—",
        source=source,
        url=f"https://test.example/{i}",
    )


@pytest.mark.asyncio
async def test_schema_has_required_columns(db: Database) -> None:
    async with db.conn.execute("PRAGMA table_info(orders)") as cur:
        cols = {row[1] async for row in cur}
    assert {"applied", "applied_at", "relevance", "url", "source"} <= cols


@pytest.mark.asyncio
async def test_save_order_if_new_dedup(db: Database) -> None:
    o = _order(1)
    assert await db.save_order_if_new(o, relevance=50) is True
    assert await db.save_order_if_new(o, relevance=99) is False


@pytest.mark.asyncio
async def test_mark_applied_and_is_applied(db: Database) -> None:
    o = _order(2)
    await db.save_order_if_new(o, relevance=80)
    assert await db.is_applied(o.url) is False
    await db.mark_applied(o.url)
    assert await db.is_applied(o.url) is True


@pytest.mark.asyncio
async def test_update_relevance(db: Database) -> None:
    o = _order(3)
    await db.save_order_if_new(o, relevance=None)
    await db.update_relevance(o.url, 77)
    async with db.conn.execute(
        "SELECT relevance FROM orders WHERE url = ?", (o.url,)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and int(row[0]) == 77


@pytest.mark.asyncio
async def test_stats_counts_by_source(db: Database) -> None:
    for i in range(4):
        await db.save_order_if_new(_order(i, source="Kwork"), relevance=10 + i * 30)
    for i in range(4, 6):
        await db.save_order_if_new(_order(i, source="FL.ru"), relevance=70 + i)

    await db.mark_applied(_order(5).url)

    stats = await db.stats(threshold=70)
    assert stats["last_24h"] == 6
    # rels: 10, 40, 70, 100, 74, 75 → ≥70 are 4 of them
    assert stats["passed_24h"] == 4
    assert stats["sent_24h"] == 1
    by_src = dict(stats["by_source_24h"])
    assert by_src == {"Kwork": 4, "FL.ru": 2}


@pytest.mark.asyncio
async def test_top_unhandled_excludes_applied(db: Database) -> None:
    for i, rel in enumerate([60, 75, 85, 95]):
        await db.save_order_if_new(_order(i), relevance=rel)
    await db.mark_applied(_order(2).url)  # rel=85 → applied

    top = await db.top_unhandled(threshold=70, limit=10)
    rels = []
    for o in top:
        async with db.conn.execute(
            "SELECT relevance FROM orders WHERE url = ?", (o.url,)
        ) as cur:
            r = await cur.fetchone()
        rels.append(int(r[0]))
    # Should return rel=95 and rel=75 (both ≥70 and not applied), in DESC order.
    assert rels == [95, 75]


@pytest.mark.asyncio
async def test_cleanup_old_keeps_applied(db: Database) -> None:
    fresh = _order(100)
    old_unapplied = _order(101)
    old_applied = _order(102)

    await db.save_order_if_new(fresh, relevance=50)
    await db.save_order_if_new(old_unapplied, relevance=50)
    await db.save_order_if_new(old_applied, relevance=50)

    # Force created_at backwards for the two "old" rows.
    for url in (old_unapplied.url, old_applied.url):
        await db.conn.execute(
            "UPDATE orders SET created_at = datetime('now','-100 days') WHERE url = ?",
            (url,),
        )
    await db.mark_applied(old_applied.url)

    deleted = await db.cleanup_old(max_age_days=30)
    assert deleted == 1  # only old_unapplied is gone

    assert await db.get_order_by_url(fresh.url) is not None
    assert await db.get_order_by_url(old_applied.url) is not None
    assert await db.get_order_by_url(old_unapplied.url) is None
