from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, or_, select

from app.bot.keyboards import back_btn
from app.config import get_settings
from app.db.base import Session
from app.db.models import Subscription, utcnow
from app.services.payments.cryptobot import poll_pending

logger = logging.getLogger(__name__)

_REMINDER_INTERVAL = 30 * 60
_TICK = 60


def _fmt_msk(dt: datetime) -> str:
    return (dt + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")


def _server_name(server_key: str) -> str:
    server = get_settings().server(server_key)
    return server.name if server else server_key


def _renew_kb(server_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Продлить", callback_data=f"prof:renew:{server_key}")],
            [back_btn("menu:main", "⬅️ В меню")],
        ]
    )


async def _notify(bot: Bot, sub: Subscription, text: str) -> None:
    try:
        await bot.send_message(sub.user_id, text, reply_markup=_renew_kb(sub.server_key))
    except TelegramAPIError as exc:
        logger.warning("Reminder to user %s failed: %s", sub.user_id, exc)
    await asyncio.sleep(0.05)


async def _reminders_pass(bot: Bot) -> None:
    now = utcnow()
    async with Session() as session:
        result = await session.execute(
            select(Subscription).where(
                or_(
                    and_(
                        Subscription.expires_at > now,
                        Subscription.expires_at <= now + timedelta(days=3),
                        Subscription.reminded_3d.is_(False),
                    ),
                    and_(
                        Subscription.expires_at > now,
                        Subscription.expires_at <= now + timedelta(days=1),
                        Subscription.reminded_1d.is_(False),
                    ),
                    and_(
                        Subscription.expires_at <= now,
                        Subscription.notified_expired.is_(False),
                    ),
                )
            )
        )
        subs = list(result.scalars().all())

        for sub in subs:
            server = _server_name(sub.server_key)
            expires = _fmt_msk(sub.expires_at)
            if sub.expires_at <= now:
                await _notify(
                    bot,
                    sub,
                    f"❌ Подписка на <b>{server}</b> истекла {expires} МСК.\n"
                    "Продлите её, чтобы снова пользоваться VPN 👇",
                )
                sub.notified_expired = True
                sub.reminded_3d = True
                sub.reminded_1d = True
            elif sub.expires_at <= now + timedelta(days=1) and not sub.reminded_1d:
                await _notify(
                    bot,
                    sub,
                    f"⏳ Подписка на <b>{server}</b> истекает уже через сутки — "
                    f"{expires} МСК!\nУспейте продлить, чтобы не остаться без защиты 👇",
                )
                sub.reminded_1d = True
                sub.reminded_3d = True
            elif not sub.reminded_3d:
                days = max(1, math.ceil((sub.expires_at - now).total_seconds() / 86400))
                await _notify(
                    bot,
                    sub,
                    f"⏳ Подписка на <b>{server}</b> истекает {expires} МСК "
                    f"(через {days} дн.).\nПродлите заранее — дни просто добавятся 👇",
                )
                sub.reminded_3d = True

        await session.commit()
    if subs:
        logger.info("Reminder pass done: %d notifications", len(subs))


async def scheduler(bot: Bot) -> None:
    settings = get_settings()
    last_reminders = 0.0
    logger.info("Background scheduler started")
    while True:
        now = time.monotonic()

        if now - last_reminders >= _REMINDER_INTERVAL or last_reminders == 0.0:
            last_reminders = now
            try:
                await _reminders_pass(bot)
            except Exception:
                logger.exception("Expiry reminders pass failed")

        if settings.cryptobot_token:
            try:
                await poll_pending(bot, Session)
            except Exception:
                logger.exception("CryptoBot polling pass failed")

        await asyncio.sleep(_TICK)
