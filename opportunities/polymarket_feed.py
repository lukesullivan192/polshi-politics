"""Polymarket US order books over the markets websocket (SUBSCRIPTION_TYPE_MARKET_DATA).

Each `marketData` message carries the market's full book: `bids` and `offers` for YES. NO asks come
from the YES bids and NO bids from the YES offers. At most 100 markets fit in one subscription, so larger sets are split.
"""

import logging
from decimal import Decimal
from urllib.parse import urlparse

import config
from opportunities import auth
from opportunities.book import Book, complement_asks, complement_bids, sorted_bids
from opportunities.feed import BookFeed

log = logging.getLogger(__name__)


class PolymarketFeed(BookFeed):
    venue = "polymarket"
    url = config.POLYMARKET_WS_URL

    def __init__(self, on_update):
        super().__init__(on_update)
        self._books: dict[str, Book] = {}

    def configured(self) -> bool:
        return auth.polymarket_configured()

    def auth_headers(self) -> dict[str, str]:
        return auth.polymarket_headers("GET", urlparse(self.url).path)

    async def subscribe(self, ws, markets: list[str]) -> None:
        size = config.POLYMARKET_WS_MAX_MARKETS_PER_SUBSCRIPTION
        for n, start in enumerate(range(0, len(markets), size)):
            await ws.send_json({
                "subscribe": {
                    "requestId": f"md-{n}",
                    "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                    "marketSlugs": markets[start:start + size],
                }
            })

    def handle(self, msg: dict) -> None:
        if "error" in msg:
            log.error("polymarket feed error: %s", msg)
            return
        data = msg.get("marketData")
        if not data:
            return  # heartbeats and subscription acks
        slug = data["marketSlug"]
        if data.get("state", "MARKET_STATE_OPEN") != "MARKET_STATE_OPEN":
            self._books.pop(slug, None)  # halted or closed: nothing to trade against
        else:
            bids = [(_px(level), Decimal(level["qty"])) for level in data.get("bids") or []]
            offers = [(_px(level), Decimal(level["qty"])) for level in data.get("offers") or []]
            self._books[slug] = Book(
                yes_asks=sorted(offers),
                no_asks=complement_asks(bids),
                yes_bids=sorted_bids(bids),
                no_bids=complement_bids(offers),
            )
        self._notify(slug)

    def book(self, market_id: str) -> Book | None:
        return self._books.get(market_id)

    def market_ids(self) -> list[str]:
        return list(self._books)

    def drop_books(self) -> None:
        self._books.clear()


def _px(level: dict) -> Decimal:
    return Decimal(level["px"]["value"])
