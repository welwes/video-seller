from __future__ import annotations

from aiogram import Router

from app.bot.handlers import (
    admin,
    buy,
    help as help_,
    profile,
    promo,
    referral,
    start,
    topup,
)

router = Router(name="handlers")
router.include_router(start.router)
router.include_router(profile.router)
router.include_router(buy.router)
router.include_router(topup.router)
router.include_router(promo.router)
router.include_router(referral.router)
router.include_router(help_.router)
router.include_router(admin.router)

__all__ = ["router"]
