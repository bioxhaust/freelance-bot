from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramConflictError, TelegramRetryAfter
from aiogram.types import CallbackQuery, Message, Update

from config import Settings
from db import Database
from exchanges import Order
from llm import LLMClient
from notifier import TelegramNotifier

logger = logging.getLogger(__name__)


ORDER_URL_RE = re.compile(r"https?://\S+")
RELEVANCE_RE = re.compile(r"·\s*(\d{1,3})\s*%")

_TONE_HINTS: dict[str, str] = {
    "tone_neutral": "нейтральный деловой тон",
    "tone_friendly": "более мягкий и дружелюбный тон",
    "tone_hard": "более уверенный, деловой и экспертный тон",
}

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
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.db = db
        self.llm = llm
        self.notifier = notifier
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

        text = message.text.strip().lower()
        if text in ("/help_ai", "/help"):
            await self._send_help(message)
            return
        if text not in ("черновик", "короче", "жестче", "дружелюбнее", "почему"):
            return

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
            return ("максимально коротко и по делу", min(400, max_chars))
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

    async def _send_help(self, message: Message) -> None:
        await message.reply(
            "Кнопки под объявлением (рекомендуется):\n"
            "• 📨 Отправить — пометить как отправленное\n"
            "• ✏️ Сгенерировать черновик / 🔄 Перегенерить\n"
            "• ✂️ Короче — компактный отклик\n"
            "• 🎭 Сменить тон — 💼 Деловой / 🤝 Дружелюбный / 🔥 Жёсткий\n"
            "• ❓ Почему — пояснение релевантности\n"
            "• 🌐 Открыть — открыть заказ на бирже\n"
            "• ⏭ Пропустить / 🗄 В архив\n\n"
            "Текстовые команды (ответом на карточку): черновик / короче / жестче / "
            "дружелюбнее / почему."
        )
