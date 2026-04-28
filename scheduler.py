from __future__ import annotations

import asyncio
import logging
from typing import Optional

from config import Settings
from db import Database
from exchanges import Exchange, Order
from llm import LLMClient
from notifier import TelegramNotifier

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
    ) -> None:
        self.settings = settings
        self.db = db
        self.exchanges = exchanges
        self.llm = llm
        self.notifier = notifier

    async def run_forever(self) -> None:
        cycle = 0
        while True:
            cycle += 1
            logger.info("Cycle %s started", cycle)
            await self.run_once()
            logger.info(
                "Cycle %s finished, sleep %ss",
                cycle,
                self.settings.poll_interval_seconds,
            )
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_once(self) -> None:
        # Fetch all sources concurrently. Slow sources can no longer block
        # fast ones; one source failing does not stall the cycle.
        results = await asyncio.gather(
            *(self._fetch_one(ex) for ex in self.exchanges),
            return_exceptions=True,
        )
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
            await self._process_orders(exchange, result)

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

    async def _process_orders(
        self, exchange: Exchange, orders: list[Order]
    ) -> None:
        fetched_count = len(orders)
        new_count = 0
        duplicates_count = 0
        llm_calls = 0
        llm_errors = 0
        prefilter_skipped = 0
        stale_skipped = 0
        passed_threshold = 0
        draft_errors = 0
        notify_errors = 0
        sent_count = 0
        relevance_sum = 0

        for idx, order in enumerate(orders, start=1):
            if idx % 50 == 0:
                logger.info(
                    "%s progress: %d/%d processed (new=%d sent=%d)",
                    exchange.source_name, idx, len(orders), new_count, sent_count,
                )

            inserted = await self.db.save_order_if_new(order, relevance=None)
            if not inserted:
                duplicates_count += 1
                continue
            new_count += 1

            max_age = self.settings.fresh_order_max_age_minutes
            age_minutes = exchange.order_age_minutes(order)
            if max_age > 0 and age_minutes is not None and age_minutes > max_age:
                stale_skipped += 1
                continue

            if not self._passes_prefilter(order.title, order.description):
                prefilter_skipped += 1
                continue

            relevance = 0
            try:
                llm_calls += 1
                relevance = await self.llm.score_relevance(
                    skills=self.settings.my_skills,
                    title=order.title,
                    description=order.description,
                )
            except Exception:
                llm_errors += 1
                logger.exception("LLM relevance scoring failed for %s", order.url)
            relevance_sum += relevance

            await self.db.update_relevance(order.url, relevance)

            if relevance >= self.settings.relevance_threshold:
                passed_threshold += 1
                draft_reply = ""
                if self.settings.draft_mode:
                    try:
                        draft_reply = await self.llm.generate_reply_draft(
                            skills=self.settings.my_skills,
                            title=order.title,
                            description=order.description,
                            max_chars=self.settings.draft_max_chars,
                        )
                    except Exception:
                        draft_errors += 1
                        logger.exception("Draft generation failed for %s", order.url)
                try:
                    await self.notifier.send_order(
                        order, relevance, draft_reply=draft_reply
                    )
                    sent_count += 1
                except Exception:
                    notify_errors += 1
                    logger.exception(
                        "Failed to send Telegram notification for %s", order.url
                    )

        avg_relevance = (relevance_sum / new_count) if new_count else 0.0
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

    def _passes_prefilter(self, title: str, description: str) -> bool:
        text = f"{title} {description}".lower()

        if self.settings.prefilter_exclude_keywords:
            if any(word in text for word in self.settings.prefilter_exclude_keywords):
                return False

        if not self.settings.prefilter_include_keywords:
            return True

        return any(word in text for word in self.settings.prefilter_include_keywords)
