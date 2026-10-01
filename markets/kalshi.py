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
                rows[market["ticker"]] = _to_row(event, market, group)
        cursor = data.get("cursor")
        if not cursor or not events:
            break
    log.info("kalshi: %d pages, %d open markets", pages, len(rows))
    return list(rows.values())


def _to_row(event: dict, market: dict, group: str) -> dict:
    rules = "\n\n".join(r for r in (market.get("rules_primary"), market.get("rules_secondary")) if r)
    return {
        "venue": VENUE,
        "market_id": market["ticker"],
        "event_id": market.get("event_ticker") or event["event_ticker"],
        "category": group,
        "title": event.get("title"),
        "outcome": market.get("yes_sub_title"),
        "status": "open",
        "resolution_date": to_utc_iso(market.get("expected_expiration_time")),
        "rules_text": rules or None,
    }
