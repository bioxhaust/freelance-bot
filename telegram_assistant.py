from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import CallbackQuery, Message, Update

from config import Settings
from db import Database
from exchanges import Order
from llm import LLMClient
from logging_setup import bind_order, reset_order
from notifier import TelegramNotifier
from runtime_state import RuntimeState

logger = logging.getLogger(__name__)


ORDER_URL_RE = re.compile(r"https?://\S+")
RELEVANCE_RE = re.compile(r"·\s*(\d{1,3})\s*%")

_TONE_HINTS: dict[str, str] = {
    "tone_neutral": "нейтральный деловой тон",
    "tone_friendly": "более мягкий и дружелюбный тон",
    "tone_hard": "более уверенный, деловой и экспертный тон",
}

def _hire_suffix(order: Order) -> str:
    phrase = order.hire_phrase()
    if not phrase:
        return ""
    return " · " + html.escape(phrase, quote=False)


_KNOWN_CALLBACKS: frozenset[str] = frozenset(
    {
        "send",
        "draft",
        "short",
        "why",
        "skip",
        "archive",
        "tone",
        "tone_back",
        "tone_neutral",
        "tone_friendly",
        "tone_hard",
        # Legacy callbacks from older messages — gracefully forward to new flow.
        "hard",
        "friendly",
    }
)


