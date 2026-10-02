"""Arb math for one pair: no I/O, just books in and the best opportunity out.

Buying one side on each venue such that exactly one leg pays $1 is an arb when the two asks plus fees
cost less than $1. Which sides to buy depends on `flipped`:

    flipped=0 (Kalshi YES == Poly YES): Kalshi YES + Poly NO,  or Kalshi NO + Poly YES
    flipped=1 (Kalshi YES == Poly NO):  Kalshi YES + Poly YES, or Kalshi NO + Poly NO
"""

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

import config
from opportunities.book import Book, Level
from opportunities.fees import kalshi_fee, polymarket_fee

QTY_STEP = Decimal("0.01")  # cost-capped sizes are rounded down to this many contracts

SIDE_PAIRS = {  # flipped -> [(kalshi side, polymarket side)]
    0: [("yes", "no"), ("no", "yes")],
    1: [("yes", "yes"), ("no", "no")],
}


@dataclass(frozen=True)
class Pair:
    pair_id: int
    kalshi_market_id: str
    poly_slug: str
    flipped: int
    resolution_date: datetime  # the later of the two markets' dates: capital is tied up until then
    kalshi_fee_multiplier: Decimal


@dataclass(frozen=True)
class Leg:
    venue: str
    market_id: str
    side: str  # 'yes' or 'no'
    qty: Decimal
    limit_price: Decimal  # worst price level used; the price to send a limit order at
    cost: Decimal  # sum of price * qty over the levels used
    fee: Decimal


@dataclass(frozen=True)
class Opportunity:
    pair_id: int
    kalshi: Leg
    poly: Leg
    qty: Decimal  # contracts per leg; pays qty dollars at resolution whichever way it goes
    cost: Decimal  # both legs, excluding fees
    fees: Decimal
    profit: Decimal  # qty - cost - fees
    days: float  # to resolution, floored at RATE_MIN_DAYS
    rate_per_day: float  # ln(final / initial) / days, with final = qty and initial = cost + fees

    @property
    def key(self) -> tuple[int, str, str]:
        return self.pair_id, self.kalshi.side, self.poly.side


def best_opportunity(
    pair: Pair,
    kalshi_book: Book,
    poly_book: Book,
    now: datetime,
    max_qty: Decimal | None = None,
    max_cost: Decimal | None = None,
    sides: tuple[str, str] | None = None,
) -> Opportunity | None:
    """The better of the pair's two directions by daily rate, or None if neither is profitable.

    `max_qty` caps contracts per leg and `max_cost` caps contract cost (both legs); `sides` pins one
    (kalshi side, polymarket side) direction.
    """
    days = days_until(pair.resolution_date, now)
    best = None
    for k_side, p_side in [sides] if sides else SIDE_PAIRS[pair.flipped]:
        opp = _evaluate(pair, k_side, kalshi_book.asks(k_side), p_side, poly_book.asks(p_side), days, max_qty, max_cost)
        if opp and (best is None or opp.rate_per_day > best.rate_per_day):
            best = opp
    return best


def days_until(when: datetime, now: datetime) -> float:
    return max((when - now).total_seconds() / 86400, config.RATE_MIN_DAYS)


def daily_rate(final: Decimal, initial: Decimal, days: float) -> float:
    """ln(final / initial) / days: the continuous daily return of turning `initial` into `final`."""
    if initial <= 0:
        return 0.0
    return math.log(final / initial) / days if final > 0 else -math.inf


def _evaluate(pair, k_side, k_asks: list[Level], p_side, p_asks: list[Level], days, max_qty, max_cost):
    """Walk both ask ladders together, cheapest first, while a contract on each still costs < $1.

    Returns the size with the most total profit after fees, within the caps.
    """
    k_fills: list[Level] = []
    p_fills: list[Level] = []
    i = j = 0
    k_left = k_asks[0][1] if k_asks else Decimal(0)
    p_left = p_asks[0][1] if p_asks else Decimal(0)
    bought = spent = Decimal(0)
    best = None
    while i < len(k_asks) and j < len(p_asks):
        k_price, p_price = k_asks[i][0], p_asks[j][0]
        if k_price + p_price >= 1:
            break
        qty = min(k_left, p_left)
        if max_qty is not None:
            qty = min(qty, max_qty - bought)
        if max_cost is not None:
            qty = min(qty, ((max_cost - spent) / (k_price + p_price)).quantize(QTY_STEP, ROUND_FLOOR))
        if qty <= 0:
            break
        k_fills.append((k_price, qty))
        p_fills.append((p_price, qty))
        bought += qty
        spent += qty * (k_price + p_price)
        opp = _build(pair, k_side, k_fills, p_side, p_fills, days)
        if opp.profit > 0 and (best is None or opp.profit > best.profit):
            best = opp
        k_left -= qty
        p_left -= qty
        if k_left <= 0:
            i += 1
            k_left = k_asks[i][1] if i < len(k_asks) else Decimal(0)
        if p_left <= 0:
            j += 1
            p_left = p_asks[j][1] if j < len(p_asks) else Decimal(0)
    return best


def _build(pair: Pair, k_side: str, k_fills: list[Level], p_side: str, p_fills: list[Level], days: float):
    kalshi = _leg("kalshi", pair.kalshi_market_id, k_side, k_fills, kalshi_fee(k_fills, pair.kalshi_fee_multiplier))
    poly = _leg("polymarket", pair.poly_slug, p_side, p_fills, polymarket_fee(p_fills))
    qty = kalshi.qty
    cost = kalshi.cost + poly.cost
    fees = kalshi.fee + poly.fee
    initial = cost + fees
    return Opportunity(pair.pair_id, kalshi, poly, qty, cost, fees, qty - initial, days, daily_rate(qty, initial, days))


def _leg(venue: str, market_id: str, side: str, fills: list[Level], fee: Decimal) -> Leg:
    return Leg(
        venue=venue,
        market_id=market_id,
        side=side,
        qty=sum((q for _, q in fills), Decimal(0)),
        limit_price=fills[-1][0],
        cost=sum((p * q for p, q in fills), Decimal(0)),
        fee=fee,
    )
