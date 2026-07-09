from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from aiogram import Bot

    from app.db.models import Payment


class PayResult(NamedTuple):
    url: str | None
    external_id: str | None


class PaymentProvider(Protocol):
    slug: str
    title: str

    def available(self) -> bool:
        ...

    async def create_invoice(self, bot: Bot, chat_id: int, payment: Payment) -> PayResult:
        ...