class TelegramAssistant:
    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        db: Database,
        llm: LLMClient,
        notifier: TelegramNotifier,
        runtime: RuntimeState,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.db = db
        self.llm = llm
        self.notifier = notifier
        self.runtime = runtime
        self.offset = 0
        # Per-process idempotency: message_ids that have already received a
        # final state (Sent / Skipped / Archived) — guards against the
        # double-click race that beats the DB roundtrip.
        self._finalised_message_ids: set[int] = set()

    async def run_forever(self) -> None:
        conflict_logged_at: float = 0.0
        while True:
            try:
                updates = await self.bot.get_updates(
                    offset=self.offset,
                    timeout=20,
                    allowed_updates=["message", "callback_query"],
                )
                for update in updates:
                    self.offset = update.update_id + 1
                    await self._handle_update(update)
            except TelegramConflictError:
                # Another instance of this bot is also calling getUpdates.
                # Telegram only lets one client at a time. Log once per
                # minute instead of spamming every 6s, and back off long
                # enough for the other instance to either die or release.
                now = asyncio.get_event_loop().time()
                if now - conflict_logged_at > 60:
                    logger.error(
                        "Telegram getUpdates conflict: another bot instance is "
                        "polling. Stop the other instance, then this one will "
                        "recover automatically."
                    )
                    conflict_logged_at = now
                await asyncio.sleep(30)
            except TelegramRetryAfter as exc:
                logger.warning("Telegram rate-limit, sleeping %.1fs", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
            except TelegramNetworkError as exc:
                # Routine network blips on long-poll: connection reset, DNS
                # hiccup, etc. Don't spam stack traces — one short warning
                # and a quick retry.
                logger.warning("Telegram network blip: %s", exc)
                await asyncio.sleep(3)
            except Exception:
                logger.exception("Telegram assistant loop failed")
                await asyncio.sleep(3)

    async def _handle_update(self, update: Update) -> None:
        if update.callback_query is not None:
            await self._handle_callback(update.callback_query)
            return

        message = update.message
        if message is None or message.text is None:
            return
        if str(message.chat.id) != self.settings.telegram_chat_id:
            return
        if message.from_user and message.from_user.is_bot:
            return

        text = message.text.strip()
        lowered = text.lower()
        if lowered in ("/help_ai", "/help", "/start"):
            await self._send_help(message)
            return
        if lowered in ("/stats",):
            await self._cmd_stats(message)
            return
        if lowered in ("/digest",):
            await self._cmd_digest(message)
            return
        if lowered in ("/pause",):
            await self.runtime.set_paused(True)
            await message.reply("⏸ Опрос бирж приостановлен. /resume чтобы возобновить.")
            return
        if lowered in ("/resume",):
            await self.runtime.set_paused(False)
            await message.reply("▶️ Опрос возобновлён.")
            return
        if lowered.startswith("/threshold"):
            await self._cmd_threshold(message, text)
            return
        if lowered in ("/skills",):
            await message.reply(
                f"<b>Текущие навыки</b> (для LLM):\n<blockquote>"
                f"{html.escape(self.settings.my_skills, quote=False)}</blockquote>",
                parse_mode="HTML",
            )
            return
        if lowered not in ("черновик", "короче", "жестче", "дружелюбнее", "почему"):
            return
        text = lowered

        parent = message.reply_to_message
        if parent is None or parent.text is None:
            await message.reply("Ответь командой на сообщение с объявлением.")
            return

        order_url = self._extract_order_url(parent.text)
        if not order_url:
            await message.reply("Не нашел ссылку заказа в исходном сообщении.")
            return

        order = await self.db.get_order_by_url(order_url)
        if order is None:
            await message.reply("Этот заказ не найден в базе.")
            return

        if text == "почему":
            explanation = await self.llm.explain_relevance(
                skills=self.settings.my_skills,
                title=order.title,
                description=order.description,
            )
            safe = html.escape(explanation, quote=False)
            await message.reply(
                f"<b>❓ Почему этот заказ:</b>\n<blockquote>{safe}</blockquote>",
                parse_mode="HTML",
            )
            return

        # Text-command path keeps the legacy "post a new message" UX —
        # the slick edit-in-place experience is for inline buttons.
        style_hint, max_chars = self._style_for(
            {
                "черновик": "draft",
                "короче": "short",
                "жестче": "tone_hard",
                "дружелюбнее": "tone_friendly",
            }[text]
        )
        draft = await self.llm.generate_reply_draft(
            skills=self.settings.my_skills,
            title=order.title,
            description=order.description,
            max_chars=max_chars,
            style_hint=style_hint,
            price=order.price,
            deadline=order.deadline,
        )
        safe = html.escape(draft, quote=False)
        await message.reply(
            f"<b>Новый черновик</b>\n<pre>{safe}</pre>",
            parse_mode="HTML",
        )

    async def _handle_callback(self, query: CallbackQuery) -> None:
        message = query.message
        if message is None or message.chat is None:
            await query.answer()
            return
        if str(message.chat.id) != self.settings.telegram_chat_id:
            await query.answer("Этот чат не разрешен")
            return
        if query.from_user and query.from_user.is_bot:
            await query.answer()
            return

        action = (query.data or "").strip().lower()
        if action not in _KNOWN_CALLBACKS:
            await query.answer("Неизвестная кнопка")
            return

        if not message.text:
            await query.answer("Не найден текст объявления")
            return
        order_url = self._extract_order_url(message.text)
        if not order_url:
            await query.answer("Не нашел ссылку заказа")
            return
        order = await self.db.get_order_by_url(order_url)
        if order is None:
            await query.answer("Этот заказ не найден в базе")
            return

        order_token = bind_order(order_url)
        try:
            await self._dispatch_callback_action(action, query, message, order)
        finally:
            reset_order(order_token)

    async def _dispatch_callback_action(
        self,
        action: str,
        query: CallbackQuery,
        message: Message,
        order: Order,
    ) -> None:
        if message.message_id in self._finalised_message_ids and action != "why":
            await query.answer("Уже завершено")
            return

        if action == "send":
            await self._on_send(query, message, order)
            return

        if action == "skip":
            await self._on_terminal(query, message, order, kind="skip")
            return

        if action == "archive":
            await self._on_terminal(query, message, order, kind="archive")
            return

        if action == "tone":
            await query.answer("Выбери тон")
            await self.notifier.show_tone_submenu(message, order)
            return

        if action == "tone_back":
            await query.answer()
            existing_draft = self._extract_draft_text(message.text or "")
            await self.notifier.restore_actions_keyboard(
                message, order, has_draft=bool(existing_draft)
            )
            return

        if action == "why":
            await query.answer("Объясняю...")
            explanation = await self.llm.explain_relevance(
                skills=self.settings.my_skills,
                title=order.title,
                description=order.description,
            )
            safe = html.escape(explanation, quote=False)
            await message.reply(
                f"<b>❓ Почему этот заказ:</b>\n<blockquote>{safe}</blockquote>",
                parse_mode="HTML",
            )
            return

        # draft / short / tone_neutral / tone_friendly / tone_hard / (legacy hard, friendly)
        await self._regen_draft_in_place(query, message, order, action)

    async def _on_send(
        self, query: CallbackQuery, message: Message, order: Order
    ) -> None:
        if message.message_id in self._finalised_message_ids:
            await query.answer("Уже отправлено")
            return

        if await self.db.is_applied(order.url):
            self._finalised_message_ids.add(message.message_id)
            await query.answer("Уже отправлено")
            try:
                await self.notifier.mark_sent(message, order)
            except Exception:
                logger.exception("mark_sent failed for %s", order.url)
            return

        existing_draft = self._extract_draft_text(message.text or "")
        draft_to_attach = ""
        if not existing_draft:
            try:
                draft_to_attach = await self.llm.generate_reply_draft(
                    skills=self.settings.my_skills,
                    title=order.title,
                    description=order.description,
                    max_chars=self.settings.draft_max_chars,
                    price=order.price,
                    deadline=order.deadline,
                )
            except Exception:
                logger.exception("Draft generation on Send failed for %s", order.url)
                draft_to_attach = ""

        await self.db.mark_applied(order.url)
        self._finalised_message_ids.add(message.message_id)
        await query.answer("✅ Помечено как отправлено")

        try:
            await self.notifier.mark_sent(message, order, draft=draft_to_attach)
        except Exception:
            logger.exception("mark_sent failed for %s", order.url)

    async def _on_terminal(
        self,
        query: CallbackQuery,
        message: Message,
        order: Order,
        *,
        kind: str,
    ) -> None:
        self._finalised_message_ids.add(message.message_id)
        if kind == "skip":
            await query.answer("Пропущено")
            try:
                await self.notifier.mark_skipped(message, order)
            except Exception:
                logger.exception("mark_skipped failed for %s", order.url)
        else:
            await query.answer("В архив")
            try:
                await self.notifier.mark_archived(message, order)
            except Exception:
                logger.exception("mark_archived failed for %s", order.url)

    async def _regen_draft_in_place(
        self,
        query: CallbackQuery,
        message: Message,
        order: Order,
        action: str,
    ) -> None:
        await query.answer("Генерирую…")
        style_hint, max_chars = self._style_for(action)
        try:
            draft = await self.llm.generate_reply_draft(
                skills=self.settings.my_skills,
                title=order.title,
                description=order.description,
                max_chars=max_chars,
                style_hint=style_hint,
                price=order.price,
                deadline=order.deadline,
            )
        except Exception:
            logger.exception("Draft generation failed for %s", order.url)
            await message.reply("Не удалось сгенерировать черновик. Попробуй ещё раз.")
            return

        relevance = self._extract_relevance(message.text or "")
        try:
            await self.notifier.update_card(message, order, relevance, draft)
        except Exception:
            logger.exception("update_card failed for %s", order.url)
            safe = html.escape(draft, quote=False)
            await message.reply(
                f"<b>Новый черновик</b>\n<pre>{safe}</pre>",
                parse_mode="HTML",
            )

    def _style_for(self, action: str) -> tuple[str, int]:
        max_chars = self.settings.draft_max_chars
        if action == "short":
            return (
                "короче обычного, но оставь сжатый план решения из 2 шагов",
                min(500, max_chars),
            )
        if action in _TONE_HINTS:
            return (_TONE_HINTS[action], max_chars)
        # Legacy callbacks from old keyboards
        if action == "hard":
            return (_TONE_HINTS["tone_hard"], max_chars)
        if action == "friendly":
            return (_TONE_HINTS["tone_friendly"], max_chars)
        return ("", max_chars)

    @staticmethod
    def _extract_order_url(text: str) -> Optional[str]:
        match = ORDER_URL_RE.search(text)
        if not match:
            return None
        return match.group(0).rstrip(").,")

    @staticmethod
    def _extract_draft_text(text: str) -> str:
        # Modern card uses '<pre>...</pre>' inside a 'Черновик отклика' block;
        # the parsed Message.text strips tags, so look for the heading marker.
        match = re.search(
            r"Черновик отклика\s*\n(?P<draft>.+)$",
            text,
            re.DOTALL,
        )
        if not match:
            return ""
        return match.group("draft").strip()

    @staticmethod
    def _extract_relevance(text: str) -> int:
        match = RELEVANCE_RE.search(text)
        if not match:
            return 0
        try:
            return max(0, min(100, int(match.group(1))))
        except ValueError:
            return 0

    async def _cmd_stats(self, message: Message) -> None:
        threshold = self.runtime.relevance_threshold
        stats = await self.db.stats(threshold=threshold)
        cache = self.llm.cache_stats()
        sources = stats.get("by_source_24h") or []
        sources_line = (
            " · ".join(f"{src} {cnt}" for src, cnt in sources)
            if sources else "—"
        )
        paused_str = "⏸ ПАУЗА" if self.runtime.paused else "▶️ работает"
        text = (
            f"<b>📊 Статистика</b>  ({paused_str})\n\n"
            f"<b>За 24 часа</b>\n"
            f"• Новых заказов: {stats['last_24h']}\n"
            f"• Прошли порог ≥{threshold}%: {stats['passed_24h']}\n"
            f"• Отправлено: {stats['sent_24h']}\n"
            f"• Источники: {sources_line}\n\n"
            f"<b>Всего</b>\n"
            f"• В базе: {stats['total']}\n"
            f"• Отправлено за всё время: {stats['applied']}\n"
            f"• Средняя релевантность: {stats['avg_relevance']:.1f}\n\n"
            f"<b>LLM-кэш</b>\n"
            f"• Hits/Misses: {cache['hits']}/{cache['misses']}\n"
            f"• Размер: drafts={cache['draft_size']} / scores={cache['score_size']}"
        )
        await message.reply(text, parse_mode="HTML", disable_web_page_preview=True)

    async def _cmd_digest(self, message: Message) -> None:
        threshold = self.runtime.relevance_threshold
        applied = await self.db.applied_today(limit=10)
        unhandled = await self.db.top_unhandled(threshold=threshold, limit=5)
        stats = await self.db.stats(threshold=threshold)

        lines: list[str] = ["<b>📅 Дайджест за день</b>", ""]
        lines.append(f"✅ <b>Отправлено:</b> {stats['sent_24h']}")
        for o in applied[:5]:
            title_safe = html.escape(o.title[:60], quote=False)
            url_safe = html.escape(o.url, quote=True)
            lines.append(
                f'   • <a href="{url_safe}">{title_safe}</a> · '
                f"{html.escape(o.source, quote=False)}{_hire_suffix(o)}"
            )

        if unhandled:
            lines.append("")
            lines.append(f"🔥 <b>Топ-{len(unhandled)} нерасхваченных (≥{threshold}%):</b>")
            for o in unhandled:
                rel = await self._relevance_for(o.url)
                title_safe = html.escape(o.title[:60], quote=False)
                url_safe = html.escape(o.url, quote=True)
                lines.append(
                    f'   • <a href="{url_safe}">{title_safe}</a> · '
                    f"{rel}% · {html.escape(o.source, quote=False)}{_hire_suffix(o)}"
                )

        lines.append("")
        lines.append(f"📥 За 24ч поступило: {stats['last_24h']}")
        text = "\n".join(lines)
        await message.reply(text, parse_mode="HTML", disable_web_page_preview=True)

    async def _cmd_threshold(self, message: Message, raw: str) -> None:
        parts = raw.split()
        if len(parts) == 1:
            await message.reply(
                f"Текущий порог: <b>{self.runtime.relevance_threshold}%</b>.\n"
                "Использование: <code>/threshold 75</code>",
                parse_mode="HTML",
            )
            return
        try:
            new_value = int(parts[1])
        except ValueError:
            await message.reply("Порог должен быть целым числом 0..100.")
            return
        applied = await self.runtime.set_threshold(new_value)
        await message.reply(
            f"✅ Порог релевантности теперь <b>{applied}%</b>.",
            parse_mode="HTML",
        )

    async def _relevance_for(self, url: str) -> int:
        async with self.db.conn.execute(
            "SELECT relevance FROM orders WHERE url = ? LIMIT 1", (url,)
        ) as cur:
            row = await cur.fetchone()
        if row is None or row[0] is None:
            return 0
        return int(row[0])

    async def _send_help(self, message: Message) -> None:
        await message.reply(
            "<b>Кнопки под карточкой:</b>\n"
            "• 📨 Отправить — пометить как отправленное\n"
            "• ✏️ Сгенерировать черновик / 🔄 Перегенерить — "
            "отклик с планом, как решать задачу\n"
            "• ✂️ Короче — тот же план, но в 2 шага\n"
            "• 🎭 Сменить тон — 💼 Деловой / 🤝 Дружелюбный / 🔥 Жёсткий\n"
            "• ❓ Почему — пояснение релевантности\n"
            "• 🌐 Открыть / ⏭ Пропустить / 🗄 В архив\n\n"
            "<b>Команды:</b>\n"
            "/stats — статистика за 24ч\n"
            "/digest — сводка дня (отправлено + топ нерасхваченных)\n"
            "/threshold N — поменять порог релевантности (0..100)\n"
            "/pause /resume — поставить опрос на паузу\n"
            "/skills — показать текущие навыки\n\n"
            "<b>Текстовые команды</b> (ответом на карточку):\n"
            "черновик / короче / жестче / дружелюбнее / почему",
            parse_mode="HTML",
        )
