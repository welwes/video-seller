from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.db.models import User


def back_btn(cb: str, text: str = "⬅️ Назад") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[back_btn("menu:main", "⬅️ В меню")]]
    )


def main_menu(user: User, settings: Settings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="🛒 Купить VPN", callback_data="menu:buy"),
            InlineKeyboardButton(text="👤 Мой VPN", callback_data="menu:profile"),
        ],
        [
            InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="menu:topup"),
            InlineKeyboardButton(text="🎁 Промокод", callback_data="menu:promo"),
        ],
        [
            InlineKeyboardButton(text="👥 Партнёрка", callback_data="menu:ref"),
            InlineKeyboardButton(text="🆘 Помощь", callback_data="menu:help"),
        ],
    ]
    if settings.trial.enabled and not user.trial_used:
        rows.append(
            [InlineKeyboardButton(text="🚀 Попробовать бесплатно", callback_data="menu:trial")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
