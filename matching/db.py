"""SQLite storage for market pairs."""

import hashlib
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS matchings (
    pair_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kalshi_market_id  TEXT NOT NULL,
    poly_slug         TEXT NOT NULL,
    -- 0: Kalshi YES == Polymarket YES. 1: Kalshi YES == Polymarket NO. NULL until verified.
    flipped           INTEGER CHECK (flipped IN (0, 1)),
    status            TEXT NOT NULL
                      CHECK (status IN ('candidate', 'verified', 'suspended', 'rejected', 'closed')),
    suspend_reason    TEXT,     -- why a pair is suspended/closed, e.g. 'kalshi_rules_changed'
    match_score       REAL,     -- fuzzy score 0-100; orders the review queue
    kalshi_rules_hash TEXT,     -- captured at verification
    poly_rules_hash   TEXT,
    kalshi_rules_text TEXT,     -- snapshot at verification, to diff against when rules change
    poly_rules_text   TEXT,
    last_checked      TEXT,     -- UTC ISO of the last refresh that confirmed both hashes
    UNIQUE (kalshi_market_id, poly_slug)
);
CREATE INDEX IF NOT EXISTS idx_matchings_status ON matchings (status);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The review CLI writes while the bot may be running, so wait on locks and use WAL.
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def rules_hash(rules_text: str | None) -> str:
    """SHA-256 of the rules with whitespace collapsed, so reflowed text isn't a 'change'."""
    return hashlib.sha256(" ".join((rules_text or "").split()).encode()).hexdigest()
