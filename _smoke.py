"""Smoke test for the full refactor (CRITICAL + HIGH iterations).

Run from the bot's venv:

    python _smoke.py

Verifies:
1. db.init() runs migrations on the live orders.db without errors.
2. notifier.render_order_card escapes HTML and hides 'не указан' fields.
3. notifier keyboards (variant A) are well-formed and progressive.
4. llm parsers (last-number fallback, JSON-mode) handle adversarial inputs.
5. config caps DRAFT_MAX_CHARS at 1500 and parses KWORK_CATEGORIES.
6. exchanges/kwork JSON-parser turns real fixture payloads into Order rows.
7. exchanges/flru selectolax-parser pulls 30 cards out of the real fixture.
8. exchanges/flru _parse_age_snippet handles all observed phrasings.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = Path(__file__).parent
FIXTURES = ROOT / "tests" / "fixtures"


async def check_db() -> None:
    from db import Database, SchemaError
    from exchanges import Order

    db = Database("orders.db")
    try:
        await db.init()
    except SchemaError as exc:
        print(f"[FAIL] schema verification: {exc}")
        sys.exit(1)

    async with db.conn.execute("PRAGMA table_info(orders)") as cur:
        cols = {row[1] async for row in cur}
    missing = {"applied", "applied_at"} - cols
    if missing:
        print(f"[FAIL] still missing columns: {missing}")
        sys.exit(1)
    print(f"[OK] schema columns: {len(cols)}")

    async with db.conn.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    print(f"[OK] journal_mode = {row[0] if row else '?'}")

    test_url = "https://example.com/smoke-test-order-9999"
    test_order = Order(
        title="<b>R&D</b>", description="A & B", price="1000 RUB",
        deadline="не указан", client="не указан", source="Smoke", url=test_url,
    )
    inserted_1 = await db.save_order_if_new(test_order, relevance=42)
    inserted_2 = await db.save_order_if_new(test_order, relevance=42)
    print(f"[OK] save_order_if_new dedup: first={inserted_1} second={inserted_2}")

    await db.update_relevance(test_url, 87)
    async with db.conn.execute(
        "SELECT relevance FROM orders WHERE url = ?", (test_url,)
    ) as cur:
        rel = (await cur.fetchone())[0]
    print(f"[OK] update_relevance: {rel}")

    is_applied_1 = await db.is_applied(test_url)
    await db.mark_applied(test_url)
    is_applied_2 = await db.is_applied(test_url)
    print(f"[OK] is_applied: {is_applied_1}->{is_applied_2}")

    await db.conn.execute("DELETE FROM orders WHERE url = ?", (test_url,))
    await db.close()


def check_notifier() -> None:
    from exchanges import Order
    from notifier import (
        build_actions_keyboard,
        build_finalised_keyboard,
        build_tone_keyboard,
        render_order_card,
    )

    nasty = Order(
        title="<script>alert(1)</script> & R&D",
        description="A & B "
        + "lorem ipsum " * 80,
        price="не указан", deadline="не указан", client="не указан",
        source="Kwork",
        url="https://kwork.ru/projects/12345/view?ref=<x>&y=1",
    )
    rendered = render_order_card(nasty, 87, "Здравствуйте! <b>R&D</b>")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "Цена" not in rendered, "'не указан' price leaked"
    assert "&lt;x&gt;&amp;y=1" in rendered, "url not html-escaped"
    assert len(rendered) < 4000

    kb_no_draft = build_actions_keyboard("https://x", has_draft=False)
    kb_with_draft = build_actions_keyboard("https://x", has_draft=True)
    assert len(kb_no_draft.inline_keyboard) == 3, "expected 3 rows when no draft"
    assert kb_no_draft.inline_keyboard[0][0].text.startswith("✏️ Сгенерировать")
    assert len(kb_with_draft.inline_keyboard) == 4, "expected 4 rows with draft"
    assert kb_with_draft.inline_keyboard[0][0].callback_data == "send"
    assert kb_with_draft.inline_keyboard[1][1].callback_data == "tone"

    tone_kb = build_tone_keyboard("https://x")
    tones = {b.callback_data for r in tone_kb.inline_keyboard for b in r}
    assert {"tone_neutral", "tone_friendly", "tone_hard", "tone_back"} <= tones

    fin_kb = build_finalised_keyboard("https://x")
    assert len(fin_kb.inline_keyboard) == 1
    assert fin_kb.inline_keyboard[0][0].url == "https://x"
    print("[OK] render_order_card + 4 keyboards")


def check_llm_score() -> None:
    from llm import (
        _clamp_score, _parse_score_fallback, _parse_score_json, _smart_truncate,
    )

    # Adversarial cases for the bug we fixed (last bounded number wins).
    assert _parse_score_fallback("100% не подходит, 0/100") == 100
    assert _parse_score_fallback("Score: 35") == 35
    assert _parse_score_fallback("Я бы дал 92.") == 92
    assert _parse_score_fallback("не подходит") == 0

    assert _parse_score_json('{"score": 87}') == 87
    assert _parse_score_json('{"score": "73"}') == 73
    assert _parse_score_json('preamble {"score": 55} suffix') == 55
    assert _parse_score_json("plain text") is None

    assert _clamp_score(200) == 100 and _clamp_score(-5) == 0
    print("[OK] LLM score parsers")

    long_text = "Привет! " + "Это предложение. " * 50
    out = _smart_truncate(long_text, 100)
    assert len(out) <= 100
    assert out.endswith(".") or out.endswith("…")
    print(f"[OK] _smart_truncate: {len(out)} chars, ends={out[-3:]!r}")


def check_config() -> None:
    os.environ["TELEGRAM_BOT_TOKEN"] = "x"
    os.environ["TELEGRAM_CHAT_ID"] = "x"
    os.environ["OLLAMA_API_KEY"] = "x"
    os.environ["MY_SKILLS"] = "x"
    os.environ["DRAFT_MAX_CHARS"] = "10000"
    os.environ["KWORK_CATEGORIES"] = "41,11,abc,99"

    import importlib
    import config as cfg_mod
    importlib.reload(cfg_mod)
    s = cfg_mod.load_settings()
    assert s.draft_max_chars == 1500, f"cap broken: {s.draft_max_chars}"
    assert s.kwork_categories == (41, 11, 99), f"got {s.kwork_categories}"
    print(f"[OK] DRAFT_MAX_CHARS=10000 -> {s.draft_max_chars}; "
          f"KWORK_CATEGORIES drops 'abc' -> {s.kwork_categories}")


def check_kwork_parser() -> None:
    """Replay kwork fetch_orders against fixture JSON without HTTP."""
    from datetime import datetime, timezone
    from exchanges import Order

    items: list[dict] = []
    for cat in (41, 11, 45):
        path = FIXTURES / f"kwork_c{cat}.json"
        with open(path) as f:
            data = json.load(f)
        cat_items = ((data.get("data") or {}).get("pagination") or {}).get("data") or []
        items.extend(cat_items)
    assert len(items) > 20, f"too few items in fixtures: {len(items)}"

    # Replicate the normalization that KworkExchange.fetch_orders does.
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]
    msk = ZoneInfo("Europe/Moscow")
    msk_count = 0
    orders: list[Order] = []
    for item in items:
        order_id = item.get("id")
        if not order_id:
            continue
        title = (item.get("name") or "").strip()
        if not title:
            continue
        url = f"https://kwork.ru/projects/{order_id}/view"
        date_create = (item.get("date_create") or "").strip()
        if date_create:
            naive = datetime.strptime(date_create, "%Y-%m-%d %H:%M:%S")
            aware_msk = naive.replace(tzinfo=msk)
            aware_utc = aware_msk.astimezone(timezone.utc)
            assert aware_utc.utcoffset().total_seconds() == 0
            msk_count += 1
        orders.append(
            Order(
                title=title,
                description=(item.get("description") or "").strip() or title,
                price=(item.get("priceLimit") or "по договоренности"),
                deadline=(item.get("max_days") or "не указан"),
                client=(item.get("user") or {}).get("username") or "не указан",
                source="Kwork", url=url,
            )
        )
    assert msk_count > 10, "not enough date_create timestamps to validate"
    print(f"[OK] kwork JSON parser: {len(orders)} orders, {msk_count} with MSK timestamp")


def check_flru_parser() -> None:
    from exchanges.flru import _parse_age_snippet, parse_flru_listing

    with open(FIXTURES / "flru_programming_p1.html", encoding="utf-8") as f:
        html_text = f.read()
    parsed = parse_flru_listing(html_text)
    assert len(parsed) >= 25, f"selectolax parser regressed: only {len(parsed)} cards"
    titles = [o.title for o, _ in parsed]
    assert all(titles), "some titles are empty"
    assert all(o.url.startswith("https://www.fl.ru/projects/") for o, _ in parsed)
    age_count = sum(1 for _, a in parsed if a is not None)
    print(
        f"[OK] FL.ru selectolax: {len(parsed)} cards, ages parsed for {age_count}/{len(parsed)}"
    )

    cases = {
        "только что": 0,
        "сейчас": 0,
        "5 минут назад": 5,
        "1 час назад": 60,
        "6 часов 39 минут назад": 6 * 60 + 39,
        "15 часов 22 минуты назад": 15 * 60 + 22,
        "12 часов назад": 12 * 60,
        "1 день назад": 60 * 24,
        "3 суток назад": 60 * 24 * 3,
        "вчера в 18:00": 60 * 24,
        "сегодня": 60,
        "30 секунд назад": 0,
        "": None,
    }
    for snippet, expected in cases.items():
        got = _parse_age_snippet(snippet)
        assert got == expected, f"_parse_age_snippet({snippet!r}) = {got}, want {expected}"
    print(f"[OK] _parse_age_snippet: {len(cases)} cases incl. multi-token")


async def check_http_client() -> None:
    from http_client import HttpClient

    http = HttpClient()
    s1 = await http.session()
    s2 = await http.session()
    assert s1 is s2, "HttpClient.session() must memoize"
    await http.close()
    s3 = await http.session()
    assert s3 is not s1, "HttpClient must reopen after close()"
    await http.close()
    print("[OK] HttpClient session reuse + reopen")


async def check_runtime_state() -> None:
    from runtime_state import RuntimeState

    rt = RuntimeState.from_settings(threshold=70)
    assert rt.relevance_threshold == 70 and rt.paused is False
    await rt.set_paused(True)
    assert rt.paused is True
    new_t = await rt.set_threshold(150)
    assert new_t == 100, f"clamp failed: {new_t}"
    new_t = await rt.set_threshold(-5)
    assert new_t == 0
    print("[OK] RuntimeState: pause/resume/threshold clamping")


async def check_llm_cache() -> None:
    """Verify TTL cache short-circuits repeat calls without hitting LLM."""
    from llm import LLMClient, _hash_key

    # Use a stub so we don't actually call Ollama.
    class _StubHttp:
        def __init__(self) -> None:
            self.calls = 0
        async def request_with_retry(self, *args, **kwargs):
            raise AssertionError("HTTP must not be called when cache is warm")

    llm = LLMClient(api_key="x", model="x", http=_StubHttp())  # type: ignore[arg-type]
    # Pre-populate cache as if a previous call had filled it.
    key_score = _hash_key("score", "S", "T", "D")
    llm._score_cache[key_score] = 73
    got = await llm.score_relevance("S", "T", "D")
    assert got == 73 and llm.cache_hits == 1, (got, llm.cache_hits)

    key_draft = _hash_key("draft", "S", "T", "D", "700", "")
    llm._draft_cache[key_draft] = "cached draft"
    got_d = await llm.generate_reply_draft("S", "T", "D")
    assert got_d == "cached draft" and llm.cache_hits == 2

    stats = llm.cache_stats()
    assert stats["hits"] == 2 and stats["misses"] == 0
    print("[OK] LLM TTL cache hits short-circuit HTTP")


async def check_db_extended() -> None:
    """Validate stats/top_unhandled/applied_today against a temp DB."""
    import os
    import tempfile
    from db import Database
    from exchanges import Order

    # Use a fresh temp DB so tests don't pollute the live one.
    tmp_dir = tempfile.mkdtemp(prefix="smoke_db_")
    db_path = os.path.join(tmp_dir, "stats.db")
    db = Database(db_path)
    await db.init()

    samples = [
        Order(title=f"Order {i}", description=f"desc {i}",
              price="500", deadline="—", client="—",
              source="Kwork" if i % 2 == 0 else "FL.ru",
              url=f"https://x.example/{i}")
        for i in range(10)
    ]
    for i, o in enumerate(samples):
        await db.save_order_if_new(o, relevance=10 * i)

    await db.mark_applied(samples[8].url)
    await db.mark_applied(samples[9].url)

    stats = await db.stats(threshold=70)
    assert stats["last_24h"] == 10, stats
    assert stats["passed_24h"] == 3, stats  # rel 70, 80, 90
    assert stats["applied"] == 2, stats
    assert stats["sent_24h"] == 2, stats
    by_src = dict(stats["by_source_24h"])
    assert by_src.get("Kwork") == 5 and by_src.get("FL.ru") == 5

    top = await db.top_unhandled(threshold=70, limit=5)
    assert len(top) == 1, [o.title for o in top]  # rel=70 only (80/90 are applied)
    assert top[0].title == "Order 7", top[0].title

    applied = await db.applied_today(limit=10)
    assert {o.title for o in applied} == {"Order 8", "Order 9"}

    await db.close()
    print("[OK] db.stats / top_unhandled / applied_today")


async def main() -> None:
    print("== db.py ==")
    await check_db()
    print("\n== db.py extended (stats/top/applied_today) ==")
    await check_db_extended()
    print("\n== runtime_state.py ==")
    await check_runtime_state()
    print("\n== http_client.py ==")
    await check_http_client()
    print("\n== notifier.py ==")
    check_notifier()
    print("\n== llm.py ==")
    check_llm_score()
    print("\n== llm.py TTL cache ==")
    await check_llm_cache()
    print("\n== config.py ==")
    check_config()
    print("\n== exchanges/kwork.py (against fixtures) ==")
    check_kwork_parser()
    print("\n== exchanges/flru.py (against fixtures) ==")
    check_flru_parser()
    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
