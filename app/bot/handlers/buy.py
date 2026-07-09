from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
)

from app.config import Plan, Server, Settings, get_settings
from app.db import repo
from app.services.payments.cryptobot import CryptoBotError, CryptoBotProvider, check_invoice
from app.services.payments.stars import StarsProvider
from app.services.subscriptions import apply_paid_plan

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import Payment, User

logger = logging.getLogger(__name__)

router = Router(name="buy")


def _menu_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu:main")]


def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_menu_row()])


async def safe_edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    message = cb.message
    if not isinstance(message, Message):
        return
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        logger.debug("edit_text skipped for callback %r", cb.data)


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    d = n % 10
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def _plan_summary(plan: Plan) -> str:
    traffic = "безлимит трафика" if plan.traffic_gb <= 0 else f"{plan.traffic_gb} ГБ трафика"
    if plan.devices <= 0:
        devices = "без лимита устройств"
    else:
        devices = f"{plan.devices} {_plural(plan.devices, 'устройство', 'устройства', 'устройств')}"
    return f"{traffic} · {devices}"


def payment_methods_view(
    payment: Payment, user: User, settings: Settings
) -> tuple[str, InlineKeyboardMarkup]:
    stars = StarsProvider().stars_amount(payment)

    if payment.kind == "plan":
        plan = settings.plan(payment.plan_id or "")
        server = settings.server(payment.server_key or "")
        subject = f"🛒 Тариф: «{html.escape(plan.title)}»" if plan is not None else "🛒 Подписка VPN"
        if server is not None:
            subject += f"\n📍 Локация: {html.escape(server.name)}"
        back_cb = f"buy:server:{payment.server_key}" if payment.server_key else "menu:buy"
    else:
        subject = "💰 Пополнение баланса"
        back_cb = "menu:topup"

    text = (
        "💳 <b>Оплата</b>\n\n"
        f"{subject}\n"
        f"💵 Сумма: <b>{payment.amount_rub} ₽</b>\n\n"
        "Выберите способ оплаты 👇"
    )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"⭐ Telegram Stars · {stars} ⭐",
                callback_data=f"paym:stars:{payment.id}",
            )
        ]
    ]
    if settings.cryptobot_token:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💎 CryptoBot (криптовалюта)",
                    callback_data=f"paym:crypto:{payment.id}",
                )
            ]
        )
    if payment.kind == "plan" and user.balance >= payment.amount_rub:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"💰 С баланса ({user.balance} ₽)",
                    callback_data=f"paym:balance:{payment.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb)])
    rows.append(_menu_row())
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _plans_view(server: Server, settings: Settings) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🛒 <b>Выберите тариф</b>", f"📍 Локация: {html.escape(server.name)}", ""]
    for plan in settings.plans:
        stars = settings.stars_price(plan)
        lines.append(
            f"▫️ <b>{html.escape(plan.title)}</b> — {plan.price_rub} ₽ / {stars} ⭐\n"
            f"    {_plan_summary(plan)}"
        )
    lines += ["", "Нажмите на тариф, чтобы перейти к оплате 👇"]

    rows = [
        [
            InlineKeyboardButton(
                text=f"{plan.title} · {plan.price_rub} ₽",
                callback_data=f"buy:plan:{plan.id}:{server.key}",
            )
        ]
        for plan in settings.plans
    ]
    if len(settings.servers) > 1:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:buy")])
    rows.append(_menu_row())
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "menu:buy")
async def on_menu_buy(cb: CallbackQuery) -> None:
    settings = get_settings()
    if len(settings.servers) == 1:
        text, kb = _plans_view(settings.servers[0], settings)
    else:
        text = "🌍 <b>Выберите локацию</b>\n\nГде вам нужен VPN-сервер?"
        rows = [
            [InlineKeyboardButton(text=srv.name, callback_data=f"buy:server:{srv.key}")]
            for srv in settings.servers
        ]
        rows.append(_menu_row())
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await safe_edit(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("buy:server:"))
@router.callback_query(F.data.startswith("prof:renew:"))
async def on_server_selected(cb: CallbackQuery) -> None:
    server_key = (cb.data or "").split(":", 2)[2]
    settings = get_settings()
    server = settings.server(server_key)
    if server is None:
        await cb.answer("⚠️ Локация не найдена, обновите меню", show_alert=True)
        return
    text, kb = _plans_view(server, settings)
    await safe_edit(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("buy:plan:"))
