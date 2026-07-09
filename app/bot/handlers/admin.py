from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.states import AdminStates
from app.config import get_settings
from app.db.models import Payment, PromoActivation, Promocode, User, utcnow
from app.db.repo import add_balance, count_stats, get_subscriptions, get_user
from app.services.subscriptions import issue_or_renew
from app.vpn import VpnAPIError, get_vpn

logger = logging.getLogger(__name__)


class AdminFilter(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return (
            event.from_user is not None
            and event.from_user.id in get_settings().admin_ids
        )


router = Router(name="admin")

panel = Router(name="admin_panel")
panel.message.filter(AdminFilter())
panel.callback_query.filter(AdminFilter(), F.data.startswith("adm:"))

denied = Router(name="admin_denied")

router.include_router(panel)
router.include_router(denied)


@denied.callback_query(F.data.startswith("adm:"))
async def adm_denied(cb: CallbackQuery) -> None:
    await cb.answer("⛔ Эта кнопка доступна только администраторам.", show_alert=True)


_PANEL_TEXT = "🛠 <b>Админ-панель</b>\n\nВыберите раздел:"

_STATUS_EMOJI = {"paid": "✅", "pending": "⏳", "canceled": "❌"}


def _fmt_msk(dt: datetime) -> str:
    return (dt + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")


def _panel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Статистика", callback_data="adm:stats")
    kb.button(text="📣 Рассылка", callback_data="adm:bcast")
    kb.button(text="🎟 Промокоды", callback_data="adm:promo")
    kb.button(text="👥 Пользователи", callback_data="adm:users")
    kb.button(text="💳 Платежи", callback_data="adm:payments")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def _back_kb(callback_data: str = "adm:menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)]
        ]
    )


async def _show(cb: CallbackQuery, bot: Bot, text: str, markup: InlineKeyboardMarkup) -> None:
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
    await bot.send_message(cb.from_user.id, text, reply_markup=markup)


def _status_num(status: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = status.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


@panel.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(_PANEL_TEXT, reply_markup=_panel_kb())


@panel.callback_query(F.data == "adm:menu")
async def adm_menu(cb: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await state.clear()
    await _show(cb, bot, _PANEL_TEXT, _panel_kb())
    await cb.answer()


@panel.callback_query(F.data == "adm:stats")
async def adm_stats(cb: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    await cb.answer()
    stats = await count_stats(session)
    lines = [
        "📊 <b>Статистика</b>",
        "",
        f"👥 Пользователей: <b>{stats['users_total']}</b> (+{stats['users_today']} сегодня)",
        f"🔑 Активных подписок: <b>{stats['active_subs']}</b>",
        "",
        "💰 Выручка:",
        f" · сегодня: <b>{stats['revenue_today']} ₽</b>",
        f" · за месяц: <b>{stats['revenue_month']} ₽</b>",
        f" · всего: <b>{stats['revenue_total']} ₽</b>",
        "",
        "🌍 Серверы:",
    ]
    for server in get_settings().servers:
        try:
            status = await get_vpn(server.key).status()
        except VpnAPIError:
            logger.exception("Failed to fetch status of server %s", server.key)
            lines.append(f" · {escape(server.name)} — ⚠️ недоступен")
            continue
        clients = _status_num(
            status,
            ("clients", "clients_total", "total_clients", "clients_count", "users", "total"),
        )
        online = _status_num(
            status,
            ("online", "online_clients", "clients_online", "online_count", "active"),
        )
        clients_str = str(clients) if clients is not None else "—"
        online_str = str(online) if online is not None else "—"
        lines.append(
            f" · {escape(server.name)} — клиентов: <b>{clients_str}</b>, онлайн: <b>{online_str}</b>"
        )
    await _show(cb, bot, "\n".join(lines), _back_kb())


@panel.callback_query(F.data == "adm:bcast")
async def adm_bcast(cb: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_broadcast)
    await _show(
        cb,
        bot,
        "📣 <b>Рассылка</b>\n\n"
        "Отправьте сообщение (текст, фото, видео — что угодно), "
        "и я разошлю его всем пользователям бота.",
        _back_kb(),
    )
    await cb.answer()


@panel.message(AdminStates.waiting_broadcast)
async def adm_bcast_run(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
) -> None:
    await state.clear()
    user_ids = list((await session.execute(select(User.id))).scalars().all())
    status_msg = await message.answer(
        f"🚀 Начинаю рассылку на {len(user_ids)} пользователей…"
    )

    sent = failed = 0
    for uid in user_ids:
        try:
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            try:
                await bot.copy_message(
                    chat_id=uid,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )
                sent += 1
            except TelegramAPIError:
                failed += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        except TelegramAPIError:
            logger.exception("Broadcast: failed to send to %s", uid)
            failed += 1
        await asyncio.sleep(0.05)

    report = (
        "✅ <b>Рассылка завершена!</b>\n\n"
        f"📬 Отправлено: <b>{sent}</b>\n"
        f"🚫 Ошибок (блокировки и т.п.): <b>{failed}</b>"
    )
    try:
        await status_msg.edit_text(report, reply_markup=_back_kb())
    except TelegramBadRequest:
        await message.answer(report, reply_markup=_back_kb())


_PROMO_HINT = (
    "Чтобы создать промокод, отправьте сообщение в формате:\n"
    "<code>КОД тип значение [макс_использований] [дней_действия]</code>\n"
    "Тип: <code>баланс</code> или <code>дни</code>.\n\n"
    "Примеры:\n"
    "<code>SUMMER баланс 100</code> — +100 ₽ на баланс, без ограничений\n"
    "<code>WEEK дни 7 50 30</code> — +7 дней подписки, 50 активаций, действует 30 дней"
)

_PROMO_KINDS = {
    "баланс": "balance",
    "balance": "balance",
    "дни": "days",
    "дней": "days",
    "день": "days",
    "days": "days",
}


async def _promo_view(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    promos = list(
        (
            await session.execute(
                select(Promocode).order_by(Promocode.created_at.desc())
            )
        ).scalars().all()
    )
    lines = ["🎟 <b>Промокоды</b>", ""]
    kb = InlineKeyboardBuilder()
    if not promos:
        lines.append("Пока нет ни одного промокода.")
    else:
        for promo in promos:
            kind = "баланс" if promo.kind == "balance" else "дни"
            unit = "₽" if promo.kind == "balance" else "дн."
            uses = f"{promo.uses}/{promo.max_uses if promo.max_uses else '∞'}"
            until = f" · до {_fmt_msk(promo.expires_at)}" if promo.expires_at else ""
            lines.append(
                f"<code>{escape(promo.code)}</code> — {kind} {promo.value} {unit}"
                f" · {uses}{until}"
            )
            kb.button(
                text=f"🗑 {promo.code}",
                callback_data=f"adm:promo_del:{promo.id}",
            )
    lines += ["", _PROMO_HINT]
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:menu"))
    return "\n".join(lines), kb.as_markup()


@panel.callback_query(F.data == "adm:promo")
async def adm_promo(
    cb: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    await state.set_state(AdminStates.waiting_promo)
    text, kb = await _promo_view(session)
    await _show(cb, bot, text, kb)
    await cb.answer()


@panel.callback_query(F.data.startswith("adm:promo_del:"))
async def adm_promo_del(
    cb: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    promo_id = int((cb.data or "").split(":")[2])
    promo = await session.get(Promocode, promo_id)
    if promo is not None:
        await session.execute(
            delete(PromoActivation).where(PromoActivation.promo_id == promo_id)
        )
        await session.delete(promo)
        await session.flush()
        await cb.answer("🗑 Промокод удалён")
    else:
        await cb.answer("Этот промокод уже удалён")
    await state.set_state(AdminStates.waiting_promo)
    text, kb = await _promo_view(session)
    await _show(cb, bot, text, kb)


@panel.message(AdminStates.waiting_promo, F.text)
async def adm_promo_create(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(f"🤔 Не понял формат.\n\n{_PROMO_HINT}", reply_markup=_back_kb())
        return

    code = parts[0].upper()
    if len(code) > 64:
        await message.answer("😅 Слишком длинный код — максимум 64 символа.")
        return

    kind = _PROMO_KINDS.get(parts[1].lower())
    if kind is None:
        await message.answer(
            "🤔 Неизвестный тип. Используйте <code>баланс</code> или <code>дни</code>.\n\n"
            f"{_PROMO_HINT}",
            reply_markup=_back_kb(),
        )
        return

    try:
        value = int(parts[2])
        max_uses = int(parts[3]) if len(parts) > 3 else 0
        valid_days = int(parts[4]) if len(parts) > 4 else 0
    except ValueError:
        await message.answer(
            f"🤔 Значение, лимит и срок должны быть числами.\n\n{_PROMO_HINT}",
            reply_markup=_back_kb(),
        )
        return

    if value <= 0 or max_uses < 0 or valid_days < 0:
        await message.answer("😅 Значение должно быть больше нуля, лимиты — не отрицательными.")
        return

    duplicate = (
        await session.execute(select(Promocode.id).where(Promocode.code == code))
    ).first()
    if duplicate is not None:
        await message.answer(
            f"⚠️ Промокод <code>{escape(code)}</code> уже существует. Придумайте другой код."
        )
        return

    expires_at = utcnow() + timedelta(days=valid_days) if valid_days > 0 else None
    session.add(
        Promocode(
            code=code,
            kind=kind,
            value=value,
            max_uses=max_uses,
            expires_at=expires_at,
        )
    )
    await session.flush()

    unit = "₽ на баланс" if kind == "balance" else "дн. подписки"
    uses_str = str(max_uses) if max_uses else "∞"
    until_str = f", действует до {_fmt_msk(expires_at)}" if expires_at else ""
    await message.answer(
        f"✅ Промокод <code>{escape(code)}</code> создан: "
        f"{value} {unit}, активаций: {uses_str}{until_str}."
    )
    await state.set_state(AdminStates.waiting_promo)
    text, kb = await _promo_view(session)
    await message.answer(text, reply_markup=kb)


async def _user_card(session: AsyncSession, target: User) -> tuple[str, InlineKeyboardMarkup]:
    settings = get_settings()
    now = utcnow()
    subs = await get_subscriptions(session, target.id)

    pay_count = (
        await session.execute(
            select(func.count(Payment.id)).where(Payment.user_id == target.id)
        )
    ).scalar_one()
    paid_sum = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount_rub), 0)).where(
                Payment.user_id == target.id, Payment.status == "paid"
            )
        )
    ).scalar_one()

    username = f"@{escape(target.username)}" if target.username else "—"
    lines = [
        f"👤 <b>Пользователь</b> <code>{target.id}</code>",
        f"Имя: {escape(target.full_name or '—')} · {username}",
        f"💰 Баланс: <b>{target.balance} ₽</b>",
        f"🚀 Триал: {'использован' if target.trial_used else 'доступен'}",
        f"Статус: {'🚫 забанен' if target.banned else '🟢 активен'}",
        "",
    ]
    if subs:
        lines.append("🔑 Подписки:")
        for sub in subs:
            server = settings.server(sub.server_key)
            server_name = server.name if server else sub.server_key
            emoji = "🟢" if sub.expires_at > now else "🔴"
            lines.append(
                f" · {escape(server_name)}: {emoji} до {_fmt_msk(sub.expires_at)} МСК"
            )
    else:
        lines.append("🔑 Подписок нет.")
    lines += ["", f"💳 Платежей: <b>{int(pay_count)}</b>, оплачено на <b>{int(paid_sum)} ₽</b>"]

    kb = InlineKeyboardBuilder()
    kb.button(text="📆 ± Дни", callback_data=f"adm:user_days:{target.id}")
    kb.button(text="💰 ± Баланс", callback_data=f"adm:user_bal:{target.id}")
    if target.banned:
        kb.button(text="✅ Разбанить", callback_data=f"adm:user_unban:{target.id}")
    else:
        kb.button(text="🚫 Забанить", callback_data=f"adm:user_ban:{target.id}")
    kb.button(text="🔎 Другой пользователь", callback_data="adm:users")
    kb.button(text="⬅️ В админ-меню", callback_data="adm:menu")
    kb.adjust(2, 1, 1, 1)
    return "\n".join(lines), kb.as_markup()


@panel.callback_query(F.data == "adm:users")
async def adm_users(cb: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_user_query)
    await _show(
        cb,
        bot,
        "👥 <b>Пользователи</b>\n\nОтправьте ID или @username пользователя:",
        _back_kb(),
    )
    await cb.answer()


@panel.message(AdminStates.waiting_user_query, F.text)
async def adm_user_query(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    query = (message.text or "").strip()
    target: User | None = None
    if query.lstrip("-").isdigit():
        target = await get_user(session, int(query))
    else:
        username = query.lstrip("@").lower()
        if username:
            target = (
                await session.execute(
                    select(User).where(func.lower(User.username) == username)
                )
            ).scalars().first()

    if target is None:
        await message.answer(
            "🤷 Пользователь не найден. Проверьте ID/@username и попробуйте ещё раз:",
            reply_markup=_back_kb(),
        )
        return

    await state.clear()
    text, kb = await _user_card(session, target)
    await message.answer(text, reply_markup=kb)


@panel.callback_query(F.data.startswith("adm:user:"))
async def adm_user_card(
    cb: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    state: FSMContext,
) -> None:
    user_id = int((cb.data or "").split(":")[2])
    target = await get_user(session, user_id)
    if target is None:
        await cb.answer("Пользователь не найден", show_alert=True)
        return
    await state.clear()
    text, kb = await _user_card(session, target)
    await _show(cb, bot, text, kb)
    await cb.answer()


@panel.callback_query(F.data.startswith("adm:user_days:"))
async def adm_user_days(cb: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    user_id = int((cb.data or "").split(":")[2])
    await state.set_state(AdminStates.waiting_user_days)
    await state.update_data(target_id=user_id)
    await _show(
        cb,
        bot,
        f"📆 Пользователь <code>{user_id}</code>\n\n"
        "Отправьте число дней: положительное — продлить, отрицательное — сократить.\n"
        "Например: <code>30</code> или <code>-7</code>",
        _back_kb(f"adm:user:{user_id}"),
    )
    await cb.answer()


@panel.message(AdminStates.waiting_user_days, F.text)
async def adm_user_days_apply(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    user_id = int(data.get("target_id", 0))
    target = await get_user(session, user_id)
    if target is None:
        await state.clear()
        await message.answer("🤷 Пользователь не найден.", reply_markup=_back_kb())
        return

    try:
        delta = int((message.text or "").strip())
    except ValueError:
        await message.answer(
            "🤔 Нужно целое число дней, например <code>30</code> или <code>-7</code>."
        )
        return
    if delta == 0:
        await message.answer("Ноль дней ничего не изменит 🙂 Отправьте другое число.")
        return

    settings = get_settings()
    subs = await get_subscriptions(session, user_id)
    if subs:
        sub = subs[0]
        server_key, traffic_gb, devices = sub.server_key, sub.traffic_gb, sub.devices
    else:
        server_key = settings.servers[0].key
        traffic_gb, devices = settings.trial.traffic_gb, settings.trial.devices

    try:
        sub = await issue_or_renew(
            session,
            user_id=user_id,
            server_key=server_key,
            days=delta,
            traffic_gb=traffic_gb,
            devices=devices,
        )
    except VpnAPIError:
        logger.exception(
            "Admin ±days: VPN API error for user %s on server %s", user_id, server_key
        )
        await message.answer("⚠️ Сервер временно недоступен, попробуйте позже.")
        return

    await state.clear()
    text, kb = await _user_card(session, target)
    sign = "+" if delta > 0 else ""
    await message.answer(
        f"✅ Готово! {sign}{delta} дн. — подписка действует до "
        f"{_fmt_msk(sub.expires_at)} МСК.\n\n{text}",
        reply_markup=kb,
    )


@panel.callback_query(F.data.startswith("adm:user_bal:"))
async def adm_user_bal(cb: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    user_id = int((cb.data or "").split(":")[2])
    await state.set_state(AdminStates.waiting_balance_delta)
    await state.update_data(target_id=user_id)
    await _show(
        cb,
        bot,
        f"💰 Пользователь <code>{user_id}</code>\n\n"
        "Отправьте сумму изменения баланса в ₽ (можно отрицательную).\n"
        "Например: <code>100</code> или <code>-50</code>",
        _back_kb(f"adm:user:{user_id}"),
    )
    await cb.answer()


@panel.message(AdminStates.waiting_balance_delta, F.text)
async def adm_user_bal_apply(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    user_id = int(data.get("target_id", 0))
    target = await get_user(session, user_id)
    if target is None:
        await state.clear()
        await message.answer("🤷 Пользователь не найден.", reply_markup=_back_kb())
        return

    try:
        delta = int((message.text or "").strip())
    except ValueError:
        await message.answer(
            "🤔 Нужно целое число, например <code>100</code> или <code>-50</code>."
        )
        return
    if delta == 0:
        await message.answer("Ноль ничего не изменит 🙂 Отправьте другое число.")
        return

    new_balance = await add_balance(session, user_id, delta)
    await state.clear()
    text, kb = await _user_card(session, target)
    sign = "+" if delta > 0 else ""
    await message.answer(
        f"✅ Баланс изменён ({sign}{delta} ₽). Новый баланс: <b>{new_balance} ₽</b>\n\n{text}",
        reply_markup=kb,
    )


async def _set_ban_on_servers(user_id: int, session: AsyncSession, ban: bool) -> None:
    subs = await get_subscriptions(session, user_id)
    for sub in subs:
        try:
            client = get_vpn(sub.server_key)
            if ban:
                await client.ban(sub.client_name, reason="banned by admin")
            else:
                await client.unban(sub.client_name)
        except Exception:
            logger.exception(
                "Failed to %s client %s on server %s",
                "ban" if ban else "unban", sub.client_name, sub.server_key,
            )


@panel.callback_query(F.data.startswith("adm:user_ban:"))
async def adm_user_ban(cb: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    user_id = int((cb.data or "").split(":")[2])
    target = await get_user(session, user_id)
    if target is None:
        await cb.answer("Пользователь не найден", show_alert=True)
        return
    target.banned = True
    await session.flush()
    await _set_ban_on_servers(user_id, session, ban=True)
    await cb.answer("🚫 Пользователь забанен")
    text, kb = await _user_card(session, target)
    await _show(cb, bot, text, kb)


@panel.callback_query(F.data.startswith("adm:user_unban:"))
async def adm_user_unban(cb: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    user_id = int((cb.data or "").split(":")[2])
    target = await get_user(session, user_id)
    if target is None:
        await cb.answer("Пользователь не найден", show_alert=True)
        return
    target.banned = False
    await session.flush()
    await _set_ban_on_servers(user_id, session, ban=False)
    await cb.answer("✅ Пользователь разбанен")
    text, kb = await _user_card(session, target)
    await _show(cb, bot, text, kb)


@panel.callback_query(F.data == "adm:payments")
async def adm_payments(cb: CallbackQuery, bot: Bot, session: AsyncSession) -> None:
    payments = list(
        (
            await session.execute(
                select(Payment).order_by(Payment.id.desc()).limit(15)
            )
        ).scalars().all()
    )
    lines = ["💳 <b>Последние платежи</b>", ""]
    if not payments:
        lines.append("Платежей пока нет.")
    for payment in payments:
        kind = "тариф" if payment.kind == "plan" else "пополнение"
        emoji = _STATUS_EMOJI.get(payment.status, "❓")
        lines.append(
            f"#{payment.id} · <code>{payment.user_id}</code> · {kind}"
            f" · {payment.provider or '—'} · {payment.amount_rub} ₽"
            f" · {emoji} · {_fmt_msk(payment.created_at)}"
        )
    await _show(cb, bot, "\n".join(lines), _back_kb())
    await cb.answer()


@panel.message(
    StateFilter(
        AdminStates.waiting_promo,
        AdminStates.waiting_user_query,
        AdminStates.waiting_user_days,
        AdminStates.waiting_balance_delta,
    )
)
async def adm_expects_text(message: Message) -> None:
    await message.answer("✍️ Отправьте текстовое сообщение.", reply_markup=_back_kb())
