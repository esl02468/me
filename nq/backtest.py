"""Strategy Analyzer: backtest intraday strategies on today's 1m NQ data.

Built-in strategies:
  ema_cross   long when EMA9 crosses above EMA21, short on cross down; flip on opposite cross
  orb         opening-range breakout: first close beyond the 15-min range, fixed stop/target in ATR
  vwap_fade   fade extensions: short when price stretches > k*ATR above VWAP, long when below

All fills are next-bar-open (no lookahead), one contract, PnL in points and
dollars (NQ = $20/pt, MNQ = $2/pt). This is an analysis tool, not trade advice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .bias import atr, ema, vwap
from .data import Candle, Session

POINT_VALUE_NQ = 20.0
POINT_VALUE_MNQ = 2.0
OPENING_RANGE_BARS = 15


@dataclass
class Trade:
    side: str  # "long" | "short"
    entry_ts: int
    entry: float
    exit_ts: int | None = None
    exit: float | None = None
    reason: str = ""

    @property
    def points(self) -> float:
        if self.exit is None:
            return 0.0
        sign = 1 if self.side == "long" else -1
        return sign * (self.exit - self.entry)


@dataclass
class Report:
    strategy: str
    trades: list[Trade] = field(default_factory=list)

    @property
    def closed(self) -> list[Trade]:
        return [t for t in self.trades if t.exit is not None]

    @property
    def total_points(self) -> float:
        return sum(t.points for t in self.closed)

    @property
    def win_rate(self) -> float:
        c = self.closed
        return (sum(1 for t in c if t.points > 0) / len(c)) if c else 0.0

    @property
    def profit_factor(self) -> float:
        wins = sum(t.points for t in self.closed if t.points > 0)
        losses = -sum(t.points for t in self.closed if t.points < 0)
        return wins / losses if losses > 0 else float("inf") if wins > 0 else 0.0

    @property
    def max_drawdown_points(self) -> float:
        equity = 0.0
        peak = 0.0
        dd = 0.0
        for t in self.closed:
            equity += t.points
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
        return dd

    def summary(self) -> dict:
        return {
            "strategy": self.strategy,
            "trades": len(self.closed),
            "win_rate": round(self.win_rate * 100, 1),
            "total_points": round(self.total_points, 2),
            "pnl_nq_usd": round(self.total_points * POINT_VALUE_NQ, 2),
            "pnl_mnq_usd": round(self.total_points * POINT_VALUE_MNQ, 2),
            "profit_factor": round(self.profit_factor, 2)
            if self.profit_factor != float("inf")
            else "inf",
            "max_drawdown_points": round(self.max_drawdown_points, 2),
            "open_position": next(
                (t.side for t in self.trades if t.exit is None), None
            ),
        }


def _close_trade(trade: Trade, candle: Candle, price: float, reason: str) -> None:
    trade.exit_ts = candle.ts
    trade.exit = price
    trade.reason = reason


def backtest_ema_cross(candles: list[Candle], fast: int = 9, slow: int = 21) -> Report:
    report = Report(strategy=f"ema_cross({fast}/{slow})")
    closes = [c.close for c in candles]
    if len(closes) < slow + 2:
        return report
    ef, es = ema(closes, fast), ema(closes, slow)
    position: Trade | None = None
    for i in range(slow, len(candles) - 1):
        crossed_up = ef[i] > es[i] and ef[i - 1] <= es[i - 1]
        crossed_dn = ef[i] < es[i] and ef[i - 1] >= es[i - 1]
        nxt = candles[i + 1]
        if crossed_up or crossed_dn:
            side = "long" if crossed_up else "short"
            if position and position.side != side:
                _close_trade(position, nxt, nxt.open, "flip")
                position = None
            if position is None:
                position = Trade(side=side, entry_ts=nxt.ts, entry=nxt.open)
                report.trades.append(position)
    if position and position.exit is None:
        last = candles[-1]
        _close_trade(position, last, last.close, "eod")
    return report


def backtest_orb(
    candles: list[Candle], or_bars: int = OPENING_RANGE_BARS,
    stop_atr: float = 1.5, target_atr: float = 3.0,
) -> Report:
    report = Report(strategy=f"orb({or_bars}m, {stop_atr}x/{target_atr}x ATR)")
    if len(candles) < or_bars + 2:
        return report
    or_high = max(c.high for c in candles[:or_bars])
    or_low = min(c.low for c in candles[:or_bars])
    position: Trade | None = None
    stop = target = 0.0
    for i in range(or_bars, len(candles) - 1):
        c = candles[i]
        nxt = candles[i + 1]
        if position is None:
            a = atr(candles[: i + 1]) or 1.0
            if c.close > or_high:
                position = Trade(side="long", entry_ts=nxt.ts, entry=nxt.open)
                stop, target = position.entry - stop_atr * a, position.entry + target_atr * a
                report.trades.append(position)
            elif c.close < or_low:
                position = Trade(side="short", entry_ts=nxt.ts, entry=nxt.open)
                stop, target = position.entry + stop_atr * a, position.entry - target_atr * a
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
    if position and position.exit is None:
        last = candles[-1]
        _close_trade(position, last, last.close, "eod")
    return report


def backtest_vwap_fade(
    candles: list[Candle], stretch_atr: float = 2.0, stop_atr: float = 1.5,
) -> Report:
    report = Report(strategy=f"vwap_fade({stretch_atr}x stretch)")
    if len(candles) < 30:
        return report
    vw = vwap(candles)
    position: Trade | None = None
    stop = 0.0
    for i in range(30, len(candles) - 1):
        c = candles[i]
        nxt = candles[i + 1]
        a = atr(candles[: i + 1]) or 1.0
        stretch = c.close - vw[i]
        if position is None:
            if stretch > stretch_atr * a:
                position = Trade(side="short", entry_ts=nxt.ts, entry=nxt.open)
                stop = position.entry + stop_atr * a
                report.trades.append(position)
            elif stretch < -stretch_atr * a:
                position = Trade(side="long", entry_ts=nxt.ts, entry=nxt.open)
                stop = position.entry - stop_atr * a
                report.trades.append(position)
        else:
            # Target: tag VWAP. Stop: fixed ATR multiple.
            if position.side == "short":
                if c.high >= stop:
                    _close_trade(position, c, stop, "stop")
                    position = None
                elif c.low <= vw[i]:
                    _close_trade(position, c, vw[i], "vwap")
                    position = None
            else:
                if c.low <= stop:
                    _close_trade(position, c, stop, "stop")
                    position = None
                elif c.high >= vw[i]:
                    _close_trade(position, c, vw[i], "vwap")
                    position = None
    if position and position.exit is None:
        last = candles[-1]
        _close_trade(position, last, last.close, "eod")
    return report


def backtest_level_bounce(
    candles: list[Candle], levels: list[float] | None = None,
    target_atr: float = 0.7, stop_atr: float = 1.4, max_hold: int = 30,
) -> Report:
    """Trade the first touch of each level, betting on a bounce.

    Engineered for hit rate rather than home runs: the target (0.7 ATR) is
    half the stop (1.4 ATR), so geometry alone favors the target being hit
    first; a momentum veto skips touches hit at full speed; one attempt per
    level per side per day. High win rate + small wins is the profile —
    profitability still depends on the levels being real.
    """
    report = Report(strategy=f"level_bounce({target_atr}x/{stop_atr}x ATR)")
    if not levels or len(candles) < 30:
        return report
    prices = sorted({round(float(p), 2) for p in levels})
    traded: set[tuple[float, str]] = set()
    position: Trade | None = None
    stop = target = 0.0
    entry_i = 0
    for i in range(20, len(candles) - 1):
        c = candles[i]
        nxt = candles[i + 1]
        a = atr(candles[: i + 1]) or 1.0
        tol = max(0.3 * a, 1.0)
        if position is None:
            mom = c.close - candles[i - 5].close
            for lv in prices:
                came_from_above = candles[i - 1].close > lv
                if (came_from_above and c.low <= lv + tol and c.close > lv - tol
                        and (lv, "long") not in traded and mom > -2 * a):
                    traded.add((lv, "long"))
                    position = Trade(side="long", entry_ts=nxt.ts, entry=nxt.open)
                    stop, target = position.entry - stop_atr * a, position.entry + target_atr * a
                elif (not came_from_above and c.high >= lv - tol and c.close < lv + tol
                        and (lv, "short") not in traded and mom < 2 * a):
                    traded.add((lv, "short"))
                    position = Trade(side="short", entry_ts=nxt.ts, entry=nxt.open)
                    stop, target = position.entry + stop_atr * a, position.entry - target_atr * a
                if position:
                    entry_i = i + 1
                    report.trades.append(position)
                    break
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
            if position and i - entry_i >= max_hold:
                _close_trade(position, c, c.close, "time")
                position = None
    if position and position.exit is None:
        last = candles[-1]
        _close_trade(position, last, last.close, "eod")
    return report


def backtest_ema_pullback(
    candles: list[Candle], target_atr: float = 1.5, stop_atr: float = 1.0,
) -> Report:
    """Trend-following: in an EMA9>EMA21 uptrend, buy the pullback that tags
    EMA21 and holds; mirror for downtrends. With-trend entries, asymmetric
    target (bigger than stop) — lower hit rate, larger winners."""
    report = Report(strategy=f"ema_pullback({target_atr}x/{stop_atr}x ATR)")
    closes = [c.close for c in candles]
    if len(closes) < 40:
        return report
    e9, e21 = ema(closes, 9), ema(closes, 21)
    position: Trade | None = None
    stop = target = 0.0
    for i in range(25, len(candles) - 1):
        c = candles[i]
        nxt = candles[i + 1]
        a = atr(candles[: i + 1]) or 1.0
        if position is None:
            up = e9[i] > e21[i] and e9[i - 3] > e21[i - 3]
            dn = e9[i] < e21[i] and e9[i - 3] < e21[i - 3]
            if up and c.low <= e21[i] and c.close > e21[i]:
                position = Trade(side="long", entry_ts=nxt.ts, entry=nxt.open)
                stop, target = position.entry - stop_atr * a, position.entry + target_atr * a
                report.trades.append(position)
            elif dn and c.high >= e21[i] and c.close < e21[i]:
                position = Trade(side="short", entry_ts=nxt.ts, entry=nxt.open)
                stop, target = position.entry + stop_atr * a, position.entry - target_atr * a
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
    if position and position.exit is None:
        last = candles[-1]
        _close_trade(position, last, last.close, "eod")
    return report


def backtest_vwap_reclaim(
    candles: list[Candle], confirm_atr: float = 0.5,
    target_atr: float = 1.2, stop_atr: float = 1.0,
) -> Report:
    """Momentum: when price crosses VWAP and closes beyond it by a margin,
    trade the continuation in the crossing direction."""
    report = Report(strategy=f"vwap_reclaim({confirm_atr}x confirm)")
    if len(candles) < 40:
        return report
    vw = vwap(candles)
    position: Trade | None = None
    stop = target = 0.0
    for i in range(25, len(candles) - 1):
        c = candles[i]
        prev = candles[i - 1]
        nxt = candles[i + 1]
        a = atr(candles[: i + 1]) or 1.0
        if position is None:
            crossed_up = prev.close < vw[i - 1] and c.close > vw[i] + confirm_atr * a
            crossed_dn = prev.close > vw[i - 1] and c.close < vw[i] - confirm_atr * a
            if crossed_up:
                position = Trade(side="long", entry_ts=nxt.ts, entry=nxt.open)
                stop, target = position.entry - stop_atr * a, position.entry + target_atr * a
                report.trades.append(position)
            elif crossed_dn:
                position = Trade(side="short", entry_ts=nxt.ts, entry=nxt.open)
                stop, target = position.entry + stop_atr * a, position.entry - target_atr * a
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
    if position and position.exit is None:
        last = candles[-1]
        _close_trade(position, last, last.close, "eod")
    return report


STRATEGIES = {
    "ema_cross": backtest_ema_cross,
    "orb": backtest_orb,
    "vwap_fade": backtest_vwap_fade,
    "ema_pullback": backtest_ema_pullback,
    "vwap_reclaim": backtest_vwap_reclaim,
}


def session_level_prices(session: Session) -> list[float]:
    """Auto levels + the user's levels.json, as plain prices."""
    from .levels import compute_auto_levels, load_levels

    prices = [d["price"] for d in compute_auto_levels(session)]
    prices += [l.price for l in load_levels(session.symbol)]
    return prices


def run_all(session: Session) -> list[Report]:
    from .strategies import extended_strategies

    reports = [fn(session.candles) for fn in STRATEGIES.values()]
    levels = session_level_prices(session)
    reports.append(backtest_level_bounce(session.candles, levels))
    pivots = levels[:9] if levels else []  # PDH/PDL/close/mid + floor pivots
    reports.extend(extended_strategies(session.candles, session.prev_close, pivots))
    # Most trades first among equals; the >51% filter does the real ranking.
    reports.sort(key=lambda r: (r.win_rate, len(r.closed)), reverse=True)
    return reports
