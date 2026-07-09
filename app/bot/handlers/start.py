from __future__ import annotations

import html
import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import back_btn, main_menu
from app.config import get_settings
from app.db.models import User
from app.services.subscriptions import issue_trial, page_url
from app.vpn import VpnAPIError

logger = logging.getLogger(__name__)

router = Router(name="start")

API_ERROR_TEXT = "⚠️ Сервер временно недоступен, попробуйте позже."


def _greeting(user: User) -> str:
    settings = get_settings()
    max_devices = max((p.devices for p in settings.plans), default=0)
    if max_devices > 0:
        devices_line = f"📱 До {max_devices} устройств на одной подписке"
    else:
        devices_line = "📱 Несколько устройств на одной подписке"
    locations_line = (
        "🌍 Несколько локаций на выбор"
        if len(settings.servers) > 1
        else "🌍 Быстрые сервера и стабильное соединение"
    )
    name = html.escape(user.full_name or user.username or "друг")
    return (
        f"👋 Привет, <b>{name}</b>!\n\n"
        f"Это <b>{settings.shop_name}</b> — быстрый VPN на современном протоколе VLESS.\n\n"
        "🚀 Высокая скорость без ограничений\n"
        f"{locations_line}\n"
        f"{devices_line}\n"
        "🆘 Живая поддержка, если что-то пойдёт не так\n\n"
        "Выберите действие в меню ниже 👇"
    )


def _menu_text(user: User) -> str:
    settings = get_settings()
    return (
        f"<b>{settings.shop_name}</b>\n\n"
        f"💰 Баланс: <b>{user.balance} ₽</b>\n\n"
        "Выберите действие 👇"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, user: User) -> None:
    await message.answer(_greeting(user), reply_markup=main_menu(user, get_settings()))


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(cb: CallbackQuery, user: User) -> None:
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_text(
                _menu_text(user), reply_markup=main_menu(user, get_settings())
            )
        except TelegramBadRequest:
            pass
    await cb.answer()


@router.callback_query(F.data == "menu:trial")
async def cb_trial(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    settings = get_settings()
    if not settings.trial.enabled:
        await cb.answer("🚫 Пробный период сейчас недоступен.", show_alert=True)
        return
    if user.trial_used:
        await cb.answer("Вы уже использовали пробный период 🙂", show_alert=True)
        return

    try:
        sub = await issue_trial(session, user)
    except ValueError:
        await cb.answer("Вы уже использовали пробный период 🙂", show_alert=True)
        return
    except VpnAPIError:
        logger.exception("Failed to issue trial for user %s", user.id)
        await cb.answer()
        if isinstance(cb.message, Message):
            await cb.message.answer(API_ERROR_TEXT)
        return

    expires_msk = (sub.expires_at + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    traffic = f"{settings.trial.traffic_gb} ГБ" if settings.trial.traffic_gb > 0 else "Безлимит"
    text = (
        "🎉 <b>Пробный период активирован!</b>\n\n"
        f"⏳ Действует до: <b>{expires_msk} МСК</b> ({settings.trial.days} дн.)\n"
        f"📶 Трафик: {traffic}\n"
        f"📱 Устройств: {settings.trial.devices}\n\n"
        "Нажмите кнопку ниже — откроется страница с подпиской и пошаговой настройкой 👇"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📱 Настроить VPN", url=page_url(sub.uuid))],
            [back_btn("menu:main", "⬅️ В меню")],
        ]
    )
    await cb.answer("🎉 Готово!")
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await cb.message.answer(text, reply_markup=kb)
