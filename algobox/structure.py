"""Structural modules: volume zones and trend strength.

  zones   where the session's volume actually built up — point of control,
          value area, and the thin prices between them that price crosses
          fast
  trend   whether the tape is going somewhere or just churning

The simple engine builds its profile from each bar's implied path
(core.path_volume_split); the tick engine builds it from real prints, so
it can also say *who* built each node.
"""

from __future__ import annotations

from nq.bias import ema

from .core import (
    SIMPLE, TICK, Context, Event, Module, Reading,
    clamp, mean, path_volume_split, profile_step, quantize, scale,
)

VALUE_AREA = 0.70


def _value_area(hist: dict[float, float], pct: float = VALUE_AREA):
    """(poc, val, vah) — the classic market-profile expansion from the
    point of control outward, taking the fatter neighbour each step until
    `pct` of the session's volume is enclosed."""
    if not hist:
        return None, None, None
    prices = sorted(hist)
    vols = [hist[p] for p in prices]
    total = sum(vols) or 1.0
    i = max(range(len(prices)), key=lambda k: vols[k])
    lo = hi = i
    acc = vols[i]
    while acc < total * pct and (lo > 0 or hi < len(prices) - 1):
        below = vols[lo - 1] if lo > 0 else float("-inf")
        above = vols[hi + 1] if hi < len(prices) - 1 else float("-inf")
        if above >= below:
            hi += 1
            acc += vols[hi]
        else:
            lo -= 1
            acc += vols[lo]
    return prices[i], prices[lo], prices[hi]


def _nodes(hist: dict[float, float], step: float, top: int = 3):
    """High- and low-volume nodes. HVNs are shelves price grinds on; LVNs
    are the gaps it crosses in a hurry, which is why they make good
    targets and bad places to hide a stop."""
    if len(hist) < 7:
        return [], []
    prices = sorted(hist)
    vols = [hist[p] for p in prices]
    avg = mean(vols) or 1.0
    highs, lows = [], []
    for k in range(2, len(prices) - 2):
        window = vols[k - 2:k + 3]
        if vols[k] == max(window) and vols[k] > avg * 1.4:
            highs.append((prices[k], vols[k]))
        elif vols[k] == min(window) and vols[k] < avg * 0.5:
            lows.append((prices[k], vols[k]))
    highs.sort(key=lambda x: -x[1])
    lows.sort(key=lambda x: x[1])
    return highs[:top], lows[:top]


