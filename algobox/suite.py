"""The suite: build one context, run eight modules, blend one verdict.

Two engines, same eight modules, same output shape:

  simple  everything derived from OHLCV bars. Runs anywhere, on any feed,
          in milliseconds.
  tick    everything derived from the print stream. Sees aggressor side,
          per-price ladders, and time — things a bar throws away.

`run_both()` runs each and reports where they disagree, which is the part
worth reading. Bars and tape agreeing is confirmation; the tape saying
"absorbed" while the bars say "breakout" is the trade.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

from nq.bias import atr as bar_atr, compute_bias
from nq.data import Session, get_chart_candles, get_session
from nq.levels import compute_auto_levels, load_levels
from nq.reversal import filter_proven, rank_levels

from .core import (
    ENGINES, SIMPLE, TICK, Context, Event, Module, Reading, clamp, mean, scale,
    tick_size,
)
from .flow import MODULES as _FLOW
from .structure import MODULES as _STRUCTURE
from .tape import MODULES as _TAPE
from .ticks import bar_seconds, build_footprint, get_ticks, source_confidence

# Display order: regime first (is this tape worth reading?), then the
# directional evidence, then structure.
MODULES: list[Module] = [
    next(m for m in _TAPE if m.key == "speed"),
    next(m for m in _STRUCTURE if m.key == "trend"),
    *_FLOW,
    next(m for m in _TAPE if m.key == "sweep"),
    next(m for m in _STRUCTURE if m.key == "zones"),
]
BY_KEY = {m.key: m for m in MODULES}

# How much each directional module moves the composite. Absorption and
# delta lead because they are the two that most often disagree with price
# — a composite that only echoes price is not worth computing.
WEIGHTS = {
    "delta": 1.3, "absorption": 1.2, "trend": 1.2, "sweep": 1.1,
    "imbalance": 1.0, "pressure": 0.9, "zones": 0.8,
}


@dataclass
class SuiteResult:
    engine: str
    symbol: str
    composite: float          # -100..+100
    label: str
    conviction: float         # 0..100
    tick_source: str
    readings: list[Reading] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    note: str = ""

    def reading(self, key: str) -> Reading | None:
        return next((r for r in self.readings if r.key == key), None)

    def to_dict(self) -> dict:
        return {
            "engine": self.engine, "symbol": self.symbol,
            "composite": round(self.composite, 1), "label": self.label,
            "conviction": round(self.conviction, 1),
            "tick_source": self.tick_source, "note": self.note,
            "modules": [r.to_dict() for r in self.readings],
            "events": [
                {"ts": e.ts, "kind": e.kind, "side": e.side,
                 "price": round(e.price, 2), "text": e.text,
                 "weight": round(e.weight, 2)}
                for e in self.events
            ],
        }


def label_for(composite: float) -> str:
    if composite >= 45:
        return "STRONG LONG"
    if composite >= 18:
        return "LONG"
    if composite <= -45:
        return "STRONG SHORT"
    if composite <= -18:
        return "SHORT"
    return "NEUTRAL"


# ---------------------------------------------------------------- context

def build_context(
    session: Session | None = None,
    symbol: str = "NQ=F",
    demo: bool | None = None,
    tf: str = "1m",
    rb: float | None = None,
    tick_path: str | None = None,
    need_ticks: bool = True,
) -> Context:
    """Fetch once, rank levels once, bucket the tape once."""
    sess = session or get_session(symbol=symbol, demo=demo)
    bias = compute_bias(sess)
    if rb or tf != "1m":
        sess = replace(sess, candles=get_chart_candles(tf, rb, symbol=sess.symbol, demo=demo))
    candles = sess.candles
    user = [asdict(l) for l in load_levels(sess.symbol)]
    scored = rank_levels(sess, user + compute_auto_levels(sess), bias)
    ctx = Context(
        symbol=sess.symbol,
        candles=candles,
        levels=filter_proven(scored) or scored,
        bias=bias,
        atr=bar_atr(candles) or 1.0,
        tick_sz=tick_size(sess.symbol, candles[-1].close if candles else None),
        bar_seconds=bar_seconds(candles),
    )
    if need_ticks and candles:
        ctx.ticks, ctx.tick_source = get_ticks(candles, sess.symbol, tick_path)
        ctx.tick_confidence = source_confidence(ctx.tick_source)
        ctx.foot = build_footprint(ctx.ticks, ctx.bar_seconds, ctx.tick_sz,
                                   origin=int(candles[0].ts))
    return ctx


# -------------------------------------------------------------------- run

def run(ctx: Context, engine: str = SIMPLE) -> SuiteResult:
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {engine!r} (use {' or '.join(ENGINES)})")
    readings = [m.run(ctx, engine) for m in MODULES]
    directional = [r for r in readings
                   if BY_KEY[r.key].directional and r.confidence > 0]
    num = sum(WEIGHTS[r.key] * r.confidence * r.score for r in directional)
    den = sum(WEIGHTS[r.key] * r.confidence for r in directional)
    composite = clamp(num / den, -100, 100) if den else 0.0
    base = mean([r.strength * r.confidence for r in directional]) if directional else 0.0

    speed = next((r for r in readings if r.key == "speed"), None)
    gauge = speed.strength if speed and speed.confidence else 50.0
    # A dead tape makes every other reading less actionable; a fast one
    # does not make them more true, so this only ever damps.
    speed_factor = clamp(scale(gauge, 5, 45, 0.55, 1.0), 0.55, 1.0)
    conviction = clamp(base * speed_factor, 0, 100)

    events = sorted(
        (e for r in readings for e in r.events),
        key=lambda e: (e.ts, e.weight))[-10:]
    notes = []
    if engine == TICK:
        notes.append({
            "file": "real tick file",
            "reconstructed": "tape RECONSTRUCTED from bars — directional, not "
                             "a record of what traded",
            "none": "no tape available",
        }.get(ctx.tick_source, ctx.tick_source))
    if not ctx.has_volume:
        notes.append("feed reported no volume; volume-based modules are damped")
    if speed and speed.state in ("DEAD", "SLOW"):
        notes.append(f"tape is {speed.state.lower()} — conviction damped "
                     f"{(1 - speed_factor) * 100:.0f}%")
    return SuiteResult(
        engine=engine, symbol=ctx.symbol, composite=composite,
        label=label_for(composite), conviction=conviction,
        tick_source=ctx.tick_source, readings=readings, events=events,
        note="; ".join(notes),
    )


def compare(a: SuiteResult, b: SuiteResult) -> dict:
    """Where the two engines disagree, worst first.

    `agreement` is 100 when every module lands on the same score and 0
    when they are maximally opposed."""
    rows = []
    for m in MODULES:
        ra, rb = a.reading(m.key), b.reading(m.key)
        if not ra or not rb:
            continue
        gap = rb.score - ra.score
        rows.append({
            "key": m.key, "name": m.name,
            "simple": round(ra.score, 1), "tick": round(rb.score, 1),
            "gap": round(gap, 1),
            "simple_state": ra.state, "tick_state": rb.state,
            "flip": (ra.score > 10 and rb.score < -10) or (ra.score < -10 and rb.score > 10),
        })
    scored = [r for r in rows if BY_KEY[r["key"]].directional]
    agreement = 100.0 - mean([min(abs(r["gap"]), 200) / 2.0 for r in scored]) if scored else 100.0
    worst = max(scored, key=lambda r: abs(r["gap"])) if scored else None
    verdict = "engines agree"
    if worst and abs(worst["gap"]) >= 40:
        direction = "more bullish" if worst["gap"] > 0 else "more bearish"
        verdict = (f"the tape is {direction} than the bars on {worst['name'].lower()} "
                   f"({worst['simple']:+.0f} vs {worst['tick']:+.0f})")
    return {
        "agreement": round(clamp(agreement, 0, 100), 1),
        "composite_gap": round(b.composite - a.composite, 1),
        "flips": [r["key"] for r in scored if r["flip"]],
        "verdict": verdict,
        "rows": rows,
    }


def run_both(ctx: Context) -> dict:
    simple, tick = run(ctx, SIMPLE), run(ctx, TICK)
    return {
        "symbol": ctx.symbol,
        "price": round(ctx.price, 2),
        "atr": round(ctx.atr, 2),
        "tick_source": ctx.tick_source,
        "bars": len(ctx.candles),
        "ticks": len(ctx.ticks),
        SIMPLE: simple.to_dict(),
        TICK: tick.to_dict(),
        "comparison": compare(simple, tick),
    }


def analyze(
    symbol: str = "NQ=F", demo: bool | None = None, engine: str = "both",
    tf: str = "1m", rb: float | None = None, tick_path: str | None = None,
) -> dict:
    """One call for callers that just want the JSON: fetch, run, return."""
    ctx = build_context(symbol=symbol, demo=demo, tf=tf, rb=rb,
                        tick_path=tick_path, need_ticks=engine != SIMPLE)
    if engine == "both":
        return run_both(ctx)
    result = run(ctx, engine)
    return {
        "symbol": ctx.symbol, "price": round(ctx.price, 2),
        "atr": round(ctx.atr, 2), "tick_source": ctx.tick_source,
        "bars": len(ctx.candles), "ticks": len(ctx.ticks),
        engine: result.to_dict(),
    }
