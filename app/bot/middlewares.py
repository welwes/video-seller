from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User as TgUser

from app.config import get_settings
from app.db.base import Session
from app.db.repo import get_or_create_user

logger = logging.getLogger(__name__)

_REF_RE = re.compile(r"^/start\s+ref(\d+)\s*$")


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with Session() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        session = data.get("session")
        if session is None:
            logger.error("UserMiddleware called without a DB session in data")
            return await handler(event, data)

        referrer_id: int | None = None
        if isinstance(event, Message) and event.text:
            match = _REF_RE.match(event.text)
            if match:
                referrer_id = int(match.group(1))
                if referrer_id == tg_user.id:
                    referrer_id = None

        user, created = await get_or_create_user(session, tg_user, referrer_id)
        if created:
            logger.info("New user %s (@%s), referrer=%s", user.id, user.username, user.referrer_id)

        if user.banned and user.id not in get_settings().admin_ids:
            if isinstance(event, CallbackQuery):
                with contextlib.suppress(Exception):
                    await event.answer()
            return None

        data["user"] = user
        return await handler(event, data)