async def on_plan_selected(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    parts = (cb.data or "").split(":", 3)
    if len(parts) != 4:
        await cb.answer("⚠️ Что-то пошло не так, попробуйте ещё раз", show_alert=True)
        return
    _, _, plan_id, server_key = parts
    settings = get_settings()
    plan = settings.plan(plan_id)
    server = settings.server(server_key)
    if plan is None or server is None:
        await cb.answer("⚠️ Тариф не найден, обновите меню", show_alert=True)
        return

    payment = await repo.create_payment(
        session,
        user_id=user.id,
        kind="plan",
        amount_rub=plan.price_rub,
        plan_id=plan.id,
        server_key=server.key,
    )
    text, kb = payment_methods_view(payment, user, settings)
    await safe_edit(cb, text, kb)
    await cb.answer()


@router.callback_query(F.data.startswith("paym:"))
async def on_payment_method(
    cb: CallbackQuery, session: AsyncSession, user: User, bot: Bot
) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        await cb.answer("⚠️ Что-то пошло не так, попробуйте ещё раз", show_alert=True)
        return
    method, payment_id = parts[1], int(parts[2])

    payment = await repo.get_payment(session, payment_id)
    if payment is None or payment.user_id != user.id:
        await cb.answer("⚠️ Платёж не найден", show_alert=True)
        return
    if payment.status == "paid":
        await cb.answer("✅ Этот платёж уже оплачен", show_alert=True)
        return
    if payment.status != "pending":
        await cb.answer("⚠️ Платёж отменён — начните заново", show_alert=True)
        return

    if method == "stars":
        payment.provider = "stars"
        payment.external_id = None
        await session.flush()
        provider = StarsProvider()
        try:
            await provider.create_invoice(bot, user.id, payment)
        except TelegramAPIError:
            logger.exception("Failed to send Stars invoice for payment %s", payment.id)
            await cb.answer("⚠️ Не удалось выставить счёт, попробуйте позже", show_alert=True)
            return
        await safe_edit(
            cb,
            f"⭐ Отправил счёт на <b>{provider.stars_amount(payment)} ⭐</b> ниже.\n"
            "Оплатите его — и всё активируется автоматически ✨",
            _menu_kb(),
        )
        await cb.answer()

    elif method == "crypto":
        provider = CryptoBotProvider()
        if not provider.available():
            await cb.answer("⚠️ Оплата криптовалютой сейчас недоступна", show_alert=True)
            return
        try:
            result = await provider.create_invoice(bot, user.id, payment)
        except CryptoBotError:
            logger.exception("Failed to create CryptoBot invoice for payment %s", payment.id)
            await cb.answer("⚠️ Не удалось создать счёт, попробуйте позже", show_alert=True)
            return
        payment.provider = "cryptobot"
        payment.external_id = result.external_id
        await session.flush()
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💎 Перейти к оплате", url=result.url or "")],
                [
                    InlineKeyboardButton(
                        text="✅ Проверить оплату", callback_data=f"paycheck:{payment.id}"
                    )
                ],
                _menu_row(),
            ]
        )
        await safe_edit(
            cb,
            f"💎 Счёт на <b>{payment.amount_rub} ₽</b> создан!\n\n"
            "1️⃣ Оплатите его по кнопке ниже\n"
            "2️⃣ Вернитесь сюда и нажмите «✅ Проверить оплату»",
            kb,
        )
        await cb.answer()

    elif method == "balance":
        if payment.kind != "plan":
            await cb.answer("⚠️ Пополнить баланс с баланса нельзя 🙂", show_alert=True)
            return
        if user.balance < payment.amount_rub:
            await cb.answer(
                f"⚠️ Недостаточно средств: на балансе {user.balance} ₽", show_alert=True
            )
            return
        payment.provider = "balance"
        await session.flush()
        if not await repo.mark_paid(session, payment):
            await cb.answer("✅ Этот платёж уже оплачен", show_alert=True)
            return
        await repo.add_balance(session, user.id, -payment.amount_rub)
        await apply_paid_plan(session, bot, payment, already_marked=True)
        await safe_edit(
            cb,
            "✅ Оплата с баланса прошла успешно! Детали отправил отдельным сообщением 👇",
            _menu_kb(),
        )
        await cb.answer("✅ Оплачено")

    else:
        await cb.answer("⚠️ Неизвестный способ оплаты", show_alert=True)