class Zones(Module):
    key = "zones"
    name = "Volume zones"
    blurb = "Point of control, value area, and the thin prices in between."

    def _emit(self, ctx: Context, engine: str, hist: dict[float, float],
              step: float, conf: float, note: str, extra: dict) -> Reading:
        poc, val, vah = _value_area(hist)
        if poc is None:
            return self.reading(ctx, engine, confidence=0.0, detail="no profile")
        price = ctx.price
        atr = ctx.atr or 1.0
        if price > vah:
            base, where = 55.0, "above value"
        elif price < val:
            base, where = -55.0, "below value"
        else:
            base = scale(price, val, vah, -25.0, 25.0)
            where = "inside value"
        stretch = abs(price - poc) / atr
        if stretch > 2.0:  # accepted away from value, or just extended?
            base *= 0.6
            where += ", extended"
        highs, lows = _nodes(hist, step)
        ts = int(ctx.candles[-1].ts)
        events = []
        near = min(lows, key=lambda x: abs(x[0] - price)) if lows else None
        if near and abs(near[0] - price) <= 3.0 * atr:  # reachable, or not worth saying
            events.append(Event(
                ts=ts, kind=self.key, side="none", price=near[0],
                text=f"thin volume at {near[0]:.2f} ({abs(near[0] - price):.2f} pts "
                     "away) — price tends to cross these fast, so it makes a "
                     "better target than a stop",
                weight=0.4))
        if stretch > 1.5:
            # A magnet, not a trade: naming a side here would put a
            # mean-reversion vote in the middle of a trend reading.
            events.append(Event(
                ts=ts, kind=self.key, side="none", price=poc,
                text=f"POC {poc:.2f} sits {stretch:.1f} ATR "
                     f"{'below' if price > poc else 'above'} price — the "
                     "session's volume magnet, and the obvious target if "
                     "this move fails",
                weight=clamp(scale(stretch, 1.5, 3.5, 0.3, 0.8), 0.3, 0.8)))
        state = ("ABOVE VALUE" if price > vah else "BELOW VALUE" if price < val
                 else "IN VALUE")
        return self.reading(
            ctx, engine, state=state, score=clamp(base, -100, 100),
            strength=clamp(40 + abs(base) * 0.6, 0, 100),
            detail=f"POC {poc:.2f}, value {val:.2f}–{vah:.2f}; price is {where} "
                   f"({stretch:.1f} ATR from POC); {note}",
            value=poc, unit="POC", confidence=conf, events=events,
            extra={**extra, "poc": round(poc, 2), "val": round(val, 2),
                   "vah": round(vah, 2), "step": step,
                   "hvn": [round(p, 2) for p, _ in highs],
                   "lvn": [round(p, 2) for p, _ in lows],
                   "profile": [[round(p, 2), round(v, 1)]
                               for p, v in sorted(hist.items())][:400]},
        )

    def simple(self, ctx: Context) -> Reading:
        if len(ctx.candles) < 20:
            return self.reading(ctx, SIMPLE, confidence=0.0)
        step = profile_step(ctx.candles, ctx.tick_sz)
        hist: dict[float, float] = {}
        for c in ctx.candles:
            for p, (sell, buy) in path_volume_split(c, step).items():
                hist[p] = hist.get(p, 0.0) + sell + buy
        return self._emit(ctx, SIMPLE, hist, step,
                          0.7 if ctx.has_volume else 0.4,
                          "volume spread along each bar's implied path",
                          {"bars": len(ctx.candles)})

    def tick(self, ctx: Context) -> Reading:
        if not ctx.ticks:
            return self.reading(ctx, TICK, confidence=0.0, detail="no tape")
        step = profile_step(ctx.candles, ctx.tick_sz)
        hist: dict[float, float] = {}
        delta: dict[float, float] = {}
        for t in ctx.ticks:
            p = quantize(t.price, step)
            hist[p] = hist.get(p, 0.0) + t.size
            delta[p] = delta.get(p, 0.0) + t.signed
        poc = max(hist, key=hist.get) if hist else 0.0
        builder = ("buyers" if delta.get(poc, 0.0) > 0 else "sellers")
        return self._emit(
            ctx, TICK, hist, step, ctx.tick_confidence,
            f"built from {len(ctx.ticks):,} prints — {builder} built the POC "
            f"({delta.get(poc, 0.0):+,.0f} delta there)",
            {"prints": len(ctx.ticks),
             "poc_delta": round(delta.get(poc, 0.0), 1)})


# ------------------------------------------------------------------ trend

