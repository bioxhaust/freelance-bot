from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from config import Settings
from db import Database
from draft_limits import draft_char_limit
from exchanges import Exchange, Order
from exchanges.flru import FLRuExchange
from github_profile import model_skills
from llm import LLMClient
from logging_setup import bind_cycle, bind_order, reset_cycle, reset_order
from notifier import TelegramNotifier
from runtime_state import RuntimeState

logger = logging.getLogger(__name__)


_FETCH_TIMEOUT_SECONDS = 90.0


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        exchanges: list[Exchange],
        llm: LLMClient,
        notifier: TelegramNotifier,
        runtime: RuntimeState,
    ) -> None:
        self.settings = settings
        self.db = db
        self.exchanges = exchanges
        self.llm = llm
        self.notifier = notifier
        self.runtime = runtime
        self.last_cycle_seconds: float = 0.0
        self.last_cycle_started_at: float = 0.0
        self._cycles_since_cleanup = 0
        self._llm_sem = asyncio.Semaphore(max(1, settings.llm_concurrency))

    async def run_forever(self) -> None:
        cycle = 0
        while True:
            cycle += 1
            if self.runtime.paused:
                logger.info("Cycle %s skipped (paused)", cycle)
            else:
                token = bind_cycle(cycle)
                try:
                    logger.info(
                        "Cycle started (threshold=%d)", self.runtime.relevance_threshold
                    )
                    started = asyncio.get_event_loop().time()
                    self.last_cycle_started_at = started
                    await self.run_once()
                    self.last_cycle_seconds = (
                        asyncio.get_event_loop().time() - started
                    )
                finally:
                    reset_cycle(token)
                logger.info(
                    "Cycle finished in %.1fs, sleep %ss",
                    self.last_cycle_seconds,
                    self.settings.poll_interval_seconds,
                )
                self._cycles_since_cleanup += 1
                if self._cycles_since_cleanup >= self.settings.cleanup_every_cycles:
                    self._cycles_since_cleanup = 0
                    try:
                        await self.db.cleanup_old(
                            max_age_days=self.settings.cleanup_max_age_days
                        )
                    except Exception:
                        logger.exception("cleanup_old failed")
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_once(self) -> None:
        # Fetch all sources concurrently. Slow sources can no longer block
        # fast ones; one source failing does not stall the cycle.
        results = await asyncio.gather(
            *(self._fetch_one(ex) for ex in self.exchanges),
            return_exceptions=True,
        )
        not_before = await self._notify_not_before()
        # Process orders sequentially across sources to avoid hammering the
        # LLM with parallel calls (LLM rate-limit > exchange rate-limit).
        for exchange, result in zip(self.exchanges, results):
            if isinstance(result, BaseException):
                logger.error(
                    "Failed to fetch orders from %s: %r",
                    exchange.source_name, result,
                )
                continue
            logger.info(
                "Processing %s: %d orders fetched", exchange.source_name, len(result)
            )
            await self._process_orders(exchange, result, not_before)

    async def _fetch_one(self, exchange: Exchange) -> list[Order]:
        try:
            return await asyncio.wait_for(
                exchange.fetch_orders(), timeout=_FETCH_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(
                "%s fetch_orders exceeded %ss timeout",
                exchange.source_name, _FETCH_TIMEOUT_SECONDS,
            )
            return []

    def _scoring_max_age(self) -> int:
        """Minutes after which a never-seen order is recorded and not scored.

        0 disables the age cap. Orders older than the cutoff stored in the
        database are never scored, even inside this window.
        """
        return self.settings.fresh_order_max_age_minutes

    async def _notify_not_before(self) -> datetime:
        """Publication cutoff. Set once, so a restart does not move it."""
        cached = getattr(self, "_notify_after", None)
        if isinstance(cached, datetime):
            return cached
        raw = await self.db.meta_get("notify_after")
        if raw:
            cutoff = datetime.fromisoformat(raw)
            logger.info("Notify cutoff is %s. Older orders will not be sent.", cutoff.isoformat())
        else:
            cutoff = datetime.now(timezone.utc).replace(microsecond=0)
            await self.db.meta_set("notify_after", cutoff.isoformat())
            logger.info(
                "Notify cutoff set to %s. Older orders will not be sent.",
                cutoff.isoformat(),
            )
        self._notify_after = cutoff
        return cutoff

    async def _process_orders(
        self, exchange: Exchange, orders: list[Order], not_before: datetime
    ) -> None:
        fetched_count = len(orders)
        # Few existing bids and a buyer who actually hires go to the model first.
        ranked = sorted(orders, key=lambda order: _priority(exchange, order))
        tallies = await asyncio.gather(
            *(self._handle_order(exchange, order, not_before) for order in ranked),
            return_exceptions=True,
        )
        total = _Tally()
        for item in tallies:
            if isinstance(item, BaseException):
                total.llm_errors += 1
                logger.error("Order handler failed: %r", item)
                continue
            total.add(item)

        new_count = total.new
        duplicates_count = total.duplicates
        llm_calls = total.llm_calls
        llm_errors = total.llm_errors
        prefilter_skipped = total.prefilter_skipped
        stale_skipped = total.stale_skipped
        passed_threshold = total.passed
        draft_errors = total.draft_errors
        notify_errors = total.notify_errors
        sent_count = total.sent
        scored = max(0, llm_calls - llm_errors)
        avg_relevance = (total.relevance_sum / scored) if scored else 0.0
        context = exchange.debug_context()
        source_label = (
            f"{exchange.source_name} ({context})" if context else exchange.source_name
        )
        logger.info(
            (
                "%s: fetched=%s new=%s duplicates=%s "
                "stale_skipped=%s prefilter_skipped=%s llm_calls=%s llm_errors=%s "
                "avg_relevance=%.1f passed=%s sent=%s draft_errors=%s notify_errors=%s"
            ),
            source_label,
            fetched_count,
            new_count,
            duplicates_count,
            stale_skipped,
            prefilter_skipped,
            llm_calls,
            llm_errors,
            avg_relevance,
            passed_threshold,
            sent_count,
            draft_errors,
            notify_errors,
        )
        for idx, order in enumerate(orders[:2], start=1):
            preview = (order.description or "").replace("\n", " ").strip()
            if len(preview) > 120:
                preview = preview[:117] + "..."
            logger.info(
                "%s sample #%s: title=%r price=%r url=%s desc=%r",
                source_label,
                idx,
                order.title,
                order.price,
                order.url,
                preview,
            )

    async def _handle_order(
        self, exchange: Exchange, order: Order, not_before: datetime
    ) -> _Tally:
        tally = _Tally()
        state = await self.db.pipeline_state(order.url)
        if state in ("done", "skipped"):
            tally.duplicates = 1
            return tally

        now = datetime.now(timezone.utc)
        if not is_fresh_enough(
            order.published_at,
            not_before=not_before,
            now=now,
            max_age_minutes=self._scoring_max_age(),
        ):
            if state == "missing":
                await self.db.save_order_if_new(order, relevance=None, skipped=True)
                if order.published_at is None:
                    logger.info("Skip %s: publication time is missing", order.url)
            else:
                await self.db.mark_skipped(order.url)
            tally.stale_skipped = 1
            return tally

        if not passes_prefilter(
            order.title,
            order.description,
            include=self.settings.prefilter_include_keywords,
            exclude=self.settings.prefilter_exclude_keywords,
        ):
            # Leave it out of the DB so a later keyword edit can still match
            # while the project is on the first pages.
            tally.prefilter_skipped = 1
            return tally

        if state == "missing":
            await self.db.save_order_if_new(order, relevance=None)
        tally.new = 1

        description = order.description
        # FL.ru listing only shows a short preview; pull the full text
        # before scoring so the LLM doesn't judge blind.
        if isinstance(exchange, FLRuExchange):
            full = await exchange.fetch_full_description(order.url)
            if full and len(full) > len(description) + 50:
                description = full
                order.description = full
                try:
                    await self.db.update_description(order.url, full)
                except Exception:
                    logger.exception(
                        "Failed to persist full description for %s", order.url
                    )

        skills = model_skills(self.settings.my_skills)
        relevance = 0
        order_token = bind_order(order.url)
        async with self._llm_sem:
            try:
                tally.llm_calls = 1
                relevance = await self.llm.score_relevance(
                    skills=skills,
                    title=order.title,
                    description=description,
                )
            except Exception:
                tally.llm_errors = 1
                logger.exception("LLM relevance scoring failed")
            else:
                tally.relevance_sum = relevance
                await self.db.update_relevance(order.url, relevance)
                if relevance >= self.runtime.relevance_threshold:
                    tally.passed = 1
                    draft_reply = ""
                    if self.settings.draft_mode:
                        try:
                            draft_reply = await self.llm.generate_reply_draft(
                                skills=skills,
                                title=order.title,
                                description=order.description,
                                max_chars=draft_char_limit(order.source),
                                price=order.price,
                                deadline=order.deadline,
                            )
                        except Exception:
                            tally.draft_errors = 1
                            logger.exception(
                                "Draft generation failed for %s", order.url
                            )
                    try:
                        await self.notifier.send_order(
                            order, relevance, draft_reply=draft_reply
                        )
                        tally.sent = 1
                    except Exception:
                        tally.notify_errors = 1
                        logger.exception(
                            "Failed to send Telegram notification for %s", order.url
                        )
            finally:
                reset_order(order_token)
        return tally


@dataclass
class _Tally:
    new: int = 0
    duplicates: int = 0
    llm_calls: int = 0
    llm_errors: int = 0
    prefilter_skipped: int = 0
    stale_skipped: int = 0
    passed: int = 0
    draft_errors: int = 0
    notify_errors: int = 0
    sent: int = 0
    relevance_sum: int = 0

    def add(self, other: _Tally) -> None:
        self.new += other.new
        self.duplicates += other.duplicates
        self.llm_calls += other.llm_calls
        self.llm_errors += other.llm_errors
        self.prefilter_skipped += other.prefilter_skipped
        self.stale_skipped += other.stale_skipped
        self.passed += other.passed
        self.draft_errors += other.draft_errors
        self.notify_errors += other.notify_errors
        self.sent += other.sent
        self.relevance_sum += other.relevance_sum


def is_fresh_enough(
    published_at: datetime | None,
    *,
    not_before: datetime,
    now: datetime,
    max_age_minutes: int,
) -> bool:
    """True only for a known publication time after the cutoff and inside the window.

    Missing timestamps are rejected. FL.ru omits "N назад" on older cards and
    prints a calendar date instead; treating that as "fresh" sent days-old orders.
    """
    if published_at is None:
        return False
    if published_at < not_before:
        return False
    if max_age_minutes > 0:
        age_minutes = (now - published_at).total_seconds() / 60
        if age_minutes > max_age_minutes:
            return False
    return True


def _priority(exchange: Exchange, order: Order) -> tuple[int, int, int]:
    offers = order.offers_count if order.offers_count is not None else 10**9
    hire = order.client_hire_percent if order.client_hire_percent is not None else -1
    age = exchange.order_age_minutes(order)
    age_key = age if age is not None else 10**9
    return (offers, -hire, age_key)


def passes_prefilter(
    title: str,
    description: str,
    *,
    include: list[str],
    exclude: list[str],
) -> bool:
    text = f"{title} {description}".lower()
    if exclude and any(keyword_matches(text, word) for word in exclude):
        return False
    if not include:
        return True
    return any(keyword_matches(text, word) for word in include)


def keyword_matches(text: str, keyword: str) -> bool:
    """Short tokens match on a word boundary so «бот» does not hit «работа».

    Longer keywords and phrases stay substrings: «python» still matches
    «python3», «telegram bot» still matches the phrase.
    """
    kw = keyword.strip().lower()
    if not kw:
        return False
    if " " in kw or len(kw) > 4:
        return kw in text
    return re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", text) is not None
