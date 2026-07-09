from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Payment, Subscription, User, utcnow

if TYPE_CHECKING:
    from aiogram.types import User as TgUser


async def get_or_create_user(
    session: AsyncSession,
    tg_user: TgUser,
    referrer_id: int | None = None,
) -> tuple[User, bool]:
    user = await session.get(User, tg_user.id)
    if user is not None:
        if user.username != tg_user.username or user.full_name != tg_user.full_name:
            user.username = tg_user.username
            user.full_name = tg_user.full_name
        return user, False

    if referrer_id == tg_user.id:
        referrer_id = None
    if referrer_id is not None and await session.get(User, referrer_id) is None:
        referrer_id = None

    user = User(
        id=tg_user.id,
        username=tg_user.username,
        full_name=tg_user.full_name,
        referrer_id=referrer_id,
    )
    session.add(user)
    await session.flush()
    return user, True


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def add_balance(session: AsyncSession, user_id: int, delta: int) -> int:
    user = await session.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found")
    user.balance = max(0, user.balance + delta)
    await session.flush()
    return user.balance


async def get_subscription(
    session: AsyncSession, user_id: int, server_key: str
) -> Subscription | None:
    result = await session.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.server_key == server_key,
        )
    )
    return result.scalar_one_or_none()


async def get_subscriptions(session: AsyncSession, user_id: int) -> list[Subscription]:
    result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
    )
    return list(result.scalars().all())


async def get_subscription_by_uuid(session: AsyncSession, uuid: str) -> Subscription | None:
    result = await session.execute(select(Subscription).where(Subscription.uuid == uuid))
    return result.scalars().first()


async def upsert_subscription(
    session: AsyncSession,
    user_id: int,
    server_key: str,
    client_name: str,
    uuid: str,
    expires_at: datetime,
    traffic_gb: int,
    devices: int,
    plan_id: str | None = None,
) -> Subscription:
    sub = await get_subscription(session, user_id, server_key)
    if sub is None:
        sub = Subscription(
            user_id=user_id,
            server_key=server_key,
            client_name=client_name,
            uuid=uuid,
            plan_id=plan_id,
            expires_at=expires_at,
            traffic_gb=traffic_gb,
            devices=devices,
        )
        session.add(sub)
    else:
        sub.client_name = client_name
        sub.uuid = uuid
        sub.expires_at = expires_at
        sub.traffic_gb = traffic_gb
        sub.devices = devices
        if plan_id is not None:
            sub.plan_id = plan_id
        sub.updated_at = utcnow()
    sub.reminded_3d = False
    sub.reminded_1d = False
    sub.notified_expired = False
    await session.flush()
    return sub


async def create_payment(
    session: AsyncSession,
    user_id: int,
    kind: str,
    amount_rub: int,
    provider: str = "",
    plan_id: str | None = None,
    server_key: str | None = None,
    external_id: str | None = None,
) -> Payment:
    payment = Payment(
        user_id=user_id,
        provider=provider,
        kind=kind,
        amount_rub=amount_rub,
        plan_id=plan_id,
        server_key=server_key,
        status="pending",
        external_id=external_id,
    )
    session.add(payment)
    await session.flush()
    return payment


async def get_payment(session: AsyncSession, payment_id: int) -> Payment | None:
    return await session.get(Payment, payment_id)


async def mark_paid(session: AsyncSession, payment: Payment) -> bool:
    now = utcnow()
    result = await session.execute(
        update(Payment)
        .where(Payment.id == payment.id, Payment.status == "pending")
        .values(status="paid", paid_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        return False
    payment.status = "paid"
    payment.paid_at = now
    return True


PENDING_INVOICE_TTL = timedelta(hours=24)


async def pending_cryptobot_payments(session: AsyncSession) -> list[Payment]:
    cutoff = utcnow() - PENDING_INVOICE_TTL
    result = await session.execute(
        select(Payment).where(
            Payment.provider == "cryptobot",
            Payment.status == "pending",
            Payment.external_id.is_not(None),
            Payment.created_at >= cutoff,
        )
    )
    return list(result.scalars().all())


async def cancel_stale_cryptobot_payments(session: AsyncSession) -> int:
    cutoff = utcnow() - PENDING_INVOICE_TTL
    result = await session.execute(
        update(Payment)
        .where(
            Payment.provider == "cryptobot",
            Payment.status == "pending",
            Payment.created_at < cutoff,
        )
        .values(status="canceled")
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)


async def count_stats(session: AsyncSession) -> dict[str, Any]:
    now = utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    users_total = (await session.execute(select(func.count(User.id)))).scalar_one()
    users_today = (
        await session.execute(select(func.count(User.id)).where(User.created_at >= day_start))
    ).scalar_one()
    active_subs = (
        await session.execute(
            select(func.count(Subscription.id)).where(Subscription.expires_at > now)
        )
    ).scalar_one()

    async def _revenue(since: datetime | None) -> int:
        stmt = select(func.coalesce(func.sum(Payment.amount_rub), 0)).where(
            Payment.status == "paid"
        )
        if since is not None:
            stmt = stmt.where(Payment.paid_at >= since)
        return int((await session.execute(stmt)).scalar_one())

    return {
        "users_total": int(users_total),
        "users_today": int(users_today),
        "active_subs": int(active_subs),
        "revenue_today": await _revenue(day_start),
        "revenue_month": await _revenue(month_start),
        "revenue_total": await _revenue(None),
    }
