"""Hard reply limits of the exchanges.

Kwork rejects a bid longer than 2000 characters. FL.ru rejects one longer
than 5000. The bot aims at that ceiling and a checker refuses anything over it.
"""

from __future__ import annotations

PLATFORM_DRAFT_LIMITS: dict[str, int] = {
    "Kwork": 2000,
    "FL.ru": 5000,
}


def draft_char_limit(source: str, *, short: bool = False) -> int:
    limit = PLATFORM_DRAFT_LIMITS.get(source, 2000)
    if short:
        return min(700, limit)
    return limit
