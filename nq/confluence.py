"""Confluence reversal signals: one explainable score instead of a strategy zoo.

A reversal is worth taking when independent kinds of evidence stack at the
same bar. Each bar near a ranked level is scored 0-100:

  level_quality  0-35   at a level (within 0.35 ATR), scaled by that
                        level's reversal-rank score (bounce history,
                        confluence, type prior — from nq.reversal)
  stretch        0-25   distance from VWAP in ATRs on the reversal side —
                        exhaustion fuel (saturates at 2.5 ATR)
  rejection      0-20   hammer/shooting-star close position, boosted when
                        the bar pierced the level and closed back
  rsi2           0-10   RSI(2) exhaustion (<10 for longs, >90 for shorts)
  volume         0-5    touch volume vs 20-bar average — capitulation
  deceleration   0-5    3-bar momentum shrinking vs the prior 3 bars

Gates: a level alone is not confluence (>=15 pts of secondary evidence
required); freight-train veto — 10-bar net move against the reversal
> 2.5 ATR kills the signal; a strong opposing bias (|score| >= 4) damps
15%. Fires at score >= FIRE_THRESHOLD (default 60, NQ_CONFLUENCE_MIN to
override), one per level-side per COOLDOWN_BARS. Outcomes are simulated
with a symmetric 1.2 ATR bracket net of costs, so the composite carries
an honest daily and journaled record. Calibration across 12 independent
synthetic sessions: threshold 60 -> ~2 signals/day, 63.6% win, net
positive; 65 -> ~0.7/day at 75%. Real sessions re-prove it daily.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .backtest import COST_POINTS, Report, Trade, _close_trade
from .bias import BiasResult, vwap
from .data import Candle
from .reversal import LevelScore
from .strategies import atr_series, rsi_series

import os as _os

FIRE_THRESHOLD = float(_os.environ.get("NQ_CONFLUENCE_MIN", "60"))
COOLDOWN_BARS = 30
LEVEL_TOL_ATR = 0.35
BRACKET_ATR = 1.2
MAX_HOLD = 40


@dataclass
class ConfluenceSignal:
    ts: int
    side: str            # "long" | "short"
    price: float         # close of the signal bar (entry is next bar open)
    score: float
    level_label: str
    level_price: float
    reasons: list[str] = field(default_factory=list)
    # filled by outcome simulation:
    entry_ts: int | None = None
    entry: float | None = None
    exit_ts: int | None = None
    exit: float | None = None
    points: float | None = None
    open: bool = False


def _wick_rejection(c: Candle, level: float, side: str) -> float:
    """0-1: rejection quality — where the bar closed within its own range
    (hammer/shooting-star shape), boosted when it pierced the level itself."""
    rng = max(c.high - c.low, 1e-9)
    if side == "long":
        close_pos = max(0.0, (c.close - c.low) / rng - 0.5) * 2  # 0..1, close near high
        pierced = 0.3 if c.low < level and c.close > level else 0.0
    else:
        close_pos = max(0.0, (c.high - c.close) / rng - 0.5) * 2
        pierced = 0.3 if c.high > level and c.close < level else 0.0
    return min(1.0, close_pos + pierced)


def detect(
    candles: list[Candle],
    scored_levels: list[LevelScore],
    bias: BiasResult | None = None,
) -> list[ConfluenceSignal]:
    n = len(candles)
    if n < 40 or not scored_levels:
        return []
    closes = [c.close for c in candles]
    atrs = atr_series(candles)
    vw = vwap(candles)
    rsi2 = rsi_series(closes, 2)
    vols = [c.volume or 0.0 for c in candles]
    signals: list[ConfluenceSignal] = []
    last_fire: dict[tuple[float, str], int] = {}

    for i in range(30, n):
        c = candles[i]
        a = atrs[i] or 1.0
        tol = max(LEVEL_TOL_ATR * a, 1.0)
        for lv in scored_levels:
            # Long at a level tagged from above; short at one tagged from below.
            if candles[i - 1].close > lv.price and c.low <= lv.price + tol:
                side = "long"
            elif candles[i - 1].close < lv.price and c.high >= lv.price - tol:
                side = "short"
            else:
                continue
            key = (lv.price, side)
            if i - last_fire.get(key, -10**9) < COOLDOWN_BARS:
                continue
            # Freight train veto: don't step in front of a strong 10-bar drive.
            drive = closes[i] - closes[i - 10]
            if (side == "long" and drive < -2.5 * a) or (side == "short" and drive > 2.5 * a):
                continue

            reasons = []
            level_q = 0.35 * lv.score
            reasons.append(f"{lv.label} lvl {lv.score:.0f}")

            stretch_atr = (vw[i] - c.close) / a if side == "long" else (c.close - vw[i]) / a
            stretch = 25.0 * min(max(stretch_atr, 0.0), 2.5) / 2.5
            if stretch > 8:
                reasons.append(f"{stretch_atr:.1f} ATR from VWAP")

            rej = 20.0 * _wick_rejection(c, lv.price, side)
            if rej > 8:
                reasons.append("rejection bar")

            r2 = rsi2[i]
            ex = (10 - r2) / 10 if side == "long" else (r2 - 90) / 10
            rsi_pts = 10.0 * min(max(ex, 0.0), 1.0)
            if rsi_pts > 4:
                reasons.append(f"RSI2 {r2:.0f}")

            avg_vol = sum(vols[max(0, i - 20):i]) / max(1, min(20, i))
            vol_ratio = (vols[i] / avg_vol) if avg_vol else 0.0
            vol_pts = 5.0 * min(max(vol_ratio - 1.0, 0.0), 1.5) / 1.5
            if vol_pts > 2:
                reasons.append(f"vol {vol_ratio:.1f}x")

            mom_now = abs(closes[i] - closes[i - 3])
            mom_prev = abs(closes[i - 3] - closes[i - 6])
            decel = 5.0 if mom_now < mom_prev else 0.0
            if decel:
                reasons.append("decelerating")

            score = level_q + stretch + rej + rsi_pts + vol_pts + decel
            # A level alone is not confluence: demand real secondary evidence.
            if score - level_q < 15:
                continue
            if bias is not None:
                opposing = (side == "long" and bias.score <= -4) or (side == "short" and bias.score >= 4)
                if opposing:
                    score *= 0.85
                    reasons.append("against strong bias (damped)")
            if score < FIRE_THRESHOLD:
                continue
            last_fire[key] = i
            signals.append(ConfluenceSignal(
                ts=c.ts, side=side, price=c.close, score=round(score, 1),
                level_label=lv.label, level_price=lv.price, reasons=reasons,
            ))
            break  # one signal per bar
    return signals


def simulate(signals: list[ConfluenceSignal], candles: list[Candle]) -> Report:
    """Attach bracket outcomes (entry next bar open, ±1.2 ATR, time stop)."""
    report = Report(strategy="confluence")
    atrs = atr_series(candles)
    idx_by_ts = {c.ts: i for i, c in enumerate(candles)}
    for sig in signals:
        i = idx_by_ts.get(sig.ts)
        if i is None or i + 1 >= len(candles):
            sig.open = True
            continue
        a = atrs[i] or 1.0
        entry_bar = candles[i + 1]
        trade = Trade(side=sig.side, entry_ts=entry_bar.ts, entry=entry_bar.open)
        report.trades.append(trade)
        sgn = 1 if sig.side == "long" else -1
        target = trade.entry + sgn * BRACKET_ATR * a
        stop = trade.entry - sgn * BRACKET_ATR * a
        for j in range(i + 1, min(i + 1 + MAX_HOLD, len(candles))):
            c = candles[j]
            hit_stop = c.low <= stop if sig.side == "long" else c.high >= stop
            hit_target = c.high >= target if sig.side == "long" else c.low <= target
            if hit_stop:  # conservative: stop checked first
                _close_trade(trade, c, stop, "stop")
                break
            if hit_target:
                _close_trade(trade, c, target, "target")
                break
        if trade.exit is None:
            j = min(i + MAX_HOLD, len(candles) - 1)
            if j > i:
                _close_trade(trade, candles[j], candles[j].close, "time")
        sig.entry_ts, sig.entry = trade.entry_ts, trade.entry
        sig.exit_ts, sig.exit = trade.exit_ts, trade.exit
        sig.points = round(trade.points, 2) if trade.exit is not None else None
        sig.open = trade.exit is None
    return report
