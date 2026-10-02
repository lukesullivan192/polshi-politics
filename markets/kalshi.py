"""Fetch open Kalshi markets in the configured categories."""

import logging

import aiohttp

import config
from markets.http import get_json
from markets.timeutil import to_utc_iso

log = logging.getLogger(__name__)

VENUE = "kalshi"


async def fetch_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Page through all open events (with nested markets) and keep active markets in tracked categories."""
    categories = config.CATEGORIES[VENUE]
    fee_multipliers = await _fee_multipliers(session)
    url = f"{config.KALSHI_BASE_URL}/events"
    rows: dict[str, dict] = {}
    cursor = ""
    pages = 0
    while True:
        params = {
            "status": "open",
            "with_nested_markets": "true",
            "limit": config.KALSHI_PAGE_SIZE,
        }
        if cursor:
            params["cursor"] = cursor
        data = await get_json(session, url, params)
        pages += 1
        events = data.get("events") or []
        for event in events:
            group = categories.get(event.get("category"))
            if group is None:
                continue
            for market in event.get("markets") or []:
                if market.get("status") != "active":
                    continue
                fee_multiplier = fee_multipliers.get(event.get("series_ticker"))
                rows[market["ticker"]] = _to_row(event, market, group, fee_multiplier)
        cursor = data.get("cursor")
        if not cursor or not events:
            break
    log.info("kalshi: %d pages, %d open markets", pages, len(rows))
    return list(rows.values())


async def _fee_multipliers(session: aiohttp.ClientSession) -> dict[str, float]:
    """Series ticker -> taker fee multiplier, for every series in the tracked categories."""
    out = {}
    for category in config.CATEGORIES[VENUE]:
        data = await get_json(session, f"{config.KALSHI_BASE_URL}/series", {"category": category})
        for series in data.get("series") or []:
            if series.get("fee_type") != "quadratic":
                log.warning("kalshi series %s has fee_type %r; fees assume quadratic", series.get("ticker"), series.get("fee_type"))
            out[series["ticker"]] = series.get("fee_multiplier")
    return out


def _to_row(event: dict, market: dict, group: str, fee_multiplier: float | None) -> dict:
    rules = "\n\n".join(r for r in (market.get("rules_primary"), market.get("rules_secondary")) if r)
    return {
        "venue": VENUE,
        "market_id": market["ticker"],
        "event_id": market.get("event_ticker") or event["event_ticker"],
        "category": group,
        "title": event.get("title"),
        "outcome": market.get("yes_sub_title"),
        "resolution_date": to_utc_iso(market.get("expected_expiration_time")),
        "rules_text": rules or None,
        "fee_multiplier": fee_multiplier,
    }
