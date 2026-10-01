"""Settings for the polshi arb bot."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Main loop
LOOP_INTERVAL_SECONDS = 15 * 60

# Storage
MARKETS_DB_PATH = ROOT / "data" / "markets" / "markets.db"
LOG_PATH = ROOT / "logs" / "polshi.log"

# APIs (public market data, no auth needed)
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_PAGE_SIZE = 200  # API max

POLYMARKET_BASE_URL = "https://gateway.polymarket.us/v1"
POLYMARKET_PAGE_SIZE = 200

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
        "Economics": "econ",
    },
    "polymarket": {
        "politics": "politics",
        "macro": "econ",
        "finance": "econ",
    },
}
