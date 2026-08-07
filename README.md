# NQ Toolkit

Real-time NQ (Nasdaq-100 futures) price fetcher, bias indicator, intraday
strategy backtester, and a live dashboard with your chart levels. Pure Python
standard library — nothing to install.

## Quick start

```bash
# 1. Live price + bias in the terminal (one shot, or --watch to stream)
python3 fetch_nq.py
python3 fetch_nq.py --watch

# 2. Strategy Analyzer: backtest today's 1-minute session
python3 analyze.py
python3 analyze.py --trades          # include the per-trade log
python3 analyze.py --strategy orb    # single strategy

# 3. AlgoBox: eight order-flow modules, on bars and on the tape
python3 -m algobox                   # both engines, side by side
python3 -m algobox --ticks trades.csv  # tick engines on real order flow

# 4. Dashboard: live chart + levels + bias + analyzer + AlgoBox
python3 dashboard.py                 # → http://localhost:8787
```

No network where you're running it? Every command accepts `--demo`
(or `NQ_DEMO=1`) to run on a realistic synthetic session.

## Run it on a Windows VPS (24/7, live, no login wall)

One elevated PowerShell on the VPS:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
irm https://raw.githubusercontent.com/esl02468/me/main/deploy/windows-setup.ps1 -OutFile setup.ps1
.\setup.ps1
```

It clones the repo to `C:\nq-toolkit`, generates a private access token,
registers a scheduled task that starts the dashboard at boot and restarts
it if it dies, opens the firewall port, and prints the URL —
`http://<vps-ip>:8787/?token=<token>` — which works from any computer or
phone. The token is the password: keep the URL private. Re-run the script
any time to update the code (URL stays the same).

`dashboard.py` flags behind this: `--host 0.0.0.0` binds publicly,
`--token <secret>` requires the token on every request (also honored as an
`X-NQ-Token` header or `NQ_TOKEN` env var).

## View it anywhere (Vercel)

The repo deploys to Vercel as-is: `index.html` is served at the root and
`api/*.py` run as Python serverless functions that fetch live data
server-side. Every push gets a preview URL; merging to `main` updates the
production URL. Append `?demo=1` to the page URL to force the synthetic
session (useful if the data source rate-limits cloud IPs). The cloud
deployment is read-only — change levels by editing `levels.json` and
pushing.

## Data sources

Live data comes from Yahoo Finance's public chart API (`NQ=F`, 1-minute
candles, ~15 min delayed for CME futures) with a Stooq last-price fallback.
No API keys needed. The dashboard server proxies the feed so the browser
never deals with CORS, and caches upstream calls (10 s) so polling stays
polite.

## Your chart levels — `levels.json`

```json
{
  "levels": [
    {"price": 23600, "label": "Weekly high", "kind": "resistance"},
    {"price": 23380, "label": "Overnight low", "kind": "support"}
  ]
}
```

Edit the file (or `POST /api/levels`) and the dashboard picks it up on the
next refresh. Auto-levels — prior-day high/low/close, VWAP, opening-range
high/low — are computed live and drawn alongside; don't duplicate them.

## Bias indicator

Five components each vote −1 / 0 / +1; the sum (−5…+5) maps to
STRONG BEARISH → STRONG BULLISH:

| Component | Bullish when… |
|---|---|
| VWAP | price above session VWAP |
| EMA 9/21 | fast EMA above slow |
| Opening range | price above the first 15 minutes' high |
| Prev close | trading above yesterday's close |
| Momentum | last 10 minutes' net move exceeds ATR |

## Instruments

Pick any Yahoo symbol from the header dropdown (NQ, MNQ, ES, MES, YM, RTY,
GC, SI, CL, BTC-USD, ETH-USD, SPY, QQQ, single stocks, or "custom…").
Everything — chart, bias, levels, analyzer, signals — recomputes for the
chosen symbol. `levels.json` supports per-symbol levels:
`{"symbols": {"NQ=F": [...], "ES=F": [...]}}` (a legacy flat `levels` list
still reads as NQ=F). The free live estimate maps futures to a real-time
ETF proxy (NQ→QQQ, ES→SPY, YM→DIA, RTY→IWM, GC→GLD, CL→USO...).

## Trading signals — the confluence engine

The old signal layer replayed backtest entries from whichever heuristic
strategies had a hot day — noisy and unexplainable. It's replaced by one
**confluence engine** (`nq/confluence.py`): every bar near a still-credible
ranked level is scored 0–100 for reversal quality —

- **level quality** (0–35): the level's reversal-rank score (bounce
  history, confluence, type prior)
