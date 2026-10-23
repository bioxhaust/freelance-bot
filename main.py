from __future__ import annotations

import asyncio

from config import load_settings
from db import Database
from exchanges import FLRuExchange, KworkExchange
from http_client import HttpClient
from llm import LLMClient
from logging_setup import setup_logging
from notifier import TelegramNotifier
from runtime_state import RuntimeState
from scheduler import Scheduler
from telegram_assistant import TelegramAssistant


async def main() -> None:
    settings = load_settings()
    runtime = RuntimeState.from_settings(settings.relevance_threshold)

    db = Database(settings.db_path)
    await db.init()

    http = HttpClient()

    exchanges = [
        KworkExchange(
            http=http,
            cookies=settings.kwork_cookies,
            max_pages=settings.source_max_pages,
            categories=settings.kwork_categories,
        ),
        FLRuExchange(
            http=http,
            cookies=settings.flru_cookies,
            max_pages=settings.source_max_pages,
        ),
    ]
    llm = LLMClient(
        api_key=settings.ollama_api_key,
        model=settings.ollama_model,
        http=http,
        api_keys=list(settings.ollama_api_keys),
    )
    await llm.prefer_funded_account()
    notifier = TelegramNotifier(
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )

    scheduler = Scheduler(
        settings=settings,
        db=db,
        exchanges=exchanges,
        llm=llm,
        notifier=notifier,
        runtime=runtime,
    )
    assistant = TelegramAssistant(
        bot=notifier.bot,
        settings=settings,
        db=db,
        llm=llm,
        notifier=notifier,
        runtime=runtime,
    )

    try:
        await asyncio.gather(
            scheduler.run_forever(),
            assistant.run_forever(),
        )
    finally:
        await notifier.close()
        await http.close()
        await db.close()


if __name__ == "__main__":
    import os

    setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
    asyncio.run(main())
