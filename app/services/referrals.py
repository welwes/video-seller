from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Payment, User
from app.db.repo import add_balance, get_user

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)


def ref_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref{user_id}"


async def reward_for_payment(session: AsyncSession, bot: Bot, payment: Payment) -> None:
    if payment.kind not in ("plan", "topup"):
        return
    if payment.provider == "balance":
        return

    settings = get_settings()
    percent = settings.referral_percent
    if percent <= 0:
        return

    payer = await get_user(session, payment.user_id)
    if payer is None or payer.referrer_id is None:
        return
    if payer.referrer_id == payer.id:
        return

    referrer = await get_user(session, payer.referrer_id)
    if referrer is None:
        return

    reward = payment.amount_rub * percent // 100
    if reward <= 0:
        return

    await add_balance(session, referrer.id, reward)
    logger.info(
        "Referral reward: %s RUB to user %s for payment %s of user %s",
        reward, referrer.id, payment.id, payer.id,
    )

    try:
        await bot.send_message(
            referrer.id,
            f"🎉 +{reward} ₽ по партнёрке!\n"
            "Ваш друг совершил оплату — вознаграждение уже на балансе 💰",
        )
    except Exception:
        logger.warning("Failed to notify referrer %s about reward", referrer.id)


async def ref_stats(session: AsyncSession, user_id: int) -> dict[str, Any]:
    settings = get_settings()
    percent = settings.referral_percent

    count = (
        await session.execute(
            select(func.count(User.id)).where(User.referrer_id == user_id)
        )
    ).scalar_one()

    amounts = (
        await session.execute(
            select(Payment.amount_rub)
            .join(User, Payment.user_id == User.id)
            .where(
                User.referrer_id == user_id,
                Payment.status == "paid",
                Payment.provider != "balance",
            )
        )
    ).scalars().all()
    earned = sum(amount * percent // 100 for amount in amounts) if percent > 0 else 0

    return {"count": int(count), "earned": int(earned)}
