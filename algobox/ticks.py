"""The tick layer: where AlgoBox's tick-level engines get their tape.

Three sources, in order of honesty:

  file           a real tick export (CSV) — the genuine article. Point
                 ALGOBOX_TICK_FILE (or --ticks) at it and every tick-level
                 module runs on real order flow.
  reconstructed  a deterministic print stream rebuilt from 1-minute OHLCV.
                 The free feeds this repo uses (Yahoo, Stooq) do not sell
                 tick data, so the tick engines would otherwise have
                 nothing to read.
  none           no bars at all.

**Read this before trusting a reconstructed number.** Reconstruction
preserves what the bar actually proves — open, high, low, close, total
volume, and a plausible path between them — and *models* the rest: the
order the prints arrived in and how size was distributed across them.

Aggressor side is derived strictly from the path (prints made on the way
up lifted the offer; prints made on the way down hit the bid; prints that
moved nothing are split in the proportion the bar closed at). Nothing
random ever touches side. An earlier draft added a random "hidden flow"
term so that reconstructed cumulative delta would diverge from price the
way real delta does; it was removed because it made the tick engines
report a *direction* that came from a seed rather than from the market.
A number that changes sign between runs is not analysis.

What this means in practice: reconstructed tick readings add resolution —
where in the bar volume traded, at which prices, in what runs, how long
price spent through a level — but they cannot reveal flow the bars do not
already imply. Every reading built this way carries `confidence < 1` and
the suite labels the source on every panel. Reconstructed absorption is a
hypothesis about what a bar implies; file-sourced absorption is a
measurement. If the difference matters to your decision, get a tick file.

Reconstruction is deterministic and content-addressed: identical OHLCV
always yields identical prints, regardless of timestamps or position in
the series, so a polling dashboard does not flicker and appending new bars
never rewrites the history of old ones.
"""

from __future__ import annotations

import csv
import math
import os
import random
from dataclasses import dataclass, field
from pathlib import Path

from nq.data import Candle

from .core import Tick, quantize, tick_size

TICK_FILE_ENV = ("ALGOBOX_TICK_FILE", "NQ_TICK_FILE")
# Prints synthesised per bar. Enough to make footprints meaningful, few
# enough that a 390-bar session stays well under a hundred thousand ticks.
MAX_PRINTS_PER_BAR = int(os.environ.get("ALGOBOX_PRINTS_PER_BAR", "90"))
RECONSTRUCTED_CONFIDENCE = 0.6


# ------------------------------------------------------------------ files

_TS_KEYS = ("ts", "time", "timestamp", "datetime", "date")
_PRICE_KEYS = ("price", "last", "px", "close")
_SIZE_KEYS = ("size", "qty", "quantity", "volume", "vol")
_SIDE_KEYS = ("side", "aggressor", "direction", "type")


def _pick(row: dict, keys) -> str | None:
    for k in keys:
        for actual in row:
            if actual and actual.strip().lower() == k:
                v = row[actual]
                if v not in (None, ""):
                    return v.strip()
    return None


def _parse_ts(raw: str) -> float | None:
    try:
        v = float(raw)
    except ValueError:
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if v > 1e17:      # nanoseconds
        return v / 1e9
    if v > 1e14:      # microseconds
        return v / 1e6
    if v > 1e11:      # milliseconds
        return v / 1e3
    return v


def _parse_side(raw: str | None) -> str:
    if not raw:
        return "?"
    s = raw.strip().lower()
    if s in ("b", "buy", "bought", "a", "ask", "askhit", "up", "1", "+1"):
        return "B"
    if s in ("s", "sell", "sold", "bid", "bidhit", "down", "-1", "0"):
        return "S"
    return "?"


