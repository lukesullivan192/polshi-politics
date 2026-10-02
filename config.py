"""Settings for the polshi arb bot."""

import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# Main loop
REFRESH_INTERVAL_SECONDS = 10 * 60  # how often to refresh markets + matchings
TRADING_LOOP_DELAY_SECONDS = 0  # pause between trading cycles

# Storage
MARKETS_DB_PATH = ROOT / "data" / "markets" / "markets.db"
MATCHINGS_DB_PATH = ROOT / "data" / "matchings" / "matchings.db"
TRADES_DB_PATH = ROOT / "data" / "trades" / "trades.db"
LOG_PATH = ROOT / "logs" / "polshi.log"

# APIs (public market data, no auth needed)
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_PAGE_SIZE = 200  # API max

POLYMARKET_BASE_URL = "https://gateway.polymarket.us/v1"
POLYMARKET_TRADING_URL = "https://api.polymarket.us/v1"  # orders and portfolio (authenticated)
POLYMARKET_PAGE_SIZE = 200

# API keys (needed for the websocket order book feeds). Set them in .env, which is gitignored;
# see .env.example. Never put real keys in this file: it is committed.
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
# RSA or Ed25519 PEM; a relative path is relative to the repo root.
KALSHI_PRIVATE_KEY_PATH = ROOT / os.getenv("KALSHI_PRIVATE_KEY_PATH", "keys/kalshi.pem")
POLYMARKET_API_KEY_ID = os.getenv("POLYMARKET_API_KEY_ID", "")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")  # base64 Ed25519 secret

# Websocket order book feeds
KALSHI_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
POLYMARKET_WS_URL = "wss://api.polymarket.us/v1/ws/markets"
POLYMARKET_WS_MAX_MARKETS_PER_SUBSCRIPTION = 100  # API limit
WS_HEARTBEAT_SECONDS = 30  # ping interval; the connection is dropped if a pong doesn't come back
WS_RECONNECT_MAX_DELAY_SECONDS = 60  # reconnect backoff doubles from 1s up to this

# HTTP
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 6  # retries on 429 / 5xx / network errors, with exponential backoff
RETRY_BASE_DELAY_SECONDS = 1.0

# Category mapping: venue's native category -> our group.
# Kalshi: matched against each event's `category` field.
# Polymarket US: passed as `categories` filters on /events and matched against the event's `category`.
# To track another category, add its native name here with the group it belongs to.
CATEGORIES = {
    "kalshi": {
        "Politics": "politics",
        "Elections": "politics",
    },
    "polymarket": {
        "politics": "politics",
    },
}

# Matching (fuzzy candidate generation; every pair is verified by hand)
EVENT_MATCH_RECALL_LIMIT = 20  # Kalshi events shortlisted per Polymarket event before rescoring
EVENT_MATCH_MIN_SCORE = 80  # 0-100 title similarity needed for an event pair
EVENT_MAX_DAYS_APART = 120  # max gap between the two events' resolution dates
OUTCOME_MATCH_MIN_SCORE = 85  # 0-100 outcome similarity needed for a market pair

# Fees (taker, since arbs cross the spread). Both are rate * contracts * price * (1 - price).
KALSHI_TAKER_FEE_RATE = Decimal("0.07")  # times the series' fee_multiplier; rounded up to the cent
POLYMARKET_TAKER_FEE_RATE = Decimal("0.0695")  # schedule effective 2026-10-01; banker's rounding to the cent

# Opportunity engine (pairs only; no minimum edge or profit, every profitable arb is logged)
RATE_MIN_DAYS = 1  # days (to resolution, or held) are floored at this when computing daily rates

# Trades engine
DRY_RUN = True  # simulate fills against the live books instead of sending orders; set False to trade for real
MAX_TRADE_SIZE_USD = Decimal("100")  # most contract cost (both legs, before fees) in open positions on one pair
MAX_PORTFOLIO_SIZE_USD = Decimal("1000")  # most contract cost in open positions across all pairs
ORDER_TIMEOUT_SECONDS = 10  # an order whose result doesn't come back in time halts trading (state unknown)
