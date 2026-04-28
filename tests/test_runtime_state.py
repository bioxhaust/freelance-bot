from __future__ import annotations

import pytest

from runtime_state import RuntimeState


@pytest.mark.asyncio
async def test_initial_values() -> None:
    rt = RuntimeState.from_settings(threshold=70)
    assert rt.paused is False
    assert rt.relevance_threshold == 70


@pytest.mark.asyncio
async def test_pause_resume() -> None:
    rt = RuntimeState.from_settings(threshold=70)
    await rt.set_paused(True)
    assert rt.paused is True
    await rt.set_paused(False)
    assert rt.paused is False


@pytest.mark.asyncio
@pytest.mark.parametrize("inp,out", [(150, 100), (-10, 0), (50, 50), (100, 100), (0, 0)])
async def test_threshold_clamping(inp: int, out: int) -> None:
    rt = RuntimeState.from_settings(threshold=70)
    assert await rt.set_threshold(inp) == out
    assert rt.relevance_threshold == out


@pytest.mark.asyncio
async def test_snapshot() -> None:
    rt = RuntimeState.from_settings(threshold=80)
    await rt.set_paused(True)
    snap = rt.snapshot()
    assert snap == {"paused": True, "relevance_threshold": 80}
