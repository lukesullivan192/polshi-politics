"""Trades engine: enters arbs, sells one-legged fills straight away, and cashes out when that beats holding.

Entry. For each opportunity (best daily rate first) it buys half the contracts available at the prices
it wants, both legs at once (asyncio.gather) as fill-or-kill orders, within MAX_TRADE_SIZE_USD per
pair and MAX_PORTFOLIO_SIZE_USD overall, skipping any whose compound daily rate (at the size taken) is
below MIN_COMPOUND_RATE_PER_DAY. If only one leg fills, that leg is sold right away.

Cashout (STANDARD_CASHOUT). An open position is sold when selling now earns a better daily rate than holding:

    (payout / initial) ^ (1 / days_held) - 1  >  (final / initial) ^ (1 / days_total) - 1

payout = what both legs sell for into the current bids, after fees; initial = entry cost + fees;
final = contracts (each pays $1); days_held and days_total (entry to resolution) floored at RATE_MIN_DAYS.

Conditional cashout (CONDITIONAL_CASHOUT). When the portfolio cap stops an opportunity getting its full
size, the open position with the lowest forward rate, (final / payout) ^ (1 / days_left) - 1, is sold to
make room, if selling realizes a profit (payout > initial) and the opportunity beats it in both daily
rate and dollars per day.

Every order is written to the trades DB and the log. With DRY_RUN on, orders are filled by PaperClient
against the live books and nothing is sent.
"""

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
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
from trades.orders import Fill, OrderRequest
from trades.paper import PaperClient
from trades.polymarket import PolymarketClient

log = logging.getLogger(__name__)

ZERO = Decimal(0)


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
        self._feeds = {"kalshi": opportunities.kalshi, "polymarket": opportunities.poly}
        if config.DRY_RUN:
            self._clients = {
                "kalshi": PaperClient(opportunities.kalshi, self._kalshi_fee),
                "polymarket": PaperClient(opportunities.poly, lambda market_id, fills: polymarket_fee(fills)),
            }
        else:
            self._clients = {"kalshi": KalshiClient(session), "polymarket": PolymarketClient(session)}
        self.halted = False
        self._positions = {p.position_id: p for p in self._load_positions()}
        log.info(
            "trades engine: %s, %d open positions, $%.2f deployed",
            "DRY RUN" if config.DRY_RUN else "LIVE", len(self._positions), self._exposure(),
        )

    # ---- called by main ----

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
            if self.halted or opp.rate_per_day < config.MIN_COMPOUND_RATE_PER_DAY:
                return  # ranked best rate first, so the rest are below the minimum too
            await self._enter(opp)
            if config.CONDITIONAL_CASHOUT and not self.halted:
                await self._maybe_swap(opp)

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
        k_fill, p_fill = await asyncio.gather(*(self._clients[r.venue].place(r) for r in reqs))
        if k_fill.qty <= 0 and p_fill.qty <= 0:
            for req, fill in zip(reqs, (k_fill, p_fill)):
                self._record_order(None, pair.pair_id, "entry", req, fill)
            self._halt_if_unknown(k_fill, p_fill)
            return
        pos = self._open_position(pair, (reqs[0].side, reqs[1].side), k_fill, p_fill, now)
        for req, fill in zip(reqs, (k_fill, p_fill)):
            self._record_order(pos.position_id, pair.pair_id, "entry", req, fill)
        if self._halt_if_unknown(k_fill, p_fill):
            return
        if pos.status == "open":
            log.info(
                "%sPOSITION %d opened: pair %d, %s contracts, cost $%.2f fees $%.2f",
                _tag(), pos.position_id, pair.pair_id, pos.qty, pos.cost, pos.fees,
            )
        else:
            log.warning("%sPOSITION %d one-legged (kalshi %s, polymarket %s); selling it", _tag(), pos.position_id, pos.kalshi_qty, pos.poly_qty)
            await self._unwind(pos)

    def _plan_entry(self, opp: Opportunity, room: Decimal) -> tuple[Pair, Opportunity, list[OrderRequest], datetime] | None:
        """Size an entry within `room` dollars of contract cost: (pair, sized opportunity, orders, now), or
        None if there's nothing worth entering."""
        pair = self._opps.pairs.get(opp.pair_id)
        if pair is None or room <= 0:
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
        if sized.rate_per_day < config.MIN_COMPOUND_RATE_PER_DAY:
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
        position with the lowest forward rate (holding it from here to resolution) and enter `opp` with the
        money, provided selling realizes a profit and `opp` earns more per day in both rate and dollars."""
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
        if pos.position_id not in self._positions and not self.halted:
            await self._enter(opp)

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
        """Sell both legs of a position at once (FOK) down to the quoted prices; a one-legged result is unwound."""
        reqs = [
            OrderRequest("kalshi", pos.kalshi_market_id, pos.kalshi_side, "sell", pos.qty, k_fills[-1][0], "fok"),
            OrderRequest("polymarket", pos.poly_slug, pos.poly_side, "sell", pos.qty, p_fills[-1][0], "fok"),
        ]
        fills = await asyncio.gather(*(self._clients[r.venue].place(r) for r in reqs))
        for req, fill in zip(reqs, fills):
            self._record_order(pos.position_id, pos.pair_id, "cashout", req, fill)
            self._apply_sale(pos, req.venue, fill)
        if self._halt_if_unknown(*fills):
            self._save(pos)
            return
        if pos.kalshi_qty <= 0 and pos.poly_qty <= 0:
            self._close(pos, "cashout")
        elif pos.kalshi_qty != pos.poly_qty:
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
        fills = await asyncio.gather(*(self._clients[r.venue].place(r) for r in reqs))
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

    def _save(self, pos: Position, profit: Decimal | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE positions SET kalshi_qty = ?, poly_qty = ?, exit_proceeds = ?, exit_fees = ?, status = ?, "
                "close_reason = ?, profit = ?, closed_at = ? WHERE position_id = ?",
                (
                    str(pos.kalshi_qty), str(pos.poly_qty), str(pos.exit_proceeds), str(pos.exit_fees), pos.status,
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
        level = logging.INFO if fill.status in ("filled", "killed") else logging.WARNING
        log.log(
            level,
            "%sORDER %s %s %s %s %s %s @ %s %s -> %s %s%s fee $%s%s",
            _tag(), purpose, req.venue, req.market_id, req.action.upper(), req.side.upper(), req.qty,
            req.limit_price, req.tif.upper(), fill.status, fill.qty,
            f" @ {fill.avg_price:.4f}" if fill.avg_price is not None else "", fill.fee,
            f" ({fill.error})" if fill.error else "",
        )

    def _halt_if_unknown(self, *fills: Fill) -> bool:
        """An order whose outcome we don't know may or may not have filled: stop trading until a human checks."""
        if any(f.status == "unknown" for f in fills):
            self.halted = True
            log.critical("TRADING HALTED: an order's result is unknown. Check positions on both venues, then restart.")
            return True
        return False

    def _book(self, venue: str, market_id: str) -> Book | None:
        """Live book; in a dry run, minus what paper fills have already taken."""
        client = self._clients[venue]
        return client.book(market_id) if isinstance(client, PaperClient) else self._feeds[venue].book(market_id)

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
