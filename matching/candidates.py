"""Fuzzy candidate generation: propose (Kalshi market, Polymarket US market) pairs for review.

Two stages:
1. Event pairs: each Polymarket event's title is compared against every Kalshi event title.
   A pair survives if the titles are similar, have the same kind words ("margin", "turnout", ...),
   name the same states, and resolve within EVENT_MAX_DAYS_APART of each other.
2. Market pairs: inside each event pair, each Polymarket outcome is paired with its most
   similar Kalshi outcome. Single-market events with generic outcomes ("Yes", "By Dec 31")
   are paired on the event match alone.

Candidates only suggest flipped=0 (same outcome). Complement pairs are set during review.
"""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from rapidfuzz import fuzz, process

import config
from matching import normalize


@dataclass(frozen=True)
class Candidate:
    kalshi_market_id: str
    poly_slug: str
    score: float  # min(event score, outcome score), 0-100


@dataclass
class _Event:
    event_id: str
    norm_title: str
    kinds: frozenset[str]
    places: frozenset[str]
    resolution_date: datetime | None
    markets: list[tuple[str, str]]  # (market_id, normalized outcome)


def _title_score(a: str, b: str, **_) -> float:
    # token_set alone scores a title 100 against any superset of its words
    # ("georgia governor" vs "georgia lieutenant governor"); averaging with token_sort penalises extras.
    return (fuzz.token_set_ratio(a, b) + fuzz.token_sort_ratio(a, b)) / 2


def _load_events(conn: sqlite3.Connection, venue: str) -> list[_Event]:
    events: dict[str, _Event] = {}
    rows = conn.execute(
        "SELECT event_id, title, outcome, market_id, resolution_date FROM markets WHERE venue = ?",
        (venue,),
    )
    for event_id, title, outcome, market_id, resolution_date in rows:
        ev = events.get(event_id)
        if ev is None:
            norm = normalize.title(title or "")
            ev = events[event_id] = _Event(
                event_id, norm, normalize.kinds(norm), normalize.places(norm), None, []
            )
        if resolution_date:
            rd = datetime.fromisoformat(resolution_date)
            ev.resolution_date = rd if ev.resolution_date is None else min(ev.resolution_date, rd)
        ev.markets.append((market_id, normalize.outcome(outcome or "")))
    return list(events.values())


def _events_compatible(p: _Event, k: _Event) -> bool:
    if p.kinds != k.kinds or p.places != k.places:
        return False
    if p.resolution_date and k.resolution_date:
        if abs((p.resolution_date - k.resolution_date).days) > config.EVENT_MAX_DAYS_APART:
            return False
    return True


def find_candidates(markets_conn: sqlite3.Connection) -> list[Candidate]:
    poly = _load_events(markets_conn, "polymarket")
    kalshi = _load_events(markets_conn, "kalshi")
    kalshi_titles = [k.norm_title for k in kalshi]

    best: dict[tuple[str, str], float] = defaultdict(float)
    for p in poly:
        if not p.norm_title:
            continue
        # Fast recall pass with a C scorer, then rescore the shortlist.
        shortlist = process.extract(
            p.norm_title, kalshi_titles, scorer=fuzz.token_set_ratio,
            limit=config.EVENT_MATCH_RECALL_LIMIT, score_cutoff=config.EVENT_MATCH_MIN_SCORE,
        )
        for _, _, j in shortlist:
            k = kalshi[j]
            event_score = _title_score(p.norm_title, k.norm_title)
            if event_score < config.EVENT_MATCH_MIN_SCORE or not _events_compatible(p, k):
                continue
            for kalshi_id, poly_slug, outcome_score in _pair_markets(p, k):
                key = (kalshi_id, poly_slug)
                best[key] = max(best[key], min(event_score, outcome_score))

    return [Candidate(k, p, round(s, 1)) for (k, p), s in best.items()]


def _pair_markets(p: _Event, k: _Event):
    if (
        len(p.markets) == 1 and len(k.markets) == 1
        and normalize.is_generic_outcome(p.markets[0][1])
        and normalize.is_generic_outcome(k.markets[0][1])
    ):
        yield k.markets[0][0], p.markets[0][0], 100.0
        return
    for poly_slug, poly_outcome in p.markets:
        if not poly_outcome or normalize.is_generic_outcome(poly_outcome):
            continue
        scored = [
            (fuzz.token_sort_ratio(poly_outcome, kalshi_outcome), kalshi_id)
            for kalshi_id, kalshi_outcome in k.markets
            if kalshi_outcome
        ]
        if not scored:
            continue
        score, kalshi_id = max(scored)
        if score >= config.OUTCOME_MATCH_MIN_SCORE:
            yield kalshi_id, poly_slug, score
