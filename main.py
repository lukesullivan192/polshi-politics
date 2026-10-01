"""Entry point: refresh Kalshi and Polymarket US markets every LOOP_INTERVAL_SECONDS."""

import asyncio
import logging
import time

import aiohttp

import config
from markets import db, kalshi, polymarket
from markets.timeutil import utc_now_iso

log = logging.getLogger("polshi")

FETCHERS = {
    kalshi.VENUE: kalshi.fetch_markets,
    polymarket.VENUE: polymarket.fetch_markets,
}


def setup_logging() -> None:
    config.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.FileHandler(config.LOG_PATH), logging.StreamHandler()):
        handler.setFormatter(fmt)
        root.addHandler(handler)


async def update_markets(session: aiohttp.ClientSession, conn) -> None:
    """Fetch both venues concurrently and sync each one that succeeded into the DB."""
    run_ts = utc_now_iso()
    start = time.monotonic()
    venues = list(FETCHERS)
    results = await asyncio.gather(*(FETCHERS[v](session) for v in venues), return_exceptions=True)
    for venue, result in zip(venues, results):
        if isinstance(result, BaseException):
            # Leave this venue's rows untouched so a failed fetch doesn't wipe them.
            log.error("%s fetch failed: %r", venue, result)
            continue
        upserted, deleted = db.sync_venue(conn, venue, result, run_ts)
        log.info("%s: %d markets stored, %d removed", venue, upserted, deleted)
    log.info("market update finished in %.1fs", time.monotonic() - start)


async def main() -> None:
    setup_logging()
    conn = db.connect(config.MARKETS_DB_PATH)
    timeout = aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            start = time.monotonic()
            try:
                await update_markets(session, conn)
            except Exception:
                log.exception("market update crashed")
            elapsed = time.monotonic() - start
            await asyncio.sleep(max(0.0, config.LOOP_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