class Trend(Module):
    key = "trend"
    name = "Trend strength"
    blurb = "Is the tape going somewhere, or just moving?"

    def simple(self, ctx: Context) -> Reading:
        cs = ctx.candles
        if len(cs) < 30:
            return self.reading(ctx, SIMPLE, confidence=0.0)
        closes = [c.close for c in cs]
        fast, slow = ema(closes, 9), ema(closes, 21)
        sep = (fast[-1] - slow[-1]) / (ctx.atr or 1.0)
        recent = cs[-20:]
        above = sum(1 for c, s in zip(recent, slow[-20:]) if c.close > s) / len(recent)
        consistency = abs(above - 0.5) * 2.0        # 0..1
        # ADX-lite over the same 20 bars: directional movement vs total range.
        dm_up = dm_dn = tr = 0.0
        for prev, cur in zip(cs[-21:-1], recent):
            up, dn = cur.high - prev.high, prev.low - cur.low
            dm_up += up if up > dn and up > 0 else 0.0
            dm_dn += dn if dn > up and dn > 0 else 0.0
            tr += max(cur.high - cur.low, abs(cur.high - prev.close),
                      abs(cur.low - prev.close))
        di_sum = dm_up + dm_dn
        adx = (abs(dm_up - dm_dn) / di_sum) if di_sum else 0.0
        score = clamp(scale(sep, -1.5, 1.5, -70, 70) * (0.5 + 0.5 * consistency), -100, 100)
        strength = clamp(adx * 60 + consistency * 40, 0, 100)
        state = ("TRENDING UP" if score > 25 and strength > 40 else
                 "TRENDING DOWN" if score < -25 and strength > 40 else
                 "DRIFT UP" if score > 10 else "DRIFT DOWN" if score < -10 else "CHOP")
        return self.reading(
            ctx, SIMPLE, state=state, score=score, strength=strength,
            detail=f"EMA9 is {sep:+.2f} ATR from EMA21, {above:.0%} of the last "
                   f"20 bars closed on that side, ADX-lite {adx * 100:.0f}",
            value=round(sep, 2), unit="ATR separation",
            confidence=0.8, series=[round(f - s, 3) for f, s in
                                    zip(fast[-120:], slow[-120:])],
            extra={"adx": round(adx * 100, 1), "consistency": round(consistency, 2)},
        )

    def tick(self, ctx: Context) -> Reading:
        ticks, bars = ctx.ticks, ctx.foot
        if len(ticks) < 500 or len(bars) < 8:
            return self.reading(ctx, TICK, confidence=0.0, detail="no tape")
        win = [t for t in ticks if t.ts >= ticks[-1].ts - 20 * ctx.bar_seconds] or ticks
        # Run persistence: how long the tape stays on one side before
        # flipping. A coin flip averages 2; real trends run longer.
        runs, run, prev = [], 0, None
        for t in win:
            if t.side == "?":
                continue
            if t.side == prev:
                run += 1
            else:
                if run:
                    runs.append(run)
                run, prev = 1, t.side
        if run:
            runs.append(run)
        persistence = (mean(runs) / 2.0) if runs else 1.0
        recent = bars[-20:]
        moved = recent[-1].close - recent[0].open
        # Direction is price's to decide. The tape's job is to say whether
        # the move was paid for — flow that contradicts price is a weak
        # trend, and calling out that contradiction is the delta module's
        # job, not this one's.
        direction = 1.0 if moved > 0 else -1.0 if moved < 0 else 0.0
        signs = [1 if b.delta > 0 else -1 if b.delta < 0 else 0 for b in recent]
        backing = (sum(1 for s in signs if s and s == direction) / len(signs)
                   if direction else 0.0)
        effort = sum(abs(b.delta) for b in recent) or 1.0
        efficiency = abs(moved) / (effort / 1000.0)  # points per 1k contracts of delta
        score = clamp(direction * scale(backing, 0.15, 0.75, 8, 85), -100, 100)
        strength = clamp(clamp((persistence - 1) * 90, 0, 55) + backing * 55, 0, 100)
        state = ("TRENDING UP" if score > 25 and strength > 40 else
                 "TRENDING DOWN" if score < -25 and strength > 40 else
                 "GRIND UP" if score > 10 else "GRIND DOWN" if score < -10 else "CHOP")
        return self.reading(
            ctx, TICK, state=state, score=score, strength=strength,
            detail=f"price {moved:+.2f} pts with {backing:.0%} of the last "
                   f"{len(signs)} bars' delta behind it; aggressor runs average "
                   f"{mean(runs):.1f} prints ({persistence:.2f}x a coin flip), "
                   f"{efficiency:.2f} pts per 1k contracts of delta",
            value=round(persistence, 2), unit="x random",
            confidence=ctx.tick_confidence,
            series=[round(b.delta, 1) for b in bars[-120:]],
            extra={"run_persistence": round(persistence, 2),
                   "delta_backing": round(backing, 2),
                   "points_per_1k_delta": round(efficiency, 3)},
        )


MODULES = [Zones(), Trend()]
