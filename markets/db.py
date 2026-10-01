"""SQLite storage for markets."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    venue       TEXT NOT NULL,  -- 'kalshi' or 'polymarket'
    market_id   TEXT NOT NULL,  -- Kalshi ticker or Polymarket US market slug
    event_id    TEXT,           -- groups related markets (buckets/rungs/candidates of one event)
    category    TEXT,           -- our group: 'politics' or 'econ'
    title       TEXT,           -- the event-level question
    outcome     TEXT,           -- the bucket/candidate this market is for
    status      TEXT,           -- 'open', 'closed' or 'missing'
    resolution_date TEXT,       -- UTC ISO; when the result is expected to be determined (payout)
    rules_text  TEXT,
    last_seen   TEXT NOT NULL,  -- UTC ISO of the last run that returned this market
    PRIMARY KEY (venue, market_id)
);
CREATE INDEX IF NOT EXISTS idx_markets_event ON markets (venue, event_id);
CREATE INDEX IF NOT EXISTS idx_markets_category ON markets (category);
"""

COLUMNS = (
    "venue", "market_id", "event_id", "category", "title", "outcome",
    "status", "resolution_date", "rules_text", "last_seen",
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def sync_venue(conn: sqlite3.Connection, venue: str, rows: list[dict], run_ts: str) -> tuple[int, int]:
    """Upsert this run's markets for a venue and delete that venue's markets not returned this run.

    Returns (upserted, deleted).
    """
    placeholders = ", ".join("?" for _ in COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c not in ("venue", "market_id"))
    sql = (
        f"INSERT INTO markets ({', '.join(COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT (venue, market_id) DO UPDATE SET {updates}"
    )
    with conn:
        conn.executemany(sql, [tuple({**r, "last_seen": run_ts}[c] for c in COLUMNS) for r in rows])
        deleted = conn.execute(
            "DELETE FROM markets WHERE venue = ? AND last_seen <> ?", (venue, run_ts)
        ).rowcount
    return len(rows), deleted
