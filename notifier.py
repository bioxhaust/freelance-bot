from __future__ import annotations

import html
import re
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from exchanges import Order

_DESC_LIMIT = 700
_TG_MSG_LIMIT = 3500
_PLACEHOLDER_VALUES = frozenset(
    {"", "—", "-", "не указан", "не указана", "не указаны", "по договоренности"}
)


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self.bot = Bot(token)
        self.chat_id = chat_id

    async def send_order(
        self, order: Order, relevance: int, draft_reply: str = ""
    ) -> Optional[Message]:
        text = render_order_card(order, relevance, draft_reply)
        return await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=build_actions_keyboard(
                order.url, has_draft=bool(draft_reply)
            ),
        )

    async def update_card(
        self,
        message: Message,
        order: Order,
        relevance: int,
        draft_reply: str,
        *,
        keyboard: Optional[InlineKeyboardMarkup] = None,
    ) -> None:
        """Re-render the card (used to swap draft text and clear/set tone submenu)."""
        new_text = render_order_card(order, relevance, draft_reply)
        if keyboard is None:
            keyboard = build_actions_keyboard(order.url, has_draft=bool(draft_reply))
        try:
            await message.edit_text(
                new_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                try:
                    await message.edit_reply_markup(reply_markup=keyboard)
                except TelegramBadRequest:
                    pass
                return
            raise

    async def show_tone_submenu(self, message: Message, order: Order) -> None:
        try:
            await message.edit_reply_markup(
                reply_markup=build_tone_keyboard(order.url)
            )
        except TelegramBadRequest:
            pass

    async def restore_actions_keyboard(
        self, message: Message, order: Order, *, has_draft: bool
    ) -> None:
        try:
            await message.edit_reply_markup(
                reply_markup=build_actions_keyboard(order.url, has_draft=has_draft)
            )
        except TelegramBadRequest:
            pass

    async def mark_skipped(self, message: Message, order: Order) -> None:
        await self._append_status_and_lock(message, order, "⏭ Пропущено")

    async def mark_archived(self, message: Message, order: Order) -> None:
        await self._append_status_and_lock(message, order, "🗄 В архив")

    async def _append_status_and_lock(
        self, message: Message, order: Order, status: str
    ) -> None:
        original_html = message.html_text or message.text or ""
        new_text = original_html
        if status not in original_html:
            new_text = f"{original_html}\n\n<b>{status}</b>"
        if len(new_text) > _TG_MSG_LIMIT:
            new_text = new_text[: _TG_MSG_LIMIT - 1] + "…"
        try:
            await message.edit_text(
                new_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=build_finalised_keyboard(order.url),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                try:
                    await message.edit_reply_markup(
                        reply_markup=build_finalised_keyboard(order.url)
                    )
                except TelegramBadRequest:
                    pass
                return
            raise

    async def mark_sent(
        self,
        message: Message,
        order: Order,
        *,
        draft: str = "",
    ) -> None:
        """Append the 'sent' status (and optionally an inline draft) to the
        original card and replace the keyboard with a single Open-button.

        - draft is appended only if the card does not already contain a draft
          block (avoids duplicating it when the user clicks Send after
          regenerating).
        - Idempotent: 'message is not modified' is swallowed silently.
        """
        original_html = message.html_text or message.text or ""
        parts: list[str] = [original_html]

        if draft and "<pre>" not in original_html:
            safe_draft = _esc(draft)
            parts.append(
                "<b>✏️ Черновик отклика</b>\n"
                f"<pre>{safe_draft}</pre>"
            )
        if "✅ Отправлено" not in original_html:
            parts.append("<b>✅ Отправлено</b>")

        new_text = "\n\n".join(parts)
        if len(new_text) > _TG_MSG_LIMIT:
            new_text = new_text[: _TG_MSG_LIMIT - 1] + "…"

        try:
            await message.edit_text(
                new_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=build_finalised_keyboard(order.url),
            )
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                try:
                    await message.edit_reply_markup(
                        reply_markup=build_finalised_keyboard(order.url)
                    )
                except TelegramBadRequest:
                    pass
                return
            raise

    async def close(self) -> None:
        await self.bot.session.close()


_SENT_END_RE = re.compile(r"[.!?…](?:\s|$)")


def render_order_card(order: Order, relevance: int, draft_reply: str) -> str:
    title = _esc(order.title) or "—"
    src = _esc(order.source)
    desc_raw = _truncate_text(order.description or "", _DESC_LIMIT)
    desc = _esc(desc_raw)

    lines: list[str] = [
        f"<b>🟢 Новый заказ · {int(relevance)}%</b>",
        f"<b>{title}</b>",
        "",
    ]
    if src:
        lines.append(f"📍 {src}")
    if (price := _safe_field(order.price)) is not None:
        lines.append(f"💰 {price}")
    if (deadline := _safe_field(order.deadline)) is not None:
        lines.append(f"⏱ {deadline}")
    if (client := _safe_field(order.client)) is not None:
        lines.append(f"👤 {client}")
    lines.append("")
    if desc:
        lines.append(desc)

    if draft_reply:
        safe_draft = _esc(draft_reply)
        lines.append("")
        lines.append("<b>✏️ Черновик отклика</b>")
        lines.append(f"<pre>{safe_draft}</pre>")

    safe_url = html.escape(order.url or "", quote=True)
    if safe_url:
        lines.append("")
        lines.append(f'<a href="{safe_url}">Открыть заказ</a>')

    text = "\n".join(lines)
    if len(text) > _TG_MSG_LIMIT:
        text = text[: _TG_MSG_LIMIT - 1] + "…"
    return text


def build_actions_keyboard(
    order_url: str, *, has_draft: bool
) -> InlineKeyboardMarkup:
    """UX variant A: progressive disclosure.

    - When no draft yet: top row is a single 'Generate draft' wide button.
    - When draft present: top row is Send / Regenerate, second row is
      Shorter / Change tone (which opens a submenu via callback `tone`).
    - Bottom rows: Why / Open + Skip / Archive — always visible.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if has_draft:
        rows.append(
            [
                InlineKeyboardButton(text="📨 Отправить", callback_data="send"),
                InlineKeyboardButton(text="🔄 Перегенерить", callback_data="draft"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="✂️ Короче", callback_data="short"),
                InlineKeyboardButton(text="🎭 Сменить тон", callback_data="tone"),
            ]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="✏️ Сгенерировать черновик", callback_data="draft")]
        )

    rows.append(
        [
            InlineKeyboardButton(text="❓ Почему", callback_data="why"),
            InlineKeyboardButton(text="🌐 Открыть", url=order_url or "https://example.com"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip"),
            InlineKeyboardButton(text="🗄 В архив", callback_data="archive"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_tone_keyboard(order_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💼 Деловой", callback_data="tone_neutral"),
                InlineKeyboardButton(text="🤝 Дружелюбный", callback_data="tone_friendly"),
                InlineKeyboardButton(text="🔥 Жёсткий", callback_data="tone_hard"),
            ],
            [InlineKeyboardButton(text="← Назад", callback_data="tone_back")],
        ]
    )


def build_finalised_keyboard(order_url: str) -> InlineKeyboardMarkup:
    safe_url = order_url or "https://example.com"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Открыть заказ", url=safe_url)]
        ]
    )


def _esc(value: str | None) -> str:
    if not value:
        return ""
    return html.escape(value, quote=False)


def _safe_field(value: str | None) -> Optional[str]:
    if not value:
        return None
    stripped = value.strip()
    if stripped.lower() in _PLACEHOLDER_VALUES:
        return None
    return _esc(stripped)


def _truncate_text(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last = -1
    for m in _SENT_END_RE.finditer(cut):
        last = m.end()
    if last >= int(limit * 0.5):
        return cut[:last].rstrip() + " …"
    return cut.rsplit(" ", 1)[0].rstrip() + " …"