- **VWAP stretch** (0–25): ATRs of exhaustion fuel
- **rejection bar** (0–20): hammer/shooting-star close, boosted on a
  pierce-and-reclaim of the level
- **RSI-2 exhaustion** (0–10), **volume surge** (0–5), **momentum
  deceleration** (0–5)

Gates: a level alone never fires (≥15 pts of secondary evidence required);
a 10-bar freight-train drive against the reversal vetoes it; strong
opposing bias damps 15%. Default threshold 60 (`NQ_CONFLUENCE_MIN`),
one signal per level-side per 30 bars, outcomes simulated with a
symmetric 1.2 ATR bracket **net of costs** and journaled.

Calibration across 12 independent synthetic sessions: threshold 60 →
~2 signals/day, 63.6% win rate, net positive; 65 → ~0.7/day at 75%.
Every marker shows its score, and hovering its bar shows the full
reasoning and outcome. On a strong trend day the engine may honestly show
**zero** signals — refusing to catch falling knives is the feature.
Signals run on whatever series the chart displays (all timeframes and
range-bar frames).

## AlgoBox — order flow, read twice

`algobox/` is a suite of eight modules, each of which answers the same
question in **two versions**:

- **simple** — everything derived from OHLCV bars. Works on any feed, any
  timeframe, in milliseconds.
- **tick-level** — everything derived from the print stream: aggressor
  side, per-price ladders, and time.

Same eight modules, same output shape, so they can be run side by side.
That comparison *is* the product. Bars and tape agreeing is confirmation;
the tape saying "buyers absorbed at 23500" while the bars say "clean
breakout" is the part you couldn't have seen otherwise.

| Module | What the bars can see | What the tape adds |
|---|---|---|
| **Speedometer** | volume/range per bar vs the session median | prints/sec and contracts/sec, plus the peak 5-second burst |
| **Trend strength** | EMA9/21 separation in ATRs, close consistency, ADX-lite | how much of the move the delta actually paid for; aggressor run lengths vs a coin flip |
| **Cumulative delta** | close-position-weighted volume, and its divergence from price | true signed volume, trade by trade |
| **Aggression** | share of volume closing in the upper half of its bar | share of prints lifting the offer, and whether buyers or sellers are the bigger prints |
| **Stacked imbalance** | price bands crossed one way only across 30 bars | the classic 3:1 diagonal test on the real ladder, stacked 3 deep |
| **Absorption** | heavy volume in a small range, delta one-sided | *which price* took the size, how one-sided it was there, and how few ticks it moved |
| **Liquidity sweep** | a level pierced on heavy volume and closed back through | contracts traded beyond the level and how many seconds price stayed through it |
| **Volume zones** | volume spread along each bar's implied path → POC, value area, HVN/LVN | true volume-at-price, and which side built the POC |

Directional modules score −100…+100; the speedometer has no side, so it
never votes — it damps conviction when the tape is dead. The composite is
a confidence-weighted blend (delta and absorption carry the most weight,
because they are the two that most often disagree with price — a composite
that only echoes price is not worth computing).

### Where the tick data comes from — read this

The free feeds this repo uses (Yahoo, Stooq) do not sell tick data. So:

```bash
export ALGOBOX_TICK_FILE=/path/to/trades.csv   # or --ticks trades.csv
```

points every tick-level module at a real tape. The CSV needs `price` and a
timestamp; `size`, `side`/`aggressor`, and `bid`/`ask` are used when
present, and aggressors are inferred with the tick rule when they aren't.

**Without a tick file, the tape is reconstructed from the same bars.**
Reconstruction preserves what the bar proves — open, high, low, close,
volume, a plausible path — and models the rest. Aggressor side comes
strictly from the path: prints made on the way up lifted the offer, prints
made on the way down hit the bid, and prints that moved nothing are split
in the proportion the bar closed at. **Nothing random ever touches side.**
An earlier draft added a random "hidden flow" term so reconstructed delta
would diverge from price the way real delta does; it was cut, because it
made the tick engines report a direction that came from a seed. A number
that changes sign between runs is not analysis.

So reconstructed tick readings add *resolution* — where in the bar the
volume traded, at which prices, in what runs, how long price spent through
a level — but they cannot reveal flow the bars don't already imply. They
carry `confidence` 0.6, every panel labels `tick_source`, and the
dashboard says so on the card. If the difference matters to your decision,
get a tick file.

Reconstruction is deterministic and content-addressed: identical OHLCV
always yields identical prints, so a polling dashboard doesn't flicker and
appending new bars never rewrites the history of old ones.

### Using it

