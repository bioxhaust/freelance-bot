from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

# override=True so .env is the source of truth even if the shell already
# exported some of these vars (which happens easily when you `source .env`
# manually for curl / debugging).
load_dotenv(override=True)

logger = logging.getLogger(__name__)

# Hard cap below Telegram's 4096-char message limit, with headroom for
# HTML tags, the rest of the card, and inline keyboard. A draft longer
# than this would either be silently truncated by Telegram or cause a
# `Bad Request: message is too long` error on long descriptions.
_DRAFT_MAX_CHARS_CAP = 1500


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    ollama_api_key: str
    # Primary key first, then spare accounts. The spare is used only after
    # the primary reports that its credits are gone.
    ollama_api_keys: tuple[str, ...]
    ollama_model: str
    my_skills: str
    prefilter_include_keywords: list[str]
    prefilter_exclude_keywords: list[str]
    relevance_threshold: int
    draft_mode: bool
    draft_max_chars: int
    poll_interval_seconds: int
    source_max_pages: int
    fresh_order_max_age_minutes: int
    kwork_cookies: str
    kwork_categories: tuple[int, ...]
    flru_cookies: str
    cleanup_max_age_days: int
    cleanup_every_cycles: int
    # Unused for the send window. Kept so existing .env files still load.
    backlog_max_age_minutes: int
    llm_concurrency: int
    # SQLite path. Docker sets /data/orders.db so the WAL sits on the volume.
    db_path: str


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required env var: {name}")
    return value


def _csv_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default).strip()
    if not raw:
        return []
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _int_tuple(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    parsed: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            parsed.append(int(chunk))
        except ValueError:
            logger.warning("Ignoring non-integer item in %s: %r", name, chunk)
    return tuple(parsed) if parsed else default


def _bounded_int(name: str, default: int, *, lo: int, hi: int) -> int:
    raw = os.getenv(name, str(default)).strip() or str(default)
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid int for %s=%r, using default %d", name, raw, default)
        value = default
    if value < lo:
        logger.warning("%s=%d is below minimum %d, clamping", name, value, lo)
        return lo
    if value > hi:
        logger.warning("%s=%d exceeds cap %d, clamping", name, value, hi)
        return hi
    return value


def _ollama_api_keys() -> tuple[str, ...]:
    keys: list[str] = []
    for name in ("OLLAMA_API_KEY", "OLLAMA_API_KEY_2", "OLLAMA_API_KEY_3"):
        value = os.getenv(name, "").strip()
        if value and value not in keys:
            keys.append(value)
    if not keys:
        raise ValueError("Missing required env var: OLLAMA_API_KEY")
    return tuple(keys)


def load_settings() -> Settings:
    ollama_keys = _ollama_api_keys()
    return Settings(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_require("TELEGRAM_CHAT_ID"),
        ollama_api_key=ollama_keys[0],
        ollama_api_keys=ollama_keys,
        ollama_model=os.getenv("OLLAMA_MODEL", "beam"),
        my_skills=_require("MY_SKILLS"),
        prefilter_include_keywords=_csv_list(
            "PREFILTER_INCLUDE_KEYWORDS",
            (
                "python,python3,py,fastapi,django,flask,aiogram,telegram,telegram bot,бот,телеграм,телеграм-бот,"
                "javascript,typescript,js,ts,node,node.js,nestjs,express,next,next.js,react,frontend,backend,fullstack,"
                "api,rest,graphql,webhook,парсер,parser,scraping,scraper,automation,автоматизация,"
                "sql,postgres,postgresql,mysql,sqlite,redis,database,бд,docker,kubernetes,k8s,linux,devops,"
                "auth,jwt,oauth,microservice,микросервис,интеграция,integration,crm,saas"
            ),
        ),
        prefilter_exclude_keywords=_csv_list(
            "PREFILTER_EXCLUDE_KEYWORDS",
            "логотип,дизайн баннера,иллюстрация,копирайтинг,озвучка,seo-текст",
        ),
        relevance_threshold=_bounded_int("RELEVANCE_THRESHOLD", 70, lo=0, hi=100),
        draft_mode=os.getenv("DRAFT_MODE", "1").strip().lower() in ("1", "true", "yes"),
        draft_max_chars=_bounded_int(
            "DRAFT_MAX_CHARS", 700, lo=200, hi=_DRAFT_MAX_CHARS_CAP
        ),
        poll_interval_seconds=_bounded_int(
            "POLL_INTERVAL_SECONDS", 60, lo=10, hi=3600
        ),
        source_max_pages=_bounded_int("SOURCE_MAX_PAGES", 10, lo=1, hi=50),
        fresh_order_max_age_minutes=_bounded_int(
            "FRESH_ORDER_MAX_AGE_MINUTES", 30, lo=0, hi=60 * 24 * 7
        ),
        kwork_cookies=os.getenv("KWORK_COOKIES", ""),
        kwork_categories=_int_tuple(
            "KWORK_CATEGORIES", (41, 37, 38, 39, 80, 255)
        ),
        flru_cookies=os.getenv("FLRU_COOKIES", ""),
        cleanup_max_age_days=_bounded_int("CLEANUP_MAX_AGE_DAYS", 30, lo=0, hi=365),
        cleanup_every_cycles=_bounded_int("CLEANUP_EVERY_CYCLES", 60, lo=1, hi=10000),
        backlog_max_age_minutes=_bounded_int(
            "BACKLOG_MAX_AGE_MINUTES", 180, lo=0, hi=60 * 24 * 7
        ),
        llm_concurrency=_bounded_int("LLM_CONCURRENCY", 3, lo=1, hi=8),
        db_path=os.getenv("DB_PATH", "orders.db").strip() or "orders.db",
    )
