"""Order requests and results, shared by the live venue clients and the paper client."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class OrderRequest:
    venue: str
    market_id: str
    side: str  # 'yes' or 'no': the contract being bought or sold
    action: str  # 'buy' or 'sell'
    qty: Decimal  # contracts
    limit_price: Decimal  # price of `side` (not always YES); worst price accepted
    tif: str  # 'fok' (fill completely or not at all) or 'ioc' (fill what's there, cancel the rest)


@dataclass(frozen=True)
class Fill:
    status: str  # 'filled', 'partial', 'killed' (nothing filled), 'rejected' (venue refused), 'unknown'
    qty: Decimal
    avg_price: Decimal | None  # price of the request's side
    fee: Decimal
    exchange_order_id: str | None = None
    error: str | None = None


def result(req: OrderRequest, qty: Decimal, notional: Decimal, fee: Decimal, order_id: str | None) -> Fill:
    """Build a Fill from a filled quantity and its total notional (sum of price * qty)."""
    if qty <= 0:
        return Fill("killed", Decimal(0), None, Decimal(0), order_id)
    status = "filled" if qty >= req.qty else "partial"
    return Fill(status, qty, notional / qty, fee, order_id)


def failed(status: str, error: str) -> Fill:
    return Fill(status, Decimal(0), None, Decimal(0), error=error)
