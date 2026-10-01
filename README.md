# polshi-arb-bot

A bot for finding arbitrage between politics and elections markets on [Kalshi](https://kalshi.com) and [Polymarket US](https://polymarket.us).

## Status

- **Market fetcher**: done. Every 15 minutes, fetches all open politics and elections markets from both venues and stores them in SQLite.
- Market matching and arbitrage detection are not built yet.

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
  kalshi.py        Kalshi fetcher (api.elections.kalshi.com /events, status=open, nested markets)
  polymarket.py    Polymarket US fetcher (gateway.polymarket.us /events, closed=false)
  db.py            SQLite schema and per-venue sync
  http.py          aiohttp GET with retry/backoff on 429/5xx
  timeutil.py      UTC timestamp helpers
data/markets/markets.db   created on first run (gitignored)
logs/polshi.log           created on first run (gitignored)
```

## Categories

`config.CATEGORIES` maps each venue's native event categories to our groups:

| group      | Kalshi (event `category`) | Polymarket US (event `category`) |
|------------|---------------------------|----------------------------------|
| `politics` | Politics, Elections       | politics                         |

To track another category, add its native name to `config.CATEGORIES` with the group it belongs to.

## `markets` table

One row per open market. Primary key is (`venue`, `market_id`).

| column            | meaning |
|-------------------|---------|
| `venue`           | `kalshi` or `polymarket` |
| `market_id`       | Kalshi market ticker or Polymarket US market slug |
| `event_id`        | Kalshi event ticker or Polymarket US event slug; groups the markets of one event |
| `category`        | our group (`politics`) |
| `title`           | the event title, shared by every market in the event (e.g. "Who will be the next Secretary General of NATO?") |
| `outcome`         | the candidate/option this market is for (Kalshi `yes_sub_title`, Polymarket US market `title`, e.g. "Kaja Kallas") |
| `status`          | always `open` (only open markets are stored) |
| `resolution_date` | UTC ISO; when the market is expected to resolve and pay out. Kalshi market `expected_expiration_time`; Polymarket US event `endDate` |
| `rules_text`      | Kalshi `rules_primary` + `rules_secondary`; Polymarket US market `description` |
| `last_seen`       | UTC ISO of the last run that returned this market |

Each run keeps only open markets (Kalshi market status `active`, Polymarket US `MARKET_STATUS_OPEN`). After a venue's fetch succeeds, any of that venue's rows that weren't returned are deleted. If a venue's fetch fails, its rows are left as they were.

Known data quirks:
- Polymarket US `resolution_date` is day-level (23:59 on the event's end date), and a few open events have an end date already in the past.
- Kalshi uses placeholder dates in 2099 for events without a set date.
