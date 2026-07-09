from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.types import LabeledPrice

from app.config import get_settings
from app.services.payments.base import PayResult

if TYPE_CHECKING:
    from aiogram import Bot

    from app.db.models import Payment

logger = logging.getLogger(__name__)


class StarsProvider:
    slug = "stars"
    title = "⭐ Telegram Stars"

    def available(self) -> bool:
        return True

    def stars_amount(self, payment: Payment) -> int:
        settings = get_settings()
        if payment.kind == "plan" and payment.plan_id:
            plan = settings.plan(payment.plan_id)
            if plan is not None:
                return settings.stars_price(plan)
        return settings.stars_price(payment.amount_rub)

    async def create_invoice(self, bot: Bot, chat_id: int, payment: Payment) -> PayResult:
        settings = get_settings()
        stars = self.stars_amount(payment)

        if payment.kind == "plan":
            plan = settings.plan(payment.plan_id or "")
            title = f"Подписка: {plan.title}" if plan is not None else "Подписка VPN"
            description = f"Доступ к VPN · {settings.shop_name}"
        else:
            title = f"Пополнение на {payment.amount_rub} ₽"
            description = f"Пополнение баланса · {settings.shop_name}"

        await bot.send_invoice(
            chat_id=chat_id,
            title=title[:32],
            description=description[:255],
            payload=f"pay:{payment.id}",
            currency="XTR",
            prices=[LabeledPrice(label=title[:32], amount=stars)],
        )
        logger.info("Sent Stars invoice for payment %s (%s ⭐)", payment.id, stars)
        return PayResult(url=None, external_id=None)
