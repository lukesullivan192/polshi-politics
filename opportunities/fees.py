"""Taker fees. Both venues charge rate * contracts * price * (1 - price), rounded to the cent."""

from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal

import config
from opportunities.book import Level

CENT = Decimal("0.01")


def kalshi_fee(fills: list[Level], fee_multiplier: Decimal) -> Decimal:
    """Kalshi rounds each fee up to the cent. Each price level is treated as its own fill, which is
    the most that can be charged."""
    rate = config.KALSHI_TAKER_FEE_RATE * fee_multiplier
    return sum(
        ((rate * qty * price * (1 - price)).quantize(CENT, ROUND_CEILING) for price, qty in fills),
        Decimal(0),
    )


def polymarket_fee(fills: list[Level]) -> Decimal:
    """Polymarket US rounds to the cent with banker's rounding, and caps the commission across
    several fills at the rounded cumulative fee."""
    raw = sum((config.POLYMARKET_TAKER_FEE_RATE * qty * price * (1 - price) for price, qty in fills), Decimal(0))
    return raw.quantize(CENT, ROUND_HALF_EVEN)
