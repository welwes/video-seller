from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import back_btn, back_to_menu
from app.config import get_settings
from app.db.models import Subscription, User, utcnow
from app.db.repo import get_subscriptions
from app.services.subscriptions import page_url
from app.vpn import VpnAPIError, extract_traffic, get_vpn

logger = logging.getLogger(__name__)

router = Router(name="profile")


def _fmt_msk(dt: datetime) -> str:
    return (dt + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")


def _days_left(expires_at: datetime) -> int:
    return max(0, math.ceil((expires_at - utcnow()).total_seconds() / 86400))


def _server_name(server_key: str) -> str:
    server = get_settings().server(server_key)
    return server.name if server else server_key


def _sub_summary(sub: Subscription) -> str:
    settings = get_settings()
    active = sub.expires_at > utcnow()
    status = "🟢 Активна" if active else "🔴 Истекла"
    plan = settings.plan(sub.plan_id) if sub.plan_id else None
    lines = [
        f"{status} · <b>{_server_name(sub.server_key)}</b>",
        f"⏳ До: <b>{_fmt_msk(sub.expires_at)} МСК</b>"
        + (f" (осталось {_days_left(sub.expires_at)} дн.)" if active else ""),
    ]
    if plan is not None:
        lines.append(f"📦 Тариф: {plan.title}")
    return "\n".join(lines)


async def _edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            pass
    await cb.answer()


@router.callback_query(F.data == "menu:profile")
async def cb_profile(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    settings = get_settings()
    subs = await get_subscriptions(session, user.id)

    if not subs:
        text = (
            "👤 <b>Мой VPN</b>\n\n"
            f"💰 Баланс: <b>{user.balance} ₽</b>\n\n"
            "У вас пока нет подписки. Самое время это исправить 😉"
        )
        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(text="🛒 Купить VPN", callback_data="menu:buy")]
        ]
        if settings.trial.enabled and not user.trial_used:
            rows.append(
                [InlineKeyboardButton(text="🚀 Попробовать бесплатно", callback_data="menu:trial")]
            )
        rows.append([back_btn("menu:main", "⬅️ В меню")])
        await _edit(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))
        return

    parts = [f"👤 <b>Мой VPN</b>\n\n💰 Баланс: <b>{user.balance} ₽</b>"]
    rows = []
    for sub in subs:
        parts.append(_sub_summary(sub))
        if len(subs) > 1:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"ℹ️ {_server_name(sub.server_key)}",
                        callback_data=f"prof:sub:{sub.id}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(text="🔗 Подписка", url=page_url(sub.uuid)),
                InlineKeyboardButton(
                    text="🔄 Продлить", callback_data=f"prof:renew:{sub.server_key}"
                ),
            ]
        )
    rows.append([back_btn("menu:main", "⬅️ В меню")])
    await _edit(cb, "\n\n".join(parts), InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("prof:sub:"))
async def cb_sub_details(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    try:
        sub_id = int(cb.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer()
        return

    sub = await session.get(Subscription, sub_id)
    if sub is None or sub.user_id != user.id:
        await cb.answer("Подписка не найдена 🤷", show_alert=True)
        return

    settings = get_settings()
    active = sub.expires_at > utcnow()
    plan = settings.plan(sub.plan_id) if sub.plan_id else None

    lines = [
        f"{'🟢 Активна' if active else '🔴 Истекла'} · <b>{_server_name(sub.server_key)}</b>",
        "",
        f"⏳ Действует до: <b>{_fmt_msk(sub.expires_at)} МСК</b>",
    ]
    if active:
        lines.append(f"📅 Осталось: {_days_left(sub.expires_at)} дн.")
    if plan is not None:
        lines.append(f"📦 Тариф: {plan.title}")
    lines.append(f"📱 Устройств: {sub.devices if sub.devices > 0 else 'без ограничений'}")

    try:
        client = await get_vpn(sub.server_key).get_client(sub.client_name)
        if client is not None:
            used, limit = extract_traffic(client)
            if used is not None and limit is not None:
                lines.append(f"📶 Трафик: {used:.1f} / {limit:.0f} ГБ")
            elif used is not None:
                lines.append(f"📶 Трафик: {used:.1f} ГБ (безлимит)")
    except (VpnAPIError, ValueError):
        logger.exception("Failed to fetch live client data for sub %s", sub.id)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 Настроить VPN", url=page_url(sub.uuid))],
            [
                InlineKeyboardButton(
                    text="🔄 Продлить", callback_data=f"prof:renew:{sub.server_key}"
                )
            ],
            [back_btn("menu:profile"), back_btn("menu:main", "⬅️ В меню")],
        ]
    )
    await _edit(cb, "\n".join(lines), kb)


@router.callback_query(F.data.startswith("prof:renew:"))
async def cb_renew(cb: CallbackQuery, user: User) -> None:
    server_key = cb.data.split(":", 2)[2]
    settings = get_settings()
    server = settings.server(server_key)
    if server is None:
        await cb.answer("Локация больше недоступна 🤷", show_alert=True)
        return

    rows: list[list[InlineKeyboardButton]] = []
    for plan in settings.plans:
        stars = settings.stars_price(plan)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan.title} — {plan.price_rub} ₽ / {stars} ⭐",
                    callback_data=f"buy:plan:{plan.id}:{server_key}",
                )
            ]
        )
    rows.append([back_btn("menu:profile"), back_btn("menu:main", "⬅️ В меню")])

    text = (
        f"🔄 <b>Продление · {server.name}</b>\n\n"
        "Выберите тариф — дни добавятся к текущей подписке, ничего не сгорит 👇"
    )
    await _edit(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))
