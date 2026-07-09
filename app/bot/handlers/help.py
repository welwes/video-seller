from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.keyboards import back_btn
from app.config import get_settings

logger = logging.getLogger(__name__)

router = Router(name="help")


def _help_text() -> str:
    settings = get_settings()
    return (
        "🆘 <b>Помощь</b>\n\n"
        "<b>Как подключиться:</b>\n"
        "1️⃣ Купите подписку (или активируйте пробный период)\n"
        "2️⃣ Откройте свою страницу подписки — кнопка «📱 Настроить VPN»\n"
        "3️⃣ Установите приложение и нажмите кнопку — подписка добавится автоматически\n"
        "4️⃣ Выберите сервер и подключитесь ✅\n\n"
        "<b>Рекомендуемые приложения:</b>\n"
        "📱 iOS — Happ, Streisand, Shadowrocket\n"
        "🤖 Android — Happ, v2rayTun, Hiddify\n"
        "💻 Windows — Hiddify, v2rayN\n"
        "🍏 macOS — Happ, Hiddify, FoXray\n"
        "📺 Android TV — Happ\n\n"
        "На странице подписки есть кнопки скачивания и пошаговая инструкция "
        "для каждой платформы.\n\n"
        f"Остались вопросы? Напишите в поддержку {settings.shop_name} — поможем 🙌"
    )


def _help_kb() -> InlineKeyboardMarkup:
    settings = get_settings()
    rows: list[list[InlineKeyboardButton]] = []
    if settings.support_url:
        rows.append([InlineKeyboardButton(text="🆘 Поддержка", url=settings.support_url)])
    if settings.channel_url:
        rows.append([InlineKeyboardButton(text="📢 Наш канал", url=settings.channel_url)])
    rows.append([back_btn("menu:main", "⬅️ В меню")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "menu:help")
async def cb_help(cb: CallbackQuery) -> None:
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_text(_help_text(), reply_markup=_help_kb())
        except TelegramBadRequest:
            pass
    await cb.answer()


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(_help_text(), reply_markup=_help_kb())
