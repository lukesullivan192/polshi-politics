"""Normalised order book: bids and asks for both sides of one binary market."""

from dataclasses import dataclass
from decimal import Decimal

Level = tuple[Decimal, Decimal]  # (price in dollars, quantity in contracts)


@dataclass(frozen=True)
class Book:
    yes_asks: list[Level]  # cheapest first
    no_asks: list[Level]  # cheapest first
    yes_bids: list[Level]  # highest first
    no_bids: list[Level]  # highest first

    def asks(self, side: str) -> list[Level]:
        return self.yes_asks if side == "yes" else self.no_asks

    def bids(self, side: str) -> list[Level]:
        return self.yes_bids if side == "yes" else self.no_bids


def complement_asks(bids) -> list[Level]:
    """Bids on one side are asks on the other: a YES bid at p is a NO ask at 1 - p."""
    return sorted((1 - price, qty) for price, qty in bids if qty > 0)


def complement_bids(asks) -> list[Level]:
    """Asks on one side are bids on the other: a YES ask at p is a NO bid at 1 - p."""
    return sorted(((1 - price, qty) for price, qty in asks if qty > 0), reverse=True)


def sorted_bids(bids) -> list[Level]:
    return sorted(((price, qty) for price, qty in bids if qty > 0), reverse=True)


def walk(levels: list[Level], qty: Decimal) -> list[Level] | None:
    """The fills from taking `qty` off these levels, best first, or None if they're too thin."""
    fills = []
    left = qty
    for price, available in levels:
        if left <= 0:
            break
        take = min(left, available)
        fills.append((price, take))
        left -= take
    return fills if left <= 0 else None
