from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.handlers.buy import payment_methods_view, safe_edit
from app.bot.states import TopupStates
from app.config import Settings, get_settings
from app.db import repo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import User

logger = logging.getLogger(__name__)

router = Router(name="topup")

MIN_AMOUNT = 10
MAX_AMOUNT = 100_000


def _topup_kb(settings: Settings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for amount in settings.topup_amounts:
        row.append(InlineKeyboardButton(text=f"{amount} ₽", callback_data=f"topup:amt:{amount}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ Своя сумма", callback_data="topup:custom")])
    rows.append([InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "menu:topup")
async def on_menu_topup(cb: CallbackQuery, user: User, state: FSMContext) -> None:
    await state.clear()
    text = (
        "💳 <b>Пополнение баланса</b>\n\n"
        f"💰 Текущий баланс: <b>{user.balance} ₽</b>\n\n"
        "Балансом можно оплачивать покупку и продление подписки.\n"
        "Выберите сумму пополнения 👇"
    )
    await safe_edit(cb, text, _topup_kb(get_settings()))
    await cb.answer()


@router.callback_query(F.data.startswith("topup:amt:"))
async def on_topup_amount(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await cb.answer("⚠️ Что-то пошло не так, попробуйте ещё раз", show_alert=True)
        return
    amount = int(parts[2])
    if not MIN_AMOUNT <= amount <= MAX_AMOUNT:
        await cb.answer("⚠️ Недопустимая сумма пополнения", show_alert=True)
        return

    payment = await repo.create_payment(
        session, user_id=user.id, kind="topup", amount_rub=amount
    )
    text, kb = payment_methods_view(payment, user, get_settings())
    await safe_edit(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data == "topup:custom")
async def on_topup_custom(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TopupStates.waiting_amount)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:topup")]]
    )
    await safe_edit(
        cb,
        "✏️ Введите сумму пополнения в рублях — целое число от 10 до 100 000.\n"
        "Например: <b>300</b>",
        kb,
    )
    await cb.answer()


@router.message(TopupStates.waiting_amount)
async def on_custom_amount(
    message: Message, session: AsyncSession, user: User, state: FSMContext
) -> None:
    raw = (message.text or "").strip().replace(" ", "").lstrip("+")
    if not raw.isdigit():
        await message.answer("🙂 Пожалуйста, отправьте сумму числом, например: <b>300</b>")
        return
    amount = int(raw)
    if not MIN_AMOUNT <= amount <= MAX_AMOUNT:
        await message.answer("⚠️ Сумма должна быть от 10 до 100 000 ₽. Попробуйте ещё раз 🙂")
        return

    await state.clear()
    payment = await repo.create_payment(
        session, user_id=user.id, kind="topup", amount_rub=amount
    )
    text, kb = payment_methods_view(payment, user, get_settings())
    await message.answer(text, reply_markup=kb)
