"""One matching refresh: add new candidates, then re-check every live pair against current rules."""

import logging
import sqlite3
import time

from markets.timeutil import utc_now_iso
from matching import db
from matching.candidates import find_candidates

log = logging.getLogger(__name__)


def refresh_matchings(markets_conn: sqlite3.Connection, matchings_conn: sqlite3.Connection) -> None:
    start = time.monotonic()
    rules = {
        (venue, market_id): rules_text
        for venue, market_id, rules_text in markets_conn.execute(
            "SELECT venue, market_id, rules_text FROM markets"
        )
    }
    now = utc_now_iso()
    with matchings_conn:
        added = _add_candidates(markets_conn, matchings_conn)
        counts = _check_pairs(matchings_conn, rules, now)
    log.info(
        "matchings: %d new candidates, %d verified ok, %d suspended, %d closed, %d stale removed (%.1fs)",
        added, counts["ok"], counts["suspended"], counts["closed"], counts["removed"],
        time.monotonic() - start,
    )


def _add_candidates(markets_conn, matchings_conn) -> int:
    candidates = find_candidates(markets_conn)
    before = matchings_conn.total_changes
    # UNIQUE (kalshi_market_id, poly_slug) means pairs already seen under any status are skipped,
    # so rejected pairs are never re-proposed.
    matchings_conn.executemany(
        "INSERT OR IGNORE INTO matchings (kalshi_market_id, poly_slug, status, match_score) "
        "VALUES (?, ?, 'candidate', ?)",
        [(c.kalshi_market_id, c.poly_slug, c.score) for c in candidates],
    )
    return matchings_conn.total_changes - before


def _check_pairs(conn, rules: dict, now: str) -> dict[str, int]:
    counts = {"ok": 0, "suspended": 0, "closed": 0, "removed": 0}
    pairs = conn.execute(
        "SELECT pair_id, kalshi_market_id, poly_slug, status, kalshi_rules_hash, poly_rules_hash "
        "FROM matchings WHERE status IN ('candidate', 'rejected', 'verified', 'suspended')"
    ).fetchall()
    for pair_id, kalshi_id, poly_slug, status, k_hash, p_hash in pairs:
        k_rules = rules.get(("kalshi", kalshi_id))
        p_rules = rules.get(("polymarket", poly_slug))
        k_gone = ("kalshi", kalshi_id) not in rules
        p_gone = ("polymarket", poly_slug) not in rules

        if status in ("candidate", "rejected"):
            # Nothing to protect once a market is gone; drop it to keep the table small.
            if k_gone or p_gone:
                conn.execute("DELETE FROM matchings WHERE pair_id = ?", (pair_id,))
                counts["removed"] += 1
            continue

        if k_gone or p_gone:
            reason = "kalshi_market_closed" if k_gone else "poly_market_closed"
            conn.execute(
                "UPDATE matchings SET status = 'closed', suspend_reason = ?, last_checked = ? "
                "WHERE pair_id = ?",
                (reason, now, pair_id),
            )
            counts["closed"] += 1
            log.warning("pair %d closed: %s", pair_id, reason)
            continue

        if status != "verified":
            continue  # suspended pairs wait for review

        changed = [
            side for side, current, saved in (
                ("kalshi", k_rules, k_hash), ("poly", p_rules, p_hash),
            )
            if db.rules_hash(current) != saved
        ]
        if changed:
            reason = "_and_".join(changed) + "_rules_changed"
            conn.execute(
                "UPDATE matchings SET status = 'suspended', suspend_reason = ? WHERE pair_id = ?",
                (reason, pair_id),
            )
            counts["suspended"] += 1
            log.warning("pair %d suspended: %s", pair_id, reason)
        else:
            conn.execute("UPDATE matchings SET last_checked = ? WHERE pair_id = ?", (now, pair_id))
            counts["ok"] += 1
    return counts