```bash
python3 -m algobox                       # both engines, side by side
python3 -m algobox --demo                # synthetic session, no network
python3 -m algobox --engine tick         # tape only
python3 -m algobox --symbol ES=F --tf 5m
python3 -m algobox --json                # machine-readable
```

```python
from algobox import analyze
analyze("NQ=F", engine="both")           # dict, JSON-ready
```

`GET /api/algobox[?symbol=&tf=&rb=&engine=&demo=1]` serves the same thing
to the dashboard's AlgoBox card, which shows both engines, the per-module
gap, and flags any module where the two point opposite ways.

**Limits.** These are transparent heuristics with hand-set thresholds, not
a fitted model, and they carry no track record — unlike the confluence
engine, AlgoBox readings are not journaled or win-rate tested. Read it as
context for a decision, not as a signal to trade.

## Chart

TradingView-style interactions: mousewheel zooms around the cursor, drag
pans, double-click resets. The crosshair shows a dynamic date-time badge on
the time axis and a price badge on the price axis. Timeframe toggle: 1m /
5m / 15m / 1h / 1D plus range bars (R2 / R5 / R10 points, built from 1m
data). Horizontal scroll (trackpad swipe or shift+wheel) pans the chart.
The **F** button (top right of the chart) jumps to the newest bars and
keeps auto-following in real time at your current zoom, best-fit scaled;
panning or zooming away from the right edge disengages it, F or
double-click re-engages.
Indicator chips toggle VWAP, EMA 9/21, Bollinger bands (20, 2σ, shaded),
and the volume pane. Each level keeps one identity color, matched exactly
between the chart line and the swatch in the Chart Levels card; thick
dashes are your levels, thin are auto. The 🔔 toggle
enables audio alerts: two-tone beep when price reaches a shown level
(high pitch = resistance, low = support) and a three-note chime on a bias
flip.

## Live estimate (free real-time)

CME futures data is ~15 min delayed on free feeds. The header's
"≈ live est." price is a nowcast: Yahoo serves QQQ (Nasdaq-100 ETF) in
real time, and the server computes the NQ/QQQ ratio over the overlapping
delayed window and applies it to QQQ's latest print. Good for context and
alerts; never for execution.

## Automated trading (Tradovate) — sim first

`trader/` contains the execution scaffold: a Tradovate REST client
(demo environment by default), per-account prop-firm risk rules
(max contracts, daily loss halt, trailing drawdown, flatten-by time,
automation gate), and a trade copier that fans one signal out to every
account whose rules allow it.

```bash
cp trader/config.example.json trader/config.json   # fill in accounts/rules
python3 -m trader.engine --paper                   # log-only, no credentials needed
python3 -m trader.engine --sim                     # orders to Tradovate DEMO
```

Live trading additionally requires the environment variable
`TRADOVATE_LIVE=YES_I_UNDERSTAND` — an explicit typed acknowledgment.

**Prop-firm warning:** many prop firms restrict or forbid fully automated
trading and/or cross-firm trade copying. The `allow_automation` flag per
account defaults to false; enable it only after reading your firm's
current policy. The risk module enforces loss limits — it cannot make
automation allowed.

## Track record — the signal journal

Every closed signal is recorded once in `journal.db` (SQLite, gitignored;
`NQ_JOURNAL_PATH` to relocate, `NQ_JOURNAL=0` to disable) on the machine
running the server — the VPS, running 24/7, is where it accumulates. The
dashboard's Track Record card and `python3 -m nq.journal --days 90` show
per-strategy/per-frame win rates and net points across days. This is the
record that turns "proven today" into "proven, period" — demand weeks of
active days before automating anything.

## Costs

Backtests charge 0.75 points of friction per closed trade (≈ MNQ round-trip
commission + a tick of slippage; override with `NQ_COST_PTS`). Win rates,
profit factors, signal qualification, and the journal are all net.

## News guard

`news.json` holds dated high-impact releases (CPI, FOMC, ...) and NFP
first-Fridays are added automatically. Within a window (5 min before to
10 min after; `NQ_NEWS_BEFORE_MIN`/`NQ_NEWS_AFTER_MIN`) the dashboard
shows a blackout banner and the trading engine refuses new entries. An
upcoming release inside the hour shows a countdown warning and triggers an
audio caution at 10 minutes. Keep news.json current from your calendar.

## Phone push (free, no AI)

Set `NQ_NTFY_TOPIC` to a long random topic name and subscribe to it in
the free ntfy app — fresh chart signals and engine order confirmations
push to your phone via ntfy.sh. The topic name is the password; keep it
secret.

## Position sizing

