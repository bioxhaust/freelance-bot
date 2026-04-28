from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any, Optional

# Per-task context. Whatever is bound here will appear in every log line
# emitted by this asyncio task until it's reset. Useful to tag every log
# inside a single scheduler cycle with the same cycle_id, or every log
# inside one Telegram callback with the same order_url.
_cycle_id: ContextVar[Optional[int]] = ContextVar("cycle_id", default=None)
_order_url: ContextVar[Optional[str]] = ContextVar("order_url", default=None)


class ContextFilter(logging.Filter):
    """Injects context vars into every LogRecord so the formatter can show
    them. We use stdlib logging instead of structlog to keep the dep
    surface minimal — for a single-tenant bot full structlog/JSON is
    overkill, but having cycle/order tags in plain logs is the 80/20.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        cid = _cycle_id.get()
        ourl = _order_url.get()
        record.cycle = f"c{cid}" if cid is not None else "-"
        record.order = _short_url(ourl) if ourl else "-"
        return True


_DEFAULT_FORMAT = (
    "%(asctime)s %(levelname)-5s [%(name)s] [%(cycle)s|%(order)s] %(message)s"
)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    # Wipe any handlers added by previous setups (or by `logging.basicConfig`
    # called early). Replace with our own.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
    handler.addFilter(ContextFilter())
    root.addHandler(handler)


def bind_cycle(cycle_id: int) -> Any:
    return _cycle_id.set(cycle_id)


def reset_cycle(token: Any) -> None:
    _cycle_id.reset(token)


def bind_order(order_url: str) -> Any:
    return _order_url.set(order_url)


def reset_order(token: Any) -> None:
    _order_url.reset(token)


def _short_url(url: str) -> str:
    """Compact URL for logs: domain + last path segment."""
    if not url:
        return "-"
    try:
        no_scheme = url.split("://", 1)[-1]
        host, _, path = no_scheme.partition("/")
        if not path:
            return host
        last = path.rstrip("/").split("/")[-1] or path
        return f"{host}/{last}"
    except Exception:
        return url[:60]
