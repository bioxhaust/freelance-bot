from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiosqlite

from exchanges import Order

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

_REQUIRED_COLUMNS = frozenset(
    {
        "id",
        "title",
        "description",
        "price",
        "deadline",
        "client",
        "source",
        "url",
        "relevance",
        "applied",
        "applied_at",
        "created_at",
    }
)

_CREATE_ORDERS_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    description  TEXT    NOT NULL,
    price        TEXT    NOT NULL,
    deadline     TEXT    NOT NULL,
    client       TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    url          TEXT    NOT NULL UNIQUE,
    relevance    INTEGER,
    applied      INTEGER NOT NULL DEFAULT 0,
    applied_at   DATETIME,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_META_SQL = """
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_orders_applied_created ON orders(applied, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_orders_source ON orders(source)",
    "CREATE INDEX IF NOT EXISTS idx_orders_relevance ON orders(relevance)",
)


class SchemaError(RuntimeError):
    """Raised when DB schema does not match expectations after init."""


class Database:
    def __init__(self, path: str = "orders.db") -> None:
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row

        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")

        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(_CREATE_ORDERS_SQL)
            await conn.execute(_CREATE_META_SQL)
            await self._migrate(conn)
            for stmt in _INDEXES:
                await conn.execute(stmt)
            await conn.execute("COMMIT")
        except Exception:
            await conn.execute("ROLLBACK")
            await conn.close()
            raise

        self._conn = conn
        await self._verify_schema()
        logger.info("Database ready: path=%s schema=%d", self.path, SCHEMA_VERSION)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        async with conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ) as cur:
            row = await cur.fetchone()
        current = int(row[0]) if row else 0

        async with conn.execute("PRAGMA table_info(orders)") as cur:
            cols = {r[1] async for r in cur}

        if "applied" not in cols:
            await conn.execute(
                "ALTER TABLE orders ADD COLUMN applied INTEGER NOT NULL DEFAULT 0"
            )
            logger.warning("Migration: added orders.applied column")
        if "applied_at" not in cols:
            await conn.execute(
                "ALTER TABLE orders ADD COLUMN applied_at DATETIME"
            )
            logger.warning("Migration: added orders.applied_at column")

        await conn.execute(
            "INSERT OR REPLACE INTO _meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        if current != SCHEMA_VERSION:
            logger.info("DB schema migrated %s -> %s", current, SCHEMA_VERSION)

    async def _verify_schema(self) -> None:
        # Fail-fast: ensure migrations actually applied. Without this check
        # missing columns silently break is_applied / mark_applied at runtime.
        async with self.conn.execute("PRAGMA table_info(orders)") as cur:
            cols = {row[1] async for row in cur}
        missing = _REQUIRED_COLUMNS - cols
        if missing:
            raise SchemaError(
                f"orders table is missing required columns: {sorted(missing)}. "
                f"Migration likely failed; check logs for ROLLBACK."
            )

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.init() must be called first")
        return self._conn

    async def exists_by_url(self, url: str) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM orders WHERE url = ? LIMIT 1", (url,)
        ) as cur:
            return await cur.fetchone() is not None

    async def save_order_if_new(
        self, order: Order, relevance: int | None = None
    ) -> bool:
        async with self._lock:
            cur = await self.conn.execute(
                """
                INSERT OR IGNORE INTO orders
                    (title, description, price, deadline, client, source, url, relevance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.title,
                    order.description,
                    order.price,
                    order.deadline,
                    order.client,
                    order.source,
                    order.url,
                    relevance,
                ),
            )
            return cur.rowcount > 0

    async def save_order(self, order: Order, relevance: int | None = None) -> None:
        await self.save_order_if_new(order, relevance)

    async def get_order_by_url(self, url: str) -> Optional[Order]:
        async with self.conn.execute(
            """
            SELECT title, description, price, deadline, client, source, url
            FROM orders
            WHERE url = ?
            LIMIT 1
            """,
            (url,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Order(
            title=row[0],
            description=row[1],
            price=row[2],
            deadline=row[3],
            client=row[4],
            source=row[5],
            url=row[6],
        )

    async def update_relevance(self, url: str, relevance: int) -> None:
        await self.conn.execute(
            "UPDATE orders SET relevance = ? WHERE url = ?",
            (int(relevance), url),
        )

    async def is_applied(self, url: str) -> bool:
        async with self.conn.execute(
            "SELECT applied FROM orders WHERE url = ? LIMIT 1", (url,)
        ) as cur:
            row = await cur.fetchone()
        return bool(row and row[0])

    async def mark_applied(self, url: str) -> None:
        await self.conn.execute(
            """
            UPDATE orders
            SET applied = 1, applied_at = CURRENT_TIMESTAMP
            WHERE url = ?
            """,
            (url,),
        )

    async def stats(self) -> dict[str, int | float]:
        async with self.conn.execute(
            """
            SELECT
                COUNT(*)                                                    AS total,
                COALESCE(SUM(applied), 0)                                   AS applied_cnt,
                COALESCE(AVG(relevance), 0.0)                               AS avg_rel,
                SUM(CASE WHEN created_at >= datetime('now','-1 day')
                         THEN 1 ELSE 0 END)                                 AS last_24h,
                SUM(CASE WHEN applied = 1
                              AND applied_at >= datetime('now','-1 day')
                         THEN 1 ELSE 0 END)                                 AS sent_24h
            FROM orders
            """
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return {"total": 0, "applied": 0, "avg_relevance": 0.0, "last_24h": 0, "sent_24h": 0}
        return {
            "total": int(row[0] or 0),
            "applied": int(row[1] or 0),
            "avg_relevance": float(row[2] or 0.0),
            "last_24h": int(row[3] or 0),
            "sent_24h": int(row[4] or 0),
        }
