"""Trades engine: enters arbs, sells one-legged fills straight away, and cashes out when that beats holding.

Entry. For each opportunity (best daily rate first) it buys half the contracts available at the prices
it wants as fill-or-kill orders, within MAX_TRADE_SIZE_USD per pair and MAX_PORTFOLIO_SIZE_USD overall,
skipping any whose continuous daily rate (at the size taken) is below MIN_RATE_PER_DAY or whose margin
(profit / (cost + fees)) is above MAX_MARGIN (too good to be real: usually a book flicker that's gone
before our order lands). Kalshi goes first and its leg is stored as a position straight away; Polymarket
is then bought for what Kalshi filled, with its limit as high as keeps the position at MIN_RATE_PER_DAY
(its book may have moved during the Kalshi round trip). A pair isn't entered again within ENTRY_COOLDOWN_SECONDS of an attempt,
and what our fills took is subtracted from the books until they update (TakenBooks), so the same
liquidity isn't chased twice.

Hedge repair. If only one leg fills, the missing leg is retried for up to HEDGE_RETRY_SECONDS at any
price that keeps the whole position at break-even or better (cost + fees <= $1 per contract). If that
doesn't work, the filled leg is sold.

Cashout (STANDARD_CASHOUT). An open position is sold when selling now earns a better daily rate than holding:

    ln(payout / initial) / days_held  >  ln(final / initial) / days_total

payout = what both legs sell for into the current bids, after fees; initial = entry cost + fees;
final = contracts (each pays $1); days_held and days_total (entry to resolution) floored at RATE_MIN_DAYS.

Conditional cashout (CONDITIONAL_CASHOUT). When the portfolio cap stops an opportunity getting its full
size, the open position with the lowest forward rate, ln(final / payout) / days_left, is sold to
make room, if selling realizes a profit (payout > initial) and the opportunity beats it in both daily
rate and dollars per day.

Every order is written to the trades DB and the log. With DRY_RUN on, orders are filled by PaperClient
against the live books and nothing is sent.

Trades log (TRADES_LOG_PATH). Every position change (opened, one-legged, hedged, closed) gets a line, followed
by a portfolio line: value at startup, value now, value once open positions resolve, and the expected
annualized return. Value = cash + open positions at cost; cash is read from both venues at startup (in a
dry run it starts at MAX_PORTFOLIO_SIZE_USD) and then moved by our own fills and settlements.
"""

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal

import aiohttp

import config
from markets.timeutil import utc_now_iso
from opportunities.book import Book, Level, walk
from opportunities.engine import OpportunityEngine
from opportunities.evaluate import Opportunity, Pair, best_opportunity, daily_rate, days_until
from opportunities.fees import kalshi_fee, polymarket_fee
from trades.kalshi import KalshiClient
from trades.orders import Fill, OrderRequest, failed
from trades.paper import PaperClient
from trades.polymarket import PolymarketClient
from trades.taken import TakenBooks

log = logging.getLogger(__name__)
ledger = logging.getLogger("trades.ledger")  # main.py also writes this one to TRADES_LOG_PATH

ZERO = Decimal(0)
TICK = Decimal("0.01")
NO_FILL = Fill("killed", Decimal(0), None, Decimal(0))


@dataclass
class Position:
    """In-memory copy of a positions row that is still open or unhedged."""

    position_id: int
    pair_id: int
    kalshi_market_id: str
    kalshi_side: str
    poly_slug: str
    poly_side: str
    kalshi_fee_multiplier: Decimal
    qty: Decimal
    kalshi_qty: Decimal
    poly_qty: Decimal
    cost: Decimal
    fees: Decimal
    exit_proceeds: Decimal
    exit_fees: Decimal
    resolution_date: datetime
    opened_at: datetime
    status: str
    close_reason: str | None

    def legs(self) -> list[tuple[str, str, str, Decimal]]:
        """(venue, market_id, side, contracts held) for each leg."""
        return [
            ("kalshi", self.kalshi_market_id, self.kalshi_side, self.kalshi_qty),
            ("polymarket", self.poly_slug, self.poly_side, self.poly_qty),
        ]


