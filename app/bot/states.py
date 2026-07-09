from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class PromoStates(StatesGroup):
    waiting_code = State()


class TopupStates(StatesGroup):
    waiting_amount = State()


class AdminStates(StatesGroup):
    waiting_broadcast = State()
    waiting_promo = State()
    waiting_user_query = State()
    waiting_user_days = State()
    waiting_balance_delta = State()
