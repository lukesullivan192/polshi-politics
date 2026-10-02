# polshi-politics-bot

A bot for finding arbitrage between politics and elections markets on [Kalshi](https://kalshi.com) and [Polymarket US](https://polymarket.us).

## Status

- **Market fetcher**: done. Fetches all open politics and elections markets from both venues into SQLite.
- **Market matcher**: done. Proposes Kalshi/Polymarket US pairs by fuzzy matching; every pair is verified by hand, and verified pairs are auto-suspended when either side's rules change.
- **Opportunity engine**: done. Streams order books for every verified pair over both venues' websockets and logs each arb (after fees) when it opens and closes, ranked by daily rate.
- **Trades engine**: built and trading live with small limits. Enters arbs, retries the missing leg of a one-legged fill before selling it, cashes out when that beats holding, logs every order to `data/trades/trades.db` and the log, and writes trade events with portfolio value to `logs/trades.log`.

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

Besides event lines (opportunities opening and closing, orders, positions), it logs a `HEARTBEAT` line every `HEARTBEAT_INTERVAL_SECONDS` (60), so a quiet stretch doesn't look like a hang:

```
HEARTBEAT 146 pairs, kalshi 146/146 books, polymarket 143/143 books, 17 open opportunities (best 0.0141%/day) | 20 positions (0 unhedged), $999.08 of $1000.00 deployed | 812 book updates, 455 cycles in 60s
```

`books` is live books out of markets subscribed. `book updates` and `cycles` count since the previous heartbeat; if both are 0 the feeds have stalled.

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
  paper.py         DRY_RUN client: fills against the live books
  taken.py         live books minus liquidity our own fills just took, until the books catch up
  orders.py        order request / fill types
  db.py            positions and orders schema
data/markets/markets.db      created on first run (gitignored)
data/matchings/matchings.db  created on first run (gitignored)
data/trades/trades.db        created on first run (gitignored)
logs/polshi.log              created on first run (gitignored)
logs/trades.log              trade events with portfolio summaries (gitignored)
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

**Ranking.** Opportunities are ranked by continuous daily rate, `ln(final / initial) / days`, the quantity we want to maximize. `final` is the payout (contracts × $1), `initial` is contract cost plus fees, and `days` is time until the later of the two markets' resolution dates, floored at `RATE_MIN_DAYS` (1). Kalshi's 2099 placeholder dates therefore rank near zero.

**Output.** Each opportunity is logged once when it opens (`OPEN pair …` with legs, limit prices, size, cost, fees, profit and daily rate) and once when it closes (`CLOSED pair …` with how long it lasted and its peak profit). `trading_cycle()` gets the ranked list for the execution engine.

**Staleness.** When a feed disconnects, its books are dropped, so any open opportunity on that venue closes. A Kalshi `seq` gap forces a reconnect and fresh snapshots.

The engine logs every profitable opportunity at its full depth. The trades engine decides how much of each one to take.

## Trades engine

Each trading cycle, after the opportunity engine has re-evaluated changed pairs, the trades engine:
1. retries selling any one-legged (`unhedged`) position,
2. checks every open position for a cashout (if `STANDARD_CASHOUT`),
3. walks the opportunities, best daily rate first, and enters what fits, stopping at the first one below `MIN_RATE_PER_DAY` and skipping any with a margin above `MAX_MARGIN`. If the portfolio cap is what stops one getting its full size, it may swap out a weaker position for it (if `CONDITIONAL_CASHOUT`).

With `DRY_RUN = True` nothing is sent. `trades/paper.py` fills each order against the live websocket books. Paper positions are stored with `dry_run = 1` and are kept separate from live ones.

**Our own fills.** A venue's websocket book takes a moment to show our fills, and the two venues don't catch up at the same time. `trades/taken.py` remembers what each fill (live or paper) took from a market and subtracts it until that market's book next changes, so the engine doesn't chase the same liquidity again.

**Restarts.** On startup the engine reloads `open` and `unhedged` positions from `trades.db` (for the current `DRY_RUN` mode only). The first loop refreshes markets before any trading, so positions whose markets resolved while the bot was off are settled first.

### Settings (`config.py`)

| setting | meaning |
|---|---|
| `DRY_RUN` | `True`: simulate fills. `False`: send real orders |
| `MAX_TRADE_SIZE_USD` | most contract cost (both legs, before fees) in open positions on **one pair**, summed across repeat entries |
| `MAX_PORTFOLIO_SIZE_USD` | most contract cost in open positions across all pairs. Each venue needs its own share of this in cash; the engine doesn't check balances |
| `MIN_RATE_PER_DAY` | lowest continuous daily rate, `ln(final / initial) / days`, worth entering (0.0005 = 0.05%/day). Checked at the size actually taken. The opportunity engine still logs everything below it |
| `MAX_MARGIN` | highest margin, profit / (cost + fees), worth entering (0.10 = 10%). Anything better is usually a price that flickers onto a book and is gone before our order lands. Checked at the size actually taken |
| `STANDARD_CASHOUT` | sell a position when cashing out beats holding (see Cashout) |
| `CONDITIONAL_CASHOUT` | when the portfolio is full, swap the weakest position for a better opportunity (see Conditional cashout) |
| `ENTRY_COOLDOWN_SECONDS` | after an entry attempt on a pair, don't enter it again for this long (2s), while the books catch up |
| `HEDGE_RETRY_SECONDS` | when one entry leg fills and the other doesn't, keep retrying the missing leg this long (1s) before selling |
| `HEDGE_RETRY_INTERVAL_SECONDS` | pause between those retries (0.2s) |
| `ORDER_TIMEOUT_SECONDS` | how long to wait for an order's result before treating it as unknown |

### Entry
- **Size.** Half the contracts available at profitable prices (the full opportunity's depth), rounded down to whole contracts. It's also capped by whatever room is left under both limits, so a pair that shows up again only tops up to `MAX_TRADE_SIZE_USD`.
- **Prices.** Each leg's limit is the worst price the full opportunity reaches, so there's twice the depth needed at or below it. If filling everything at those limits would lose money, the limits drop to the prices the smaller size needs.
- **Orders.** Both legs are fill-or-kill. Kalshi goes first; Polymarket is sent only once Kalshi has filled, so a killed Kalshi order (the usual failure: its book shows liquidity that's already gone) leaves us flat instead of holding a Polymarket leg to sell at a loss. The Kalshi leg is stored as a position before Polymarket is sent. Polymarket is ordered for exactly what Kalshi filled, with its limit raised as far as keeps the whole position at `MIN_RATE_PER_DAY` if everything fills there, since its book may have moved during the Kalshi round trip.
- **Client errors.** An order call that throws something the client doesn't handle is treated as an unknown result (trading halts, see below).
- **Cooldown.** A pair isn't entered again within `ENTRY_COOLDOWN_SECONDS` of an attempt. Together with the taken-liquidity tracking this stops a second entry on a book that doesn't show our first fill yet.
- **One leg fills, the other is killed:** the missing leg is retried (fill-or-kill) every `HEDGE_RETRY_INTERVAL_SECONDS` for up to `HEDGE_RETRY_SECONDS`, at any price that keeps the whole position at break-even or better (cost + fees ≤ $1 per contract). The limit is set as high as break-even allows, in case the book moved a tick. If the hedge completes the position is `open` as usual (logged `POSITION … hedged`, orders stored with purpose `entry`). Otherwise the filled leg is sold into the bids (immediate-or-cancel), and the position is closed with reason `unwind`. If the bids can't absorb everything, the rest is retried every cycle.
- **An order's result is unknown** (timeout, network error, 5xx): trading halts with a `CRITICAL` log line until restart, because the order may or may not have filled. Check both venues by hand before restarting.

### Cashout
An open position is sold when selling now earns a better daily rate than holding to resolution:

```
ln(payout / initial) / days_held  >  ln(final / initial) / days_total
```

- `payout`: what both legs fetch selling the whole position into the current bids, after fees
- `initial`: entry cost plus entry fees
- `final`: contracts held (each pays $1)
- `days_held`: days since entry
- `days_total`: days from entry to resolution

Both day counts are floored at `RATE_MIN_DAYS` (1). Within the first day this means any payout above `initial` plus about one day of the hold rate triggers a cashout.

Both legs are sold as fill-or-kill orders, Kalshi first and then Polymarket for what Kalshi sold. If Kalshi doesn't sell, nothing changes. If Polymarket doesn't sell, it is sold straight away (immediate-or-cancel).

### Conditional cashout
When an opportunity can't get its full size because the portfolio is at `MAX_PORTFOLIO_SIZE_USD`, the engine considers selling an open position to make room. It picks the position with the lowest **forward rate**, which is what holding it from now to resolution earns on the money you'd get by selling it now:

```
ln(final / payout) / days_left
```

The entry rate doesn't matter here. That money is already spent. The real choice is between leaving `payout` in this position or moving it into the new trade, and the forward rate is what staying put earns. A position whose prices have moved in your favour has a low forward rate: most of its profit is already in the bid, so it's the cheapest money to free up.

The swap happens only if all of these hold:
- selling realizes a profit (`payout > initial`), so positions are never dumped at a loss and a just-opened position (still below `initial` after fees) is never churned,
- the opportunity's daily rate (after its entry fees) beats the position's forward rate (after exit fees),
- in dollars per day it beats holding too: `deployable × new_rate > payout × forward_rate`, where `deployable` is what the new trade can actually absorb with the freed room. This stops a large position being sold to fund a small trade, with the rest sitting idle.

The position is sold as in a normal cashout (stored with reason `cashout`, logged as `SWAP`), then the opportunity is entered. The swap is checked before the entry, since an entry attempt starts the pair's cooldown. Each opportunity frees room by selling at most one position per cycle; if it still can't get its full size, the next cycle can swap out another. Positions on the same pair are never swapped for themselves.

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

Every order and position change is also logged (`ORDER …`, `ENTER …`, `POSITION … opened/hedged/closed`, `CASHOUT …`), with a `[DRY]` prefix in dry runs.

### Trades log (`logs/trades.log`)

Only trade events: `START` on launch, then `OPENED`, `ONE-LEGGED`, `HEDGED` and `CLOSED (unwind|cashout|settled)` for every position change. Each event is followed by a portfolio line:

```
portfolio: start $177.40 | now $177.40 (+0.00) | at resolution $177.65 (+0.25) | expected annualized 2.97% on the portfolio, 69.21% on $9.75 deployed (2 positions, 17.4 days to resolution on average)
```

- **start**: portfolio value when the bot launched
- **now**: cash plus open positions at cost (entry cost and fees), so it moves with realized profit and loss, not with the bid
- **at resolution**: cash plus $1 per contract of every open position, if they're held to resolution
- **expected annualized**: continuous, `365 × ln(at resolution / now) / days` on the whole portfolio, and `365 × ln(payout / cost) / days` on the deployed money alone (the daily rate × 365). `days` is the payout-weighted average time to resolution (floored at `RATE_MIN_DAYS`)

Cash is read once at startup from both venues (Kalshi `GET /portfolio/balance`, Polymarket US `GET /v1/account/balances` buying power) and is then moved by our own fills and settlements. Trades made by hand on the venues aren't seen until the next restart. In a dry run, cash starts at `MAX_PORTFOLIO_SIZE_USD`, less what open paper positions cost. These lines also go to the main log.

### Before going live
- Run in dry-run mode, then check `data/trades/trades.db` and the log.
- The live clients follow the venues' docs, but haven't placed an order yet. Before trusting a fill, compare the first live fills (prices on NO orders especially, which both venues quote in YES terms) against each venue's website.