class TradeEngine:
    def __init__(self, conn: sqlite3.Connection, opportunities: OpportunityEngine, session: aiohttp.ClientSession):
        self._conn = conn
        self._opps = opportunities
        self._books = {"kalshi": TakenBooks(opportunities.kalshi), "polymarket": TakenBooks(opportunities.poly)}
        if config.DRY_RUN:
            self._clients = {
                "kalshi": PaperClient(self._books["kalshi"], self._kalshi_fee),
                "polymarket": PaperClient(self._books["polymarket"], lambda market_id, fills: polymarket_fee(fills)),
            }
        else:
            self._clients = {"kalshi": KalshiClient(session), "polymarket": PolymarketClient(session)}
        self.halted = False
        self._last_entry: dict[int, float] = {}  # pair_id -> monotonic time of the last entry attempt
        self._positions = {p.position_id: p for p in self._load_positions()}
        self._cash: Decimal | None = None  # across both venues; None if the balances couldn't be read
        self._initial_value: Decimal | None = None
        log.info(
            "trades engine: %s, %d open positions, $%.2f deployed",
            "DRY RUN" if config.DRY_RUN else "LIVE", len(self._positions), self._exposure(),
        )

    # ---- called by main ----

    async def start(self) -> None:
        """Read the starting cash and write the opening line of the trades log."""
        if config.DRY_RUN:
            self._cash = config.MAX_PORTFOLIO_SIZE_USD - sum((_basis(p) for p in self._positions.values()), ZERO)
        else:
            try:
                balances = await asyncio.gather(*(c.balance() for c in self._clients.values()))
                self._cash = sum(balances, ZERO)
            except Exception as e:
                log.error("couldn't read venue balances (%r); the trades log won't show portfolio values", e)
        if self._cash is not None:
            self._initial_value = self._cash + sum((_basis(p) for p in self._positions.values()), ZERO)
        self._ledger(
            f"{_tag()}START {'DRY RUN' if config.DRY_RUN else 'LIVE'}"
            + ("" if self._cash is None else f", cash ${self._cash:.2f}")
            + f", {len(self._positions)} open positions"
        )

    def status(self) -> str:
        """Positions and exposure, for the heartbeat."""
        unhedged = sum(p.status == "unhedged" for p in self._positions.values())
        return (
            f"{len(self._positions)} positions ({unhedged} unhedged), "
            f"${self._exposure():.2f} of ${config.MAX_PORTFOLIO_SIZE_USD:.2f} deployed"
            + (", HALTED" if self.halted else "")
        )

    def held_markets(self) -> dict[str, set[str]]:
        """Markets with an open position, so the feeds keep streaming them even if the pair stops being verified."""
        held = {"kalshi": set(), "polymarket": set()}
        for pos in self._positions.values():
            held["kalshi"].add(pos.kalshi_market_id)
            held["polymarket"].add(pos.poly_slug)
        return held

    def settle(self, markets_conn: sqlite3.Connection) -> None:
        """Close open positions whose markets have both left the venues, assuming the winning leg paid $1."""
        live = {(v, m) for v, m in markets_conn.execute("SELECT venue, market_id FROM markets")}
        for pos in list(self._positions.values()):
            if pos.status != "open":
                continue
            if ("kalshi", pos.kalshi_market_id) in live or ("polymarket", pos.poly_slug) in live:
                continue
            pos.exit_proceeds += pos.qty
            if self._cash is not None:
                self._cash += pos.qty
            pos.kalshi_qty = pos.poly_qty = ZERO
            self._close(pos, "settled")

    async def cycle(self, opportunities: list[Opportunity]) -> None:
        if self.halted:
            return
        for pos in [p for p in self._positions.values() if p.status == "unhedged"]:
            await self._unwind(pos)
        if config.STANDARD_CASHOUT:
            for pos in [p for p in self._positions.values() if p.status == "open"]:
                await self._maybe_cash_out(pos)
        for opp in opportunities:
            if self.halted or opp.rate_per_day < config.MIN_RATE_PER_DAY:
                return  # ranked best rate first, so the rest are below the minimum too
            # Swap before entering: entering starts the pair's cooldown, which would stop the swap's plan.
            if config.CONDITIONAL_CASHOUT:
                await self._maybe_swap(opp)
            if not self.halted:
                await self._enter(opp)

    # ---- entry ----

    async def _enter(self, opp: Opportunity) -> None:
        room = min(
            config.MAX_TRADE_SIZE_USD - self._exposure(opp.pair_id),
            config.MAX_PORTFOLIO_SIZE_USD - self._exposure(),
        )
        plan = self._plan_entry(opp, room)
        if plan is None:
            return
        pair, sized, reqs, now = plan
        log.info(
            "%sENTER pair %d: %s contracts, expected cost $%.2f fees $%.2f profit $%.2f, %.4f%%/day",
            _tag(), pair.pair_id, sized.qty, sized.cost, sized.fees, sized.profit, sized.rate_per_day * 100,
        )
        self._last_entry[pair.pair_id] = time.monotonic()
        k_req, p_req = reqs
        # Kalshi first: a killed Kalshi order leaves us flat instead of holding a Polymarket leg to unwind.
        (k_fill,) = await self._place([k_req])
        if k_fill.qty <= 0:
            self._record_order(None, pair.pair_id, "entry", k_req, k_fill)
            self._halt_if_unknown(k_fill)
            return
        # Store the Kalshi leg before sending Polymarket, so it's tracked whatever happens next.
        pos = self._open_position(pair, (k_req.side, p_req.side), k_fill, NO_FILL, now)
        self._record_order(pos.position_id, pair.pair_id, "entry", k_req, k_fill)
        p_req = replace(p_req, qty=k_fill.qty, limit_price=self._entry_hedge_limit(pos, p_req.limit_price))
        (p_fill,) = await self._place([p_req])
        self._record_order(pos.position_id, pair.pair_id, "entry", p_req, p_fill)
        if self._halt_if_unknown(p_fill):
            return
        if p_fill.qty > 0:
            pos.poly_qty += p_fill.qty
            pos.cost += _notional(p_fill)
            pos.fees += p_fill.fee
        if pos.kalshi_qty == pos.poly_qty:
            pos.status, pos.close_reason = "open", None
        self._save(pos)
        if pos.status == "open":
            log.info(
                "%sPOSITION %d opened: pair %d, %s contracts, cost $%.2f fees $%.2f",
                _tag(), pos.position_id, pair.pair_id, pos.qty, pos.cost, pos.fees,
            )
            self._ledger(f"{_tag()}OPENED {_describe(pos)}")
        else:
            log.warning("%sPOSITION %d one-legged (kalshi %s, polymarket %s); retrying the missing leg", _tag(), pos.position_id, pos.kalshi_qty, pos.poly_qty)
            self._ledger(f"{_tag()}ONE-LEGGED {_describe(pos)}")
            if await self._repair_hedge(pos) or self.halted:
                return
            log.warning("%sPOSITION %d still one-legged; selling it", _tag(), pos.position_id)
            await self._unwind(pos)

    def _plan_entry(self, opp: Opportunity, room: Decimal) -> tuple[Pair, Opportunity, list[OrderRequest], datetime] | None:
        """Size an entry within `room` dollars of contract cost: (pair, sized opportunity, orders, now), or
        None if there's nothing worth entering."""
        pair = self._opps.pairs.get(opp.pair_id)
        if pair is None or room <= 0:
            return None
        if time.monotonic() - self._last_entry.get(pair.pair_id, -config.ENTRY_COOLDOWN_SECONDS) < config.ENTRY_COOLDOWN_SECONDS:
            return None
        k_book = self._book("kalshi", pair.kalshi_market_id)
        p_book = self._book("polymarket", pair.poly_slug)
        if k_book is None or p_book is None:
            return None
        now = datetime.now(timezone.utc)
        full = best_opportunity(pair, k_book, p_book, now)
        if full is None:
            return None
        sides = (full.kalshi.side, full.poly.side)
        # Half the contracts available at the prices we want, so the book can thin out before we arrive.
        half = _whole(full.qty / 2)
        sized = best_opportunity(pair, k_book, p_book, now, max_qty=half, max_cost=room, sides=sides)
        if sized is None or _whole(sized.qty) < 1:
            return None
        qty = _whole(sized.qty)
        if qty != sized.qty:
            sized = best_opportunity(pair, k_book, p_book, now, max_qty=qty, sides=sides)
            if sized is None or sized.qty != qty:
                return None
        if sized.rate_per_day < config.MIN_RATE_PER_DAY or sized.profit > config.MAX_MARGIN * (sized.cost + sized.fees):
            return None
        # Limit at the prices the full opportunity reaches (2x the depth we need), unless filling all of
        # it at those prices would lose money; then limit at what this size needs.
        k_limit, p_limit = full.kalshi.limit_price, full.poly.limit_price
        if not self._profitable_at(pair, qty, k_limit, p_limit):
            k_limit, p_limit = sized.kalshi.limit_price, sized.poly.limit_price
        reqs = [
            OrderRequest("kalshi", pair.kalshi_market_id, sides[0], "buy", qty, k_limit, "fok"),
            OrderRequest("polymarket", pair.poly_slug, sides[1], "buy", qty, p_limit, "fok"),
        ]
        return pair, sized, reqs, now

    def _entry_hedge_limit(self, pos: Position, limit: Decimal) -> Decimal:
        """Raise the planned Polymarket limit as far as keeps the position at MIN_RATE_PER_DAY if all the
        missing contracts fill there, the worst case."""
        need = pos.kalshi_qty - pos.poly_qty
        days = days_until(pos.resolution_date, datetime.now(timezone.utc))

        def worth_it(price: Decimal) -> bool:
            initial = pos.cost + pos.fees + price * need + polymarket_fee([(price, need)])
            return daily_rate(pos.qty, initial, days) >= config.MIN_RATE_PER_DAY

        while limit + TICK < 1 and worth_it(limit + TICK):
            limit += TICK
        return limit

    async def _repair_hedge(self, pos: Position) -> bool:
        """Buy the missing contracts on the short leg of a one-legged entry, retrying for HEDGE_RETRY_SECONDS,
        at any price that keeps the position at break-even or better. True if the position is now hedged."""
        deadline = time.monotonic() + config.HEDGE_RETRY_SECONDS
        while True:
            short_kalshi = pos.kalshi_qty < pos.poly_qty
            venue, market_id, side = (
                ("kalshi", pos.kalshi_market_id, pos.kalshi_side) if short_kalshi
                else ("polymarket", pos.poly_slug, pos.poly_side)
            )
            need = abs(pos.kalshi_qty - pos.poly_qty)
            book = self._book(venue, market_id)
            fills = walk(book.asks(side), need) if book else None
            if fills is not None:
                fee = kalshi_fee(fills, pos.kalshi_fee_multiplier) if short_kalshi else polymarket_fee(fills)
                if pos.cost + pos.fees + _total(fills) + fee <= pos.qty:
                    # Limit as high as break-even allows, in case the book has moved a tick since we saw it.
                    limit = fills[-1][0]
                    while limit + TICK < 1 and self._breaks_even(pos, short_kalshi, need, limit + TICK):
                        limit += TICK
                    req = OrderRequest(venue, market_id, side, "buy", need, limit, "fok")
                    (fill,) = await self._place([req])
                    self._record_order(pos.position_id, pos.pair_id, "entry", req, fill)
                    if self._halt_if_unknown(fill):
                        self._save(pos)
                        return False
                    if fill.qty > 0:
                        if short_kalshi:
                            pos.kalshi_qty += fill.qty
                        else:
                            pos.poly_qty += fill.qty
                        pos.cost += _notional(fill)
                        pos.fees += fill.fee
                    if pos.kalshi_qty == pos.poly_qty:
                        pos.status, pos.close_reason = "open", None
                        self._save(pos)
                        log.info(
                            "%sPOSITION %d hedged: pair %d, %s contracts, cost $%.2f fees $%.2f",
                            _tag(), pos.position_id, pos.pair_id, pos.qty, pos.cost, pos.fees,
                        )
                        self._ledger(f"{_tag()}HEDGED {_describe(pos)}")
                        return True
            if time.monotonic() >= deadline:
                self._save(pos)
                return False
            await asyncio.sleep(config.HEDGE_RETRY_INTERVAL_SECONDS)

    def _breaks_even(self, pos: Position, short_kalshi: bool, need: Decimal, limit: Decimal) -> bool:
        """Whether the position still breaks even if all `need` contracts fill at `limit`, the worst case
        (price plus fee per contract rises with price)."""
        worst = [(limit, need)]
        fee = kalshi_fee(worst, pos.kalshi_fee_multiplier) if short_kalshi else polymarket_fee(worst)
        return pos.cost + pos.fees + limit * need + fee <= pos.qty

    def _profitable_at(self, pair: Pair, qty: Decimal, k_price: Decimal, p_price: Decimal) -> bool:
        cost = qty * (k_price + p_price)
        fees = kalshi_fee([(k_price, qty)], pair.kalshi_fee_multiplier) + polymarket_fee([(p_price, qty)])
        return cost + fees < qty

    def _open_position(self, pair: Pair, sides, k_fill: Fill, p_fill: Fill, now: datetime) -> Position:
        hedged = k_fill.qty == p_fill.qty
        pos = Position(
            position_id=0,
            pair_id=pair.pair_id,
            kalshi_market_id=pair.kalshi_market_id,
            kalshi_side=sides[0],
            poly_slug=pair.poly_slug,
            poly_side=sides[1],
            kalshi_fee_multiplier=pair.kalshi_fee_multiplier,
            qty=max(k_fill.qty, p_fill.qty),
            kalshi_qty=k_fill.qty,
            poly_qty=p_fill.qty,
            cost=_notional(k_fill) + _notional(p_fill),
            fees=k_fill.fee + p_fill.fee,
            exit_proceeds=ZERO,
            exit_fees=ZERO,
            resolution_date=pair.resolution_date,
            opened_at=now,
            status="open" if hedged else "unhedged",
            close_reason=None if hedged else "unwind",
        )
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO positions (pair_id, kalshi_market_id, kalshi_side, poly_slug, poly_side, "
                "kalshi_fee_multiplier, qty, kalshi_qty, poly_qty, cost, fees, resolution_date, status, "
                "close_reason, opened_at, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pos.pair_id, pos.kalshi_market_id, pos.kalshi_side, pos.poly_slug, pos.poly_side,
                    str(pos.kalshi_fee_multiplier), str(pos.qty), str(pos.kalshi_qty), str(pos.poly_qty),
                    str(pos.cost), str(pos.fees), _iso(pos.resolution_date), pos.status, pos.close_reason,
                    _iso(now), int(config.DRY_RUN),
                ),
            )
        pos.position_id = cur.lastrowid
        self._positions[pos.position_id] = pos
        return pos

    # ---- cashout ----

    async def _maybe_cash_out(self, pos: Position) -> None:
        quote = self._exit_quote(pos)
        if quote is None:
            return
        k_fills, p_fills, payout = quote
        initial = pos.cost + pos.fees
        now = datetime.now(timezone.utc)
        days_held = max((now - pos.opened_at).total_seconds() / 86400, config.RATE_MIN_DAYS)
        days_total = days_until(pos.resolution_date, pos.opened_at)
        cash_out_rate = daily_rate(payout, initial, days_held)
        hold_rate = daily_rate(pos.qty, initial, days_total)
        if cash_out_rate <= hold_rate:
            return
        log.info(
            "%sCASHOUT position %d: payout $%.2f on $%.2f = %.4f%%/day over %.1f days held > %.4f%%/day holding",
            _tag(), pos.position_id, payout, initial, cash_out_rate * 100, days_held, hold_rate * 100,
        )
        await self._sell(pos, k_fills, p_fills)

    async def _maybe_swap(self, opp: Opportunity) -> None:
        """Conditional cashout: if the portfolio cap is what stops `opp` getting its full size, sell the open
        position with the lowest forward rate (holding it from here to resolution) to make room for `opp`
        (entered next by cycle), provided selling realizes a profit and `opp` earns more per day in both
        rate and dollars."""
        pair_room = config.MAX_TRADE_SIZE_USD - self._exposure(opp.pair_id)
        portfolio_room = config.MAX_PORTFOLIO_SIZE_USD - self._exposure()
        if portfolio_room >= pair_room:
            return  # the portfolio cap isn't what's limiting this pair
        plan = self._plan_entry(opp, pair_room)
        if plan is None:
            return
        sized = plan[1]
        now = datetime.now(timezone.utc)
        worst = None
        for pos in self._positions.values():
            if pos.status != "open" or pos.pair_id == opp.pair_id:
                continue
            quote = self._exit_quote(pos)
            if quote is None or quote[2] <= pos.cost + pos.fees:
                continue  # can't sell it all, or selling now would lock in a loss
            forward_rate = daily_rate(pos.qty, quote[2], days_until(pos.resolution_date, now))
            if worst is None or forward_rate < worst[1]:
                worst = (pos, forward_rate, quote)
        if worst is None:
            return
        pos, forward_rate, (k_fills, p_fills, payout) = worst
        # Dollars per day: what the new trade can absorb at its rate vs what the payout earns staying put.
        deployable = min(sized.cost + sized.fees, portfolio_room + pos.cost)
        if sized.rate_per_day <= forward_rate or deployable * Decimal(sized.rate_per_day) <= payout * Decimal(forward_rate):
            return
        log.info(
            "%sSWAP position %d (%.4f%%/day holding from here, payout $%.2f) for pair %d (%.4f%%/day on $%.2f)",
            _tag(), pos.position_id, forward_rate * 100, payout, opp.pair_id, sized.rate_per_day * 100, deployable,
        )
        await self._sell(pos, k_fills, p_fills)  # stored as a cashout

    def _exit_quote(self, pos: Position) -> tuple[list[Level], list[Level], Decimal] | None:
        """(kalshi fills, polymarket fills, payout after fees) for selling the whole position into the
        current bids, or None if the bids can't absorb it."""
        k_book = self._book("kalshi", pos.kalshi_market_id)
        p_book = self._book("polymarket", pos.poly_slug)
        if k_book is None or p_book is None:
            return None
        k_fills = walk(k_book.bids(pos.kalshi_side), pos.qty)
        p_fills = walk(p_book.bids(pos.poly_side), pos.qty)
        if k_fills is None or p_fills is None:
            return None
        proceeds = _total(k_fills) + _total(p_fills)
        exit_fees = kalshi_fee(k_fills, pos.kalshi_fee_multiplier) + polymarket_fee(p_fills)
        return k_fills, p_fills, proceeds - exit_fees

    async def _sell(self, pos: Position, k_fills: list[Level], p_fills: list[Level]) -> None:
        """Sell both legs of a position (FOK) down to the quoted prices, Kalshi first and Polymarket for what
        Kalshi sold; a one-legged result is unwound."""
        k_req = OrderRequest("kalshi", pos.kalshi_market_id, pos.kalshi_side, "sell", pos.qty, k_fills[-1][0], "fok")
        (k_fill,) = await self._place([k_req])
        self._record_order(pos.position_id, pos.pair_id, "cashout", k_req, k_fill)
        if self._halt_if_unknown(k_fill) or k_fill.qty <= 0:
            return  # nothing sold (or we don't know): the position is as it was
        self._apply_sale(pos, "kalshi", k_fill)
        p_req = OrderRequest("polymarket", pos.poly_slug, pos.poly_side, "sell", k_fill.qty, p_fills[-1][0], "fok")
        (p_fill,) = await self._place([p_req])
        self._record_order(pos.position_id, pos.pair_id, "cashout", p_req, p_fill)
        self._apply_sale(pos, "polymarket", p_fill)
        if self._halt_if_unknown(p_fill):
            self._save(pos)
            return
        if pos.kalshi_qty <= 0 and pos.poly_qty <= 0:
            self._close(pos, "cashout")
        elif pos.kalshi_qty == pos.poly_qty:
            self._save(pos)
        else:
            # One leg sold and the other didn't: we're exposed, so sell the rest right away.
            pos.status, pos.close_reason = "unhedged", "cashout"
            self._save(pos)
            log.warning("%sPOSITION %d cashout one-legged; selling the other leg", _tag(), pos.position_id)
            await self._unwind(pos)

    # ---- unwinding one-legged positions ----

    async def _unwind(self, pos: Position) -> None:
        """Sell whatever is still held on either leg into the current bids (IOC). Retried every cycle until flat."""
        reqs = []
        for venue, market_id, side, held in pos.legs():
            if held <= 0:
                continue
            book = self._book(venue, market_id)
            bids = book.bids(side) if book else []
            if not bids:
                log.warning("position %d: no %s bids on %s %s to sell into; will retry", pos.position_id, side, venue, market_id)
                continue
            fills = walk(bids, held) or bids
            reqs.append(OrderRequest(venue, market_id, side, "sell", held, fills[-1][0], "ioc"))
        if not reqs:
            return
        fills = await self._place(reqs)
        for req, fill in zip(reqs, fills):
            self._record_order(pos.position_id, pos.pair_id, "unwind", req, fill)
            self._apply_sale(pos, req.venue, fill)
        if self._halt_if_unknown(*fills):
            self._save(pos)
            return
        if pos.kalshi_qty <= 0 and pos.poly_qty <= 0:
            self._close(pos, pos.close_reason or "unwind")
        else:
            self._save(pos)

    # ---- bookkeeping ----

    def _apply_sale(self, pos: Position, venue: str, fill: Fill) -> None:
        if fill.qty <= 0:
            return
        if venue == "kalshi":
            pos.kalshi_qty -= fill.qty
        else:
            pos.poly_qty -= fill.qty
        pos.exit_proceeds += _notional(fill)
        pos.exit_fees += fill.fee

    def _close(self, pos: Position, reason: str) -> None:
        pos.status, pos.close_reason = "closed", reason
        profit = pos.exit_proceeds - pos.exit_fees - pos.cost - pos.fees
        self._save(pos, profit)
        del self._positions[pos.position_id]
        log.info(
            "%sPOSITION %d closed (%s): cost $%.2f fees $%.2f, exit $%.2f fees $%.2f, profit $%.2f",
            _tag(), pos.position_id, reason, pos.cost, pos.fees, pos.exit_proceeds, pos.exit_fees, profit,
        )
        self._ledger(
            f"{_tag()}CLOSED ({reason}) position {pos.position_id} pair {pos.pair_id}: cost ${pos.cost:.2f} fees ${pos.fees:.2f}, "
            f"exit ${pos.exit_proceeds:.2f} fees ${pos.exit_fees:.2f}, profit ${profit:.2f}"
        )

    def _save(self, pos: Position, profit: Decimal | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE positions SET qty = ?, kalshi_qty = ?, poly_qty = ?, cost = ?, fees = ?, exit_proceeds = ?, "
                "exit_fees = ?, status = ?, close_reason = ?, profit = ?, closed_at = ? WHERE position_id = ?",
                (
                    str(pos.qty), str(pos.kalshi_qty), str(pos.poly_qty), str(pos.cost), str(pos.fees), str(pos.exit_proceeds), str(pos.exit_fees), pos.status,
                    pos.close_reason, None if profit is None else str(profit),
                    utc_now_iso() if pos.status == "closed" else None, pos.position_id,
                ),
            )

    def _record_order(self, position_id, pair_id: int, purpose: str, req: OrderRequest, fill: Fill) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO orders (position_id, pair_id, purpose, venue, market_id, side, action, tif, "
                "qty_requested, limit_price, status, qty_filled, avg_price, fee, exchange_order_id, error, "
                "dry_run, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    position_id, pair_id, purpose, req.venue, req.market_id, req.side, req.action, req.tif,
                    str(req.qty), str(req.limit_price), fill.status, str(fill.qty),
                    None if fill.avg_price is None else str(fill.avg_price), str(fill.fee),
                    fill.exchange_order_id, fill.error, int(config.DRY_RUN), utc_now_iso(),
                ),
            )
        if self._cash is not None and fill.qty > 0:
            sign = -1 if req.action == "buy" else 1
            self._cash += sign * _notional(fill) - fill.fee
        level = logging.INFO if fill.status in ("filled", "killed") else logging.WARNING
        log.log(
            level,
            "%sORDER %s %s %s %s %s %s @ %s %s -> %s %s%s fee $%s%s",
            _tag(), purpose, req.venue, req.market_id, req.action.upper(), req.side.upper(), req.qty,
            req.limit_price, req.tif.upper(), fill.status, fill.qty,
            f" @ {fill.avg_price:.4f}" if fill.avg_price is not None else "", fill.fee,
            f" ({fill.error})" if fill.error else "",
        )

    def _ledger(self, event: str) -> None:
        ledger.info(event)
        ledger.info("    %s", self._portfolio_summary())

    def _portfolio_summary(self) -> str:
        """Value at startup, now and once open positions resolve, plus the expected annualized continuous return."""
        if self._cash is None:
            return "portfolio: unknown (couldn't read venue balances at startup)"
        positions = list(self._positions.values())
        hedged = [p for p in positions if p.status == "open"]
        value = self._cash + sum((_basis(p) for p in positions), ZERO)
        # One-legged positions are about to be sold; count them at cost.
        at_resolution = value + sum((p.qty - _basis(p) for p in hedged), ZERO)
        out = (
            f"portfolio: start ${self._initial_value:.2f} | now ${value:.2f} ({value - self._initial_value:+.2f}) | "
            f"at resolution ${at_resolution:.2f} ({at_resolution - self._initial_value:+.2f})"
        )
        if not hedged:
            return out + " | nothing deployed"
        now = datetime.now(timezone.utc)
        payout = sum((p.qty for p in hedged), ZERO)
        deployed = sum((_basis(p) for p in hedged), ZERO)
        days = sum(float(p.qty) * days_until(p.resolution_date, now) for p in hedged) / float(payout)  # payout-weighted
        on_portfolio = daily_rate(at_resolution, value, days) * 365
        on_deployed = daily_rate(payout, deployed, days) * 365
        return out + (
            f" | expected annualized {on_portfolio * 100:.2f}% on the portfolio, {on_deployed * 100:.2f}% on "
            f"${deployed:.2f} deployed ({len(hedged)} positions, {days:.1f} days to resolution on average)"
        )

    def _halt_if_unknown(self, *fills: Fill) -> bool:
        """An order whose outcome we don't know may or may not have filled: stop trading until a human checks."""
        if any(f.status == "unknown" for f in fills):
            self.halted = True
            log.critical("TRADING HALTED: an order's result is unknown. Check positions on both venues, then restart.")
            return True
        return False

    async def _place(self, reqs: list[OrderRequest]) -> list[Fill]:
        """Send the orders at once. Live fills are recorded as taken from the books (paper fills record themselves)."""
        fills = await asyncio.gather(*(self._place_one(r) for r in reqs))
        if not config.DRY_RUN:
            for req, fill in zip(reqs, fills):
                self._books[req.venue].take(req, fill.qty)
        return list(fills)

    async def _place_one(self, req: OrderRequest) -> Fill:
        """A client error we didn't foresee leaves the order's outcome unknown, which halts trading."""
        try:
            return await self._clients[req.venue].place(req)
        except Exception as e:
            log.exception("ORDER %s %s %s %s crashed", req.action, req.venue, req.market_id, req.side)
            return failed("unknown", repr(e))

    def _book(self, venue: str, market_id: str) -> Book | None:
        """Live book minus what our fills have taken since it last changed."""
        return self._books[venue].book(market_id)

    def _exposure(self, pair_id: int | None = None) -> Decimal:
        return sum(
            (p.cost for p in self._positions.values() if pair_id is None or p.pair_id == pair_id),
            ZERO,
        )

    def _kalshi_fee(self, market_id: str, fills: list[Level]) -> Decimal:
        multiplier = Decimal(1)
        for pair in self._opps.pairs.values():
            if pair.kalshi_market_id == market_id:
                multiplier = pair.kalshi_fee_multiplier
                break
        else:
            for pos in self._positions.values():
                if pos.kalshi_market_id == market_id:
                    multiplier = pos.kalshi_fee_multiplier
                    break
        return kalshi_fee(fills, multiplier)

    def _load_positions(self) -> list[Position]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status IN ('open', 'unhedged') AND dry_run = ?", (int(config.DRY_RUN),)
        ).fetchall()
        return [
            Position(
                position_id=r["position_id"],
                pair_id=r["pair_id"],
                kalshi_market_id=r["kalshi_market_id"],
                kalshi_side=r["kalshi_side"],
                poly_slug=r["poly_slug"],
                poly_side=r["poly_side"],
                kalshi_fee_multiplier=Decimal(r["kalshi_fee_multiplier"]),
                qty=Decimal(r["qty"]),
                kalshi_qty=Decimal(r["kalshi_qty"]),
                poly_qty=Decimal(r["poly_qty"]),
                cost=Decimal(r["cost"]),
                fees=Decimal(r["fees"]),
                exit_proceeds=Decimal(r["exit_proceeds"]),
                exit_fees=Decimal(r["exit_fees"]),
                resolution_date=datetime.fromisoformat(r["resolution_date"]),
                opened_at=datetime.fromisoformat(r["opened_at"]),
                status=r["status"],
                close_reason=r["close_reason"],
            )
            for r in rows
        ]


