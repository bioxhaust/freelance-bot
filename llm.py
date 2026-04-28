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

_DEFAULT_DRAFT_CACHE_TTL = 30 * 60  # 30 minutes
_DEFAULT_DRAFT_CACHE_SIZE = 256
_DEFAULT_SCORE_CACHE_TTL = 60 * 60  # 1 hour
_DEFAULT_SCORE_CACHE_SIZE = 512


class LLMClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        http: Optional[HttpClient] = None,
        *,
        draft_cache_ttl: int = _DEFAULT_DRAFT_CACHE_TTL,
        score_cache_ttl: int = _DEFAULT_SCORE_CACHE_TTL,
    ) -> None:
        self.api_key = api_key
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
    ) -> str:
        cache_key = _hash_key(
            "draft", skills, title, description, str(max_chars), style_hint
        )
        async with self._cache_lock:
            cached = self._draft_cache.get(cache_key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        self.cache_misses += 1

        prompt = (
            "Ты помогаешь писать отклики на фриланс-заказы.\n"
            "Сформируй краткий, уверенный и дружелюбный отклик на русском языке.\n"
            "Требования:\n"
            "- без воды и без шаблонного пафоса;\n"
            "- 3-6 предложений;\n"
            "- подчеркни релевантный опыт и предложи следующий шаг;\n"
            "- не придумывай факты, опирайся на навыки ниже.\n"
            f"- максимум {max_chars} символов.\n\n"
            f"Дополнительный стиль: {style_hint or 'нейтральный деловой тон'}.\n\n"
            f"Мои навыки:\n{skills}\n\n"
            f"Название заказа:\n{title}\n\n"
            f"Описание заказа:\n{description}\n"
        )
        draft = _smart_truncate(await self._chat(prompt), max_chars)
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

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        response = await self.http.request_with_retry(
            "POST", OLLAMA_CLOUD_URL, json=payload, headers=headers
        )
        async with response:
            response.raise_for_status()
            data = await response.json()
        return data["message"]["content"]


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
