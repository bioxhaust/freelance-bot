"""Public GitHub context for scoring and reply drafts.

Built from the public repositories of https://github.com/bioxhaust.
The model may cite only these projects.
"""

from __future__ import annotations

GITHUB_PORTFOLIO = """
Публичный GitHub: https://github.com/bioxhaust
В отклике можно сослаться на один репозиторий из списка, если он прямо относится к заказу. Ссылку давай целиком. Не упоминай проекты, цифры, клиентов и опыт, которых нет ниже и нет в моих навыках.

- https://github.com/bioxhaust/fabulaone-portfolio — магазин цифрового аудио: каталог, оплата, личный кабинет, индивидуальные заказы, партнёрская программа. Стек: Next.js 15, React 19, Payload CMS 3, PostgreSQL, Redis, ЮKassa, S3-совместимое хранилище, Docker.
- https://github.com/bioxhaust/vpnbot-portfolio — Telegram-бот и веб-кабинет продажи подписок: тарифы, оплата ЮKassa / Crypto Pay / Telegram Stars, автовыдача через Marzban, рефералка, промокоды, админка. Стек: Python, python-telegram-bot, FastAPI, SQLite.
- https://github.com/bioxhaust/marzban-telegram-bot — бот подписок перед Marzban: инвайты, Crypto Pay, Telegram Stars, реферальный баланс.
- https://github.com/bioxhaust/polybot — бумажный бот по крипто-рынкам Polymarket: стаканы, вероятность по опционам Deribit, спот Binance, отчёт по Brier score. Реальных ордеров не отправляет. Python.
- https://github.com/bioxhaust/settled-edge — сканер фандинга и базиса между площадками на Rust: комиссия, проскальзывание, устаревшая котировка, маржа.
- https://github.com/bioxhaust/freelance-bot — ассистент фрилансера: ленты Kwork и FL.ru, скоринг заказа LLM, черновик отклика в Telegram.
""".strip()


def model_skills(user_skills: str) -> str:
    """Skills from .env plus the public GitHub portfolio."""
    base = (user_skills or "").strip()
    if not base:
        return GITHUB_PORTFOLIO
    return f"{base}\n\n{GITHUB_PORTFOLIO}"
