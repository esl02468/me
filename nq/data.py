"""Market data layer for NQ futures.

Sources, in order of preference:
  1. Yahoo Finance chart API (NQ=F) — free, no key, 1m intraday candles.
  2. Stooq CSV — daily history for prior-day context, and a last-price
     fallback when Yahoo is unreachable.
  3. Demo generator — deterministic synthetic sessions for offline testing
     (set NQ_DEMO=1 or pass demo=True).

Sessions
--------
CME equity-index futures trade a Globex day that opens 18:00 ET and runs to
17:00 ET the next afternoon; the cash-hours part (RTH) is 09:30-16:00 ET.
Yahoo's 1m feed hands back the whole Globex day, so "the first 15 minutes",
"the gap", "midday" and "session VWAP" mean the *overnight* open unless the
RTH portion is carved out. `Session` carries both: `candles` is everything
in the current Globex day (what the chart shows), `analysis_candles` is
what the bias engine, backtests and signal engine run on — the RTH bars
once the cash open has produced an opening range, the full Globex day
before that. NQ_SESSION=globex keeps the old behaviour everywhere.

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
from datetime import timedelta

from . import clock

YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
YAHOO_URL = (
    "https://{host}/v8/finance/chart/{symbol}"
    "?interval={interval}&range={range}&includePrePost=true"
)
STOOQ_QUOTE_URL = "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"
STOOQ_DAILY_URL = "https://stooq.com/q/d/l/?s={symbol}&d1={d1}&d2={d2}&i=d"

DEFAULT_SYMBOL = "NQ=F"
STOOQ_SYMBOL = "nq.f"
# Yahoo symbol -> Stooq continuous-contract symbol, for the fallbacks.
STOOQ_SYMBOLS = {
    "NQ=F": "nq.f", "MNQ=F": "nq.f",
    "ES=F": "es.f", "MES=F": "es.f",
    "YM=F": "ym.f", "MYM=F": "ym.f",
    "GC=F": "gc.f", "SI=F": "si.f", "CL=F": "cl.f",
}
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) nq-toolkit/1.1"
RETRY_STATUSES = (429, 500, 502, 503, 504)

# Minimum RTH bars before analysis switches from the Globex day to RTH:
# one opening range. Before that the cash session has nothing to say.
RTH_MIN_BARS = 15


def session_mode() -> str:
    """'rth' (default) or 'globex' — which bars analysis runs on."""
    return "globex" if os.environ.get("NQ_SESSION", "").strip().lower() == "globex" else "rth"


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
    """One Globex trading day's intraday candles plus context."""

    symbol: str
    candles: list[Candle] = field(default_factory=list)
    prev_close: float | None = None
    prev_high: float | None = None
    prev_low: float | None = None
    prev_open: float | None = None
    week_high: float | None = None
    week_low: float | None = None
    source: str = "unknown"
    # Session structure (unix seconds). None when unknown / not yet reached.
    session_start_ts: int | None = None   # Globex open, 18:00 ET
    rth_start_ts: int | None = None       # first RTH bar actually present
    rth_open_ts: int | None = None        # 09:30 ET of the trade date
    rth_close_ts: int | None = None       # 16:00 ET of the trade date
    overnight_high: float | None = None   # 18:00 -> 09:30 extremes
    overnight_low: float | None = None
    mode: str = field(default_factory=session_mode)

    @property
    def last(self) -> float | None:
        return self.candles[-1].close if self.candles else None

    @property
    def rth(self) -> list[Candle]:
        """Bars inside 09:30-16:00 ET."""
        return rth_filter(self.candles)

    @property
    def analysis_candles(self) -> list[Candle]:
        """What the bias engine, backtests and signal engine run on.

        In 'rth' mode: the RTH bars once at least RTH_MIN_BARS of them
        exist, otherwise the full Globex day (overnight traders still get
        a reading). In 'globex' mode: always the full day. Daily bars are
        never filtered — they have no time of day.
        """
        return analysis_filter(self.candles, self.mode)

    @property
    def analysis_scope(self) -> str:
        """'rth', 'globex' or 'daily' — which bars analysis_candles are."""
        if not self.candles:
            return self.mode
        if _is_daily(self.candles):
            return "daily"
        if self.mode == "rth" and len(self.rth) >= RTH_MIN_BARS:
            return "rth"
        return "globex"

    @property
    def analysis_start_ts(self) -> int | None:
        a = self.analysis_candles
        return a[0].ts if a else None


