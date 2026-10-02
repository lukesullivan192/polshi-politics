"""Entry point: one loop that alternates between refreshing data and trading.

Every REFRESH_INTERVAL_SECONDS it refreshes markets (both venues) and then matchings; the rest
of the time it runs trading cycles back to back: find opportunities, then cash out, unwind and enter
trades. Trading never runs during a refresh. The websocket order book feeds and a status heartbeat
run in the background the whole time.
"""

import asyncio
import logging
import time

import aiohttp

import config
from markets import db as markets_db, kalshi, polymarket
from markets.timeutil import utc_now_iso
from matching import db as matchings_db
from matching.matcher import refresh_matchings
from opportunities.engine import OpportunityEngine
from trades import db as trades_db
from trades.engine import TradeEngine

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
        upserted, deleted = markets_db.sync_venue(conn, venue, result, run_ts)
        log.info("%s: %d markets stored, %d removed", venue, upserted, deleted)
    log.info("market update finished in %.1fs", time.monotonic() - start)


async def refresh(session, markets_conn, matchings_conn, engine: OpportunityEngine, trader: TradeEngine) -> None:
    await update_markets(session, markets_conn)
    refresh_matchings(markets_conn, matchings_conn)
    trader.settle(markets_conn)
    engine.reload_pairs(markets_conn, matchings_conn, trader.held_markets())


async def trading_cycle(engine: OpportunityEngine, trader: TradeEngine, timeout: float) -> None:
    """Find opportunities (waits up to `timeout` for a book change), then cash out, unwind and enter."""
    opportunities = await engine.cycle(timeout)
    await trader.cycle(opportunities)


async def heartbeat(engine: OpportunityEngine, trader: TradeEngine) -> None:
    """Log one status line every HEARTBEAT_INTERVAL_SECONDS, with book updates and cycles since the last one."""
    last_updates = engine.kalshi.updates + engine.poly.updates
    last_cycles = engine.cycles
    while True:
        await asyncio.sleep(config.HEARTBEAT_INTERVAL_SECONDS)
        updates = engine.kalshi.updates + engine.poly.updates
        log.info(
            "HEARTBEAT %s | %s | %d book updates, %d cycles in %ds",
            engine.status(), trader.status(), updates - last_updates, engine.cycles - last_cycles,
            config.HEARTBEAT_INTERVAL_SECONDS,
        )
        last_updates, last_cycles = updates, engine.cycles


async def main() -> None:
    setup_logging()
    markets_conn = markets_db.connect(config.MARKETS_DB_PATH)
    matchings_conn = matchings_db.connect(config.MATCHINGS_DB_PATH)
    trades_conn = trades_db.connect(config.TRADES_DB_PATH)
    timeout = aiohttp.ClientTimeout(total=config.REQUEST_TIMEOUT_SECONDS)
    engine = OpportunityEngine()
    async with aiohttp.ClientSession(timeout=timeout) as session:
        trader = TradeEngine(trades_conn, engine, session)
        feeds = engine.start(session)  # noqa: F841 (keeps a reference so the tasks aren't collected)
        pulse = asyncio.create_task(heartbeat(engine, trader))  # noqa: F841
        last_refresh = None
        while True:
            until_refresh = 0 if last_refresh is None else last_refresh + config.REFRESH_INTERVAL_SECONDS - time.monotonic()
            if until_refresh <= 0:
                last_refresh = time.monotonic()
                try:
                    await refresh(session, markets_conn, matchings_conn, engine, trader)
                except Exception:
                    log.exception("refresh crashed")
                continue
            try:
                await trading_cycle(engine, trader, until_refresh)
            except Exception:
                log.exception("trading cycle crashed")
            await asyncio.sleep(config.TRADING_LOOP_DELAY_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
