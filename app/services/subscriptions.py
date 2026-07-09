from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sqlalchemy import update

from app.config import get_settings
from app.db import repo
from app.db.models import User, utcnow
from app.vpn import VpnAPIError, extract_expiry, extract_uuid, get_vpn

if TYPE_CHECKING:
    from aiogram import Bot
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import Payment, Subscription

logger = logging.getLogger(__name__)


def client_name_for(user_id: int) -> str:
    return f"tg{user_id}"


def page_url(uuid: str) -> str:
    return f"{get_settings().public_base_url}/{uuid}"


def format_msk_date(dt: datetime) -> str:
    return (dt + timedelta(hours=3)).strftime("%d.%m.%Y")


def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")]]
    )


def _page_kb(uuid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 Настроить VPN", url=page_url(uuid))],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")],
        ]
    )


async def _notify_user(
    bot: Bot, user_id: int, text: str, kb: InlineKeyboardMarkup | None = None
) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=kb)
    except TelegramAPIError:
        logger.warning("Failed to send notification to user %s", user_id)


async def issue_or_renew(
    session: AsyncSession,
    user_id: int,
    server_key: str,
    days: int,
    traffic_gb: int,
    devices: int,
    plan_id: str | None = None,
) -> Subscription:
    vpn = get_vpn(server_key)
    name = client_name_for(user_id)
    now = utcnow()

    sub = await repo.get_subscription(session, user_id, server_key)
    client = await vpn.get_client(name)

    uuid: str | None
    if client is not None:
        await vpn.edit(name, days=days, traffic_gb=traffic_gb, devices=devices)
        refreshed = await vpn.get_client(name)
        server_expiry = extract_expiry(refreshed) if refreshed is not None else None
        if server_expiry is not None:
            expires_at = server_expiry
        else:
            old_expires = sub.expires_at if sub is not None else (extract_expiry(client) or now)
            expires_at = max(now, old_expires) + timedelta(days=days)
        uuid = (
            (extract_uuid(refreshed) if refreshed is not None else None)
            or extract_uuid(client)
            or (sub.uuid if sub is not None else None)
        )
    else:
        try:
            await vpn.create(name, days, traffic_gb=traffic_gb, devices=devices)
        except VpnAPIError as exc:
            if exc.status != 409 and exc.code != "user_already_exists":
                raise
            logger.info("Client %s already exists on %s, falling back to edit", name, server_key)
            await vpn.edit(name, days=days, traffic_gb=traffic_gb, devices=devices)
        created = await vpn.get_client(name)
        if created is None:
            raise VpnAPIError(404, "user_not_found", f"Client {name} missing right after create")
        uuid = extract_uuid(created)
        expires_at = extract_expiry(created) or (now + timedelta(days=days))

    if not uuid:
        raise VpnAPIError(0, "no_uuid", f"Could not obtain uuid for client {name} on {server_key}")

    return await repo.upsert_subscription(
        session,
        user_id=user_id,
        server_key=server_key,
        client_name=name,
        uuid=uuid,
        expires_at=expires_at,
        traffic_gb=traffic_gb,
        devices=devices,
        plan_id=plan_id,
    )


