"""Live Polymarket US orders via POST /v1/orders with synchronous execution.

Order prices always refer to the YES side: buying NO at p is sent as BUY_SHORT at 1 - p. The
response's `executions` list the fills (also priced in YES terms) and the commission charged.
"""

import asyncio
import logging
from decimal import Decimal
from urllib.parse import urlparse

import aiohttp

import config
from opportunities import auth
from trades.orders import Fill, OrderRequest, failed, result

log = logging.getLogger(__name__)

TIF = {"fok": "TIME_IN_FORCE_FILL_OR_KILL", "ioc": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"}
INTENT = {
    ("buy", "yes"): "ORDER_INTENT_BUY_LONG",
    ("sell", "yes"): "ORDER_INTENT_SELL_LONG",
    ("buy", "no"): "ORDER_INTENT_BUY_SHORT",
    ("sell", "no"): "ORDER_INTENT_SELL_SHORT",
}
FILL_TYPES = {"EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL"}


class PolymarketClient:
    venue = "polymarket"

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._url = f"{config.POLYMARKET_TRADING_URL}/orders"
        self._path = urlparse(self._url).path

    async def balance(self) -> Decimal:
        """Cash in the account, in dollars: buying power, which leaves out collateral held against positions."""
        url = f"{config.POLYMARKET_TRADING_URL}/account/balances"
        async with self._session.get(url, headers=auth.polymarket_headers("GET", urlparse(url).path)) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        usd = [b for b in data["balances"] if b.get("currency") == "USD"]
        return Decimal(str(usd[0]["buyingPower"])) if usd else Decimal(0)

    async def place(self, req: OrderRequest) -> Fill:
        yes_price = req.limit_price if req.side == "yes" else 1 - req.limit_price
        body = {
            "marketSlug": req.market_id,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{yes_price:.4f}", "currency": "USD"},
            "quantity": float(req.qty),
            "tif": TIF[req.tif],
            "intent": INTENT[(req.action, req.side)],
            "synchronousExecution": True,
            "maxBlockTime": max(1, config.ORDER_TIMEOUT_SECONDS - 2),
        }
        try:
            async with self._session.post(
                self._url,
                json=body,
                headers=auth.polymarket_headers("POST", self._path),
                timeout=aiohttp.ClientTimeout(total=config.ORDER_TIMEOUT_SECONDS),
            ) as resp:
                text = await resp.text()
                if resp.status >= 500:
                    return failed("unknown", f"HTTP {resp.status}: {text[:300]}")
                if resp.status >= 400:
                    return failed("rejected", f"HTTP {resp.status}: {text[:300]}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return failed("unknown", repr(e))
        executions = data.get("executions") or []
        fills = [e for e in executions if e.get("type") in FILL_TYPES]
        qty = sum((Decimal(e["lastShares"]) for e in fills), Decimal(0))
        if qty <= 0:
            rejected = [e for e in executions if e.get("type") == "EXECUTION_TYPE_REJECTED"]
            if rejected:
                return failed("rejected", rejected[0].get("orderRejectReason") or rejected[0].get("text") or "rejected")
            return result(req, qty, Decimal(0), Decimal(0), data.get("id"))
        notional_yes = sum((Decimal(e["lastPx"]["value"]) * Decimal(e["lastShares"]) for e in fills), Decimal(0))
        notional = notional_yes if req.side == "yes" else qty - notional_yes
        fee = sum((Decimal((e.get("commissionNotionalCollected") or {}).get("value") or "0") for e in fills), Decimal(0))
        return result(req, qty, notional, fee, data.get("id"))
