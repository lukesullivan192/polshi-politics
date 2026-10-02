# polshi-arb-bot

A bot for finding arbitrage between politics and elections markets on [Kalshi](https://kalshi.com) and [Polymarket US](https://polymarket.us).

## Status

- **Market fetcher**: done. Fetches all open politics and elections markets from both venues into SQLite.
- **Market matcher**: done. Proposes Kalshi/Polymarket US pairs by fuzzy matching; every pair is verified by hand, and verified pairs are auto-suspended when either side's rules change.
- **Opportunity engine**: done. Streams order books for every verified pair over both venues' websockets and logs each arb (after fees) when it opens and closes, ranked by daily rate.
- **Trades engine**: built, running in dry-run mode (`DRY_RUN = True`). Enters arbs, sells one-legged fills, cashes out when that beats holding, and logs every order to `data/trades/trades.db` and the log. Live order placement is written but hasn't been run against the venues yet.

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

The market fetcher only uses public endpoints, but the websocket order book feeds need API keys on both venues:

```bash
cp .env.example .env     # then fill it in; .env is gitignored
mkdir -p keys            # put your Kalshi private key at keys/kalshi.pem (*.pem is gitignored)
```

| setting | where to get it |
|---|---|
| `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` | Kalshi account settings → API keys (RSA or Ed25519 PEM) |
| `POLYMARKET_API_KEY_ID`, `POLYMARKET_API_SECRET` | Polymarket US developer settings (base64 Ed25519 secret, or a path to a file holding it, e.g. `keys/polymarket.key`) |

`config.py` reads these from `.env`. Don't put keys in `config.py` itself: it's committed. Without keys the bot still refreshes markets and matchings, but finds no opportunities.

## Usage

```bash
venv/bin/python main.py
```

One loop handles everything. Every `REFRESH_INTERVAL_SECONDS` (10 minutes) it refreshes markets, then matchings, settles positions whose markets are gone, then points the order book feeds at the verified pairs and any markets with open positions; the rest of the time it runs trading cycles back to back, `TRADING_LOOP_DELAY_SECONDS` (0) apart. A trading cycle waits for a book change, so an idle market doesn't spin the CPU. Trading never runs during a refresh; the feeds keep streaming in the background. Logs go to `logs/polshi.log` and the console. Stop with Ctrl+C.

Review pairs by hand (safe to run while the bot is running):

```bash
venv/bin/python -m matching.review                 # suspended pairs first, then candidates by score
venv/bin/python -m matching.review --stats         # count pairs by status
venv/bin/python -m matching.review --suspend ID    # suspend a verified pair by hand
```

## Layout

```
main.py            the loop: refresh (markets, then matchings) every 10 min, trading in between
config.py          intervals, paths, API URLs and keys (from .env), category mapping, matching thresholds, fees, sizing
markets/
  kalshi.py        Kalshi fetcher (api.elections.kalshi.com /events, status=open, nested markets)
  polymarket.py    Polymarket US fetcher (gateway.polymarket.us /events, closed=false)
  db.py            SQLite schema and per-venue sync
  http.py          aiohttp GET with retry/backoff on 429/5xx
  timeutil.py      UTC timestamp helpers
matching/
  normalize.py     title/outcome normalisation (state codes, districts, parties, kind words)
  candidates.py    fuzzy candidate generation (rapidfuzz)
  matcher.py       per-refresh: add candidates, re-check hashes, suspend/close pairs
  db.py            matchings schema and rules hashing
  review.py        hand-review CLI
opportunities/
  feed.py          websocket base: connect, subscribe, reconnect with backoff, drop books on disconnect
  kalshi_feed.py   Kalshi orderbook_delta channel (snapshot + deltas, seq-checked)
  polymarket_feed.py  Polymarket US markets websocket (full book per message)
  auth.py          signed handshake headers (Kalshi RSA-PSS/Ed25519, Polymarket US Ed25519)
  book.py          normalised book: YES and NO asks
  fees.py          taker fees for each venue
  evaluate.py      arb math for one pair (pure, no I/O)
  engine.py        loads verified pairs, re-evaluates on book changes, logs OPEN/CLOSED, ranks
trades/
  engine.py        sizing, entries, one-leg unwinds, cashouts, settlement, DB + log records
  kalshi.py        live Kalshi orders (POST /portfolio/events/orders, V2 YES-book bid/ask)
  polymarket.py    live Polymarket US orders (POST /v1/orders, synchronous execution)
  paper.py         DRY_RUN client: fills against the live books, remembers what it took
  orders.py        order request / fill types
  db.py            positions and orders schema
data/markets/markets.db      created on first run (gitignored)
data/matchings/matchings.db  created on first run (gitignored)
data/trades/trades.db        created on first run (gitignored)
logs/polshi.log              created on first run (gitignored)
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
| `resolution_date` | UTC ISO; when the market is expected to resolve and pay out. Kalshi market `expected_expiration_time`; Polymarket US event `endDate` |
| `rules_text`      | Kalshi `rules_primary` + `rules_secondary`; Polymarket US market `description` |
| `fee_multiplier`  | Kalshi series `fee_multiplier` (scales the taker fee; 0 means fee-free). NULL for Polymarket US |
| `last_seen`       | UTC ISO of the last run that returned this market |

Each run keeps only open markets (Kalshi market status `active`, Polymarket US `MARKET_STATUS_OPEN`). After a venue's fetch succeeds, any of that venue's rows that weren't returned are deleted. If a venue's fetch fails, its rows are left as they were.

Known data quirks:
- Polymarket US `resolution_date` is day-level (23:59 on the event's end date), and a few open events have an end date already in the past.
- Kalshi uses placeholder dates in 2099 for events without a set date.

## `matchings` table

One row per proposed (Kalshi market, Polymarket US market) pair.

| column              | meaning |
|---------------------|---------|
| `pair_id`           | primary key |
| `kalshi_market_id`  | Kalshi market ticker |
| `poly_slug`         | Polymarket US market slug (unique together with `kalshi_market_id`) |
| `flipped`           | `0`: Kalshi YES == Polymarket YES. `1`: Kalshi YES == Polymarket NO. NULL until verified. Getting this wrong turns an arb into a guaranteed loss |
| `status`            | `candidate`, `verified`, `suspended`, `rejected` or `closed`; only `verified` pairs may be traded |
| `suspend_reason`    | e.g. `kalshi_rules_changed`, `poly_market_closed`, `manual` |
| `match_score`       | fuzzy score 0-100; orders the review queue |
| `kalshi_rules_hash`, `poly_rules_hash` | SHA-256 of each side's rules (whitespace collapsed), captured at verification |
| `kalshi_rules_text`, `poly_rules_text` | rules snapshot at verification, shown as a diff when a pair is suspended |
| `last_checked`      | UTC ISO of the last refresh that confirmed both hashes |

### How pairs move

```
candidate --review--> verified | rejected
verified  --rules changed / manual--> suspended --review--> verified | rejected
verified, suspended --a market closes--> closed
```

Each refresh:
1. **Candidates.** Polymarket US event titles are fuzzy-matched to Kalshi event titles. A pair needs a title score of at least `EVENT_MATCH_MIN_SCORE`, the same kind words (margin, turnout, seats, vice, county, state, ...), the same states and district numbers, and resolution dates within `EVENT_MAX_DAYS_APART`. Inside each event pair, outcomes are matched by name (party tags and accents stripped, party names unified) at `OUTCOME_MATCH_MIN_SCORE` or above. New pairs are added as `candidate`. Pairs already in the table, including rejected ones, are never re-added.
2. **Checks.** For each `verified` pair, both rules hashes are recomputed from `markets.db`. A mismatch sets `suspended`, and a missing market sets `closed`. Otherwise `last_checked` is updated. Candidates and rejected pairs whose markets have closed are deleted.

Candidates always suggest same-outcome pairs. Complement (`flipped=1`) pairs are only set during review, and only when both sides' rules allow exactly two outcomes.

## Opportunity engine

The engine streams order books for both markets of every `verified` pair and re-evaluates a pair whenever either book changes.

**Directions.** Each pair can be arbed two ways. Buying one side on each venue so that exactly one leg pays $1:

| `flipped` | direction A | direction B |
|---|---|---|
| 0 | Kalshi YES + Poly NO | Kalshi NO + Poly YES |
| 1 | Kalshi YES + Poly YES | Kalshi NO + Poly NO |

Kalshi's book only has bids, so a YES ask at `p` is a NO bid at `1 - p`. Polymarket US's book is YES bids and offers, so a NO ask at `p` is a YES bid at `1 - p`.

**Sizing.** Both ask ladders are walked together, cheapest first, while one contract on each costs less than $1. The size kept is the one with the most total profit after fees, with contract cost capped at `MAX_TRADE_SIZE_USD`. There is no minimum edge or profit: every arb that makes money after fees is reported.

**Fees** (taker, since arbs cross the spread):
- Kalshi: `0.07 × fee_multiplier × contracts × p × (1 − p)`, rounded up to the cent, per price level.
- Polymarket US: `0.0695 × contracts × p × (1 − p)`, banker's rounding to the cent ([schedule effective 2026-10-01](https://docs.polymarket.us/fees)). Volume rebates aren't counted.

**Ranking.** Opportunities are ranked by daily rate, `(final / initial) ^ (1 / days) − 1`. `final` is the payout (contracts × $1), `initial` is contract cost plus fees, and `days` is time until the later of the two markets' resolution dates, floored at `RATE_MIN_DAYS` (1). Kalshi's 2099 placeholder dates therefore rank near zero.

**Output.** Each opportunity is logged once when it opens (`OPEN pair …` with legs, limit prices, size, cost, fees, profit and daily rate) and once when it closes (`CLOSED pair …` with how long it lasted and its peak profit). `trading_cycle()` gets the ranked list for the execution engine.

**Staleness.** When a feed disconnects, its books are dropped, so any open opportunity on that venue closes. A Kalshi `seq` gap forces a reconnect and fresh snapshots.

The engine logs every profitable opportunity at its full depth. The trades engine decides how much of each one to take.

## Trades engine

Each trading cycle, after the opportunity engine has re-evaluated changed pairs, the trades engine:
1. retries selling any one-legged (`unhedged`) position,
2. checks every open position for a cashout,
3. walks the opportunities, best daily rate first, and enters what fits, stopping at the first one below `MIN_COMPOUND_RATE_PER_DAY`.

With `DRY_RUN = True` (the default) nothing is sent. `trades/paper.py` fills each order against the live websocket books, and remembers what it took from each market until that market's book next changes. Paper positions are stored with `dry_run = 1` and are kept separate from live ones.

### Settings (`config.py`)

| setting | meaning |
|---|---|
| `DRY_RUN` | `True`: simulate fills. `False`: send real orders |
| `MAX_TRADE_SIZE_USD` | most contract cost (both legs, before fees) in open positions on **one pair**, summed across repeat entries |
| `MAX_PORTFOLIO_SIZE_USD` | most contract cost in open positions across all pairs. Each venue needs its own share of this in cash; the engine doesn't check balances |
| `MIN_COMPOUND_RATE_PER_DAY` | lowest compound daily rate, `(final / initial) ^ (1 / days) − 1`, worth entering (0.001 = 0.1%/day). Checked at the size actually taken. The opportunity engine still logs everything below it |
| `ORDER_TIMEOUT_SECONDS` | how long to wait for an order's result before treating it as unknown |

### Entry
- **Size.** Half the contracts available at profitable prices (the full opportunity's depth), rounded down to whole contracts. It's also capped by whatever room is left under both limits, so a pair that shows up again only tops up to `MAX_TRADE_SIZE_USD`.
- **Prices.** Each leg's limit is the worst price the full opportunity reaches, so there's twice the depth needed at or below it. If filling everything at those limits would lose money, the limits drop to the prices the smaller size needs.
- **Orders.** Both legs are sent at once with `asyncio.gather` as fill-or-kill orders.
- **One leg fills, the other is killed:** the filled leg is sold straight away into the bids (immediate-or-cancel), and the position is logged and closed with reason `unwind`. If the bids can't absorb everything, the rest is retried every cycle.
- **An order's result is unknown** (timeout, network error, 5xx): trading halts with a `CRITICAL` log line until restart, because the order may or may not have filled. Check both venues by hand before restarting.

### Cashout
An open position is sold when selling now earns a better daily rate than holding to resolution:

```
(payout / initial) ^ (1 / days_held) − 1  >  (final / initial) ^ (1 / days_total) − 1
```

- `payout`: what both legs fetch selling the whole position into the current bids, after fees
- `initial`: entry cost plus entry fees
- `final`: contracts held (each pays $1)
- `days_held`: days since entry
- `days_total`: days from entry to resolution

Both day counts are floored at `RATE_MIN_DAYS` (1). Within the first day this means any payout above `initial` plus about one day of the hold rate triggers a cashout.

Both legs are sold at once as fill-or-kill orders. If only one sells, the other is sold straight away (immediate-or-cancel).

### Settlement
When both of a position's markets have left the venues (they've closed), the position is closed with reason `settled`, assuming the winning leg pays $1 per contract.

### `positions` table (`data/trades/trades.db`)

One row per entry. Money is stored as decimal text.

| column | meaning |
|---|---|
| `position_id`, `pair_id` | ids |
| `kalshi_market_id`, `kalshi_side`, `poly_slug`, `poly_side` | the two legs (`yes`/`no`) |
| `kalshi_fee_multiplier` | Kalshi fee multiplier at entry, for exit fees |
| `qty` | contracts bought per leg |
| `kalshi_qty`, `poly_qty` | contracts still held per leg |
| `cost`, `fees` | entry contract cost and fees, both legs |
| `exit_proceeds`, `exit_fees` | from sales (unwind or cashout), or $1 per contract on settlement |
| `resolution_date` | the later of the two markets' resolution dates |
| `status` | `open` (hedged), `unhedged` (one leg left to sell), `closed` |
| `close_reason` | `unwind`, `cashout` or `settled` |
| `profit` | `exit_proceeds − exit_fees − cost − fees`, set on close |
| `opened_at`, `closed_at` | UTC ISO |
| `dry_run` | 1 for paper positions |

### `orders` table

One row per order sent or simulated: `position_id`, `pair_id`, `purpose` (`entry`, `unwind`, `cashout`), `venue`, `market_id`, `side`, `action` (`buy`/`sell`), `tif` (`fok`/`ioc`), `qty_requested`, `limit_price` (price of `side`), `status` (`filled`, `partial`, `killed`, `rejected`, `unknown`), `qty_filled`, `avg_price`, `fee`, `exchange_order_id`, `error`, `dry_run`, `created_at`.

Every order and position change is also logged (`ORDER …`, `ENTER …`, `POSITION … opened/closed`, `CASHOUT …`), with a `[DRY]` prefix in dry runs.

### Before going live
- Run in dry-run mode, then check `data/trades/trades.db` and the log.
- The live clients follow the venues' docs, but haven't placed an order yet. Before trusting a fill, compare the first live fills (prices on NO orders especially, which both venues quote in YES terms) against each venue's website.
