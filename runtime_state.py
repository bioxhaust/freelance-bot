from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class RuntimeState:
    """In-memory mutable state — what the user can change at runtime via
    Telegram commands without restart. Source-of-truth on startup is the
    Settings dataclass (loaded from .env).
    """

    paused: bool = False
    relevance_threshold: int = 70
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def from_settings(cls, threshold: int) -> "RuntimeState":
        return cls(paused=False, relevance_threshold=int(threshold))

    async def set_paused(self, value: bool) -> None:
        async with self._lock:
            self.paused = bool(value)
            logger.info("Runtime: paused=%s", self.paused)

    async def set_threshold(self, value: int) -> int:
        async with self._lock:
            self.relevance_threshold = max(0, min(100, int(value)))
            logger.info("Runtime: relevance_threshold=%d", self.relevance_threshold)
            return self.relevance_threshold

    def snapshot(self) -> dict[str, object]:
        return {
            "paused": self.paused,
            "relevance_threshold": self.relevance_threshold,
        }
