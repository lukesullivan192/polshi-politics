"""Opportunity engine: re-evaluates pairs whenever one of their books changes and keeps the live set.

Every profitable arb is logged when it opens and when it closes; nothing is filtered by size or edge.
`cycle()` returns the open opportunities ranked by daily rate for the execution engine.
"""

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal

import aiohttp

from opportunities.evaluate import Opportunity, Pair, best_opportunity
from opportunities.kalshi_feed import KalshiFeed
from opportunities.polymarket_feed import PolymarketFeed

log = logging.getLogger(__name__)


class OpportunityEngine:
    def __init__(self):
        self.kalshi = KalshiFeed(self._on_update)
        self.poly = PolymarketFeed(self._on_update)
        self._pairs: dict[int, Pair] = {}
        self._pairs_by_market: dict[tuple[str, str], list[int]] = {}
        self._dirty: set[int] = set()  # pairs to re-evaluate
        self._changed = asyncio.Event()
        self._open: dict[int, tuple[Opportunity, float, Decimal]] = {}  # pair_id -> (latest, opened at, peak profit)

    @property
    def pairs(self) -> dict[int, Pair]:
        return self._pairs

    def start(self, session: aiohttp.ClientSession) -> list[asyncio.Task]:
        return [asyncio.create_task(feed.run(session)) for feed in (self.kalshi, self.poly)]

    def reload_pairs(
        self,
        markets_conn: sqlite3.Connection,
        matchings_conn: sqlite3.Connection,
        held: dict[str, set[str]] | None = None,
    ) -> None:
        """Load verified pairs (call after each refresh) and point the feeds at their markets, plus any
        `held` markets (venue -> market ids with open positions)."""
        markets = {
            (venue, market_id): (resolution_date, fee_multiplier)
            for venue, market_id, resolution_date, fee_multiplier in markets_conn.execute(
                "SELECT venue, market_id, resolution_date, fee_multiplier FROM markets"
            )
        }
        pairs = {}
        for pair_id, kalshi_id, poly_slug, flipped in matchings_conn.execute(
            "SELECT pair_id, kalshi_market_id, poly_slug, flipped FROM matchings WHERE status = 'verified'"
        ):
            k_date, k_fee_multiplier = markets.get(("kalshi", kalshi_id), (None, None))
            p_date, _ = markets.get(("polymarket", poly_slug), (None, None))
            if k_date is None or p_date is None:
                log.warning("pair %d: missing resolution date, skipped", pair_id)
                continue
            pairs[pair_id] = Pair(
                pair_id=pair_id,
                kalshi_market_id=kalshi_id,
                poly_slug=poly_slug,
                flipped=flipped,
                resolution_date=max(datetime.fromisoformat(k_date), datetime.fromisoformat(p_date)),
                kalshi_fee_multiplier=Decimal(str(k_fee_multiplier if k_fee_multiplier is not None else 1)),
            )
        self._pairs = pairs
        self._pairs_by_market = {}
        for pair in pairs.values():
            self._pairs_by_market.setdefault(("kalshi", pair.kalshi_market_id), []).append(pair.pair_id)
            self._pairs_by_market.setdefault(("polymarket", pair.poly_slug), []).append(pair.pair_id)
        for pair_id in [p for p in self._open if p not in pairs]:
            self._close(pair_id, "pair no longer verified")
        held = held or {}
        self.kalshi.set_markets({p.kalshi_market_id for p in pairs.values()} | held.get("kalshi", set()))
        self.poly.set_markets({p.poly_slug for p in pairs.values()} | held.get("polymarket", set()))
        self._dirty.update(pairs)
        self._changed.set()
        log.info("opportunity engine: %d verified pairs", len(pairs))

    async def cycle(self, timeout: float) -> list[Opportunity]:
        """Wait up to `timeout` for a book change, re-evaluate the affected pairs, and return every
        open opportunity, best daily rate first."""
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._changed.clear()
        dirty, self._dirty = self._dirty, set()
        now = datetime.now(timezone.utc)
        for pair_id in dirty:
            pair = self._pairs.get(pair_id)
            if pair is None:
                continue
            k_book = self.kalshi.book(pair.kalshi_market_id)
            p_book = self.poly.book(pair.poly_slug)
            opp = best_opportunity(pair, k_book, p_book, now) if k_book and p_book else None
            if opp is None:
                if pair_id in self._open:
                    self._close(pair_id, "gone" if k_book and p_book else "book unavailable")
            elif pair_id in self._open:
                _, opened, peak = self._open[pair_id]
                self._open[pair_id] = (opp, opened, max(peak, opp.profit))
            else:
                self._open[pair_id] = (opp, time.monotonic(), opp.profit)
                log.info("OPEN %s", describe(opp))
        return sorted((o for o, _, _ in self._open.values()), key=lambda o: o.rate_per_day, reverse=True)

    def _on_update(self, venue: str, market_id: str) -> None:
        pair_ids = self._pairs_by_market.get((venue, market_id))
        if pair_ids:
            self._dirty.update(pair_ids)
            self._changed.set()

    def _close(self, pair_id: int, reason: str) -> None:
        opp, opened, peak = self._open.pop(pair_id)
        log.info("CLOSED pair %d (%s) after %.1fs, peak profit $%.2f", pair_id, reason, time.monotonic() - opened, peak)


def describe(opp: Opportunity) -> str:
    k, p = opp.kalshi, opp.poly
    return (
        f"pair {opp.pair_id}: kalshi {k.market_id} {k.side.upper()} <= {k.limit_price} + "
        f"polymarket {p.market_id} {p.side.upper()} <= {p.limit_price} | qty {opp.qty:.2f} "
        f"cost ${opp.cost:.2f} fees ${opp.fees:.2f} profit ${opp.profit:.2f} | "
        f"{opp.rate_per_day:.4%}/day over {opp.days:.0f} days"
    )
