from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class HttpClient:
    """Shared aiohttp ClientSession with keep-alive, retries, and Retry-After.

    Replaces the per-call `async with aiohttp.ClientSession()` pattern that
    was creating a new TLS handshake on every request.
    """

    def __init__(
        self,
        *,
        connector_limit: int = 20,
        connector_limit_per_host: int = 8,
        total_timeout: float = 60.0,
        sock_connect: float = 10.0,
        sock_read: float = 45.0,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> None:
        self._timeout = aiohttp.ClientTimeout(
            total=total_timeout,
            sock_connect=sock_connect,
            sock_read=sock_read,
        )
        self._connector_limit = connector_limit
        self._connector_limit_per_host = connector_limit_per_host
        self._session: Optional[aiohttp.ClientSession] = None
        self._user_agent = user_agent
        self._lock = asyncio.Lock()

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    connector = aiohttp.TCPConnector(
                        limit=self._connector_limit,
                        limit_per_host=self._connector_limit_per_host,
                        ttl_dns_cache=300,
                    )
                    self._session = aiohttp.ClientSession(
                        connector=connector,
                        timeout=self._timeout,
                        headers={"User-Agent": self._user_agent},
                    )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def request_with_retry(
        self,
        method: str,
        url: str,
        *,
        max_attempts: int = 4,
        backoff_base: float = 0.6,
        retry_statuses: tuple[int, ...] = (408, 425, 429, 500, 502, 503, 504),
        **kwargs: Any,
    ) -> aiohttp.ClientResponse:
        """Issue an HTTP request, retrying on transient failures.

        On a retryable status the response is released (not consumed) before
        the next attempt; the caller receives the final response and must
        either `async with` it or call `release()`.
        """
        session = await self.session()
        last_exc: BaseException | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await session.request(method, url, **kwargs)
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                last_exc = exc
                delay = backoff_base * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
                logger.warning(
                    "HTTP %s %s network error %r, retry %d/%d in %.2fs",
                    method, url, exc, attempt, max_attempts, delay,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(delay)
                continue

            if resp.status in retry_statuses and attempt < max_attempts:
                delay = self._delay_from_response(resp, attempt, backoff_base)
                logger.warning(
                    "HTTP %s %s -> %s, retry %d/%d in %.2fs",
                    method, url, resp.status, attempt, max_attempts, delay,
                )
                resp.release()
                await asyncio.sleep(delay)
                continue
            return resp

        if last_exc is not None:
            raise last_exc
        raise aiohttp.ClientError(f"HTTP {method} {url} failed after {max_attempts} attempts")

    @staticmethod
    def _delay_from_response(
        resp: aiohttp.ClientResponse, attempt: int, base: float
    ) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        return base * (2 ** (attempt - 1)) + random.uniform(0, 0.3)
