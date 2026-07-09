from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from app.config import get_settings
from app.db.repo import cancel_stale_cryptobot_payments, pending_cryptobot_payments
from app.services.payments.base import PayResult

if TYPE_CHECKING:
    from aiogram import Bot
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.db.models import Payment

logger = logging.getLogger(__name__)

API_BASE = "https://pay.crypt.bot/api/"
_TIMEOUT = aiohttp.ClientTimeout(total=15)


class CryptoBotError(Exception):
    pass


async def _call(method: str, params: dict[str, Any] | None = None) -> Any:
    token = get_settings().cryptobot_token
    if not token:
        raise CryptoBotError("CRYPTOBOT_TOKEN is not configured")
    url = API_BASE + method
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
            async with http.post(
                url, json=params or {}, headers={"Crypto-Pay-API-Token": token}
            ) as resp:
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise CryptoBotError(f"{method} request failed: {exc}") from exc

    if not isinstance(data, dict) or not data.get("ok"):
        error = data.get("error") if isinstance(data, dict) else data
        raise CryptoBotError(f"{method} returned error: {error!r}")
    return data.get("result")


class CryptoBotProvider:
    slug = "crypto"
    title = "💎 CryptoBot"

    def available(self) -> bool:
        return bool(get_settings().cryptobot_token)

    async def create_invoice(self, bot: Bot, chat_id: int, payment: Payment) -> PayResult:
        settings = get_settings()
        if payment.kind == "plan":
            plan = settings.plan(payment.plan_id or "")
            description = (
                f"{settings.shop_name}: подписка «{plan.title}»"
                if plan is not None
                else f"{settings.shop_name}: подписка VPN"
            )
        else:
            description = f"{settings.shop_name}: пополнение баланса на {payment.amount_rub} ₽"

        result = await _call(
            "createInvoice",
            {
                "currency_type": "fiat",
                "fiat": "RUB",
                "amount": str(payment.amount_rub),
                "payload": f"pay:{payment.id}",
                "description": description[:1024],
            },
        )
        if not isinstance(result, dict):
            raise CryptoBotError(f"createInvoice returned unexpected result: {result!r}")

        url = (
            result.get("bot_invoice_url")
            or result.get("mini_app_invoice_url")
            or result.get("pay_url")
        )
        invoice_id = result.get("invoice_id")
        if not url or invoice_id is None:
            raise CryptoBotError(f"createInvoice result missing url/invoice_id: {result!r}")
        logger.info("Created CryptoBot invoice %s for payment %s", invoice_id, payment.id)
        return PayResult(url=str(url), external_id=str(invoice_id))


async def check_invoice(external_id: str) -> bool:
    result = await _call("getInvoices", {"invoice_ids": external_id})
    if isinstance(result, dict):
        items = result.get("items") or []
    elif isinstance(result, list):
        items = result
    else:
        items = []
    for invoice in items:
        if isinstance(invoice, dict) and str(invoice.get("invoice_id")) == str(external_id):
            return invoice.get("status") == "paid"
    return False


async def poll_pending(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    from app.services.subscriptions import apply_paid_plan

    async with session_factory() as session:
        canceled = await cancel_stale_cryptobot_payments(session)
        if canceled:
            await session.commit()
            logger.info("Canceled %d stale pending CryptoBot invoice(s)", canceled)
        payments = await pending_cryptobot_payments(session)
        for payment in payments:
            try:
                paid = await check_invoice(payment.external_id or "")
            except CryptoBotError as exc:
                logger.warning("CryptoBot check failed for payment %s: %s", payment.id, exc)
                continue
            if not paid:
                continue
            try:
                await apply_paid_plan(session, bot, payment)
                await session.commit()
            except Exception:
                logger.exception("Failed to apply paid CryptoBot payment %s", payment.id)
                await session.rollback()
