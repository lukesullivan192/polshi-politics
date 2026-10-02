"""Kalshi order books over the `orderbook_delta` websocket channel.

Kalshi sends an `orderbook_snapshot` per market, then `orderbook_delta`s. Both carry bids only, for
YES and NO; asks come from the opposite side's bids. `seq` increments by one per message on a
subscription, so a gap means a missed update and the books are rebuilt from a fresh subscription.
"""

import logging
from decimal import Decimal
from urllib.parse import urlparse

import config
from opportunities import auth
from opportunities.book import Book, complement_asks, sorted_bids
from opportunities.feed import BookFeed, FeedError

log = logging.getLogger(__name__)


class KalshiFeed(BookFeed):
    venue = "kalshi"
    url = config.KALSHI_WS_URL

    def __init__(self, on_update):
        super().__init__(on_update)
        self._bids: dict[str, dict[str, dict[Decimal, Decimal]]] = {}  # ticker -> side -> price -> qty
        self._seq: int | None = None

    def configured(self) -> bool:
        return auth.kalshi_configured()

    def auth_headers(self) -> dict[str, str]:
        return auth.kalshi_headers("GET", urlparse(self.url).path)

    async def subscribe(self, ws, markets: list[str]) -> None:
        self._seq = None
        await ws.send_json(
            {"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": markets}}
        )

    def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "error":
            log.error("kalshi feed error: %s", msg.get("msg"))
            return
        if kind not in ("orderbook_snapshot", "orderbook_delta"):
            return
        seq = msg.get("seq")
        if self._seq is not None and seq != self._seq + 1:
            raise FeedError(f"seq gap: expected {self._seq + 1}, got {seq}")
        self._seq = seq
        body = msg["msg"]
        ticker = body["market_ticker"]
        if kind == "orderbook_snapshot":
            self._bids[ticker] = {
                "yes": _levels(body.get("yes_dollars_fp")),
                "no": _levels(body.get("no_dollars_fp")),
            }
        else:
            book = self._bids.get(ticker)
            if book is None:
                raise FeedError(f"delta before snapshot for {ticker}")
            levels = book[body["side"]]
            price = Decimal(body["price_dollars"])
            qty = levels.get(price, Decimal(0)) + Decimal(body["delta_fp"])
            if qty > 0:
                levels[price] = qty
            else:
                levels.pop(price, None)
        self._notify(ticker)

    def book(self, market_id: str) -> Book | None:
        bids = self._bids.get(market_id)
        if bids is None:
            return None
        return Book(
            yes_asks=complement_asks(bids["no"].items()),
            no_asks=complement_asks(bids["yes"].items()),
            yes_bids=sorted_bids(bids["yes"].items()),
            no_bids=sorted_bids(bids["no"].items()),
        )

    def market_ids(self) -> list[str]:
        return list(self._bids)

    def drop_books(self) -> None:
        self._bids.clear()


def _levels(raw) -> dict[Decimal, Decimal]:
    return {Decimal(price): Decimal(qty) for price, qty in raw or []}