The dashboard's Position Size card converts your stop distance and dollar
risk into MNQ ($2/pt) and NQ ($20/pt) contract counts — sized to what your
account's remaining loss room actually allows.

## Reversal-likelihood ranking

`python3 rank_levels.py` scores every level (yours + auto) on how likely it
is to act as a reversal area, and ranks them:

- **empirical** — today's tape at the level: touch episodes → bounce rate
  (rejection ≥ 1 ATR within 15 bars) vs. breaks (close through the band)
- **kind prior** — PDH/PDL > opening range > VWAP > prev close; user levels
  carry a deliberate-S/R prior
- **confluence** — other levels stacked within 0.5 ATR
- **bias alignment** — supports score higher in bullish tape, resistances in
  bearish tape
- **break penalty** — levels already violated today lose credibility

Output is a 0–100 confidence score (HIGH ≥ 70, MODERATE ≥ 45, LOW below) —
a transparent heuristic, **not a true probability**. The dashboard shows the
same ranking as `#rank · right%` badges in the Chart Levels card.

**>51%-right filter (default on):** the CLI and dashboard hide what today's
tape has *proven wrong* — levels that were tested and held ≤ 51% of the
time, and strategies whose win rate is ≤ 51%. Untested levels stay visible
(marked "untested"): the untouched levels ahead of price are exactly where
the next reversal can happen, so only evidence against a level removes it.
Use `--all` (CLI) or untick the checkbox (dashboard) to see everything;
`--min-rate 0.6` raises the bar.

## Strategy leaderboard

`/api/leaderboard` and the dashboard's leaderboard card rank **every**
published strategy across 1m/5m/15m/1h on the selected instrument —
typically 80+ strategy×timeframe combinations — by **expectancy per trade
in ATR units**, so timeframes compare fairly (raw points would just rank
bar size). Net of costs, minimum 5 trades, ✓ marking win rate > 51% with
profit factor > 1.

There is no public register of "the world's best" trading strategies: the
genuinely elite ones are never published. What this ranks is the public
canon — Raschke's Holy Grail and 80-20, the original Turtle channel
breakouts, Crabel's NR7, the TTM squeeze, Supertrend, Connors RSI-2,
Donchian, MACD, Bollinger/Keltner, opening-range and session-structure
plays — measured honestly on your own instrument. One session is never
proof; the journal is where multi-day evidence accumulates.

## Strategy Analyzer — 34 strategies

Backtests today's 1-minute candles with next-bar-open fills, one contract,
no costs beyond the charged friction, across 34 named published setups:
EMA cross, opening-range breakout, VWAP fade/reclaim, EMA pullback, level
bounce, Connors RSI-2, RSI-50 cross, Bollinger reversion + breakout,
Keltner fade, MACD cross, Donchian and Turtle-55 breakouts, inside-bar
breakout, engulfing reversal, three-bar pullback, gap fade, gap-and-go,
initial-balance breakout, midday VWAP reversion, momentum thrust, ATR
trend ride, opening drive, floor-pivot bounce, swing failure, Raschke's
Holy Grail and 80-20, Crabel's NR7, the TTM squeeze, Supertrend flip,
EMA-50 reclaim, failed-break liquidity sweeps, and VWAP σ-band reversion. All share one execution engine so
results are comparable; the >51% filter surfaces the ones earning trust
today. Add a strategy in ~5 lines in `nq/strategies.py`.

PnL is reported in points and dollars for both NQ ($20/pt) and MNQ ($2/pt).
Add a strategy by writing a function in `nq/backtest.py` and registering it
in `STRATEGIES`.

**This is an analysis tool, not trade advice.** Fills ignore slippage and
commissions; delayed data means live readings lag the tape.

## Layout

```
nq/data.py         market data (Yahoo → Stooq → demo generator)
nq/bias.py         bias engine (VWAP, EMA, ATR, opening range)
nq/backtest.py     strategies + reports
nq/levels.py       levels.json load/save
nq/confluence.py   the reversal signal engine
algobox/core.py    AlgoBox types + the simple/tick module contract
algobox/ticks.py   tick files, bar→tick reconstruction, footprints
algobox/flow.py    delta, aggression, imbalance, absorption
algobox/tape.py    speedometer, liquidity sweeps
algobox/structure.py  volume zones, trend strength
algobox/suite.py   runs both engines and compares them
fetch_nq.py        terminal fetcher/bias CLI
analyze.py         terminal backtest CLI
dashboard.py       stdlib HTTP server + JSON API
index.html         the dashboard UI (self-contained)
tests/             python3 -m unittest discover tests
```
