"""A venue's live books minus liquidity we've just taken.

Our own fills take a moment to show up in a venue's websocket book, and the two venues don't catch up at
the same time. So what we take from a market is remembered and subtracted until that market's book next
changes; otherwise the engine would see (and try to buy) the same liquidity again. Dry runs rely on this
completely, since simulated fills never reach the real book.

If a book changes for an unrelated reason before our fill shows up, the subtraction is dropped early;
ENTRY_COOLDOWN_SECONDS covers that gap for entries.
"""

from decimal import Decimal

from opportunities.book import Book, Level, walk
from opportunities.feed import BookFeed
from trades.orders import OrderRequest


class TakenBooks:
    def __init__(self, feed: BookFeed):
        self.venue = feed.venue
        self._feed = feed
        self._taken: dict[str, tuple[int, dict[tuple, Decimal]]] = {}  # market -> (book version, level -> qty)

    def book(self, market_id: str) -> Book | None:
        """The live book minus what we've taken from it since it last changed."""
        book = self._feed.book(market_id)
        if book is None:
            return None
        taken = self._current(market_id)
        if not taken:
            return book
        return Book(
            yes_asks=_remaining(book.yes_asks, taken, ("yes", "ask")),
            no_asks=_remaining(book.no_asks, taken, ("no", "ask")),
            yes_bids=_remaining(book.yes_bids, taken, ("yes", "bid")),
            no_bids=_remaining(book.no_bids, taken, ("no", "bid")),
        )

    def take(self, req: OrderRequest, qty: Decimal) -> list[Level]:
        """Record taking `qty` contracts for `req`, best prices first within its limit; returns the levels taken.
        Live fills are recorded after the order returns, so if the book already shows the fill this
        subtracts it twice until the next change, which only makes us see less liquidity."""
        book = self.book(req.market_id)
        if book is None or qty <= 0:
            return []
        levels = within_limit(book, req)
        fills = walk(levels, qty) or levels
        kind = "ask" if req.action == "buy" else "bid"
        taken = self._current(req.market_id)
        for price, q in fills:
            taken[(req.side, kind, price)] = taken.get((req.side, kind, price), Decimal(0)) + q
        self._taken[req.market_id] = (self._feed.versions.get(req.market_id, 0), taken)
        return fills

    def _current(self, market_id: str) -> dict[tuple, Decimal]:
        """What we've taken since the market's book last changed; a change means the venue's book is newer
        than our fills, so start over."""
        version, taken = self._taken.get(market_id, (None, {}))
        return taken if version == self._feed.versions.get(market_id, 0) else {}


def within_limit(book: Book, req: OrderRequest) -> list[Level]:
    """The levels `req` could fill against: asks at or below its limit for a buy, bids at or above for a sell."""
    if req.action == "buy":
        return [(p, q) for p, q in book.asks(req.side) if p <= req.limit_price]
    return [(p, q) for p, q in book.bids(req.side) if p >= req.limit_price]


def _remaining(levels: list[Level], taken: dict[tuple, Decimal], key: tuple[str, str]) -> list[Level]:
    out = []
    for price, qty in levels:
        left = qty - taken.get((*key, price), Decimal(0))
        if left > 0:
            out.append((price, left))
    return out
