"""Fetch open Polymarket US markets in the configured categories."""

import logging

import aiohttp

import config
from markets.http import get_json
from markets.timeutil import to_utc_iso

log = logging.getLogger(__name__)

VENUE = "polymarket"


async def fetch_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Page through open events in tracked categories and keep markets that are open for trading."""
    categories = config.CATEGORIES[VENUE]
    url = f"{config.POLYMARKET_BASE_URL}/events"
    rows: dict[str, dict] = {}
    offset = 0
    pages = 0
    while True:
        # Repeated `categories` params are OR'd by the API.
        params = [("categories", c) for c in categories]
        params += [
            ("closed", "false"),
            ("limit", config.POLYMARKET_PAGE_SIZE),
            ("offset", offset),
        ]
        data = await get_json(session, url, params)
        pages += 1
        events = data.get("events") or []
        for event in events:
            group = categories.get(event.get("category"))
            if group is None:
                continue
            for market in event.get("markets") or []:
                if market.get("status") != "MARKET_STATUS_OPEN":
                    continue
                rows[market["slug"]] = _to_row(event, market, group)
        if len(events) < config.POLYMARKET_PAGE_SIZE:
            break
        offset += config.POLYMARKET_PAGE_SIZE
    log.info("polymarket: %d pages, %d open markets", pages, len(rows))
    return list(rows.values())


def _to_row(event: dict, market: dict, group: str) -> dict:
    return {
        "venue": VENUE,
        "market_id": market["slug"],
        "event_id": event["slug"],
        "category": group,
        "title": event.get("title"),
        "outcome": market.get("title"),
        "status": "open",
        # Event endDate is the release/decision date; market endDate adds a ~2 week buffer.
        "resolution_date": to_utc_iso(event.get("endDate")),
        "rules_text": market.get("description") or None,
    }
