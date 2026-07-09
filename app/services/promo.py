from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import PromoActivation, Promocode, User, utcnow
from app.db.repo import add_balance, get_subscription
from app.vpn import VpnAPIError

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)


def _fmt_msk(dt: datetime) -> str:
    return (dt + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")


async def activate(session: AsyncSession, bot: Bot, user: User, code: str) -> str:
    normalized = (code or "").strip().upper()
    if not normalized:
        return "🤔 Похоже, это не промокод. Отправьте код одним сообщением."

    promo = (
        await session.execute(select(Promocode).where(Promocode.code == normalized))
    ).scalar_one_or_none()
    if promo is None:
        return "❌ Такой промокод не найден. Проверьте написание и попробуйте ещё раз."

    if promo.expires_at is not None and promo.expires_at < utcnow():
        return "⌛ Увы, срок действия этого промокода уже истёк."

    already = (
        await session.execute(
            select(PromoActivation.id).where(
                PromoActivation.promo_id == promo.id,
                PromoActivation.user_id == user.id,
            )
        )
    ).first()
    if already is not None:
        return "🙅 Вы уже активировали этот промокод — второй раз не получится 😉"

    reserve = await session.execute(
        update(Promocode)
        .where(
            Promocode.id == promo.id,
            or_(Promocode.max_uses == 0, Promocode.uses < Promocode.max_uses),
        )
        .values(uses=Promocode.uses + 1)
    )
    if reserve.rowcount == 0:
        return "😔 У этого промокода закончились активации."

    session.add(PromoActivation(promo_id=promo.id, user_id=user.id))
    await session.flush()

    if promo.kind == "balance":
        new_balance = await add_balance(session, user.id, promo.value)
        return (
            "✅ Промокод активирован!\n"
            f"💰 На баланс зачислено <b>{promo.value} ₽</b>. "
            f"Текущий баланс: <b>{new_balance} ₽</b>."
        )

    from app.services.subscriptions import issue_or_renew

    settings = get_settings()
    server = settings.servers[0]
    existing = await get_subscription(session, user.id, server.key)
    if existing is not None:
        traffic_gb, devices = existing.traffic_gb, existing.devices
    else:
        traffic_gb, devices = settings.trial.traffic_gb, settings.trial.devices

    try:
        sub = await issue_or_renew(
            session,
            user_id=user.id,
            server_key=server.key,
            days=promo.value,
            traffic_gb=traffic_gb,
            devices=devices,
        )
    except VpnAPIError:
        logger.exception(
            "Promo %s: VPN API error while issuing %s day(s) for user %s",
            normalized, promo.value, user.id,
        )
        await session.execute(
            update(Promocode)
            .where(Promocode.id == promo.id)
            .values(uses=Promocode.uses - 1)
        )
        await session.execute(
            delete(PromoActivation).where(
                PromoActivation.promo_id == promo.id,
                PromoActivation.user_id == user.id,
            )
        )
        return "⚠️ Сервер временно недоступен, попробуйте позже."

    return (
        "✅ Промокод активирован!\n"
        f"🚀 Подписка продлена на <b>{promo.value} дн.</b> — "
        f"действует до {_fmt_msk(sub.expires_at)} МСК."
    )
