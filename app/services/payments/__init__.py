from __future__ import annotations

from app.services.payments.base import PaymentProvider, PayResult
from app.services.payments.cryptobot import CryptoBotError, CryptoBotProvider
from app.services.payments.stars import StarsProvider


def get_providers() -> list[PaymentProvider]:
    providers: list[PaymentProvider] = [StarsProvider()]
    cryptobot = CryptoBotProvider()
    if cryptobot.available():
        providers.append(cryptobot)
    return providers


__all__ = [
    "PayResult",
    "PaymentProvider",
    "StarsProvider",
    "CryptoBotProvider",
    "CryptoBotError",
    "get_providers",
]
