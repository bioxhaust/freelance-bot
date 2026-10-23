from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from http_client import HttpClient

from .base import Exchange, Order

logger = logging.getLogger(__name__)

_MOSCOW_TZ = ZoneInfo("Europe/Moscow")


class KworkExchange(Exchange):
    source_name = "Kwork"
    projects_url = "https://kwork.ru/projects"

    def __init__(
        self,
        http: HttpClient,
        cookies: str = "",
        max_pages: int = 10,
        categories: tuple[int, ...] = (41, 11, 45),
        page_concurrency: int = 4,
    ) -> None:
        self.http = http
        self.cookies = cookies
        self.max_pages = max(1, max_pages)
        self.categories = tuple(categories) if categories else (41, 11, 45)
        self._page_sem = asyncio.Semaphore(max(1, page_concurrency))
        self._created_at_by_url: dict[str, datetime] = {}

    def debug_context(self) -> str:
        return f"categories={','.join(str(x) for x in self.categories)}"

    def order_age_minutes(self, order: Order) -> Optional[int]:
        created_at = self._created_at_by_url.get(order.url)
        if created_at is None:
            return None
        delta = datetime.now(timezone.utc) - created_at
        return max(0, int(delta.total_seconds() // 60))

    async def fetch_orders(self) -> list[Order]:
        # Page 1 tells us last_page, so we do not request empty pages.
        results = await asyncio.gather(
            *(self._fetch_category(category_id) for category_id in self.categories),
            return_exceptions=True,
        )

        items: list[dict] = []
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("Kwork category fetch failed: %r", result)
                continue
            items.extend(result)

        orders: list[Order] = []
        seen_urls: set[str] = set()
        created_at_by_url: dict[str, datetime] = {}
        category_mismatches = 0

        for item in items:
            order_id = item.get("id")
            if not order_id:
                continue

            user = item.get("user") or {}
            title = (item.get("name") or "").strip()
            if not title:
                continue

            description = (item.get("description") or "").strip() or title
            price_limit = (item.get("priceLimit") or "").strip()
            price = f"{price_limit} RUB" if price_limit else "по договоренности"
            deadline = (item.get("max_days") or "").strip() or "не указан"
            client = (user.get("username") or "").strip() or "не указан"
            hire_percent, orders_count = parse_kwork_hire_stats(user)
            offers_count = _stat_int(item.get("kwork_count"), hi=1_000_000)
            url = f"https://kwork.ru/projects/{order_id}/view"
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # category_id arrives as a string ("41"). Compare as int, and
            # drop projects the feed mixed in from another category.
            actual_cat = kwork_category_id(item.get("category_id"))
            if (
                actual_cat is not None
                and self.categories
                and actual_cat not in self.categories
            ):
                category_mismatches += 1
                continue

            date_create = (item.get("date_create") or "").strip()
            if date_create:
                try:
                    naive = datetime.strptime(date_create, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
                else:
                    aware_msk = naive.replace(tzinfo=_MOSCOW_TZ)
                    created_at_by_url[url] = aware_msk.astimezone(timezone.utc)

            orders.append(
                Order(
                    title=title,
                    description=description,
                    price=price,
                    deadline=deadline,
                    client=client,
                    source=self.source_name,
                    url=url,
                    client_hire_percent=hire_percent,
                    client_orders_count=orders_count,
                    offers_count=offers_count,
                )
            )

        if category_mismatches:
            logger.info(
                "Kwork category mismatches: %d items returned outside requested %s",
                category_mismatches, self.categories,
            )

        self._created_at_by_url = created_at_by_url
        return orders

    async def _fetch_category(self, category_id: int) -> list[dict]:
        items, last_page = await self._fetch_page(category_id, 1)
        last_page = max(1, min(last_page, self.max_pages))
        if last_page == 1:
            return items
        rest = await asyncio.gather(
            *(
                self._fetch_page(category_id, page)
                for page in range(2, last_page + 1)
            ),
            return_exceptions=True,
        )
        for result in rest:
            if isinstance(result, BaseException):
                logger.warning(
                    "Kwork page fetch failed for c=%s: %r", category_id, result
                )
                continue
            page_items, _last = result
            items.extend(page_items)
        return items

    async def _fetch_page(self, category_id: int, page: int) -> tuple[list[dict], int]:
        async with self._page_sem:
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ru,en;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            }
            if self.cookies:
                headers["Cookie"] = self.cookies

            response = await self.http.request_with_retry(
                "POST",
                self.projects_url,
                data={"page": str(page), "c": str(category_id)},
                headers=headers,
            )
            async with response:
                response.raise_for_status()
                payload = await response.json(content_type=None)

        pagination = ((payload.get("data") or {}).get("pagination") or {})
        page_items = pagination.get("data") or []
        try:
            last_page = int(pagination.get("last_page") or 1)
        except (TypeError, ValueError):
            last_page = 1
        return page_items, max(1, last_page)


def parse_kwork_hire_stats(user: dict) -> tuple[Optional[int], Optional[int]]:
    """Read buyer hire rate from a Kwork project user object.

    `wants_hired_percent` is the share of this buyer's projects where they
    picked a freelancer. `wants_count` is how many projects that share covers.
    Both arrive as strings. Missing or garbage values become None; 0 is kept.
    """
    data = user.get("data") if isinstance(user, dict) else None
    if not isinstance(data, dict):
        return None, None
    return (
        _stat_int(data.get("wants_hired_percent"), hi=100),
        _stat_int(data.get("wants_count"), hi=1_000_000),
    )


def kwork_category_id(value: object) -> Optional[int]:
    """Kwork sends category_id as a string. None if it is missing or junk."""
    return _stat_int(value, hi=1_000_000)


def _stat_int(raw: object, *, hi: int) -> Optional[int]:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", ".")
    if not text:
        return None
    try:
        value = int(round(float(text)))
    except (TypeError, ValueError):
        return None
    if value < 0 or value > hi:
        return None
    return value