async def issue_trial(session: AsyncSession, user: User) -> Subscription:
    settings = get_settings()
    if not settings.trial.enabled:
        raise ValueError("trial_disabled")
    if user.trial_used:
        raise ValueError("trial_used")

    claimed = await session.execute(
        update(User)
        .where(User.id == user.id, User.trial_used.is_(False))
        .values(trial_used=True)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 0:
        raise ValueError("trial_used")
    user.trial_used = True
    await session.commit()

    server = settings.servers[0]
    try:
        sub = await issue_or_renew(
            session,
            user_id=user.id,
            server_key=server.key,
            days=settings.trial.days,
            traffic_gb=settings.trial.traffic_gb,
            devices=settings.trial.devices,
            plan_id=None,
        )
    except Exception:
        await session.execute(
            update(User)
            .where(User.id == user.id)
            .values(trial_used=False)
            .execution_options(synchronize_session=False)
        )
        user.trial_used = False
        await session.commit()
        raise
    return sub


async def apply_paid_plan(
    session: AsyncSession, bot: Bot, payment: Payment, *, already_marked: bool = False
) -> Subscription | None:
    if not already_marked and not await repo.mark_paid(session, payment):
        logger.info("Payment %s is already paid — skipping", payment.id)
        return None

    settings = get_settings()
    sub: Subscription | None = None

    if payment.kind == "plan":
        sub = await _apply_plan_payment(session, bot, payment)
    else:
        balance = await repo.add_balance(session, payment.user_id, payment.amount_rub)
        await _notify_user(
            bot,
            payment.user_id,
            f"✅ Баланс пополнен на <b>{payment.amount_rub} ₽</b>\n"
            f"💰 Текущий баланс: <b>{balance} ₽</b>",
            _menu_kb(),
        )

    try:
        from app.services.referrals import reward_for_payment

        await reward_for_payment(session, bot, payment)
    except Exception:
        logger.exception("Referral reward failed for payment %s", payment.id)

    await _notify_admins(bot, session, payment, settings)
    return sub


async def _apply_plan_payment(
    session: AsyncSession, bot: Bot, payment: Payment
) -> Subscription | None:
    settings = get_settings()
    plan = settings.plan(payment.plan_id or "")
    server_key = payment.server_key or (settings.servers[0].key if settings.servers else "")

    if plan is None or not server_key:
        logger.error(
            "Paid payment %s references unknown plan %r / server %r — refunding to balance",
            payment.id, payment.plan_id, payment.server_key,
        )
        balance = await repo.add_balance(session, payment.user_id, payment.amount_rub)
        await _notify_user(
            bot,
            payment.user_id,
            "⚠️ Этот тариф больше недоступен, поэтому мы зачислили "
            f"<b>{payment.amount_rub} ₽</b> на ваш баланс "
            f"(текущий баланс: <b>{balance} ₽</b>).\n"
            "Выберите другой тариф и оплатите его с баланса 💙",
            _menu_kb(),
        )
        return None

    try:
        sub = await issue_or_renew(
            session,
            user_id=payment.user_id,
            server_key=server_key,
            days=plan.days,
            traffic_gb=plan.traffic_gb,
            devices=plan.devices,
            plan_id=plan.id,
        )
    except VpnAPIError:
        logger.exception("Failed to issue subscription for paid payment %s", payment.id)
        balance = await repo.add_balance(session, payment.user_id, payment.amount_rub)
        await _notify_user(
            bot,
            payment.user_id,
            "⚠️ Оплата получена, но VPN-сервер временно недоступен.\n"
            f"Мы зачислили <b>{payment.amount_rub} ₽</b> на ваш баланс "
            f"(текущий баланс: <b>{balance} ₽</b>) — попробуйте оформить подписку "
            "чуть позже, оплатив с баланса. Извините за неудобства 🙏",
            _menu_kb(),
        )
        return None

    server = settings.server(server_key)
    location_line = f"\n📍 Локация: {html.escape(server.name)}" if server is not None else ""
    await _notify_user(
        bot,
        payment.user_id,
        "🎉 Оплата получена!\n\n"
        f"✅ Подписка активна до <b>{format_msk_date(sub.expires_at)}</b> (МСК)"
        f"{location_line}\n\n"
        "Нажмите кнопку ниже — откроется страница с настройкой VPN для любого устройства 👇",
        _page_kb(sub.uuid),
    )
    return sub


async def _notify_admins(
    bot: Bot, session: AsyncSession, payment: Payment, settings=None
) -> None:
    settings = settings or get_settings()
    user = await repo.get_user(session, payment.user_id)
    if user is not None and user.username:
        who = f"@{user.username}"
    elif user is not None and user.full_name:
        who = html.escape(user.full_name)
    else:
        who = "пользователь"

    if payment.kind == "plan":
        plan = settings.plan(payment.plan_id or "")
        what = f"тариф «{html.escape(plan.title)}»" if plan is not None else "тариф"
    else:
        what = "пополнение"

    text = f"💸 Оплата: {who} ({payment.user_id}) — {what}, {payment.amount_rub} ₽, {payment.provider}"
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except TelegramAPIError:
            logger.warning("Failed to notify admin %s about payment %s", admin_id, payment.id)
