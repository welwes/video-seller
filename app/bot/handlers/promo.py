from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import back_to_menu, main_menu
from app.bot.states import PromoStates
from app.config import get_settings
from app.db.models import User
from app.services.promo import activate

logger = logging.getLogger(__name__)

router = Router(name="promo")

_PROMPT = (
    "🎁 <b>Промокод</b>\n\n"
    "Есть промокод? Отправьте его одним сообщением — и я сразу активирую 👇"
)


@router.callback_query(F.data == "menu:promo")
async def menu_promo(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoStates.waiting_code)
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_text(_PROMPT, reply_markup=back_to_menu())
        except TelegramBadRequest:
            await cb.message.answer(_PROMPT, reply_markup=back_to_menu())
    await cb.answer()


@router.message(PromoStates.waiting_code, F.text)
async def promo_code_entered(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    user: User,
    bot: Bot,
) -> None:
    await state.clear()
    result = await activate(session, bot, user, message.text or "")
    await message.answer(result, reply_markup=main_menu(user, get_settings()))


@router.message(PromoStates.waiting_code)
async def promo_code_not_text(message: Message) -> None:
    await message.answer(
        "✍️ Отправьте промокод обычным текстом, пожалуйста 🙂",
        reply_markup=back_to_menu(),
    )
