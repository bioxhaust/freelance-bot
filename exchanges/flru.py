from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urljoin

from aiohttp import ClientResponseError
from selectolax.parser import HTMLParser, Node

from http_client import HttpClient

from .base import Exchange, Order

logger = logging.getLogger(__name__)


_BASE_URL = "https://www.fl.ru"

# Multi-token "X часов Y минут назад" pattern, plus "только что" / "сегодня" / "вчера".
# All regex go through `lower()` text first.
_AGE_NUMERIC: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"(\d+)\s*сек"), 0),                   # round seconds to 0 minutes
    (re.compile(r"(\d+)\s*мин"), 1),
    (re.compile(r"(\d+)\s*час"), 60),
    (re.compile(r"(\d+)\s*(?:дн|ден|сут)"), 60 * 24),
    (re.compile(r"(\d+)\s*нед"), 60 * 24 * 7),
    (re.compile(r"(\d+)\s*мес"), 60 * 24 * 30),
    (re.compile(r"(\d+)\s*(?:год|лет)"), 60 * 24 * 365),
)


class FLRuExchange(Exchange):
    source_name = "FL.ru"
    category_urls = (
        "https://www.fl.ru/projects/category/programmirovanie/",
        "https://www.fl.ru/projects/category/saity/",
    )

    def __init__(
        self,
        http: HttpClient,
        cookies: str = "",
        max_pages: int = 10,
        page_concurrency: int = 3,
    ) -> None:
        self.http = http
        self.cookies = cookies
        self.max_pages = max(1, max_pages)
        self._page_sem = asyncio.Semaphore(max(1, page_concurrency))
        self._age_minutes_by_url: dict[str, int] = {}

    def debug_context(self) -> str:
        return "categories=" + ",".join(
            url.rstrip("/").split("/")[-1] for url in self.category_urls
        )

    def order_age_minutes(self, order: Order) -> Optional[int]:
        return self._age_minutes_by_url.get(order.url)

    async def fetch_orders(self) -> list[Order]:
        # FL.ru pagination is sequential per-category (we need to know when
        # 404 happens to stop), but categories themselves run concurrently.
        results = await asyncio.gather(
            *(self._fetch_category(base) for base in self.category_urls),
            return_exceptions=True,
        )

        all_orders: list[Order] = []
        seen_urls: set[str] = set()
        age_by_url: dict[str, int] = {}

        for result in results:
            if isinstance(result, BaseException):
                logger.warning("FL.ru category fetch failed: %r", result)
                continue
            for order, age in result:
                if order.url in seen_urls:
                    continue
                seen_urls.add(order.url)
                if age is not None:
                    age_by_url[order.url] = age
                all_orders.append(order)

        self._age_minutes_by_url = age_by_url
        return all_orders

    async def _fetch_category(self, base_url: str) -> list[tuple[Order, Optional[int]]]:
        out: list[tuple[Order, Optional[int]]] = []
        for page in range(1, self.max_pages + 1):
            url = base_url if page == 1 else f"{base_url}?page={page}"
            try:
                page_html = await self._get_html(url)
            except ClientResponseError as exc:
                if exc.status == 404 and page > 1:
                    break
                raise
            page_orders = parse_flru_listing(page_html)
            if not page_orders:
                break

            new_in_page = 0
            for order, age in page_orders:
                out.append((order, age))
                new_in_page += 1

            if new_in_page == 0:
                break
        return out

    async def _get_html(self, url: str) -> str:
        async with self._page_sem:
            headers = {"Accept-Language": "ru,en;q=0.9"}
            if self.cookies:
                headers["Cookie"] = self.cookies
            response = await self.http.request_with_retry(
                "GET", url, headers=headers
            )
            async with response:
                response.raise_for_status()
                return await response.text()


# --- Pure parsing helpers (no IO) — easy to unit-test against fixtures ---


def parse_flru_listing(html_text: str) -> list[tuple[Order, Optional[int]]]:
    """Extract orders + ages from a single FL.ru listing page HTML.

    Selectors derived from real fl.ru page (Apr 2026):
      - card:        div.b-page__lenta_item
      - title link:  h2.b-post__title a
      - description: div.b-post__txt
      - price:       div.b-post__price span.text-4
      - footer/age:  div.b-post__foot ('X часов Y минут назад' / 'только что')
    """
    tree = HTMLParser(html_text)
    out: list[tuple[Order, Optional[int]]] = []
    seen: set[str] = set()

    for card in tree.css("div.b-page__lenta_item"):
        title_a = card.css_first("h2.b-post__title a")
        if title_a is None:
            continue
        href = (title_a.attributes.get("href") or "").strip()
        if not href:
            continue
        url = urljoin(_BASE_URL, href)
        if url in seen:
            continue
        seen.add(url)

        title = _clean(title_a.text(strip=True) or "")
        if not title:
            continue

        desc_node = card.css_first("div.b-post__txt")
        description = _clean(desc_node.text(strip=True) if desc_node else "") or title

        price = "по договоренности"
        price_node = card.css_first("div.b-post__price span.text-4") or card.css_first(
            "span.text-4"
        )
        if price_node:
            price = _clean(price_node.text(strip=True)) or price

        age = _extract_age_minutes_from_card(card)

        out.append(
            (
                Order(
                    title=title,
                    description=description,
                    price=price,
                    deadline="не указан",
                    client="не указан",
                    source=FLRuExchange.source_name,
                    url=url,
                ),
                age,
            )
        )
    return out


def _extract_age_minutes_from_card(card: Node) -> Optional[int]:
    foot = card.css_first("div.b-post__foot")
    snippet = ""
    if foot is not None:
        snippet = foot.text(strip=True).lower()
    if not snippet:
        snippet = card.text(strip=True).lower()

    return _parse_age_snippet(snippet)


def _parse_age_snippet(snippet: str) -> Optional[int]:
    if not snippet:
        return None
    if "только что" in snippet or "сейчас" in snippet:
        return 0
    # Multi-token expressions ("X часов Y минут назад") accumulate.
    if "назад" in snippet:
        total = 0
        matched_any = False
        for pat, mult in _AGE_NUMERIC:
            for m in pat.finditer(snippet):
                num = next((g for g in m.groups() if g), None)
                if num is None:
                    continue
                total += int(num) * mult
                matched_any = True
        if matched_any:
            return total
    if "сегодня" in snippet:
        return 60
    if "вчера" in snippet:
        return 60 * 24
    return None


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
