"""One-shot script: refetch full descriptions for every FL.ru order in the
DB whose stored description is the listing-page preview (ends with '...'),
then re-score them via LLM.

Run with the bot stopped, from the bot dir with venv active:

    python rescore_flru.py            # dry run, just show counts
    python rescore_flru.py --apply    # actually fetch + rescore + UPDATE

Concurrency: 1 (sequential), to keep it gentle on fl.ru and predictable
in token cost. ETA prints every 10 orders.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from config import load_settings
from db import Database
from exchanges.flru import FLRuExchange
from github_profile import model_skills
from http_client import HttpClient
from llm import LLMClient
from logging_setup import setup_logging

logger = logging.getLogger("rescore_flru")


async def main(apply: bool, threshold: int, max_count: int | None) -> None:
    setup_logging("INFO")
    settings = load_settings()

    db = Database("orders.db")
    await db.init()

    async with db.conn.execute(
        """
        SELECT url, title, description, relevance
        FROM orders
        WHERE source = 'FL.ru'
          AND description LIKE '%...'
          AND applied = 0
        ORDER BY (relevance IS NULL) DESC, relevance DESC
        """
    ) as cur:
        rows = [r async for r in cur]

    total = len(rows)
    if max_count is not None:
        rows = rows[:max_count]

    print(f"Found {total} FL.ru orders with truncated description.")
    print(f"Will process: {len(rows)}")
    if not apply:
        print("(dry run — pass --apply to actually fetch + rescore)")
        await db.close()
        return

    http = HttpClient()
    flru = FLRuExchange(http=http, cookies=settings.flru_cookies, detail_concurrency=3)
    llm = LLMClient(api_key=settings.ollama_api_key, model=settings.ollama_model, http=http)

    started = time.time()
    upgraded = 0
    score_changed = 0
    fetch_failed = 0
    score_promotions: list[tuple[str, int, int, str]] = []  # url, old, new, title

    for idx, (url, title, old_desc, old_rel) in enumerate(rows, start=1):
        full = await flru.fetch_full_description(url)
        if not full or len(full) <= len(old_desc) + 50:
            fetch_failed += 1
            continue

        await db.update_description(url, full)
        upgraded += 1

        try:
            new_rel = await llm.score_relevance(
                skills=model_skills(settings.my_skills), title=title, description=full
            )
        except Exception:
            logger.exception("LLM rescore failed for %s", url)
            continue

        if new_rel != (old_rel or 0):
            score_changed += 1
            await db.update_relevance(url, new_rel)
            if new_rel >= threshold > (old_rel or 0):
                score_promotions.append((url, old_rel or 0, new_rel, title))

        if idx % 10 == 0 or idx == len(rows):
            elapsed = time.time() - started
            rate = idx / elapsed if elapsed else 0
            eta = (len(rows) - idx) / rate if rate else 0
            print(
                f"[{idx}/{len(rows)}] upgraded={upgraded} score_changed={score_changed} "
                f"fetch_failed={fetch_failed} elapsed={elapsed:.0f}s ETA={eta:.0f}s"
            )

    print()
    print(f"DONE. upgraded={upgraded}/{len(rows)}, score_changed={score_changed}, "
          f"fetch_failed={fetch_failed}")
    if score_promotions:
        print(f"\nPromoted to ≥{threshold}% (was below):")
        for url, old, new, title in score_promotions[:20]:
            print(f"  {old:>3}→{new:>3}%  {title[:60]!r}  {url}")
        if len(score_promotions) > 20:
            print(f"  ... and {len(score_promotions) - 20} more")

    cache = llm.cache_stats()
    print(f"\nLLM cache: hits={cache['hits']} misses={cache['misses']}")

    await http.close()
    await db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="actually do the work")
    p.add_argument("--threshold", type=int, default=70)
    p.add_argument("--max", type=int, default=None,
                   help="cap how many orders to process (for a small test run)")
    args = p.parse_args()
    try:
        asyncio.run(main(apply=args.apply, threshold=args.threshold, max_count=args.max))
    except KeyboardInterrupt:
        sys.exit(130)
