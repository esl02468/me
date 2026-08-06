"""Extended strategy library: 18 classic intraday setups on one engine.

Every strategy is a signal function run through the same execution engine
(next-bar-open fills, ATR-scaled bracket, optional time stop), so results
are comparable and adding a strategy is ~5 lines. Together with the five
hand-written strategies in backtest.py and level_bounce, the analyzer
scans 24 named strategies per session.

These are the textbook versions of widely known setups — the point is
breadth of honest measurement, not secret sauce. The >51% filter decides
which of them earned trust on the day.
"""

from __future__ import annotations

from typing import Callable

from .backtest import Report, Trade, _close_trade
from .bias import ema, vwap
from .data import Candle


# ---------- indicator series (computed once, O(n)) ----------

def atr_series(candles: list[Candle], period: int = 14) -> list[float]:
    out = [0.0]
    trs: list[float] = []
    for prev, cur in zip(candles[:-1], candles[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
        window = trs[-period:]
        out.append(sum(window) / len(window))
    return out


def rsi_series(closes: list[float], period: int) -> list[float]:
    out = [50.0] * len(closes)
    gain = loss = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        if i <= period:
            gain += g / period
            loss += l / period
        else:
            gain = (gain * (period - 1) + g) / period
            loss = (loss * (period - 1) + l) / period
        if i >= period:
            out[i] = 100.0 if loss == 0 else 100.0 - 100.0 / (1 + gain / loss)
    return out


def macd_series(closes: list[float]) -> tuple[list[float], list[float]]:
    f, s = ema(closes, 12), ema(closes, 26)
    line = [a - b for a, b in zip(f, s)]
    return line, ema(line, 9)


def rolling_extreme(vals: list[float], period: int, high: bool) -> list[float]:
    """Prior-`period` rolling max/min, shifted one bar (excludes current)."""
    out = [vals[0]] * len(vals)
    for i in range(1, len(vals)):
        window = vals[max(0, i - period):i]
        out[i] = max(window) if high else min(window)
    return out


def stdev_series(vals: list[float], period: int = 20) -> list[float]:
    out = [0.0] * len(vals)
    for i in range(period, len(vals)):
        w = vals[i - period + 1: i + 1]
        m = sum(w) / period
        out[i] = (sum((v - m) ** 2 for v in w) / period) ** 0.5
    return out


# ---------- generic execution engine ----------

def run_engine(
    name: str, candles: list[Candle], signal: Callable[[int], str | None],
    target_atr: float, stop_atr: float, max_hold: int | None = None,
    warmup: int = 30,
) -> Report:
    report = Report(strategy=name)
    n = len(candles)
    if n < warmup + 10:
        return report
    atrs = atr_series(candles)
    position: Trade | None = None
    stop = target = 0.0
    entry_i = 0
    for i in range(warmup, n - 1):
        c = candles[i]
        nxt = candles[i + 1]
        a = atrs[i] or 1.0
        if position is None:
            side = signal(i)
            if side in ("long", "short"):
                position = Trade(side=side, entry_ts=nxt.ts, entry=nxt.open)
                sgn = 1 if side == "long" else -1
                stop = position.entry - sgn * stop_atr * a
                target = position.entry + sgn * target_atr * a
                entry_i = i + 1
                report.trades.append(position)
        else:
            if position.side == "long":
                if c.low <= stop:
                    _close_trade(position, c, stop, "stop")
                    position = None
                elif c.high >= target:
                    _close_trade(position, c, target, "target")
                    position = None
            else:
                if c.high >= stop:
                    _close_trade(position, c, stop, "stop")
                    position = None
                elif c.low <= target:
                    _close_trade(position, c, target, "target")
                    position = None
            if position and max_hold and i - entry_i >= max_hold:
                _close_trade(position, c, c.close, "time")
                position = None
    if position and position.exit is None:
        last = candles[-1]
        _close_trade(position, last, last.close, "eod")
    return report


# ---------- the library ----------

def extended_strategies(
    candles: list[Candle],
    prev_close: float | None = None,
    pivot_levels: list[float] | None = None,
) -> list[Report]:
    n = len(candles)
    if n < 45:
        return []
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    atrs = atr_series(candles)
    e9, e20, e21 = ema(closes, 9), ema(closes, 20), ema(closes, 21)
    rsi2 = rsi_series(closes, 2)
    rsi14 = rsi_series(closes, 14)
    macd, macd_sig = macd_series(closes)
    sd20 = stdev_series(closes, 20)
    boll_up = [e20[i] + 2 * sd20[i] for i in range(n)]
    boll_lo = [e20[i] - 2 * sd20[i] for i in range(n)]
    kel_up = [e20[i] + 1.5 * atrs[i] for i in range(n)]
    kel_lo = [e20[i] - 1.5 * atrs[i] for i in range(n)]
    don_hi = rolling_extreme(highs, 20, True)
    don_lo = rolling_extreme(lows, 20, False)
    vw = vwap(candles)

    up = lambda i: e9[i] > e21[i]
    dn = lambda i: e9[i] < e21[i]

    def sig_rsi2_revert(i):
        if rsi2[i] < 10 and up(i):
            return "long"
        if rsi2[i] > 90 and dn(i):
            return "short"

    def sig_rsi14_cross50(i):
        if rsi14[i - 1] < 50 <= rsi14[i]:
            return "long"
        if rsi14[i - 1] > 50 >= rsi14[i]:
            return "short"

    def sig_boll_revert(i):
        if closes[i - 1] < boll_lo[i - 1] and closes[i] > boll_lo[i]:
            return "long"
        if closes[i - 1] > boll_up[i - 1] and closes[i] < boll_up[i]:
            return "short"

    def sig_boll_break(i):
        if closes[i] > boll_up[i] and closes[i - 1] <= boll_up[i - 1]:
            return "long"
        if closes[i] < boll_lo[i] and closes[i - 1] >= boll_lo[i - 1]:
            return "short"

    def sig_keltner_fade(i):
        if closes[i] > kel_up[i]:
            return "short"
        if closes[i] < kel_lo[i]:
            return "long"

    def sig_macd_cross(i):
        if macd[i - 1] <= macd_sig[i - 1] and macd[i] > macd_sig[i]:
            return "long"
        if macd[i - 1] >= macd_sig[i - 1] and macd[i] < macd_sig[i]:
            return "short"

    def sig_donchian_break(i):
        if closes[i] > don_hi[i]:
            return "long"
        if closes[i] < don_lo[i]:
            return "short"

    def sig_inside_bar_break(i):
        inside = highs[i - 1] <= highs[i - 2] and lows[i - 1] >= lows[i - 2]
        if inside and closes[i] > highs[i - 2]:
            return "long"
        if inside and closes[i] < lows[i - 2]:
            return "short"

    def sig_engulfing(i):
        c, p = candles[i], candles[i - 1]
        fell = closes[i - 1] < closes[i - 6]
        rose = closes[i - 1] > closes[i - 6]
        bull = c.close > c.open and p.close < p.open and c.close > p.open and c.open < p.close
        bear = c.close < c.open and p.close > p.open and c.close < p.open and c.open > p.close
        if bull and fell:
            return "long"
        if bear and rose:
            return "short"

    def sig_three_bar_pullback(i):
        if up(i) and all(closes[j] < closes[j - 1] for j in (i - 3, i - 2, i - 1)) and closes[i] > closes[i - 1]:
            return "long"
        if dn(i) and all(closes[j] > closes[j - 1] for j in (i - 3, i - 2, i - 1)) and closes[i] < closes[i - 1]:
            return "short"

    def sig_gap_fade(i):
        if i != 1 or not prev_close:
            return None
        gap = candles[0].open - prev_close
        if gap > 1.5 * atrs[i]:
            return "short"
        if gap < -1.5 * atrs[i]:
            return "long"

    def sig_gap_go(i):
        if i != 15 or not prev_close:
            return None
        gap = candles[0].open - prev_close
        held_up = gap > 1.5 * atrs[i] and closes[i] > candles[0].open
        held_dn = gap < -1.5 * atrs[i] and closes[i] < candles[0].open
        if held_up:
            return "long"
        if held_dn:
            return "short"

    ib_n = min(60, n // 3)
    ib_hi = max(highs[:ib_n])
    ib_lo = min(lows[:ib_n])

    def sig_ib_break(i):
        if i <= ib_n:
            return None
        if closes[i] > ib_hi and closes[i - 1] <= ib_hi:
            return "long"
        if closes[i] < ib_lo and closes[i - 1] >= ib_lo:
            return "short"

    def sig_midday_vwap_revert(i):
        if not (n * 2 // 5 <= i <= n * 7 // 10):
            return None
        if closes[i] - vw[i] > 1.5 * atrs[i]:
            return "short"
        if vw[i] - closes[i] > 1.5 * atrs[i]:
            return "long"

    def sig_momentum_thrust(i):
        roc = closes[i] - closes[i - 10]
        if roc > 2.5 * atrs[i] and up(i):
            return "long"
        if roc < -2.5 * atrs[i] and dn(i):
            return "short"

    def sig_atr_trend_ride(i):
        if closes[i] > e20[i] + atrs[i] and up(i):
            return "long"
        if closes[i] < e20[i] - atrs[i] and dn(i):
            return "short"

    def sig_opening_drive(i):
        if i != 5:
            return None
        net = closes[5] - candles[0].open
        if net > 0.75 * atrs[i]:
            return "long"
        if net < -0.75 * atrs[i]:
            return "short"

    pivots = sorted(pivot_levels or [])

    def sig_pivot_bounce(i):
        if not pivots:
            return None
        tol = max(0.3 * atrs[i], 1.0)
        for lv in pivots:
            if candles[i - 1].close > lv and lows[i] <= lv + tol and closes[i] > lv:
                return "long"
            if candles[i - 1].close < lv and highs[i] >= lv - tol and closes[i] < lv:
                return "short"

    def sig_swing_failure(i):
        if highs[i] > don_hi[i] and closes[i] < don_hi[i]:
            return "short"
        if lows[i] < don_lo[i] and closes[i] > don_lo[i]:
            return "long"

    specs = [
        # (name, signal, target_atr, stop_atr, max_hold, warmup)
        ("rsi2_revert", sig_rsi2_revert, 1.0, 1.5, 20, 30),
        ("rsi14_cross50", sig_rsi14_cross50, 1.5, 1.0, None, 30),
        ("boll_revert", sig_boll_revert, 1.0, 1.5, 25, 30),
        ("boll_break", sig_boll_break, 1.5, 1.0, None, 30),
        ("keltner_fade", sig_keltner_fade, 0.8, 1.6, 25, 30),
        ("macd_cross", sig_macd_cross, 1.5, 1.0, None, 35),
        ("donchian_break", sig_donchian_break, 2.0, 1.0, None, 30),
        ("inside_bar_break", sig_inside_bar_break, 1.5, 1.0, None, 30),
        ("engulfing_reversal", sig_engulfing, 1.2, 1.2, 30, 10),
        ("three_bar_pullback", sig_three_bar_pullback, 1.5, 1.0, None, 30),
        ("gap_fade", sig_gap_fade, 1.5, 1.5, 60, 1),
        ("gap_go", sig_gap_go, 2.0, 1.0, None, 15),
        ("ib_break", sig_ib_break, 2.0, 1.0, None, 30),
        ("midday_vwap_revert", sig_midday_vwap_revert, 0.8, 1.6, 25, 30),
        ("momentum_thrust", sig_momentum_thrust, 1.5, 1.0, None, 30),
        ("atr_trend_ride", sig_atr_trend_ride, 3.0, 1.5, None, 30),
        ("opening_drive", sig_opening_drive, 2.0, 1.0, None, 5),
        ("pivot_bounce", sig_pivot_bounce, 0.8, 1.6, 25, 30),
        ("swing_failure", sig_swing_failure, 1.2, 1.2, 25, 30),
    ]
    return [
        run_engine(name, candles, fn, t, s, hold, warmup=w)
        for name, fn, t, s, hold, w in specs
    ]
