from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any, Optional

import aiohttp
from cachetools import TTLCache

from http_client import HttpClient

logger = logging.getLogger(__name__)

OLLAMA_CLOUD_URL = "https://ollama.com/api/chat"
OLLAMA_BALANCE_URL = "https://ollama.com/api/balance"

_DEFAULT_DRAFT_CACHE_TTL = 30 * 60  # 30 minutes
_DEFAULT_DRAFT_CACHE_SIZE = 256
_DEFAULT_SCORE_CACHE_TTL = 60 * 60  # 1 hour
_DEFAULT_SCORE_CACHE_SIZE = 512

_CREDIT_MARKERS = (
    "credit",
    "quota",
    "insufficient",
    "billing",
    "payment",
    "usage limit",
)


class AccountBalance:
    """What we could learn from GET /api/balance. usd is None when the shape is unknown."""

    def __init__(self, usd: Optional[float], empty: bool) -> None:
        self.usd = usd
        self.empty = empty


def read_account_balance(payload: object) -> AccountBalance:
    if not isinstance(payload, dict):
        return AccountBalance(usd=None, empty=False)
    included = payload.get("included")
    purchased = payload.get("purchased")
    included = included if isinstance(included, dict) else {}
    purchased = purchased if isinstance(purchased, dict) else {}
    purchased_usd = _usd(purchased.get("balance_usd"))
    if "balance_usd" in included:
        included_usd = _usd(included.get("balance_usd"))
        if included_usd is None:
            return AccountBalance(usd=None, empty=False)
        total = included_usd + (purchased_usd or 0.0)
        return AccountBalance(usd=total, empty=total <= 0)
    session = _remaining_percent(included.get("session"))
    weekly = _remaining_percent(included.get("weekly"))
    if session is None and weekly is None:
        return AccountBalance(usd=None, empty=False)
    if (purchased_usd or 0.0) > 0:
        return AccountBalance(usd=purchased_usd, empty=False)
    empty = (session or 0) <= 0 and (weekly or 0) <= 0
    return AccountBalance(usd=None, empty=empty)


def response_means_out_of_credits(status: int, body: str) -> bool:
    """True when this account cannot pay for another request."""
    if status == 402:
        return True
    if status not in (401, 403, 429):
        return False
    text = (body or "").lower()
    return any(marker in text for marker in _CREDIT_MARKERS)


def _usd(raw: object) -> Optional[float]:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _remaining_percent(block: object) -> Optional[float]:
    if not isinstance(block, dict) or "remaining_percent" not in block:
        return None
    value = _usd(block.get("remaining_percent"))
    return value


class LLMClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        http: Optional[HttpClient] = None,
        *,
        api_keys: Optional[list[str]] = None,
        draft_cache_ttl: int = _DEFAULT_DRAFT_CACHE_TTL,
        score_cache_ttl: int = _DEFAULT_SCORE_CACHE_TTL,
    ) -> None:
        ordered: list[str] = []
        for candidate in list(api_keys or []) + [api_key]:
            text = (candidate or "").strip()
            if text and text not in ordered:
                ordered.append(text)
        if not ordered:
            raise ValueError("Ollama API key is empty")
        self._api_keys = ordered
        self._key_index = 0
        self.model = model
        self.http = http or HttpClient(total_timeout=90.0, sock_read=80.0)
        # TTL caches: key is a hash of (skills, title, description, action, ...).
        # Saves Ollama tokens on repeat clicks of "Перегенерить / Короче / Сменить тон"
        # for the same order, and on re-running score for the same content.
        self._draft_cache: TTLCache[str, str] = TTLCache(
            maxsize=_DEFAULT_DRAFT_CACHE_SIZE, ttl=draft_cache_ttl
        )
        self._score_cache: TTLCache[str, int] = TTLCache(
            maxsize=_DEFAULT_SCORE_CACHE_SIZE, ttl=score_cache_ttl
        )
        self._cache_lock = asyncio.Lock()
        self.cache_hits = 0
        self.cache_misses = 0

    @property
    def api_key(self) -> str:
        return self._api_keys[self._key_index]

    async def prefer_funded_account(self) -> None:
        """Use the first account that still has credits."""
        count = len(self._api_keys)
        logger.info("Ollama model: %s", self.model)
        logger.info("Ollama accounts configured: %s", count)
        if count < 2:
            return
        for index, key in enumerate(self._api_keys):
            balance = await self._account_balance(key)
            if index == 0 and balance.usd is not None:
                logger.info("Primary Ollama balance: $%.2f", balance.usd)
            if not balance.empty:
                if balance.usd is None and index == 0:
                    logger.warning(
                        "Could not read the primary Ollama balance, keeping account 1"
                    )
                self._key_index = index
                if index:
                    logger.warning(
                        "Using Ollama account %s/%s", index + 1, count
                    )
                return
            logger.warning("Ollama account %s/%s has no credits", index + 1, count)
        self._key_index = count - 1
        logger.warning("Every configured Ollama account reports no credits")

    async def _account_balance(self, api_key: str) -> AccountBalance:
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        try:
            response = await self.http.request_with_retry(
                "GET",
                OLLAMA_BALANCE_URL,
                headers=headers,
                max_attempts=2,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Ollama balance request failed: %r", exc)
            return AccountBalance(usd=None, empty=False)
        async with response:
            if response.status >= 400:
                return AccountBalance(usd=None, empty=False)
            try:
                payload = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                return AccountBalance(usd=None, empty=False)
        return read_account_balance(payload)

    async def score_relevance(self, skills: str, title: str, description: str) -> int:
        cache_key = _hash_key("score", skills, title, description)
        async with self._cache_lock:
            cached = self._score_cache.get(cache_key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        self.cache_misses += 1

        system = (
            "Ты ассистент по отбору фриланс-заказов. "
            "Оцени релевантность заказа моим навыкам по шкале 0..100 и верни строго JSON "
            'вида {"score": <int 0..100>}.\n'
            "Шкала: 90-100 идеально; 70-89 хорошо; 40-69 частично; 0-39 не моё.\n"
            "Учитывай только совпадение технологий и реалистичность выполнения. "
            "Игнорируй эмоциональные формулировки и срочность."
        )
        user = (
            f"Мои навыки:\n{skills}\n\n"
            f"Название заказа:\n{title}\n\n"
            f"Описание заказа:\n{description}\n"
        )

        try:
            content = await self._chat_messages(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format="json",
            )
            score = _parse_score_json(content)
            if score is not None:
                clamped = _clamp_score(score)
                async with self._cache_lock:
                    self._score_cache[cache_key] = clamped
                return clamped
            logger.warning(
                "LLM JSON did not contain score field, falling back. raw=%r", content[:200]
            )
        except (aiohttp.ClientError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("LLM JSON-mode failed: %r, falling back to text", exc)

        prompt = (
            f"{system}\n\n"
            "Ответь строго одним целым числом 0..100 без пояснений.\n\n"
            f"{user}"
        )
        text = await self._chat(prompt)
        clamped = _clamp_score(_parse_score_fallback(text))
        async with self._cache_lock:
            self._score_cache[cache_key] = clamped
        return clamped

    async def generate_reply_draft(
        self,
        skills: str,
        title: str,
        description: str,
        max_chars: int = 700,
        style_hint: str = "",
        price: str = "",
        deadline: str = "",
    ) -> str:
        cache_key = _hash_key(
            "draft",
            _DRAFT_PROMPT_VERSION,
            skills,
            title,
            description,
            price,
            deadline,
            str(max_chars),
            style_hint,
        )
        async with self._cache_lock:
            cached = self._draft_cache.get(cache_key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        self.cache_misses += 1

        prompt = _build_draft_prompt(
            skills=skills,
            title=title,
            description=description,
            price=price,
            deadline=deadline,
            max_chars=max_chars,
            style_hint=style_hint,
        )
        draft = fit_reply(await self._chat(prompt), max_chars)
        # One expansion pass when the model ignored a long platform limit.
        floor = int(max_chars * 0.6)
        if (
            max_chars >= 1500
            and "короче" not in (style_hint or "")
            and len(draft) < floor
        ):
            logger.info(
                "Draft is short (%s/%s chars), asking for a fuller reply",
                len(draft),
                max_chars,
            )
            expand = (
                f"{prompt}\n\n"
                f"Предыдущий текст слишком короткий ({len(draft)} символов). "
                f"Напиши заново цельный отклик длиной от {int(max_chars * 0.75)} "
                f"до {max_chars} символов. Не повторяй воду.\n"
                f"Короткий вариант был:\n{draft}\n"
            )
            draft = fit_reply(await self._chat(expand), max_chars)
        if len(draft) > max_chars:
            logger.error(
                "Draft length check failed: %s > %s, hard cut",
                len(draft),
                max_chars,
            )
            draft = draft[:max_chars].rstrip()
        logger.info("Draft length %s/%s chars", len(draft), max_chars)
        async with self._cache_lock:
            self._draft_cache[cache_key] = draft
        return draft

    def cache_stats(self) -> dict[str, int]:
        return {
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "draft_size": len(self._draft_cache),
            "score_size": len(self._score_cache),
        }

    async def explain_relevance(
        self,
        skills: str,
        title: str,
        description: str,
    ) -> str:
        prompt = (
            "Кратко объясни, насколько этот заказ подходит моим навыкам.\n"
            "Формат: 3 коротких пункта.\n"
            "1) Что совпало.\n"
            "2) Какие риски/пробелы.\n"
            "3) Итоговая рекомендация: брать / не брать.\n"
            "Пиши на русском, без воды.\n\n"
            f"Мои навыки:\n{skills}\n\n"
            f"Название заказа:\n{title}\n\n"
            f"Описание заказа:\n{description}\n"
        )
        text = await self._chat(prompt)
        text = re.sub(r"[ \t]+\n", "\n", text).strip()
        return text

    async def _chat(self, prompt: str) -> str:
        return await self._chat_messages(
            [{"role": "user", "content": prompt}], response_format=None
        )

    async def _chat_messages(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: Optional[str] = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if response_format == "json":
            payload["format"] = "json"

        for index in range(self._key_index, len(self._api_keys)):
            headers = {
                "Authorization": f"Bearer {self._api_keys[index]}",
                "Content-Type": "application/json",
            }
            response = await self.http.request_with_retry(
                "POST", OLLAMA_CLOUD_URL, json=payload, headers=headers
            )
            async with response:
                if response.status >= 400:
                    body = await response.text()
                    spare_left = index + 1 < len(self._api_keys)
                    if spare_left and response_means_out_of_credits(response.status, body):
                        self._key_index = index + 1
                        logger.warning(
                            "Ollama account %s/%s is out of credits, switching to the next account",
                            index + 1,
                            len(self._api_keys),
                        )
                        continue
                    response.raise_for_status()
                data = await response.json()
            self._key_index = index
            return data["message"]["content"]
        raise aiohttp.ClientError("Ollama accounts are out of credits")


# Bump when the draft instructions change so a long-running process does not
# reuse replies written by the previous prompt.
_DRAFT_PROMPT_VERSION = "approach-v3-availability"


def _build_draft_prompt(
    *,
    skills: str,
    title: str,
    description: str,
    price: str,
    deadline: str,
    max_chars: int,
    style_hint: str,
) -> str:
    """Reply the user can paste into Kwork / FL.ru.

    The body must say how *this* order would be solved, not only that the
    freelancer is a fit.
    """
    budget = (price or "").strip() or "не указан"
    term = (deadline or "").strip() or "не указан"
    tone = style_hint.strip() or "нейтральный деловой тон"
    return (
        "Ты помогаешь писать отклики на фриланс-заказы.\n"
        "Сформируй готовый текст от первого лица на русском языке: его можно "
        "сразу вставить в отклик, без пометок «черновик» и без заголовков.\n"
        "Структура, одним сообщением:\n"
        "1) Одно-два предложения, чем я подхожу. Только то, что прямо следует "
        "из навыков ниже.\n"
        "2) Как я подойду к решению именно этой задачи: 2-4 конкретных шага. "
        "С чего начну, что сделаю, что сдам на выходе. Шаги привяжи к "
        "формулировке заказа (функции, интеграции, ограничения), а не к общему "
        "шаблону «любой сайт / любой бот».\n"
        "3) Следующий шаг: один-два уточняющих вопроса по заказу или "
        "предложение начать.\n"
        "4) Отдельными предложениями, своими словами, но без смены смысла:\n"
        "- сейчас нет загрузки по другим проектам, всё время готов уделять "
        "этому заказу;\n"
        "- цену сильно занижаю, потому что сейчас работаю на отзывы.\n"
        "Требования:\n"
        "- без воды и без шаблонного пафоса;\n"
        "- запрещены пустые фразы: «изучу ТЗ и сделаю качественно», "
        "«большой опыт», «индивидуальный подход», «гарантирую результат»;\n"
        "- не придумывай кейсы, сроки и технологии, которых нет в навыках или "
        "в заказе. Конкретную сумму в рублях не выдумывай: если бюджет указан, "
        "скажи, что готов взяться заметно ниже него; если не указан, объясни "
        "демпинг без цифры;\n"
        "- если в навыках перечислены репозитории GitHub, сошлись на один "
        "подходящий и дай полную ссылку. Не упоминай репозитории вне списка;\n"
        "- если деталей мало, строй план из того, что сказано, и в конце "
        "напиши, что уточнишь;\n"
        "- если стиль просит короче, подход не выкидывай: сожми его до 2 шагов, "
        "а про свободную загрузку и цену ради отзывов оставь одним предложением;\n"
        "- учитывай бюджет и срок, если они указаны: не обещай объём, который "
        "в них явно не помещается;\n"
        f"- максимум {max_chars} символов. Это жёсткий лимит площадки, "
        "лишнее будет обрезано проверкой длины.\n"
        + (
            "- стиль просит короткий текст, уложись в этот потолок.\n"
            if "короче" in tone
            else (
                f"- пиши развёрнуто и используй не меньше {max(1, int(max_chars * 0.75))} "
                "символов: раскрой каждый шаг, что сделаешь, в каком порядке, "
                "что сдашь на выходе. Не добивай лимит повторами.\n"
            )
        )
        + "\n"
        f"Дополнительный стиль: {tone}.\n\n"
        f"Мои навыки:\n{skills}\n\n"
        f"Название заказа:\n{title}\n\n"
        f"Описание заказа:\n{description}\n\n"
        f"Бюджет: {budget}\n"
        f"Срок: {term}\n"
    )


_JSON_SCORE_RE = re.compile(r'"score"\s*:\s*(\d{1,3})')
_NUMBER_RE = re.compile(r"\b(\d{1,3})\b")
_SENTENCE_END_RE = re.compile(r"[.!?…](?:\s|$)")


def _hash_key(*parts: str) -> str:
    """Stable cache key. SHA-1 truncated to 32 hex chars (128 bits) — plenty
    for collision resistance at our scale."""
    h = hashlib.sha1()
    for part in parts:
        h.update(part.encode("utf-8", errors="replace"))
        h.update(b"\x1f")  # unit separator
    return h.hexdigest()[:32]


def _clamp_score(score: int) -> int:
    return max(0, min(100, int(score)))


def _parse_score_json(content: str) -> Optional[int]:
    text = content.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "score" in obj:
            return int(obj["score"])
        if isinstance(obj, (int, float)):
            return int(obj)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    if (m := _JSON_SCORE_RE.search(text)):
        return int(m.group(1))
    return None


def _parse_score_fallback(text: str) -> int:
    """Heuristic parser for non-JSON text. Picks the LAST 0..100 number.

    A model answer like "100% не подходит, 0/100" must not score 100.
    The last bounded number is far more likely to be the final answer.
    """
    if (m := _JSON_SCORE_RE.search(text)):
        return int(m.group(1))
    candidates = [int(n) for n in _NUMBER_RE.findall(text) if 0 <= int(n) <= 100]
    if not candidates:
        return 0
    return candidates[-1]


def fit_reply(text: str, limit: int) -> str:
    """Cut a reply so len(result) <= limit. The exchange checker depends on this."""
    if limit < 1:
        return ""
    fitted = _smart_truncate(text or "", limit)
    if len(fitted) > limit:
        fitted = fitted[:limit].rstrip()
    return fitted


def _smart_truncate(text: str, max_chars: int) -> str:
    """Trim to <= max_chars, preferring sentence boundaries.

    Differs from str slice [:N]: collapses runs of spaces (but keeps
    paragraph breaks), then if cut needed, falls back to last sentence
    end that uses at least 50% of the budget; else cuts at last word.
    """
    text = re.sub(r"[ \t]+", " ", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last = -1
    for m in _SENTENCE_END_RE.finditer(cut):
        last = m.end()
    if last >= int(max_chars * 0.5):
        return cut[:last].rstrip()
    return cut.rsplit(" ", 1)[0].rstrip() + "…"
