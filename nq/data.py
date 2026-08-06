"""Market data layer for NQ futures.

Sources, in order of preference:
  1. Yahoo Finance chart API (NQ=F) — free, no key, 1m intraday candles.
  2. Stooq CSV quote (nq.f) — fallback for last price only.
  3. Demo generator — deterministic synthetic session for offline testing
     (set NQ_DEMO=1 or pass demo=True).

Only the Python standard library is used.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
YAHOO_URL = (
    "https://{host}/v8/finance/chart/{symbol}"
    "?interval={interval}&range={range}&includePrePost=true"
)
STOOQ_URL = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"

DEFAULT_SYMBOL = "NQ=F"
STOOQ_SYMBOL = "nq.f"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) nq-toolkit/1.0"


@dataclass
class Candle:
    ts: int  # unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Session:
    """One trading day's worth of intraday candles plus context."""

    symbol: str
    candles: list[Candle] = field(default_factory=list)
    prev_close: float | None = None
    prev_high: float | None = None
    prev_low: float | None = None
    prev_open: float | None = None
    week_high: float | None = None
    week_low: float | None = None
    source: str = "unknown"

    @property
    def last(self) -> float | None:
        return self.candles[-1].close if self.candles else None


def _http_get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_yahoo(payload: dict) -> tuple[list[Candle], dict]:
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    timestamps = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    candles = []
    for i, ts in enumerate(timestamps):
        o, h, l, c = (quote[k][i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        v = quote.get("volume", [0] * len(timestamps))[i] or 0
        candles.append(Candle(ts=int(ts), open=o, high=h, low=l, close=c, volume=v))
    return candles, meta


def fetch_yahoo_session(
    symbol: str = DEFAULT_SYMBOL, interval: str = "1m", day_range: str = "1d"
) -> Session:
    last_err: Exception | None = None
    for host in YAHOO_HOSTS:
        url = YAHOO_URL.format(host=host, symbol=urllib.parse.quote(symbol),
                               interval=interval, range=day_range)
        try:
            payload = json.loads(_http_get(url))
            break
        except (urllib.error.URLError, OSError, ValueError) as e:
            last_err = e
    else:
        raise last_err  # every host failed
    candles, meta = _parse_yahoo(payload)
    sess = Session(symbol=symbol, candles=candles, source="yahoo")
    sess.prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    return sess


def fetch_yahoo_daily_context(symbol: str = DEFAULT_SYMBOL) -> dict:
    """Prior-day OHLC and rolling 5-day (weekly) range from the daily series."""
    candles = fetch_yahoo_session(symbol, interval="1d", day_range="5d").candles
    out: dict = {}
    if len(candles) >= 2:
        prev = candles[-2]
        out.update(prev_open=prev.open, prev_high=prev.high,
                   prev_low=prev.low, prev_close=prev.close)
    past = candles[:-1] or candles
    if past:
        out.update(week_high=max(c.high for c in past),
                   week_low=min(c.low for c in past))
    return out


def fetch_stooq_quote(symbol: str = STOOQ_SYMBOL) -> float | None:
    """Last-price fallback. Stooq CSV: Symbol,Date,Time,Open,High,Low,Close,Volume."""
    try:
        text = _http_get(STOOQ_URL.format(symbol=symbol)).decode()
        lines = text.strip().splitlines()
        if len(lines) < 2:
            return None
        close = lines[1].split(",")[6]
        return float(close) if close not in ("N/D", "") else None
    except (urllib.error.URLError, ValueError, IndexError, OSError):
        return None


def demo_session(symbol: str = DEFAULT_SYMBOL, minutes: int = 390, seed: int = 42) -> Session:
    """Deterministic synthetic 1m session that looks like an NQ day.

    Random walk with a regime drift, volatility clustering, and a lunch lull —
    enough structure for the bias engine and backtester to produce meaningful
    output offline.
    """
    rng = random.Random(seed)
    base = 23_450.0
    now = int(time.time())
    start = now - minutes * 60
    price = base
    drift = rng.choice([-0.35, -0.15, 0.15, 0.35])
    candles: list[Candle] = []
    for i in range(minutes):
        # Vol clustering: hot open, quiet lunch, active close.
        frac = i / minutes
        vol_mult = 1.6 if frac < 0.12 else (0.6 if 0.4 < frac < 0.6 else 1.0)
        sigma = 6.0 * vol_mult
        # Occasional impulse bar.
        if rng.random() < 0.03:
            sigma *= 3
        o = price
        move = rng.gauss(drift * vol_mult, sigma)
        c = o + move
        h = max(o, c) + abs(rng.gauss(0, sigma * 0.4))
        l = min(o, c) - abs(rng.gauss(0, sigma * 0.4))
        candles.append(
            Candle(
                ts=start + i * 60,
                open=round(o, 2),
                high=round(h, 2),
                low=round(l, 2),
                close=round(c, 2),
                volume=rng.randint(500, 4000) * vol_mult,
            )
        )
        price = c
    sess = Session(symbol=symbol, candles=candles, source="demo")
    sess.prev_close = round(base - drift * 40 + rng.gauss(0, 25), 2)
    sess.prev_high = round(max(sess.prev_close, base) + 60, 2)
    sess.prev_low = round(min(sess.prev_close, base) - 60, 2)
    sess.prev_open = round(sess.prev_close - drift * 30 + rng.gauss(0, 20), 2)
    sess.week_high = round(sess.prev_high + 85, 2)
    sess.week_low = round(sess.prev_low - 110, 2)
    return sess


def is_demo() -> bool:
    return os.environ.get("NQ_DEMO", "").strip() in ("1", "true", "yes")


# Futures -> real-time ETF proxy for the free live estimate.
NOWCAST_PROXY = {
    "NQ=F": "QQQ", "MNQ=F": "QQQ",
    "ES=F": "SPY", "MES=F": "SPY",
    "YM=F": "DIA", "MYM=F": "DIA",
    "RTY=F": "IWM", "M2K=F": "IWM",
    "GC=F": "GLD", "SI=F": "SLV", "CL=F": "USO",
}


def nowcast_from_qqq(fut_session: Session) -> dict | None:
    """Free real-time estimate: Yahoo serves US ETFs in real time while CME
    futures are ~15 min delayed. Compute the futures/ETF ratio over the
    timestamps both series share, then apply it to the ETF's latest print.
    An estimate for charting/alerts — never for execution."""
    proxy = NOWCAST_PROXY.get(fut_session.symbol)
    if not proxy:
        return None
    try:
        etf = fetch_yahoo_session(proxy)
    except (urllib.error.URLError, KeyError, ValueError, OSError):
        return None
    fut_by_ts = {c.ts: c.close for c in fut_session.candles}
    ratios = [fut_by_ts[c.ts] / c.close for c in etf.candles if c.ts in fut_by_ts and c.close]
    if len(ratios) < 5 or not etf.candles:
        return None
    tail = ratios[-30:]
    ratio = sum(tail) / len(tail)
    last = etf.candles[-1]
    return {
        "price": round(last.close * ratio, 2),
        "ratio": round(ratio, 4),
        "proxy": proxy,
        "proxy_price": round(last.close, 2),
        "proxy_ts": last.ts,
        "basis": f"{proxy} nowcast",
    }


# Chart timeframes: tf -> (yahoo interval, yahoo range)
TF_MAP = {
    "1m": ("1m", "1d"),
    "5m": ("5m", "5d"),
    "15m": ("15m", "5d"),
    "1h": ("60m", "1mo"),
    "1d": ("1d", "6mo"),
}
RANGE_BAR_SIZES = (2.0, 5.0, 10.0)  # points


def aggregate_candles(candles: list[Candle], seconds: int) -> list[Candle]:
    """Bucket 1m candles into a larger fixed timeframe."""
    out: list[Candle] = []
    for c in candles:
        bucket = c.ts - (c.ts % seconds)
        if out and out[-1].ts == bucket:
            last = out[-1]
            last.high = max(last.high, c.high)
            last.low = min(last.low, c.low)
            last.close = c.close
            last.volume += c.volume
        else:
            out.append(Candle(bucket, c.open, c.high, c.low, c.close, c.volume))
    return out


def build_range_bars(candles: list[Candle], rng: float) -> list[Candle]:
    """Approximate range bars from 1m candles.

    Each 1m bar's path is approximated as open -> nearer extreme -> farther
    extreme -> close; a bar closes whenever its high-low span reaches `rng`.
    """
    bars: list[Candle] = []
    o = h = l = None
    ts = 0
    for c in candles:
        seq = (c.open, c.low, c.high, c.close) if c.close >= c.open else (c.open, c.high, c.low, c.close)
        for p in seq:
            if o is None:
                o = h = l = p
                ts = c.ts
                continue
            h = max(h, p)
            l = min(l, p)
            while h - l >= rng:
                if p >= l + rng:  # filled upward
                    bars.append(Candle(ts, round(o, 2), round(l + rng, 2), round(l, 2), round(l + rng, 2)))
                    o = l + rng
                else:  # filled downward
                    bars.append(Candle(ts, round(o, 2), round(h, 2), round(h - rng, 2), round(h - rng, 2)))
                    o = h - rng
                ts = c.ts
                h = max(o, p)
                l = min(o, p)
    if o is not None and (not bars or bars[-1].ts != ts or bars[-1].close != o or h != l):
        bars.append(Candle(ts, round(o, 2), round(h, 2), round(l, 2), round(candles[-1].close, 2)))
    return bars


def demo_daily(symbol: str = DEFAULT_SYMBOL, days: int = 120, seed: int = 7) -> list[Candle]:
    rng = random.Random(seed)
    price = 22_400.0
    now = int(time.time())
    out = []
    for i in range(days):
        drift = rng.gauss(8, 90)
        o = price
        c = o + drift
        h = max(o, c) + abs(rng.gauss(0, 60))
        l = min(o, c) - abs(rng.gauss(0, 60))
        out.append(Candle(now - (days - i) * 86400, round(o, 2), round(h, 2), round(l, 2), round(c, 2), rng.randint(200000, 600000)))
        price = c
    return out


def get_chart_candles(
    tf: str = "1m", range_pts: float | None = None,
    symbol: str = DEFAULT_SYMBOL, demo: bool | None = None,
) -> list[Candle]:
    """Candles for the chart: a fixed timeframe from TF_MAP, or range bars
    built from today's 1m data when `range_pts` is set."""
    if demo is None:
        demo = is_demo()
    if range_pts:
        base = demo_session(symbol).candles if demo else fetch_yahoo_session(symbol).candles
        return build_range_bars(base, float(range_pts))
    if tf not in TF_MAP:
        raise ValueError(f"unknown timeframe {tf!r} (use {'/'.join(TF_MAP)} or range bars)")
    if demo:
        if tf == "1d":
            return demo_daily(symbol)
        if tf == "1m":
            return demo_session(symbol).candles
        mins = {"5m": 5, "15m": 15, "1h": 60}[tf]
        days = 5 if tf in ("5m", "15m") else 21
        long_sess = demo_session(symbol, minutes=390 * days, seed=11)
        return aggregate_candles(long_sess.candles, mins * 60)
    interval, day_range = TF_MAP[tf]
    return fetch_yahoo_session(symbol, interval=interval, day_range=day_range).candles


def get_session(symbol: str = DEFAULT_SYMBOL, demo: bool | None = None) -> Session:
    """Fetch today's 1m session, filling prior-day levels; falls back gracefully."""
    if demo is None:
        demo = is_demo()
    if demo:
        return demo_session(symbol)
    try:
        sess = fetch_yahoo_session(symbol)
        try:
            ctx = fetch_yahoo_daily_context(symbol)
            sess.prev_open = ctx.get("prev_open")
            sess.prev_high = ctx.get("prev_high")
            sess.prev_low = ctx.get("prev_low")
            sess.prev_close = ctx.get("prev_close") or sess.prev_close
            sess.week_high = ctx.get("week_high")
            sess.week_low = ctx.get("week_low")
        except (urllib.error.URLError, KeyError, OSError):
            pass
        if sess.candles:
            return sess
    except (urllib.error.URLError, KeyError, ValueError, OSError):
        pass
    # Yahoo failed — try stooq for at least a last price.
    last = fetch_stooq_quote()
    if last is not None:
        c = Candle(ts=int(time.time()), open=last, high=last, low=last, close=last)
        return Session(symbol=symbol, candles=[c], source="stooq")
    raise RuntimeError(
        "No market data source reachable (yahoo + stooq failed). "
        "Set NQ_DEMO=1 to run with synthetic data."
    )
