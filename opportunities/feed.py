"""Base websocket order book feed: connect, subscribe, keep books current, reconnect with backoff."""

import asyncio
import json
import logging
from collections.abc import Callable

import aiohttp

import config
from opportunities.book import Book

log = logging.getLogger(__name__)


class FeedError(Exception):
    """The feed's books can't be trusted any more (e.g. missed messages); reconnect to resync."""


class BookFeed:
    venue: str
    url: str

    def __init__(self, on_update: Callable[[str, str], None]):
        self._on_update = on_update  # called with (venue, market_id) whenever a book changes
        self._markets: frozenset[str] = frozenset()
        self._markets_changed = asyncio.Event()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._closing: asyncio.Task | None = None

    # Implemented by each venue.
    def configured(self) -> bool: ...
    def auth_headers(self) -> dict[str, str]: ...
    async def subscribe(self, ws: aiohttp.ClientWebSocketResponse, markets: list[str]) -> None: ...
    def handle(self, msg: dict) -> None: ...
    def book(self, market_id: str) -> Book | None: ...
    def market_ids(self) -> list[str]: ...  # markets that currently have a book
    def drop_books(self) -> None: ...

    def set_markets(self, market_ids) -> None:
        """Track exactly these markets. A change reconnects, so the new subscription starts from fresh snapshots."""
        market_ids = frozenset(market_ids)
        if market_ids == self._markets:
            return
        self._markets = market_ids
        self._markets_changed.set()
        if self._ws is not None and not self._ws.closed:
            self._closing = asyncio.get_running_loop().create_task(self._ws.close())

    async def run(self, session: aiohttp.ClientSession) -> None:
        if not self.configured():
            log.error("%s: API keys not configured (see .env.example); no order books from this venue", self.venue)
            return
        delay = 1.0
        while True:
            if not self._markets:
                self._markets_changed.clear()
                await self._markets_changed.wait()
                continue
            markets = self._markets
            self._markets_changed.clear()
            try:
                async with session.ws_connect(
                    self.url, headers=self.auth_headers(), heartbeat=config.WS_HEARTBEAT_SECONDS
                ) as ws:
                    self._ws = ws
                    await self.subscribe(ws, sorted(markets))
                    log.info("%s feed connected, %d markets", self.venue, len(markets))
                    delay = 1.0
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self.handle(json.loads(msg.data))
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            raise FeedError(f"websocket error: {ws.exception()!r}")
            except (aiohttp.ClientError, asyncio.TimeoutError, FeedError) as e:
                log.warning("%s feed: %r", self.venue, e)
            except Exception:
                # e.g. a message in a shape we don't expect; resync rather than let the feed die.
                log.exception("%s feed crashed", self.venue)
            finally:
                self._ws = None
                self._clear_books()
            if self._markets_changed.is_set():
                continue  # closed on purpose to resubscribe
            log.warning("%s feed disconnected, reconnecting in %.0fs", self.venue, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, config.WS_RECONNECT_MAX_DELAY_SECONDS)

    def _clear_books(self) -> None:
        """Books go stale the moment the connection drops, so forget them rather than trade on them."""
        stale = self.market_ids()
        self.drop_books()
        for market_id in stale:
            self._on_update(self.venue, market_id)
