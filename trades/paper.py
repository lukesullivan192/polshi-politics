"""Paper client for DRY_RUN: fills orders against the live websocket books without sending anything.

Simulated fills don't move the real book, so each fill is recorded in the venue's TakenBooks, which
subtracts it until that market's book next changes. Otherwise a dry run would buy the same liquidity
over and over.
"""

from collections.abc import Callable
from decimal import Decimal

from opportunities.book import Level
from trades.orders import Fill, OrderRequest, failed, result
from trades.taken import TakenBooks, within_limit


class PaperClient:
    def __init__(self, books: TakenBooks, fee: Callable[[str, list[Level]], Decimal]):
        self.venue = books.venue
        self._books = books
        self._fee = fee  # (market_id, fills) -> fee

    async def place(self, req: OrderRequest) -> Fill:
        book = self._books.book(req.market_id)
        if book is None:
            return failed("rejected", "no book")
        available = sum((q for _, q in within_limit(book, req)), Decimal(0))
        if req.tif == "fok" and available < req.qty:
            return result(req, Decimal(0), Decimal(0), Decimal(0), None)
        fills = self._books.take(req, min(req.qty, available))
        qty = sum((q for _, q in fills), Decimal(0))
        notional = sum((p * q for p, q in fills), Decimal(0))
        return result(req, qty, notional, self._fee(req.market_id, fills) if fills else Decimal(0), "paper")