def _basis(pos: Position) -> Decimal:
    """Net cash a position has taken: entry cost and fees, less anything already sold."""
    return pos.cost + pos.fees - pos.exit_proceeds + pos.exit_fees


def _describe(pos: Position) -> str:
    out = (
        f"position {pos.position_id} pair {pos.pair_id}: kalshi {pos.kalshi_market_id} {pos.kalshi_side.upper()} {pos.kalshi_qty} + "
        f"polymarket {pos.poly_slug} {pos.poly_side.upper()} {pos.poly_qty}, cost ${pos.cost:.2f} fees ${pos.fees:.2f}"
    )
    if pos.status != "open":
        return out
    return out + f", pays ${pos.qty:.2f} on {pos.resolution_date:%Y-%m-%d} (profit ${pos.qty - pos.cost - pos.fees:.2f})"


def _whole(qty: Decimal) -> Decimal:
    """Orders are sent in whole contracts."""
    return qty.to_integral_value(ROUND_FLOOR)


def _notional(fill: Fill) -> Decimal:
    return fill.qty * fill.avg_price if fill.qty > 0 else ZERO


def _total(fills: list[Level]) -> Decimal:
    return sum((p * q for p, q in fills), ZERO)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tag() -> str:
    return "[DRY] " if config.DRY_RUN else ""
