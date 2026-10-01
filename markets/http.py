"""Shared HTTP helper with retry/backoff."""

import asyncio
import logging

import aiohttp

import config

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


async def get_json(session: aiohttp.ClientSession, url: str, params=None) -> dict:
    """GET a URL and return parsed JSON, retrying on rate limits, server errors and network errors."""
    for attempt in range(config.MAX_RETRIES + 1):
        delay = config.RETRY_BASE_DELAY_SECONDS * 2**attempt
        try:
            async with session.get(url, params=params) as resp:
                if resp.status in RETRY_STATUSES and attempt < config.MAX_RETRIES:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        delay = max(delay, float(retry_after))
                    log.warning("HTTP %s from %s, retrying in %.1fs", resp.status, url, delay)
                else:
                    resp.raise_for_status()
                    return await resp.json()
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            if attempt >= config.MAX_RETRIES:
                raise
            log.warning("%s from %s, retrying in %.1fs", type(e).__name__, url, delay)
        await asyncio.sleep(delay)
    raise RuntimeError("unreachable")
