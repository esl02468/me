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

## Strategy Analyzer

Backtests today's 1-minute candles with next-bar-open fills, one contract,
no costs. Built-ins:

- `ema_cross` — EMA 9/21 crossover, always-in flip
- `orb` — 15-minute opening-range breakout, 1.5× ATR stop / 3× ATR target
- `vwap_fade` — fade 2× ATR stretches from VWAP back to VWAP

PnL is reported in points and dollars for both NQ ($20/pt) and MNQ ($2/pt).
Add a strategy by writing a function in `nq/backtest.py` and registering it
in `STRATEGIES`.

**This is an analysis tool, not trade advice.** Fills ignore slippage and
commissions; delayed data means live readings lag the tape.

## Layout

```
nq/data.py       market data (Yahoo → Stooq → demo generator)
nq/bias.py       bias engine (VWAP, EMA, ATR, opening range)
nq/backtest.py   strategies + reports
nq/levels.py     levels.json load/save
fetch_nq.py      terminal fetcher/bias CLI
analyze.py       terminal backtest CLI
dashboard.py     stdlib HTTP server + JSON API
static/dashboard.html   the dashboard UI (self-contained)
```
