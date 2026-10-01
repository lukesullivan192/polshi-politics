# polshi-arb-bot

Finds arbitrage opportunities between politics and economy markets on [Kalshi](https://kalshi.com) and [Polymarket US](https://polymarket.us).

## Status

- **Market fetcher**: done. Every 15 minutes, fetches all open politics and econ markets from both venues and stores them in SQLite.

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

No API keys are needed; the fetcher only uses public market-data endpoints.

## Usage

```bash
venv/bin/python main.py
```

Runs a market update immediately, then every 15 minutes (`LOOP_INTERVAL_SECONDS` in `config.py`). Logs go to `logs/polshi.log` and the console. Stop with Ctrl+C.

## Layout

```
main.py            15-minute loop; fetches both venues with asyncio.gather
config.py          intervals, paths, API URLs, category mapping
markets/
  kalshi.py        Kalshi fetcher (/events, status=open, nested markets)
  polymarket.py    Polymarket US fetcher (gateway.polymarket.us /events)
  db.py            SQLite schema and per-venue sync
  http.py          aiohttp GET with retry/backoff on 429/5xx
  timeutil.py      UTC timestamp helpers
data/markets/markets.db   created on first run (gitignored)
logs/polshi.log           created on first run (gitignored)
```

## Categories

Each venue's native categories are mapped to two groups in `config.CATEGORIES`:

| group      | Kalshi (event `category`) | Polymarket US (event `category`) |
|------------|---------------------------|----------------------------------|
| `politics` | Politics, Elections       | politics                         |
| `econ`     | Economics                 | macro, finance                   |

To track another category, add its native name to `config.CATEGORIES` with the group it belongs to.

## `markets` table

| column       | meaning |
|--------------|---------|
| `venue`      | `kalshi` or `polymarket` |
| `market_id`  | Kalshi ticker or Polymarket US market slug (PK with `venue`) |
| `event_id`   | Kalshi event ticker or Polymarket US event slug; groups related markets |
| `category`   | `politics` or `econ` |
| `title`      | the event-level question (e.g. "GDP growth in 2036?") |
| `outcome`    | the bucket/candidate this market is for (e.g. "0.0% or Below") |
| `status`     | `open` (only open markets are stored) |
| `resolution_date` | UTC ISO; when the market is expected to resolve and pay out. Kalshi `expected_expiration_time`; Polymarket US event `endDate` (the release/decision date) |
| `rules_text` | Kalshi `rules_primary` + `rules_secondary`; Polymarket US `description` |
| `last_seen`  | UTC ISO of the last run that returned this market |

Only open markets are fetched (Kalshi market status `active`, Polymarket US `MARKET_STATUS_OPEN`). After each successful fetch, any of that venue's rows that weren't returned are deleted. If a venue's fetch fails, its rows are left as they were.
