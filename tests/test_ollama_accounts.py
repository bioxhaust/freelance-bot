from __future__ import annotations

import json

import aiohttp
import pytest

from llm import (
    LLMClient,
    read_account_balance,
    response_means_out_of_credits,
)


def test_balance_sums_included_and_purchased() -> None:
    balance = read_account_balance(
        {
            "included": {"balance_usd": 2.5, "allowance_usd": 2.5},
            "purchased": {"balance_usd": 1},
        }
    )
    assert balance.usd == 3.5
    assert balance.empty is False


def test_zero_balance_is_empty() -> None:
    balance = read_account_balance(
        {"included": {"balance_usd": 0}, "purchased": {"balance_usd": 0}}
    )
    assert balance.empty is True


def test_legacy_weekly_limit_still_has_room() -> None:
    balance = read_account_balance(
        {
            "included": {
                "session": {"remaining_percent": 0},
                "weekly": {"remaining_percent": 40},
            },
            "purchased": {"balance_usd": 0},
        }
    )
    assert balance.empty is False


def test_payment_required_means_out_of_credits() -> None:
    assert response_means_out_of_credits(402, "") is True
    assert response_means_out_of_credits(429, "rate limit") is False
    assert response_means_out_of_credits(403, "insufficient credits") is True
    assert response_means_out_of_credits(400, "bad model") is False


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._payload = payload

    async def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)

    async def json(self, content_type: object = None) -> object:
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                None,  # type: ignore[arg-type]
                (),
                status=self.status,
                message="failed",
            )

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Http:
    def __init__(self, script: list[tuple[int, object]]) -> None:
        self.script = list(script)
        self.authorizations: list[str] = []

    async def request_with_retry(self, method: str, url: str, **kwargs: object) -> _Response:
        headers = kwargs.get("headers")
        assert isinstance(headers, dict)
        self.authorizations.append(str(headers["Authorization"]))
        status, payload = self.script.pop(0)
        return _Response(status, payload)


@pytest.mark.asyncio
async def test_chat_switches_to_the_spare_account() -> None:
    http = _Http(
        [
            (402, {"error": "insufficient credits"}),
            (200, {"message": {"content": "готов"}}),
        ]
    )
    client = LLMClient(
        "primary",
        "gpt-oss:120b",
        http=http,  # type: ignore[arg-type]
        api_keys=["primary", "spare"],
    )
    assert await client._chat("привет") == "готов"
    assert client.api_key == "spare"
    assert http.authorizations == ["Bearer primary", "Bearer spare"]

    http.script.append((200, {"message": {"content": "ещё"}}))
    assert await client._chat("ещё") == "ещё"
    assert http.authorizations[-1] == "Bearer spare"


@pytest.mark.asyncio
async def test_startup_moves_to_spare_when_primary_balance_is_empty() -> None:
    http = _Http(
        [
            (
                200,
                {"included": {"balance_usd": 0}, "purchased": {"balance_usd": 0}},
            )
        ]
    )
    client = LLMClient(
        "primary",
        "m",
        http=http,  # type: ignore[arg-type]
        api_keys=["primary", "spare"],
    )
    await client.prefer_funded_account()
    assert client.api_key == "spare"
