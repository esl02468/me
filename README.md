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

# 3. Dashboard: live chart + levels + bias + analyzer
python3 dashboard.py                 # → http://localhost:8787
```

No network where you're running it? Every command accepts `--demo`
(or `NQ_DEMO=1`) to run on a realistic synthetic session.

Tests are plain `unittest`, nothing to install:

```bash
python3 -m unittest discover -s tests
```

## Sessions: RTH vs. Globex

CME index futures trade a Globex day from 18:00 ET to 17:00 ET the next
afternoon; the cash-hours part (RTH) is 09:30–16:00 ET. The free 1-minute
feed hands back the *whole* Globex day, so a naive "first 15 minutes" is
the overnight open at 18:00, "the gap" is always zero, and "midday" lands
at 3 a.m. Everything here is session-aware:

- The **chart** shows the full Globex day, with a dotted rule at the cash
  open and VWAP re-anchored there (and at each cash open on multi-day
  frames).
- **Bias, levels, backtests, signals and the edge report** run on the RTH
  bars once the cash session has printed an opening range (15 bars). Before
  09:45 ET they run on the Globex day so overnight traders still get a
  reading; the header says which (`analysis on RTH 09:30–16:00 ET`).
- Auto levels gain **ON high / ON low** (the overnight range) once RTH is
  under way; the opening range and VWAP are the cash session's.
- Prior-day high/low/close are picked by session date, so the Sunday
  evening open and the weekend read Friday as "yesterday".
- `NQ_SESSION=globex` on the server switches every consumer to the whole
  day. All time-of-day logic uses real US Eastern/Central time with the
  exact DST rule (`nq/clock.py`), on any platform, with no packages.

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
candles, ~15 min delayed for CME futures; two days are requested and sliced
to the current Globex day), with one polite retry on rate limits, both
Yahoo hosts tried, a Stooq daily-history fallback for prior-day levels and
a Stooq last-price fallback when Yahoo is out entirely. No API keys needed. The dashboard server proxies the feed so the browser
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
| Opening range | price above the cash session's first 15 minutes' high |
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
(max contracts, daily loss halt, trailing drawdown, flatten-by time in
**Chicago time** whatever the VPS clock says, automation gate), and a trade
copier that fans one signal out to every account whose rules allow it.
Signals are only acted on when the bar that produced them is live by the
wall clock, so the last bar of the day cannot fire again all evening.

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

`/api/edge` and the dashboard's edge panel run **every** published
strategy across 1m/5m/15m/1h on the selected instrument — typically 80+
strategy×timeframe combinations — and report each one's per-trade Sharpe
together with its **Deflated Sharpe Ratio** (Bailey & López de Prado,
2014): the probability the row has a real edge *after* subtracting the best
result luck is expected to produce across every combination searched. A row
clears at DSR ≥ 0.95 — and only with **30 or more closed trades**. The
statistic is asymptotic; five trades cannot estimate the skew and kurtosis
it depends on, and a 5-for-5 streak used to score 1.0 and "clear". Rows
below the sample floor are shown with "too few trades" instead of a
verdict. The expected result is that nothing clears, and the panel says so.

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

**This is an analysis tool, not trade advice.** Fills ignore slippage and
commissions; delayed data means live readings lag the tape.

## Layout

```
nq/clock.py       US Eastern/Central time, exact DST, RTH/Globex boundaries
nq/data.py        market data (Yahoo → Stooq → demo generator), Session model
nq/bias.py        bias engine (VWAP, EMA, ATR, opening range)
nq/backtest.py    core strategies, trade/report accounting
nq/strategies.py  the 28-setup published library on one execution engine
nq/levels.py      levels.json load/save, auto levels
nq/reversal.py    reversal-likelihood ranking of levels
nq/confluence.py  the confluence reversal signal engine
nq/deflate.py     deflated Sharpe ratio
nq/journal.py     SQLite signal journal
nq/news.py        scheduled-news blackout guard
nq/notify.py      ntfy.sh phone push
trader/           Tradovate client, prop-firm risk rails, copier engine
api/              Vercel serverless wrappers around dashboard.py builders
fetch_nq.py       terminal fetcher/bias CLI
analyze.py        terminal backtest CLI
rank_levels.py    terminal level ranking CLI
dashboard.py      stdlib HTTP server + JSON API
index.html        the dashboard UI (self-contained)
tests/            unittest suite (also run in GitHub Actions)
```
