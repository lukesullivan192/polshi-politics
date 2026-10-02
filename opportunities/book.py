"""Normalised order book: the asks for both sides of one binary market."""

from dataclasses import dataclass
from decimal import Decimal

Level = tuple[Decimal, Decimal]  # (price in dollars, quantity in contracts)


@dataclass(frozen=True)
class Book:
    yes_asks: list[Level]  # cheapest first
    no_asks: list[Level]  # cheapest first

    def asks(self, side: str) -> list[Level]:
        return self.yes_asks if side == "yes" else self.no_asks


def complement_asks(bids) -> list[Level]:
    """Bids on one side are asks on the other: a YES bid at p is a NO ask at 1 - p."""
    return sorted((1 - price, qty) for price, qty in bids if qty > 0)
