"""Bias engine: turns an intraday session into a directional bias score.

Five components, each voting -1 / 0 / +1:

  vwap      price above/below session VWAP
  ema       EMA9 vs EMA21 (fast trend)
  or        price vs opening range (first 15 minutes) high/low
  prev      price vs prior-day close (gap-and-go context)
  momentum  last 10 bars net change vs recent ATR (is the tape pushing?)

All of it runs on `Session.analysis_candles`: the RTH bars once the cash
open has printed an opening range, the Globex day before that (see
nq.data). So "opening range" means 09:30-09:45 ET, not 18:00-18:15.

Composite score in [-5, +5] maps to a label:
  >= 3   STRONG BULLISH      <= -3  STRONG BEARISH
  1..2   BULLISH             -1..-2 BEARISH
  0      NEUTRAL
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import clock
from .data import Candle, Session, _bar_seconds

OPENING_RANGE_BARS = 15  # first 15 one-minute bars
OPENING_RANGE_SECONDS = OPENING_RANGE_BARS * 60


def newest_session_bars(candles: list[Candle]) -> list[Candle]:
    """Bars belonging to the newest Globex day in a (possibly multi-day)
    intraday series. Daily series come back whole."""
    if not candles:
        return []
    if candles[-1].ts - candles[0].ts < 86400 or _bar_seconds(candles) >= 6 * 3600:
        return list(candles)
    start = clock.session_start(candles[-1].ts)
    return [c for c in candles if c.ts >= start]


def opening_range(candles: list[Candle]) -> tuple[float, float] | None:
    """(high, low) of the first 15 minutes of the newest session in the
    series, at whatever bar size the series has. None when the session
    has not printed a full opening range yet, or bars are too coarse."""
    bars = newest_session_bars(candles)
    if not bars:
        return None
    width = _bar_seconds(bars)
    if width > OPENING_RANGE_SECONDS:
        return None
    n = max(1, OPENING_RANGE_SECONDS // width)
    if len(bars) < n:
        return None
    first = bars[:n]
    return max(c.high for c in first), min(c.low for c in first)


def session_vwap(candles: list[Candle]) -> float | None:
    """VWAP anchored at the newest session's first bar in the series."""
    bars = newest_session_bars(candles)
    return vwap(bars)[-1] if bars else None


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def vwap(candles: list[Candle]) -> list[float]:
    out = []
    cum_pv = 0.0
    cum_v = 0.0
    for c in candles:
        typical = (c.high + c.low + c.close) / 3.0
        v = c.volume or 1.0  # futures feeds sometimes omit volume
        cum_pv += typical * v
        cum_v += v
        out.append(cum_pv / cum_v)
    return out


def atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for prev, cur in zip(candles[:-1], candles[1:]):
        trs.append(
            max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        )
    window = trs[-period:]
    return sum(window) / len(window)


@dataclass
class BiasResult:
    score: int
    label: str
    components: dict[str, int] = field(default_factory=dict)
    detail: dict[str, float | None] = field(default_factory=dict)


def _label(score: int) -> str:
    if score >= 3:
        return "STRONG BULLISH"
    if score >= 1:
        return "BULLISH"
    if score <= -3:
        return "STRONG BEARISH"
    if score <= -1:
        return "BEARISH"
    return "NEUTRAL"


def compute_bias(session: Session) -> BiasResult:
    candles = session.analysis_candles
    if not candles:
        return BiasResult(score=0, label="NEUTRAL")

    closes = [c.close for c in candles]
    price = closes[-1]
    components: dict[str, int] = {}
    detail: dict[str, float | None] = {"price": price}

    # 1. VWAP (anchored at the session's first analysed bar)
    vw = session_vwap(candles)
    detail["vwap"] = round(vw, 2)
    components["vwap"] = 1 if price > vw else (-1 if price < vw else 0)

    # 2. EMA9 vs EMA21
    if len(closes) >= 21:
        e9, e21 = ema(closes, 9)[-1], ema(closes, 21)[-1]
        detail["ema9"], detail["ema21"] = round(e9, 2), round(e21, 2)
        components["ema"] = 1 if e9 > e21 else (-1 if e9 < e21 else 0)
    else:
        components["ema"] = 0

    # 3. Opening range
    orange = opening_range(candles)
    if orange:
        or_high, or_low = orange
        detail["or_high"], detail["or_low"] = round(or_high, 2), round(or_low, 2)
        components["or"] = 1 if price > or_high else (-1 if price < or_low else 0)
    else:
        components["or"] = 0

    # 4. Prior-day close
    if session.prev_close:
        detail["prev_close"] = round(session.prev_close, 2)
        components["prev"] = 1 if price > session.prev_close else (-1 if price < session.prev_close else 0)
    else:
        components["prev"] = 0

    # 5. Momentum: net change of last 10 bars vs ATR
    if len(closes) >= 11:
        net = closes[-1] - closes[-11]
        a = atr(candles) or 1.0
        detail["momentum_pts"] = round(net, 2)
        components["momentum"] = 1 if net > a else (-1 if net < -a else 0)
    else:
        components["momentum"] = 0

    score = sum(components.values())
    return BiasResult(score=score, label=_label(score), components=components, detail=detail)