def load_tick_file(path: str | Path) -> list[Tick]:
    """Read a tick CSV. Recognised columns (case-insensitive, any order):

        ts|time|timestamp|datetime   unix s/ms/us/ns or ISO-8601
        price|last|px                required
        size|qty|volume              defaults to 1
        side|aggressor               B/S, buy/sell, bid/ask — optional
        bid, ask                     optional; used to classify when
                                     `side` is absent

    When neither `side` nor a bid/ask pair is present, aggressors are
    inferred with the tick rule (uptick = buyer, downtick = seller, flat
    carries the previous classification) — the standard Lee-Ready fallback.
    """
    ticks: list[Tick] = []
    with open(path, newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        has_header = csv.Sniffer().has_header(sample) if sample else False
        if not has_header:
            reader = csv.DictReader(fh, dialect=dialect,
                                    fieldnames=["ts", "price", "size", "side"])
        else:
            reader = csv.DictReader(fh, dialect=dialect)
        prev_side = "?"
        prev_price: float | None = None
        for row in reader:
            raw_ts, raw_px = _pick(row, _TS_KEYS), _pick(row, _PRICE_KEYS)
            if raw_ts is None or raw_px is None:
                continue
            ts = _parse_ts(raw_ts)
            try:
                price = float(raw_px)
            except ValueError:
                continue
            if ts is None:
                continue
            raw_size = _pick(row, _SIZE_KEYS)
            try:
                size = float(raw_size) if raw_size else 1.0
            except ValueError:
                size = 1.0
            bid = ask = None
            for key, target in (("bid", "bid"), ("ask", "ask"), ("offer", "ask")):
                raw = _pick(row, (key,))
                if raw:
                    try:
                        val = float(raw)
                    except ValueError:
                        continue
                    if target == "bid":
                        bid = val
                    else:
                        ask = val
            side = _parse_side(_pick(row, _SIDE_KEYS))
            if side == "?" and bid is not None and ask is not None:
                if price >= ask:
                    side = "B"
                elif price <= bid:
                    side = "S"
            if side == "?":  # tick rule
                if prev_price is None or price == prev_price:
                    side = prev_side
                else:
                    side = "B" if price > prev_price else "S"
            prev_side, prev_price = side, price
            ticks.append(Tick(ts=ts, price=price, size=size, side=side, bid=bid, ask=ask))
    ticks.sort(key=lambda t: t.ts)
    return ticks


def tick_file_path(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() else None
    for env in TICK_FILE_ENV:
        p = os.environ.get(env, "").strip()
        if p and Path(p).exists():
            return p
    return None


# --------------------------------------------------------- reconstruction

def bar_seconds(candles: list[Candle], default: int = 60) -> int:
    """Median spacing between bars — works for 1m, 5m, daily, range bars."""
    if len(candles) < 3:
        return default
    diffs = sorted(b.ts - a.ts for a, b in zip(candles[:-1], candles[1:]) if b.ts > a.ts)
    return int(diffs[len(diffs) // 2]) if diffs else default


def _bar_seed(c: Candle, tick_sz: float) -> int:
    """Seed drawn from the bar's *contents*, never its timestamp.

    Same OHLCV in, same prints out — across processes, across sessions,
    and no matter where the bar sits in the series. Only the parts of the
    model that carry no directional information (print sizes, sub-tick
    jitter) consume this."""
    key = (round(c.open / tick_sz), round(c.high / tick_sz),
           round(c.low / tick_sz), round(c.close / tick_sz), round(c.volume or 0))
    h = 2166136261
    for v in key:
        h = ((h ^ (int(v) & 0xFFFFFFFF)) * 16777619) & 0xFFFFFFFF
    return h


def _flat_sides(count: int, buy_share: float) -> list[str]:
    """Split `count` flat prints between the sides in `buy_share` proportion,
    spread evenly rather than drawn — a coin flip here would be a fabricated
    directional signal, which is exactly what this module refuses to do."""
    n_buy = int(round(count * buy_share))
    out = []
    for j in range(count):
        took = ((j + 1) * n_buy) // count > (j * n_buy) // count if count else False
        out.append("B" if took else "S")
    return out


def _bar_path(c: Candle, n: int, tick_sz: float, rng: random.Random) -> list[float]:
    """A plausible intrabar price path: open -> nearer extreme -> farther
    extreme -> close, interpolated with tick-grid noise. This is the same
    path convention nq.data.build_range_bars uses, so range bars and
    reconstructed ticks tell a consistent story."""
    pts = [c.open, c.low, c.high, c.close] if c.close >= c.open else [c.open, c.high, c.low, c.close]
    legs = [(pts[i], pts[i + 1]) for i in range(3)]
    spans = [abs(b - a) for a, b in legs]
    total = sum(spans)
    if total <= 0:
        return [c.close] * n

    def snap(p: float) -> float:
        """Onto the tick grid, but never outside the bar. Rounding to the
        grid can push a price past a high or low that is not itself on the
        grid (any synthetic or adjusted series); the bar's own extreme
        wins, because that one actually traded."""
        q = quantize(min(max(p, c.low), c.high), tick_sz)
        return q if c.low <= q <= c.high else min(max(p, c.low), c.high)

    counts = [max(1, int(round(n * s / total))) for s in spans]
    path: list[float] = []
    for (a, b), k in zip(legs, counts):
        for j in range(k):
            frac = (j + 1) / k
            path.append(snap(a + (b - a) * frac + rng.gauss(0, tick_sz * 0.7)))
    path[-1] = c.close  # the last print of a bar is its close, exactly
    return path


def reconstruct(
    candles: list[Candle], symbol: str, prints_per_bar: int = MAX_PRINTS_PER_BAR
) -> list[Tick]:
    """Rebuild a deterministic print stream from OHLCV bars.

    Preserved exactly: open, close, high/low containment, per-bar volume
    (to rounding), bar timing. Modelled: print sequence, print sizes, and
    aggressor side. See the module docstring before drawing conclusions.
    """
    if not candles:
        return []
    tick_sz = tick_size(symbol, candles[-1].close)
    span = bar_seconds(candles)
    ticks: list[Tick] = []
    for c in candles:
        vol = c.volume or 0.0
        if vol <= 0:  # feed omitted volume — scale prints off the bar's range
            vol = max(10.0, (c.high - c.low) / tick_sz * 8.0)
        n = int(min(prints_per_bar, max(4, round(math.sqrt(vol) * 2))))
        rng = random.Random(_bar_seed(c, tick_sz))
        path = _bar_path(c, n, tick_sz, rng)
        n = len(path)
        # Sizes: mostly small prints, an occasional block, summing to `vol`.
        weights = []
        for _ in range(n):
            w = rng.random() ** 2 + 0.05
            if rng.random() < 0.02:
                w *= 8.0
            weights.append(w)
        wsum = sum(weights) or 1.0
        # Aggressor comes from the path and nothing else: prints made on
        # the way up lifted the offer, prints made on the way down hit the
        # bid. Prints that did not move price are split in the proportion
        # the bar closed at. No random component touches side — a
        # reconstruction that guessed direction would be inventing the one
        # thing a trader is here to find out.
        rng_span = c.high - c.low
        buy_share = ((c.close - c.low) / rng_span if rng_span > 0
                     else (1.0 if c.close >= c.open else 0.0))
        sides: list[str] = []
        flat_at: list[int] = []
        prev = c.open
        for price in path:
            if price > prev:
                sides.append("B")
            elif price < prev:
                sides.append("S")
            else:
                flat_at.append(len(sides))
                sides.append("?")
            prev = price
        for slot, side in zip(flat_at, _flat_sides(len(flat_at), buy_share)):
            sides[slot] = side
        for k, price in enumerate(path):
            side = sides[k]
            size = max(1.0, round(vol * weights[k] / wsum))
            ts = c.ts + span * (k + 0.5) / n
            bid, ask = (price - tick_sz, price) if side == "B" else (price, price + tick_sz)
            ticks.append(Tick(ts=ts, price=price, size=size, side=side,
                              bid=round(bid, 10), ask=round(ask, 10)))
    return ticks


def get_ticks(
    candles: list[Candle], symbol: str, path: str | None = None
) -> tuple[list[Tick], str]:
    """(ticks, source) — a real tick file if one is configured, else a
    deterministic reconstruction of the bars you already have."""
    if not candles:
        return [], "none"
    resolved = tick_file_path(path)
    if resolved:
        try:
            ticks = load_tick_file(resolved)
        except (OSError, csv.Error, ValueError):
            ticks = []
        if ticks:
            lo, hi = candles[0].ts, candles[-1].ts + bar_seconds(candles)
            window = [t for t in ticks if lo <= t.ts <= hi]
            # A file covering a different day is still a real tape; use it
            # whole rather than silently returning nothing.
            return (window or ticks), "file"
    return reconstruct(candles, symbol), "reconstructed"


def source_confidence(source: str) -> float:
    return {"file": 1.0, "reconstructed": RECONSTRUCTED_CONFIDENCE}.get(source, 0.0)


# -------------------------------------------------------------- footprint

@dataclass
class FootBar:
    """A bar with its volume broken out by price and by aggressor —
    the footprint chart's underlying data structure."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    delta: float = 0.0
    trades: int = 0
    duration: float = 0.0
    bid_vol: dict = field(default_factory=dict)  # price -> volume sold into the bid
    ask_vol: dict = field(default_factory=dict)  # price -> volume bought at the ask

    def total_at(self, price: float) -> float:
        return self.bid_vol.get(price, 0.0) + self.ask_vol.get(price, 0.0)

    @property
    def prices(self) -> list[float]:
        return sorted(set(self.bid_vol) | set(self.ask_vol))


def build_footprint(ticks: list[Tick], span: int, tick_sz: float,
                    origin: int = 0) -> list[FootBar]:
    """Bucket ticks into `span`-second bars carrying per-price bid/ask volume.

    `origin` phases the grid — pass the first candle's timestamp so every
    footprint bar covers exactly one candle. Without it, a feed whose bars
    are not aligned to the minute produces footprints that straddle two
    candles, and the tick engines stop being comparable to the simple ones.
    """
    bars: list[FootBar] = []
    cur: FootBar | None = None
    bucket = None
    for t in ticks:
        b = origin + ((int(t.ts) - origin) // span) * span
        if cur is None or b != bucket:
            cur = FootBar(ts=b, open=t.price, high=t.price, low=t.price, close=t.price)
            bars.append(cur)
            bucket = b
        p = quantize(t.price, tick_sz)
        cur.high = max(cur.high, t.price)
        cur.low = min(cur.low, t.price)
        cur.close = t.price
        cur.volume += t.size
        cur.trades += 1
        cur.duration = t.ts - cur.ts
        if t.side == "B":
            cur.ask_vol[p] = cur.ask_vol.get(p, 0.0) + t.size
            cur.delta += t.size
        elif t.side == "S":
            cur.bid_vol[p] = cur.bid_vol.get(p, 0.0) + t.size
            cur.delta -= t.size
        else:
            half = t.size / 2.0
            cur.ask_vol[p] = cur.ask_vol.get(p, 0.0) + half
            cur.bid_vol[p] = cur.bid_vol.get(p, 0.0) + half
    return bars


def volume_at_price(ticks: list[Tick], tick_sz: float) -> dict[float, list[float]]:
    """price -> [volume hitting the bid, volume lifting the ask]."""
    out: dict[float, list[float]] = {}
    for t in ticks:
        p = quantize(t.price, tick_sz)
        slot = out.setdefault(p, [0.0, 0.0])
        if t.side == "B":
            slot[1] += t.size
        elif t.side == "S":
            slot[0] += t.size
        else:
            slot[0] += t.size / 2.0
            slot[1] += t.size / 2.0
    return out
