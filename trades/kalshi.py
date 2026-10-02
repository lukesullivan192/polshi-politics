"""Live Kalshi orders via POST /portfolio/events/orders (V2).

V2 quotes everything on the YES book: `bid` buys YES, `ask` sells YES. Buying NO at p is selling YES
at 1 - p, and selling NO at p is buying YES at 1 - p. Fill prices come back in YES terms too.
"""

import asyncio
import logging
import uuid
from decimal import Decimal
from urllib.parse import urlparse

import aiohttp

import config
from opportunities import auth
from trades.orders import Fill, OrderRequest, failed, result

log = logging.getLogger(__name__)

TIF = {"fok": "fill_or_kill", "ioc": "immediate_or_cancel"}


class KalshiClient:
    venue = "kalshi"

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._url = f"{config.KALSHI_BASE_URL}/portfolio/events/orders"
        self._path = urlparse(self._url).path

    async def balance(self) -> Decimal:
        """Cash in the account, in dollars (positions not included)."""
        url = f"{config.KALSHI_BASE_URL}/portfolio/balance"
        async with self._session.get(url, headers=auth.kalshi_headers("GET", urlparse(url).path)) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        return Decimal(data["balance_dollars"])

    async def place(self, req: OrderRequest) -> Fill:
        yes_price = req.limit_price if req.side == "yes" else 1 - req.limit_price
        body = {
            "ticker": req.market_id,
            "client_order_id": str(uuid.uuid4()),
            "side": "bid" if (req.action == "buy") == (req.side == "yes") else "ask",
            "count": f"{req.qty:.2f}",
            "price": f"{yes_price:.4f}",
            "time_in_force": TIF[req.tif],
            "self_trade_prevention_type": "taker_at_cross",
        }
        try:
            async with self._session.post(
                self._url,
                json=body,
                headers=auth.kalshi_headers("POST", self._path),
                timeout=aiohttp.ClientTimeout(total=config.ORDER_TIMEOUT_SECONDS),
            ) as resp:
                text = await resp.text()
                if resp.status >= 500:
                    return failed("unknown", f"HTTP {resp.status}: {text[:300]}")
                if resp.status >= 400:
                    # Refused before matching (bad params, no funds, rate limit, FOK not fillable).
                    return failed("rejected", f"HTTP {resp.status}: {text[:300]}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return failed("unknown", repr(e))
        qty = Decimal(data.get("fill_count") or "0")
        if qty <= 0:
            return result(req, qty, Decimal(0), Decimal(0), data.get("order_id"))
        avg_yes = Decimal(data["average_fill_price"])
        avg = avg_yes if req.side == "yes" else 1 - avg_yes
        fee = Decimal(data.get("average_fee_paid") or "0") * qty
        return result(req, qty, avg * qty, fee, data.get("order_id"))