# ---------------------------------------------------------------- sessions

def _is_daily(candles: list[Candle]) -> bool:
    return len(candles) >= 2 and (candles[1].ts - candles[0].ts) >= 6 * 3600


def _bar_seconds(candles: list[Candle]) -> int:
    """Typical bar length, from the median spacing (robust to gaps)."""
    if len(candles) < 2:
        return 60
    diffs = sorted(b.ts - a.ts for a, b in zip(candles[:-1], candles[1:]) if b.ts > a.ts)
    return diffs[len(diffs) // 2] if diffs else 60


def rth_filter(candles: list[Candle]) -> list[Candle]:
    """Bars whose interval overlaps 09:30-16:00 ET on their trade date.

    Overlap rather than start-time so a 60m bar starting 09:00 (which holds
    the cash open) is kept while the 16:00 bar (settlement hour) is not.
    """
    if not candles or _is_daily(candles):
        return list(candles)
    width_min = max(1, _bar_seconds(candles) // 60)
    out = []
    for c in candles:
        mod = clock.et_minutes(c.ts)
        if mod + width_min > clock.RTH_OPEN_MIN and mod < clock.RTH_CLOSE_MIN:
            out.append(c)
    return out


def analysis_filter(candles: list[Candle], mode: str | None = None) -> list[Candle]:
    mode = mode or session_mode()
    if mode != "rth" or not candles or _is_daily(candles):
        return list(candles)
    rth = rth_filter(candles)
    return rth if len(rth) >= RTH_MIN_BARS else list(candles)


def current_session_slice(candles: list[Candle]) -> list[Candle]:
    """The bars of the Globex day the newest bar belongs to."""
    if not candles:
        return []
    start = clock.session_start(candles[-1].ts)
    return [c for c in candles if c.ts >= start]


def annotate_session(sess: Session) -> Session:
    """Fill the session-structure fields from the candles present."""
    if not sess.candles or _is_daily(sess.candles):
        return sess
    last_ts = sess.candles[-1].ts
    sess.session_start_ts = clock.session_start(last_ts)
    sess.rth_open_ts = clock.rth_open_ts(last_ts)
    sess.rth_close_ts = clock.rth_close_ts(last_ts)
    rth = sess.rth
    sess.rth_start_ts = rth[0].ts if rth else None
    overnight = [c for c in sess.candles if c.ts < sess.rth_open_ts]
    if overnight:
        sess.overnight_high = max(c.high for c in overnight)
        sess.overnight_low = min(c.low for c in overnight)
    return sess


# ------------------------------------------------------------------- yahoo

def _http_get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_get_retry(url: str, timeout: float = 15.0, attempts: int = 2) -> bytes:
    """One polite retry on rate-limit / upstream errors."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return _http_get(url, timeout)
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in RETRY_STATUSES or i == attempts - 1:
                raise
        except (urllib.error.URLError, OSError) as e:
            last = e
            if i == attempts - 1:
                raise
        time.sleep(1.0 + i)
    raise last  # pragma: no cover


def _parse_yahoo(payload: dict) -> tuple[list[Candle], dict]:
    chart = payload.get("chart") or {}
    results = chart.get("result")
    if not results:
        err = chart.get("error") or {}
        raise ValueError(
            f"yahoo: {err.get('code', 'no result')}: {err.get('description', 'empty chart payload')}"
        )
    result = results[0]
    meta = result.get("meta", {})
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    volumes = quote.get("volume") or [0] * len(timestamps)
    candles = []
    for i, ts in enumerate(timestamps):
        try:
            o, h, l, c = (quote[k][i] for k in ("open", "high", "low", "close"))
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c):
            continue
        v = volumes[i] if i < len(volumes) else 0
        candles.append(Candle(ts=int(ts), open=o, high=h, low=l, close=c, volume=v or 0))
    return candles, meta


def fetch_yahoo_session(
    symbol: str = DEFAULT_SYMBOL, interval: str = "1m", day_range: str = "1d"
) -> Session:
    last_err: Exception | None = None
    payload = None
    for host in YAHOO_HOSTS:
        url = YAHOO_URL.format(host=host, symbol=urllib.parse.quote(symbol),
                               interval=interval, range=day_range)
        try:
            payload = json.loads(_http_get_retry(url))
            break
        except (urllib.error.URLError, OSError, ValueError) as e:
            last_err = e
    if payload is None:
        raise last_err  # every host failed
    candles, meta = _parse_yahoo(payload)
    sess = Session(symbol=symbol, candles=candles, source="yahoo")
    sess.prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    return sess


def fetch_yahoo_intraday(symbol: str = DEFAULT_SYMBOL) -> Session:
    """Today's Globex day of 1m bars.

    Two days are requested and sliced to the current session so the
    overnight portion is present whatever Yahoo's idea of "1d" is for the
    exchange. Falls back to 1d if 2d fails.
    """
    try:
        sess = fetch_yahoo_session(symbol, "1m", "2d")
    except (urllib.error.URLError, OSError, ValueError):
        sess = fetch_yahoo_session(symbol, "1m", "1d")
    sess.candles = current_session_slice(sess.candles)
    return annotate_session(sess)


def _context_from_daily(candles: list[Candle], before_ts: int | None) -> dict:
    """Prior-day OHLC and rolling 5-day range from daily bars that belong to
    sessions before the one opening at `before_ts`.

    Daily bars are stamped at their session open by Yahoo and at midnight
    ET by the Stooq loader; either way today's bar is never earlier than
    the session open, and yesterday's always is.
    """
    past = [c for c in candles if before_ts is None or c.ts < before_ts - 600]
    out: dict = {}
    if past:
        prev = past[-1]
        out.update(prev_open=prev.open, prev_high=prev.high,
                   prev_low=prev.low, prev_close=prev.close)
        week = past[-5:]
        out.update(week_high=max(c.high for c in week),
                   week_low=min(c.low for c in week))
    return out


def fetch_yahoo_daily_context(symbol: str = DEFAULT_SYMBOL, before_ts: int | None = None) -> dict:
    """Prior-day OHLC and weekly range relative to the session opening at
    `before_ts`, so a weekend or the Sunday-evening open still reads
    Friday's bar as 'yesterday'. Without `before_ts` the newest bar is
    assumed to be the live one and dropped."""
    candles = fetch_yahoo_session(symbol, interval="1d", day_range="1mo").candles
    if before_ts is None:
        candles = candles[:-1]
    return _context_from_daily(candles, before_ts)


# ------------------------------------------------------------------- stooq

def stooq_symbol(symbol: str) -> str | None:
    return STOOQ_SYMBOLS.get(symbol.upper())


def fetch_stooq_quote(symbol: str = STOOQ_SYMBOL) -> float | None:
    """Last-price fallback. Stooq CSV: Symbol,Date,Time,Open,High,Low,Close,Volume."""
    try:
        text = _http_get(STOOQ_QUOTE_URL.format(symbol=symbol)).decode()
        lines = text.strip().splitlines()
        if len(lines) < 2:
            return None
        close = lines[1].split(",")[6]
        return float(close) if close not in ("N/D", "") else None
    except (urllib.error.URLError, ValueError, IndexError, OSError):
        return None


def fetch_stooq_daily(symbol: str = STOOQ_SYMBOL, days: int = 20) -> list[Candle]:
    """Daily bars from Stooq: Date,Open,High,Low,Close,Volume."""
    end = clock.eastern(time.time()).date()
    start = end - timedelta(days=days)
    url = STOOQ_DAILY_URL.format(symbol=symbol, d1=start.strftime("%Y%m%d"),
                                 d2=end.strftime("%Y%m%d"))
    try:
        text = _http_get(url).decode()
    except (urllib.error.URLError, OSError):
        return []
    out = []
    for line in text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            y, m, d = (int(x) for x in parts[0].split("-"))
            o, h, l, c = (float(x) for x in parts[1:5])
            v = float(parts[5]) if len(parts) > 5 and parts[5] not in ("", "N/D") else 0.0
        except ValueError:
            continue
        # Midnight ET of the trade date: after that session's 18:00 open,
        # before the next one's — see _context_from_daily.
        ts = clock.et_ts(y, m, d, 0, 0)
        out.append(Candle(ts, o, h, l, c, v))
    return out


def fetch_stooq_daily_context(symbol: str = DEFAULT_SYMBOL, before_ts: int | None = None) -> dict:
    ss = stooq_symbol(symbol)
    if not ss:
        return {}
    return _context_from_daily(fetch_stooq_daily(ss), before_ts)


# -------------------------------------------------------------------- demo

def _demo_bar_profile(minute_of_day: int) -> float:
    """Volatility multiplier by Eastern time of day for the synthetic tape:
    thin overnight, a bump at the 08:30 releases, hot RTH open, quiet
    lunch, active close."""
    if minute_of_day >= 18 * 60 or minute_of_day < 8 * 60:
        return 0.35
    if minute_of_day < 9 * 60 + 30:
        return 0.6 if minute_of_day < 8 * 60 + 30 else 0.8
    if minute_of_day < 10 * 60 + 15:
        return 1.6
    if 12 * 60 <= minute_of_day < 13 * 60 + 30:
        return 0.6
    if minute_of_day < 16 * 60:
        return 1.0 if minute_of_day < 15 * 60 + 15 else 1.25
    return 0.5  # 16:00-17:00 settlement hour


def _last_demo_trade_date(now: float):
    """Trade date of the most recent session whose RTH has at least an hour
    of bars as of `now` — so the demo always has a meaningful cash session,
    even when run at 3 a.m. or on a Sunday."""
    d = clock.trade_date(now)
    # Before 10:30 ET on the trade date the RTH portion is too thin: back up.
    if clock.et_minutes(now) < 10 * 60 + 30 or clock.et_date(now) != d:
        d = d - timedelta(days=1)
    while d.weekday() >= 5:  # weekend -> Friday
        d = d - timedelta(days=1)
    return d


def demo_sessions(
    symbol: str = DEFAULT_SYMBOL, days: int = 1, seed: int = 42, now: float | None = None
) -> list[Session]:
    """`days` consecutive synthetic Globex sessions ending with the latest
    completed-enough one. Deterministic for a given seed. Each session is
    18:00 ET -> 17:00 ET with a realistic time-of-day volatility profile,
    so RTH slicing, opening ranges and overnight levels all exercise the
    same code paths as live data."""
    rng = random.Random(seed)
    now = time.time() if now is None else now
    last_date = _last_demo_trade_date(now)
    dates = []
    d = last_date
    while len(dates) < days:
        if d.weekday() < 5:
            dates.append(d)
        d = d - timedelta(days=1)
    dates.reverse()

    price = 23_450.0 - 8.0 * days
    sessions: list[Session] = []
    rth_highs: list[float] = []
    rth_lows: list[float] = []
    for td in dates:
        open_ts = clock.session_start(clock.et_ts(td.year, td.month, td.day, 12, 0))  # 18:00 ET prior day
        close_ts = clock.et_ts(td.year, td.month, td.day, 17, 0)
        drift = rng.choice([-0.35, -0.15, 0.15, 0.35])
        candles: list[Candle] = []
        ts = open_ts
        while ts < close_ts:
            mod = clock.et_minutes(ts)
            vol_mult = _demo_bar_profile(mod)
            sigma = 6.0 * vol_mult
            if rng.random() < 0.03:
                sigma *= 3
            o = price
            move = rng.gauss(drift * vol_mult, sigma)
            c = o + move
            h = max(o, c) + abs(rng.gauss(0, sigma * 0.4))
            l = min(o, c) - abs(rng.gauss(0, sigma * 0.4))
            candles.append(Candle(ts, round(o, 2), round(h, 2), round(l, 2), round(c, 2),
                                  rng.randint(500, 4000) * vol_mult))
            price = c
            ts += 60
        sess = Session(symbol=symbol, candles=candles, source="demo")
        if sessions:
            rth_prev = sessions[-1].rth or sessions[-1].candles
            sess.prev_open = rth_prev[0].open
            sess.prev_high = max(c.high for c in rth_prev)
            sess.prev_low = min(c.low for c in rth_prev)
            sess.prev_close = rth_prev[-1].close
            rth_highs.append(sess.prev_high)
            rth_lows.append(sess.prev_low)
            sess.week_high = max(rth_highs[-5:])
            sess.week_low = min(rth_lows[-5:])
        else:
            base = candles[0].open
            sess.prev_close = round(base - drift * 40 + rng.gauss(0, 25), 2)
            sess.prev_high = round(max(sess.prev_close, base) + 60, 2)
            sess.prev_low = round(min(sess.prev_close, base) - 60, 2)
            sess.prev_open = round(sess.prev_close - drift * 30 + rng.gauss(0, 20), 2)
            sess.week_high = round(sess.prev_high + 85, 2)
            sess.week_low = round(sess.prev_low - 110, 2)
        annotate_session(sess)
        sessions.append(sess)
    return sessions


def demo_session(
    symbol: str = DEFAULT_SYMBOL, minutes: int | None = None, seed: int = 42,
    now: float | None = None,
) -> Session:
    """Deterministic synthetic Globex session that looks like an NQ day.

    `minutes` is kept for callers that want a multi-day tape: it is turned
    into whole sessions (390 RTH minutes per day)."""
    days = max(1, round(minutes / 390)) if minutes else 1
    sessions = demo_sessions(symbol, days=days, seed=seed, now=now)
    if days == 1:
        return sessions[-1]
    merged = Session(symbol=symbol, source="demo")
    merged.candles = [c for s in sessions for c in s.candles]
    last = sessions[-1]
    for k in ("prev_close", "prev_high", "prev_low", "prev_open", "week_high", "week_low"):
        setattr(merged, k, getattr(last, k))
    return annotate_session(merged)


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
        return build_range_bars(get_session(symbol, demo, context=False).candles, float(range_pts))
    if tf not in TF_MAP:
        raise ValueError(f"unknown timeframe {tf!r} (use {'/'.join(TF_MAP)} or range bars)")
    if tf == "1m":
        return get_session(symbol, demo, context=False).candles
    if demo:
        if tf == "1d":
            return demo_daily(symbol)
        mins = {"5m": 5, "15m": 15, "1h": 60}[tf]
        days = 5 if tf in ("5m", "15m") else 21
        long_sess = demo_session(symbol, minutes=390 * days, seed=11)
        return aggregate_candles(long_sess.candles, mins * 60)
    interval, day_range = TF_MAP[tf]
    try:
        return fetch_yahoo_session(symbol, interval=interval, day_range=day_range).candles
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f"chart data unavailable for {symbol} {tf}: {e}") from e


def get_session(
    symbol: str = DEFAULT_SYMBOL, demo: bool | None = None, context: bool = True
) -> Session:
    """Fetch today's Globex day of 1m bars, filling prior-day levels
    (skipped with context=False — one fewer upstream call when only the
    bars are wanted); falls back gracefully."""
    if demo is None:
        demo = is_demo()
    if demo:
        return demo_session(symbol)
    sess: Session | None = None
    try:
        sess = fetch_yahoo_intraday(symbol)
    except (urllib.error.URLError, KeyError, ValueError, TypeError, OSError):
        sess = None
    if sess is not None and sess.candles:
        if not context:
            return sess
        ctx: dict = {}
        try:
            ctx = fetch_yahoo_daily_context(symbol, sess.session_start_ts)
        except (urllib.error.URLError, KeyError, ValueError, TypeError, OSError):
            ctx = {}
        if not ctx.get("prev_close"):
            ctx = {**fetch_stooq_daily_context(symbol, sess.session_start_ts), **ctx}
        sess.prev_open = ctx.get("prev_open")
        sess.prev_high = ctx.get("prev_high")
        sess.prev_low = ctx.get("prev_low")
        sess.prev_close = ctx.get("prev_close") or sess.prev_close
        sess.week_high = ctx.get("week_high")
        sess.week_low = ctx.get("week_low")
        return sess
    # Yahoo failed — try stooq for at least a last price.
    last = fetch_stooq_quote(stooq_symbol(symbol) or STOOQ_SYMBOL)
    if last is not None:
        c = Candle(ts=int(time.time()), open=last, high=last, low=last, close=last)
        return annotate_session(Session(symbol=symbol, candles=[c], source="stooq"))
    raise RuntimeError(
        "No market data source reachable (yahoo + stooq failed). "
        "Set NQ_DEMO=1 to run with synthetic data."
    )
