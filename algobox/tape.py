"""Tape-dynamics modules: speed and liquidity sweeps.

  speed   how fast the market is actually going — the gauge that tells you
          whether every other reading deserves to be acted on
  sweep   price poking through a level, taking the stops resting there, and
          coming straight back

Speed is deliberately non-directional. A violent tape is not bullish or
bearish; it is a reason to widen stops and to trust confluence less. The
suite uses it as a conviction multiplier rather than a vote.
"""

from __future__ import annotations

from .core import (
    SIMPLE, TICK, Context, Event, Module, Reading, clamp, mean, scale,
)

SPEED_STATES = ((0.4, "DEAD"), (0.8, "SLOW"), (1.5, "NORMAL"), (2.5, "FAST"))


def _speed_state(ratio: float) -> str:
    for cutoff, label in SPEED_STATES:
        if ratio < cutoff:
            return label
    return "VIOLENT"


class Speed(Module):
    key = "speed"
    name = "Speedometer"
    blurb = "Activity now versus this session's own normal."
    directional = False

    def _emit(self, ctx: Context, engine: str, ratio: float, conf: float,
              detail: str, value: float, unit: str, extra: dict,
              series: list[float]) -> Reading:
        gauge = scale(ratio, 0.0, 3.0, 0.0, 100.0)
        return self.reading(
            ctx, engine, state=_speed_state(ratio), score=0.0, strength=gauge,
            detail=detail, value=round(value, 2), unit=unit, confidence=conf,
            series=series, extra={**extra, "ratio": round(ratio, 2),
                                  "gauge": round(gauge, 1)},
        )

    def simple(self, ctx: Context) -> Reading:
        cs = ctx.candles
        if len(cs) < 20:
            return self.reading(ctx, SIMPLE, confidence=0.0)
        # Volume when the feed gives it, bar range when it doesn't.
        if ctx.has_volume:
            act = [c.volume or 0.0 for c in cs]
            unit, label = "contracts/bar", "volume"
        else:
            act = [c.high - c.low for c in cs]
            unit, label = "points/bar", "range"
        baseline = sorted(act)[len(act) // 2] or 1.0  # median, not mean
        recent = mean(act[-3:])
        ratio = recent / baseline
        return self._emit(
            ctx, SIMPLE, ratio, 0.7,
            f"last 3 bars ran {ratio:.1f}x the session's median {label}",
            recent, unit, {"baseline": round(baseline, 2)},
            [round(a / baseline, 3) for a in act[-120:]])

    def tick(self, ctx: Context) -> Reading:
        ticks = ctx.ticks
        if len(ticks) < 200:
            return self.reading(ctx, TICK, confidence=0.0, detail="no tape")
        span = float(ctx.bar_seconds)
        end = ticks[-1].ts
        elapsed = max(1.0, end - ticks[0].ts)
        sess_tps = len(ticks) / elapsed
        win = [t for t in ticks if t.ts >= end - span] or ticks[-200:]
        win_secs = max(1.0, win[-1].ts - win[0].ts)
        tps = len(win) / win_secs
        cps = sum(t.size for t in win) / win_secs
        ratio = tps / sess_tps if sess_tps else 1.0
        # Fastest 5-second burst in the trailing window — the number that
        # separates "busy" from "something just happened".
        buckets: dict[int, int] = {}
        for t in ticks[-4000:]:
            buckets[int(t.ts // 5)] = buckets.get(int(t.ts // 5), 0) + 1
        burst = (max(buckets.values()) / 5.0) if buckets else 0.0
        series = [round(v / 5.0 / (sess_tps or 1), 3)
                  for _, v in sorted(buckets.items())][-120:]
        return self._emit(
            ctx, TICK, ratio, ctx.tick_confidence,
            f"{tps:.1f} prints/sec and {cps:,.0f} contracts/sec over the last "
            f"{win_secs:.0f}s — {ratio:.1f}x session pace "
            f"(peak burst {burst:.1f}/sec)",
            tps, "prints/sec",
            {"contracts_per_sec": round(cps, 1),
             "session_prints_per_sec": round(sess_tps, 2),
             "peak_burst_per_sec": round(burst, 1)},
            series)


# ----------------------------------------------------------------- sweeps

SWEEP_LOOKBACK_BARS = 60
SWEEP_PIERCE_ATR = 0.12
SWEEP_MAX_SECONDS = 120     # a stop run that lingers is just a breakout


def _candidate_levels(ctx: Context, limit: int = 8):
    """Ranked levels close enough to matter, best first."""
    near = [lv for lv in ctx.levels
            if ctx.atr and abs(lv.price - ctx.price) <= 4.0 * ctx.atr]
    return sorted(near or ctx.levels, key=lambda lv: -lv.score)[:limit]


class Sweep(Module):
    key = "sweep"
    name = "Liquidity sweep"
    blurb = "Stops taken above a high or below a low, then price rejected."

    def _emit(self, ctx: Context, engine: str, events: list[Event],
              conf: float, detail: str, extra: dict) -> Reading:
        recent = sorted(events, key=lambda e: e.ts)[-5:]
        net = sum((1 if e.side == "long" else -1) * e.weight for e in recent)
        score = clamp(net * 50.0, -100, 100)
        state = ("HIGHS SWEPT" if score < -20 else "LOWS SWEPT" if score > 20
                 else "BOTH SWEPT" if recent else "NONE")
        return self.reading(
            ctx, engine, state=state, score=score,
            strength=clamp(len(recent) * 18.0 + abs(net) * 30.0, 0, 100),
            detail=detail, value=float(len(events)), unit="sweeps",
            confidence=conf, events=recent, extra=extra,
        )

    def simple(self, ctx: Context) -> Reading:
        cs = ctx.candles
        levels = _candidate_levels(ctx)
        if len(cs) < 25 or not levels:
            return self.reading(ctx, SIMPLE, confidence=0.0,
                                detail="no ranked levels in range")
        vols = [c.volume or 1.0 for c in cs]
        avg_vol = mean(vols[-40:]) or 1.0
        start = max(1, len(cs) - SWEEP_LOOKBACK_BARS)
        pierce = SWEEP_PIERCE_ATR * ctx.atr
        events: list[Event] = []
        for i in range(start, len(cs)):
            c, prev = cs[i], cs[i - 1]
            age = len(cs) - 1 - i
            decay = scale(age, 0, SWEEP_LOOKBACK_BARS, 1.0, 0.2)
            hot = (vols[i] / avg_vol) if avg_vol else 1.0
            for lv in levels:
                swept_high = (prev.close < lv.price and c.high > lv.price + pierce
                              and c.close < lv.price)
                swept_low = (prev.close > lv.price and c.low < lv.price - pierce
                             and c.close > lv.price)
                if not (swept_high or swept_low):
                    continue
                if hot < 1.15:
                    continue  # a quiet poke through is not a stop run
                side = "short" if swept_high else "long"
                beyond = (c.high - lv.price) if swept_high else (lv.price - c.low)
                events.append(Event(
                    ts=int(c.ts), kind=self.key, side=side, price=lv.price,
                    text=f"{lv.label} swept by {beyond:.2f} pts on {hot:.1f}x "
                         "volume, closed back through",
                    weight=clamp(decay * scale(hot, 1.15, 3.0, 0.4, 1.0), 0.15, 1.0)))
                break
        return self._emit(
            ctx, SIMPLE, events, 0.65,
            (events[-1].text if events else
             f"no level swept and reclaimed in the last {SWEEP_LOOKBACK_BARS} bars"),
            {"detected": len(events), "levels_watched": len(levels)})

    def tick(self, ctx: Context) -> Reading:
        ticks = ctx.ticks
        levels = _candidate_levels(ctx)
        if len(ticks) < 500 or not levels:
            return self.reading(ctx, TICK, confidence=0.0,
                                detail="no tape or no ranked levels in range")
        window = [t for t in ticks if t.ts >= ticks[-1].ts
                  - SWEEP_LOOKBACK_BARS * ctx.bar_seconds] or ticks
        last_ts = window[-1].ts
        pierce = max(SWEEP_PIERCE_ATR * ctx.atr, ctx.tick_sz)
        events: list[Event] = []
        for lv in levels:
            for above in (True, False):
                for exc in _excursions(window, lv.price, above):
                    dur = exc["end"] - exc["start"]
                    beyond = abs(exc["extreme"] - lv.price)
                    if beyond < pierce or dur > SWEEP_MAX_SECONDS:
                        continue
                    # Was the excursion driven by the side that gets trapped?
                    driver = exc["buy"] if above else exc["sell"]
                    total = exc["buy"] + exc["sell"]
                    if total <= 0 or driver / total < 0.55:
                        continue
                    age = last_ts - exc["end"]
                    decay = scale(age, 0, SWEEP_LOOKBACK_BARS * ctx.bar_seconds, 1.0, 0.2)
                    events.append(Event(
                        ts=int(exc["end"]), kind=self.key,
                        side="short" if above else "long", price=lv.price,
                        text=f"{lv.label}: {driver:,.0f} aggressive contracts "
                             f"{beyond:.2f} pts through it for {dur:.0f}s, then back",
                        weight=clamp(decay * scale(driver / total, 0.55, 0.9, 0.4, 1.0),
                                     0.15, 1.0)))
        swept = sum(e.weight for e in events)
        return self._emit(
            ctx, TICK, events, ctx.tick_confidence,
            (events[-1].text if events else
             "no level was pierced and abandoned inside the window"),
            {"detected": len(events), "levels_watched": len(levels),
             "weighted": round(swept, 2)})


def _excursions(ticks, level: float, above: bool) -> list[dict]:
    """Completed trips beyond `level` — start, end, extreme, and the
    aggressive volume that traded while price was through it.

    An excursion still in progress is deliberately excluded: a sweep is
    only a sweep once price has come back."""
    out: list[dict] = []
    cur: dict | None = None
    for t in ticks:
        beyond = t.price > level if above else t.price < level
        if beyond:
            if cur is None:
                cur = {"start": t.ts, "end": t.ts, "extreme": t.price,
                       "buy": 0.0, "sell": 0.0}
            cur["end"] = t.ts
            cur["extreme"] = (max(cur["extreme"], t.price) if above
                              else min(cur["extreme"], t.price))
            if t.side == "B":
                cur["buy"] += t.size
            elif t.side == "S":
                cur["sell"] += t.size
        elif cur is not None:
            out.append(cur)
            cur = None
    return out


MODULES = [Speed(), Sweep()]
