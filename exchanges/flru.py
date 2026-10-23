from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from aiohttp import ClientResponseError
from selectolax.parser import HTMLParser, Node

from http_client import HttpClient

from .base import Exchange, Order

logger = logging.getLogger(__name__)


_BASE_URL = "https://www.fl.ru"
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")
_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_ABSOLUTE_RE = re.compile(
    r"(\d{1,2})\s+([а-яё]+)\s*,\s*(\d{1,2}):(\d{2})",
    re.IGNORECASE,
)

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
        detail_concurrency: int = 5,
    ) -> None:
        self.http = http
        self.cookies = cookies
        self.max_pages = max(1, max_pages)
        self._page_sem = asyncio.Semaphore(max(1, page_concurrency))
        self._detail_sem = asyncio.Semaphore(max(1, detail_concurrency))

    def debug_context(self) -> str:
        return "categories=" + ",".join(
            url.rstrip("/").split("/")[-1] for url in self.category_urls
        )

    async def fetch_orders(self) -> list[Order]:
        # FL.ru pagination is sequential per-category (we need to know when
        # 404 happens to stop), but categories themselves run concurrently.
        results = await asyncio.gather(
            *(self._fetch_category(base) for base in self.category_urls),
            return_exceptions=True,
        )

        all_orders: list[Order] = []
        seen_urls: set[str] = set()

        for result in results:
            if isinstance(result, BaseException):
                logger.warning("FL.ru category fetch failed: %r", result)
                continue
            for order, _age in result:
                if order.url in seen_urls:
                    continue
                seen_urls.add(order.url)
                all_orders.append(order)

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

    async def fetch_full_description(self, order_url: str) -> Optional[str]:
        """GET the order's detail page and pull the full description.

        Returns None on parse / network failure (caller should keep using the
        listing-page preview in that case). Concurrency is limited by
        _detail_sem so we don't hammer fl.ru with 100+ concurrent GETs.
        """
        async with self._detail_sem:
            headers = {"Accept-Language": "ru,en;q=0.9"}
            if self.cookies:
                headers["Cookie"] = self.cookies
            try:
                response = await self.http.request_with_retry(
                    "GET", order_url, headers=headers
                )
                async with response:
                    response.raise_for_status()
                    html_text = await response.text()
            except Exception as exc:
                logger.warning(
                    "FL.ru full description fetch failed for %s: %r", order_url, exc
                )
                return None

        return parse_flru_full_description(html_text)


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

        published = _extract_published_at(card)
        age = _age_minutes(published)
        offers = _extract_offers_count(card)

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
                    offers_count=offers,
                    published_at=published,
                ),
                age,
            )
        )
    return out


_REPLIES_RE = re.compile(r"(\d+)\s*ответ", re.IGNORECASE)


def _extract_offers_count(card: Node) -> Optional[int]:
    node = card.css_first("span[data-id='fl-view-count-href']")
    if node is None:
        node = card.css_first("svg[title='Количество ответов']")
    if node is None:
        return None
    snippet = node.text(strip=True).lower().replace("\xa0", " ")
    match = _REPLIES_RE.search(snippet)
    if match is None:
        return None
    return int(match.group(1))


def _extract_published_at(card: Node, now: Optional[datetime] = None) -> Optional[datetime]:
    """Publication time from the listing timestamp, not the whole card.

    FL.ru prints "6 часов 39 минут назад" for the first day and then an
    absolute Moscow time like "22 октября, 08:01". The card body often
    contains "назад" in the task text, so the fallback must not use it.
    """
    span = card.css_first("span.text-gray-opacity-4")
    snippet = span.text(strip=True) if span is not None else ""
    if not snippet:
        foot = card.css_first("div.b-post__foot")
        snippet = foot.text(strip=True) if foot is not None else ""
    return parse_flru_published_at(snippet, now or datetime.now(timezone.utc))


def parse_flru_published_at(snippet: str, now: datetime) -> Optional[datetime]:
    """UTC publication time, or None when the listing has no timestamp."""
    text = (snippet or "").lower().replace("\xa0", " ")
    if not text:
        return None
    match = _ABSOLUTE_RE.search(text)
    if match:
        month = _MONTHS.get(match.group(2))
        if month is None:
            return None
        msk_now = now.astimezone(_MOSCOW_TZ)
        try:
            local = datetime(
                msk_now.year,
                month,
                int(match.group(1)),
                int(match.group(3)),
                int(match.group(4)),
                tzinfo=_MOSCOW_TZ,
            )
        except ValueError:
            return None
        if local - msk_now > timedelta(days=1):
            local = local.replace(year=local.year - 1)
        return local.astimezone(timezone.utc)
    age = _parse_age_snippet(text)
    if age is None:
        return None
    return now - timedelta(minutes=age)


def _age_minutes(published: Optional[datetime], now: Optional[datetime] = None) -> Optional[int]:
    if published is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0, int((current - published).total_seconds() // 60))


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


def parse_flru_full_description(html_text: str) -> Optional[str]:
    """Extract the full description from an fl.ru order detail page.

    Strategy:
    1. Prefer schema.org JSON-LD <script type="application/ld+json"> with
       @type=Product.description — most stable, doesn't depend on CSS classes.
    2. Fallback to div.fl-project-content__description-text.
    3. Returns None if neither yields anything; caller keeps the listing
       preview.
    """
    tree = HTMLParser(html_text)

    for script in tree.css('script[type="application/ld+json"]'):
        raw = script.text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        for entry in _iter_jsonld(data):
            if (
                isinstance(entry, dict)
                and entry.get("@type") == "Product"
                and isinstance(entry.get("description"), str)
            ):
                desc = entry["description"].strip()
                if len(desc) >= 50:
                    return _normalize_paragraphs(desc)

    css_node = tree.css_first("div.fl-project-content__description-text")
    if css_node is not None:
        text = css_node.text(strip=True)
        if len(text) >= 50:
            return _normalize_paragraphs(text)

    return None


def _iter_jsonld(node):
    """JSON-LD may be a single object, a list, or wrapped in @graph."""
    if isinstance(node, list):
        for item in node:
            yield from _iter_jsonld(item)
    elif isinstance(node, dict):
        yield node
        for child in node.get("@graph", []) or []:
            yield from _iter_jsonld(child)


def _normalize_paragraphs(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
