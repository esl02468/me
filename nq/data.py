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

YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
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
    url = YAHOO_URL.format(symbol=urllib.parse.quote(symbol), interval=interval, range=day_range)
    payload = json.loads(_http_get(url))
    candles, meta = _parse_yahoo(payload)
    sess = Session(symbol=symbol, candles=candles, source="yahoo")
    sess.prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    return sess


def fetch_yahoo_prev_day(symbol: str = DEFAULT_SYMBOL) -> tuple[float | None, float | None]:
    """Prior-day high/low from the 5-day daily series."""
    url = YAHOO_URL.format(symbol=urllib.parse.quote(symbol), interval="1d", range="5d")
    payload = json.loads(_http_get(url))
    candles, _ = _parse_yahoo(payload)
    if len(candles) >= 2:
        prev = candles[-2]
        return prev.high, prev.low
    return None, None


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
    return sess


def is_demo() -> bool:
    return os.environ.get("NQ_DEMO", "").strip() in ("1", "true", "yes")


def get_session(symbol: str = DEFAULT_SYMBOL, demo: bool | None = None) -> Session:
    """Fetch today's 1m session, filling prior-day levels; falls back gracefully."""
    if demo is None:
        demo = is_demo()
    if demo:
        return demo_session(symbol)
    try:
        sess = fetch_yahoo_session(symbol)
        try:
            sess.prev_high, sess.prev_low = fetch_yahoo_prev_day(symbol)
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
