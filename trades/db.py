"""SQLite storage for positions and every order sent (or simulated). Money is stored as decimal text."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    position_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id          INTEGER NOT NULL,
    kalshi_market_id TEXT NOT NULL,
    kalshi_side      TEXT NOT NULL,     -- 'yes' or 'no'
    poly_slug        TEXT NOT NULL,
    poly_side        TEXT NOT NULL,
    kalshi_fee_multiplier TEXT NOT NULL,
    qty              TEXT NOT NULL,     -- contracts bought on each leg
    kalshi_qty       TEXT NOT NULL,     -- contracts still held on each leg
    poly_qty         TEXT NOT NULL,
    cost             TEXT NOT NULL,     -- contract cost paid at entry, both legs
    fees             TEXT NOT NULL,     -- entry fees, both legs
    exit_proceeds    TEXT NOT NULL DEFAULT '0',  -- from selling legs (unwind or cashout)
    exit_fees        TEXT NOT NULL DEFAULT '0',
    resolution_date  TEXT NOT NULL,     -- UTC ISO; the later of the two markets' dates
    status           TEXT NOT NULL CHECK (status IN ('open', 'unhedged', 'closed')),
    close_reason     TEXT CHECK (close_reason IN ('unwind', 'cashout', 'settled')),
    profit           TEXT,              -- set when closed
    opened_at        TEXT NOT NULL,
    closed_at        TEXT,
    dry_run          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);

CREATE TABLE IF NOT EXISTS orders (
    order_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id      INTEGER,
    pair_id          INTEGER NOT NULL,
    purpose          TEXT NOT NULL CHECK (purpose IN ('entry', 'unwind', 'cashout')),
    venue            TEXT NOT NULL,
    market_id        TEXT NOT NULL,
    side             TEXT NOT NULL,
    action           TEXT NOT NULL,
    tif              TEXT NOT NULL,
    qty_requested    TEXT NOT NULL,
    limit_price      TEXT NOT NULL,
    status           TEXT NOT NULL,     -- filled, partial, killed, rejected, unknown
    qty_filled       TEXT NOT NULL,
    avg_price        TEXT,
    fee              TEXT NOT NULL,
    exchange_order_id TEXT,
    error            TEXT,
    dry_run          INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_position ON orders (position_id);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn
