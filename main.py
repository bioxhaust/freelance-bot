from __future__ import annotations

import asyncio
import logging

from config import load_settings
from db import Database
from exchanges import FLRuExchange, KworkExchange
from http_client import HttpClient
from llm import LLMClient
from notifier import TelegramNotifier
from scheduler import Scheduler
from telegram_assistant import TelegramAssistant


async def main() -> None:
    settings = load_settings()

    db = Database("orders.db")
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
    )
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
    )
    assistant = TelegramAssistant(
        bot=notifier.bot,
        settings=settings,
        db=db,
        llm=llm,
        notifier=notifier,
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    asyncio.run(main())
