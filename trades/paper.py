"""Paper client for DRY_RUN: fills orders against the live websocket books without sending anything.

Simulated fills don't move the real book, so the client remembers what it took from each market and
subtracts it until that market's book next changes. Otherwise a dry run would buy the same liquidity
over and over.
"""

from collections.abc import Callable
from decimal import Decimal

from opportunities.book import Book, Level, walk
from opportunities.feed import BookFeed
from trades.orders import Fill, OrderRequest, failed, result


class PaperClient:
    def __init__(self, feed: BookFeed, fee: Callable[[str, list[Level]], Decimal]):
        self.venue = feed.venue
        self._feed = feed
        self._fee = fee  # (market_id, fills) -> fee
        self._taken: dict[str, tuple[int, dict[tuple, Decimal]]] = {}  # market -> (book version, level -> qty)

    def book(self, market_id: str) -> Book | None:
        """The live book minus what this client has already taken from it."""
        book = self._feed.book(market_id)
        if book is None:
            return None
        taken = self._current_taken(market_id)
        if not taken:
            return book
        return Book(
            yes_asks=_remaining(book.yes_asks, taken, ("yes", "ask")),
            no_asks=_remaining(book.no_asks, taken, ("no", "ask")),
            yes_bids=_remaining(book.yes_bids, taken, ("yes", "bid")),
            no_bids=_remaining(book.no_bids, taken, ("no", "bid")),
        )

    async def place(self, req: OrderRequest) -> Fill:
        book = self.book(req.market_id)
        if book is None:
            return failed("rejected", "no book")
        taken = self._current_taken(req.market_id)
        kind = "ask" if req.action == "buy" else "bid"
        levels = book.asks(req.side) if req.action == "buy" else book.bids(req.side)
        in_limit = [
            (price, qty)
            for price, qty in levels
            if (price <= req.limit_price if req.action == "buy" else price >= req.limit_price)
        ]
        fills = walk(in_limit, req.qty)
        if fills is None:
            if req.tif == "fok":
                return result(req, Decimal(0), Decimal(0), Decimal(0), None)
            fills = in_limit
        for price, qty in fills:
            taken[(req.side, kind, price)] = taken.get((req.side, kind, price), Decimal(0)) + qty
        self._taken[req.market_id] = (self._feed.versions.get(req.market_id, 0), taken)
        qty = sum((q for _, q in fills), Decimal(0))
        notional = sum((p * q for p, q in fills), Decimal(0))
        return result(req, qty, notional, self._fee(req.market_id, fills) if fills else Decimal(0), "paper")

    def _current_taken(self, market_id: str) -> dict[tuple, Decimal]:
        """What we've taken since the market's book last changed; a change means the venue's real book
        no longer includes our simulated fills, so start over."""
        version, taken = self._taken.get(market_id, (None, {}))
        return taken if version == self._feed.versions.get(market_id, 0) else {}


def _remaining(levels: list[Level], taken: dict[tuple, Decimal], key: tuple[str, str]) -> list[Level]:
    out = []
    for price, qty in levels:
        left = qty - taken.get((*key, price), Decimal(0))
        if left > 0:
            out.append((price, left))
    return out
