from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import handlers
from app.bot.middlewares import DbSessionMiddleware, UserMiddleware
from app.bot.tasks import scheduler
from app.config import get_settings
from app.db.base import init_db
from app.vpn import close_all

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    await init_db()

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())

    db_middleware = DbSessionMiddleware()
    user_middleware = UserMiddleware()
    dp.message.outer_middleware(db_middleware)
    dp.message.outer_middleware(user_middleware)
    dp.callback_query.outer_middleware(db_middleware)
    dp.callback_query.outer_middleware(user_middleware)
    dp.pre_checkout_query.outer_middleware(db_middleware)

    dp.include_router(handlers.router)

    scheduler_task = asyncio.create_task(scheduler(bot), name="scheduler")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Starting polling as %s", settings.shop_name)
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        await close_all()
        with contextlib.suppress(Exception):
            await bot.session.close()
        logger.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
