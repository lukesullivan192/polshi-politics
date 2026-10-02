"""Hand review of market pairs.

    venv/bin/python -m matching.review                 review suspended pairs, then candidates
    venv/bin/python -m matching.review --stats         count pairs by status
    venv/bin/python -m matching.review --suspend ID    suspend a verified pair by hand
"""

import argparse
import difflib
import sqlite3
import textwrap

import config
from markets.timeutil import utc_now_iso
from matching import db

CHECKLIST = """\
Before verifying, check:
  1. same subject: same person/party, office, race and year
  2. same trigger: wins / called / certified / sworn in
  3. same deadline, and the same handling of runoffs, recounts, withdrawals, ties
  4. direction: [v] Kalshi YES == Poly YES.  [f] Kalshi YES == Poly NO -- only if the
     rules on BOTH sides allow exactly two outcomes; if a third is possible, reject."""

PROMPT = "[v]erify  [f]lip-verify  [r]eject  [s]kip  [q]uit > "


def _market(markets_conn, venue: str, market_id: str) -> dict | None:
    row = markets_conn.execute(
        "SELECT title, outcome, resolution_date, rules_text FROM markets WHERE venue = ? AND market_id = ?",
        (venue, market_id),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(("title", "outcome", "resolution_date", "rules_text"), row))


def _show_market(label: str, market_id: str, m: dict) -> None:
    print(f"{label:<7}{market_id}    resolves {m['resolution_date']}")
    print(f"       {m['title']}  ->  {m['outcome']}")
    for para in (m["rules_text"] or "(no rules text)").splitlines():
        print(textwrap.fill(para, width=100, initial_indent="       ", subsequent_indent="       "))
    print()


def _show_diff(label: str, old: str | None, new: str | None) -> None:
    if db.rules_hash(old) == db.rules_hash(new):
        return
    print(f"--- {label} rules changed since verification ---")
    for line in difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(), "verified", "current", lineterm=""
    ):
        print("   " + line)
    print()


def _verify(conn, pair_id: int, flipped: int, k: dict, p: dict) -> None:
    with conn:
        conn.execute(
            "UPDATE matchings SET status = 'verified', flipped = ?, suspend_reason = NULL, "
            "kalshi_rules_hash = ?, poly_rules_hash = ?, kalshi_rules_text = ?, poly_rules_text = ?, "
            "last_checked = ? WHERE pair_id = ?",
            (flipped, db.rules_hash(k["rules_text"]), db.rules_hash(p["rules_text"]),
             k["rules_text"], p["rules_text"], utc_now_iso(), pair_id),
        )


def review(markets_conn, conn) -> None:
    queue = conn.execute(
        "SELECT pair_id, kalshi_market_id, poly_slug, status, suspend_reason, match_score, flipped, "
        "kalshi_rules_text, poly_rules_text FROM matchings WHERE status IN ('suspended', 'candidate') "
        "ORDER BY status = 'candidate', match_score DESC, pair_id"
    ).fetchall()
    if not queue:
        print("Nothing to review.")
        return
    print(CHECKLIST)
    for n, (pair_id, kalshi_id, poly_slug, status, reason, score, flipped,
            k_old, p_old) in enumerate(queue, 1):
        k = _market(markets_conn, "kalshi", kalshi_id)
        p = _market(markets_conn, "polymarket", poly_slug)
        if k is None or p is None:
            continue  # market closed since the last refresh; the next refresh cleans it up
        print("\n" + "=" * 100)
        header = f"[{n}/{len(queue)}] pair {pair_id}  {status}"
        if status == "suspended":
            header += f" ({reason}), was flipped={flipped}"
        else:
            header += f"  score {score}"
        print(header + "\n")
        if status == "suspended":
            _show_diff("KALSHI", k_old, k["rules_text"])
            _show_diff("POLY", p_old, p["rules_text"])
        _show_market("KALSHI", kalshi_id, k)
        _show_market("POLY", poly_slug, p)

        while True:
            choice = input(PROMPT).strip().lower()
            if choice in ("v", "f", "r", "s", "q"):
                break
        if choice == "q":
            return
        if choice == "s":
            continue
        if choice == "r":
            with conn:
                conn.execute("UPDATE matchings SET status = 'rejected' WHERE pair_id = ?", (pair_id,))
            continue
        if choice == "f" and input("Flipped: Kalshi YES pays exactly when Poly NO pays? type 'yes' > ").strip() != "yes":
            print("Not verified; left in the queue.")
            continue
        _verify(conn, pair_id, 1 if choice == "f" else 0, k, p)


def suspend(conn, pair_id: int) -> None:
    with conn:
        cur = conn.execute(
            "UPDATE matchings SET status = 'suspended', suspend_reason = 'manual' "
            "WHERE pair_id = ? AND status = 'verified'",
            (pair_id,),
        )
    print(f"pair {pair_id} suspended" if cur.rowcount else f"pair {pair_id} is not a verified pair")


def stats(conn) -> None:
    for status, count in conn.execute(
        "SELECT status, COUNT(*) FROM matchings GROUP BY status ORDER BY status"
    ):
        print(f"{status:<10} {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Review Kalshi / Polymarket US market pairs.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--stats", action="store_true", help="count pairs by status")
    group.add_argument("--suspend", type=int, metavar="PAIR_ID", help="suspend a verified pair")
    args = parser.parse_args()

    conn = db.connect(config.MATCHINGS_DB_PATH)
    if args.stats:
        stats(conn)
    elif args.suspend is not None:
        suspend(conn, args.suspend)
    else:
        markets_conn = sqlite3.connect(f"file:{config.MARKETS_DB_PATH}?mode=ro", uri=True)
        try:
            review(markets_conn, conn)
        except (KeyboardInterrupt, EOFError):
            print()


if __name__ == "__main__":
    main()