@router.callback_query(F.data.startswith("paycheck:"))
async def on_paycheck(cb: CallbackQuery, session: AsyncSession, user: User, bot: Bot) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) != 2 or not parts[1].isdigit():
        await cb.answer("⚠️ Что-то пошло не так, попробуйте ещё раз", show_alert=True)
        return

    payment = await repo.get_payment(session, int(parts[1]))
    if payment is None or payment.user_id != user.id:
        await cb.answer("⚠️ Платёж не найден", show_alert=True)
        return
    if payment.status == "paid":
        await cb.answer("✅ Этот платёж уже оплачен", show_alert=True)
        return
    if payment.status != "pending":
        await cb.answer("⚠️ Платёж отменён — начните оплату заново", show_alert=True)
        return
    if not payment.external_id:
        await cb.answer("⚠️ Счёт не найден — начните оплату заново", show_alert=True)
        return

    try:
        paid = await check_invoice(payment.external_id)
    except CryptoBotError:
        logger.exception("CryptoBot check failed for payment %s", payment.id)
        await cb.answer("⚠️ Не удалось проверить оплату, попробуйте чуть позже", show_alert=True)
        return

    if not paid:
        await cb.answer(
            "⏳ Оплата пока не поступила.\n"
            "Если вы уже оплатили — подождите минутку и нажмите ещё раз.",
            show_alert=True,
        )
        return

    await apply_paid_plan(session, bot, payment)
    await safe_edit(
        cb,
        "✅ Оплата получена, спасибо! Детали отправил отдельным сообщением 👇",
        _menu_kb(),
    )
    await cb.answer("✅ Оплата найдена!")


@router.pre_checkout_query()
async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery, session: AsyncSession) -> None:
    stale = "Счёт устарел — начните оплату заново."
    payload = pre_checkout_query.invoice_payload or ""
    if not payload.startswith("pay:") or not payload[4:].isdigit():
        await pre_checkout_query.answer(ok=False, error_message=stale)
        return
    payment = await repo.get_payment(session, int(payload[4:]))
    if (
        payment is None
        or payment.user_id != pre_checkout_query.from_user.id
        or payment.status != "pending"
    ):
        await pre_checkout_query.answer(ok=False, error_message=stale)
        return
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(
    message: Message, session: AsyncSession, user: User, bot: Bot
) -> None:
    sp = message.successful_payment
    payload = sp.invoice_payload if sp is not None else ""
    if not payload.startswith("pay:") or not payload[4:].isdigit():
        logger.error("Unexpected invoice payload: %r", payload)
        return

    payment = await repo.get_payment(session, int(payload[4:]))
    if payment is None:
        logger.error("Successful payment with unknown payment id: %r", payload)
        await message.answer(
            "⚠️ Оплата прошла, но платёж не нашёлся в системе. "
            "Напишите в поддержку — мы во всём разберёмся 🙏"
        )
        return

    if await repo.mark_paid(session, payment):
        payment.provider = "stars"
        await session.flush()
        await apply_paid_plan(session, bot, payment, already_marked=True)
        return

    logger.warning("Duplicate Stars charge for already-paid payment %s", payment.id)
    refunded = False
    if sp is not None and sp.telegram_payment_charge_id:
        try:
            refunded = await bot.refund_star_payment(
                user_id=user.id,
                telegram_payment_charge_id=sp.telegram_payment_charge_id,
            )
        except TelegramAPIError:
            logger.exception("Failed to refund duplicate Stars payment %s", payment.id)
    if refunded:
        await message.answer(
            "⚠️ Этот счёт уже был оплачен ранее, поэтому мы вернули вам ⭐ обратно."
        )
    else:
        await message.answer(
            "⚠️ Этот счёт уже был оплачен ранее. "
            "Напишите в поддержку — мы вернём ⭐ вручную 🙏"
        )
