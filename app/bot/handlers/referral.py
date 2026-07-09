from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import User
from app.services.referrals import ref_link, ref_stats

logger = logging.getLogger(__name__)

router = Router(name="referral")


@router.callback_query(F.data == "menu:ref")
async def menu_ref(
    cb: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    user: User,
) -> None:
    settings = get_settings()
    me = await bot.me()
    link = ref_link(me.username or "", user.id)
    stats = await ref_stats(session, user.id)

    text = (
        "👥 <b>Партнёрская программа</b>\n\n"
        f"Приглашайте друзей и получайте <b>{settings.referral_percent}%</b> "
        "с каждой их оплаты прямо на свой баланс 💸\n\n"
        "🔗 Ваша персональная ссылка:\n"
        f"<code>{link}</code>\n\n"
        f"📊 Приглашено друзей: <b>{stats['count']}</b>\n"
        f"💰 Заработано: <b>{stats['earned']} ₽</b>"
    )
    share_text = f"Попробуй {settings.shop_name} — быстрый и надёжный VPN! {link}"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться", switch_inline_query=share_text)],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")],
        ]
    )

    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            pass
    await cb.answer()
