"""AlgoBox core: the shared types and the two-engine contract.

Every AlgoBox module answers the same question twice:

  simple(ctx)  reads OHLCV bars — the data every free feed gives you.
               Cheap, always available, approximate.
  tick(ctx)    reads the trade-by-trade stream — price, size, and which
               side was the aggressor. Sees what a bar hides: who was
               hitting, at which price, and whether they got filled.

Both return the same `Reading`, so the suite can run either engine, or run
both and show you where they disagree. Disagreement is information: when
the bars say "quiet drift up" and the tape says "sellers are being
absorbed at 23500", the tape is usually the one telling the truth.

Scores are directional, -100 (max bearish) .. +100 (max bullish).
`strength` is 0..100 conviction, independent of direction — a violent
two-sided fight scores near 0 directionally with high strength.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nq.bias import BiasResult
from nq.data import Candle
from nq.reversal import LevelScore

SIMPLE = "simple"
TICK = "tick"
ENGINES = (SIMPLE, TICK)

# Instrument tick sizes; anything unlisted is inferred in tick_size().
TICK_SIZE = {
    "NQ=F": 0.25, "MNQ=F": 0.25,
    "ES=F": 0.25, "MES=F": 0.25,
    "YM=F": 1.0, "MYM=F": 1.0,
    "RTY=F": 0.1, "M2K=F": 0.1,
    "GC=F": 0.1, "SI=F": 0.005, "CL=F": 0.01,
}


def tick_size(symbol: str, price: float | None = None) -> float:
    """Minimum price increment for `symbol` — the grid ticks land on."""
    if symbol in TICK_SIZE:
        return TICK_SIZE[symbol]
    if symbol.endswith("=F"):
        return 0.25
    if symbol.endswith("-USD"):  # crypto
        return 1.0 if (price or 0) >= 1000 else 0.01
    return 0.01  # equities / ETFs


def point_value(symbol: str) -> float:
    """Dollars per point, for sizing absorption/imbalance in money terms."""
    return {"NQ=F": 20.0, "MNQ=F": 2.0, "ES=F": 50.0, "MES=F": 5.0,
            "YM=F": 5.0, "MYM=F": 0.5, "RTY=F": 50.0, "M2K=F": 5.0,
            "GC=F": 100.0, "CL=F": 1000.0}.get(symbol, 1.0)


@dataclass
class Tick:
    """One print off the tape.

    `side` is the *aggressor*: "B" means the buyer lifted the offer (traded
    at the ask), "S" means the seller hit the bid, "?" means the feed did
    not say and no bid/ask was available to infer it.
    """

    ts: float
    price: float
    size: float
    side: str = "?"
    bid: float | None = None
    ask: float | None = None

    @property
    def signed(self) -> float:
        return self.size if self.side == "B" else -self.size if self.side == "S" else 0.0


@dataclass
class Event:
    """A discrete, timestamped thing a module noticed."""

    ts: int
    kind: str            # module key that raised it
    side: str            # "long" | "short" | "none"
    price: float
    text: str
    weight: float = 1.0  # 0..1 — how much this event should move a decision


@dataclass
class Reading:
    """One module's verdict under one engine."""

    key: str
    name: str
    engine: str
    state: str                 # short human label, e.g. "ABSORPTION"
    score: float               # -100..+100, directional
    strength: float            # 0..100, conviction regardless of direction
    detail: str                # one line of plain English
    value: float | None = None  # the module's headline number
    unit: str = ""
    confidence: float = 1.0    # 0..1 — damped when the inputs are weak
    series: list[float] = field(default_factory=list)  # optional sparkline
    events: list[Event] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name, "engine": self.engine,
            "state": self.state, "score": round(self.score, 1),
            "strength": round(self.strength, 1), "detail": self.detail,
            "value": round(self.value, 4) if self.value is not None else None,
            "unit": self.unit, "confidence": round(self.confidence, 2),
            "series": [round(v, 3) for v in self.series[-120:]],
            "events": [
                {"ts": e.ts, "kind": e.kind, "side": e.side,
                 "price": round(e.price, 2), "text": e.text,
                 "weight": round(e.weight, 2)}
                for e in self.events[-12:]
            ],
            "extra": self.extra,
        }


@dataclass
class Context:
    """Everything the modules are allowed to look at, built once per run."""

    symbol: str
    candles: list[Candle]
    ticks: list[Tick] = field(default_factory=list)
    tick_source: str = "none"   # "file" | "reconstructed" | "none"
    tick_confidence: float = 1.0  # damped when the tape is reconstructed
    levels: list[LevelScore] = field(default_factory=list)
    bias: BiasResult | None = None
    atr: float = 1.0
    tick_sz: float = 0.25
    bar_seconds: int = 60
    # Built once by the suite so eight modules don't each re-bucket the tape.
    foot: list = field(default_factory=list)  # list[ticks.FootBar]
    vap: dict = field(default_factory=dict)   # price -> [bid volume, ask volume]

    @property
    def price(self) -> float:
        return self.candles[-1].close if self.candles else 0.0

    @property
    def has_volume(self) -> bool:
        """Some futures feeds omit volume entirely; modules that need it
        must say so rather than quietly scoring noise."""
        return any((c.volume or 0) > 0 for c in self.candles)


# Below this, no engine speaks. A reconstructed tape has thousands of
# prints after five bars and several tick modules would happily score
# them, but a tick reading with no simple reading beside it is a
# comparison with one side missing — worse than an honest blank.
MIN_CONTEXT_BARS = 20


class Module:
    """Base class. Subclasses implement both engines and nothing else."""

    key = "module"
    name = "Module"
    blurb = ""
    directional = True  # False for gauges like speed, which have no side

    def simple(self, ctx: Context) -> Reading:  # pragma: no cover - abstract
        raise NotImplementedError

    def tick(self, ctx: Context) -> Reading:  # pragma: no cover - abstract
        raise NotImplementedError

    def run(self, ctx: Context, engine: str) -> Reading:
        if len(ctx.candles) < MIN_CONTEXT_BARS:
            return self.reading(ctx, engine, confidence=0.0,
                                detail=f"needs at least {MIN_CONTEXT_BARS} bars, "
                                       f"have {len(ctx.candles)}")
        return self.tick(ctx) if engine == TICK else self.simple(ctx)

    # -- helper for subclasses --------------------------------------------
    def reading(self, ctx: Context, engine: str, **kw) -> Reading:
        kw.setdefault("state", "—")
        kw.setdefault("score", 0.0)
        kw.setdefault("strength", 0.0)
        kw.setdefault("detail", "not enough data")
        return Reading(key=self.key, name=self.name, engine=engine, **kw)


# ---------------------------------------------------------------- helpers

def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def scale(v: float, lo: float, hi: float, out_lo: float = 0.0, out_hi: float = 100.0) -> float:
    """Map v from [lo, hi] onto [out_lo, out_hi], clamped at both ends."""
    if hi == lo:
        return out_lo
    return out_lo + (out_hi - out_lo) * clamp((v - lo) / (hi - lo), 0.0, 1.0)


def mean(vals) -> float:
    vals = list(vals)
    return sum(vals) / len(vals) if vals else 0.0


def stdev(vals) -> float:
    vals = list(vals)
    if len(vals) < 2:
        return 0.0
    m = mean(vals)
    return (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


def zscore(v: float, vals) -> float:
    s = stdev(vals)
    return 0.0 if s == 0 else (v - mean(vals)) / s


def quantize(price: float, tick_sz: float) -> float:
    return round(round(price / tick_sz) * tick_sz, 10)


def path_volume_split(c: Candle, step: float) -> dict[float, list[float]]:
    """Bar -> {price bucket: [sell volume, buy volume]}, no tick data needed.

    The bar's path is taken as open -> nearer extreme -> farther extreme ->
    close (the same convention nq.data.build_range_bars uses). Volume is
    apportioned along that path by distance travelled, and each leg is
    attributed to the side that drove it: prices traversed upward were
    bought at the offer, prices traversed downward were sold into the bid.

    This is the *simple* engine's stand-in for a footprint. It is a
    deterministic reading of what the bar implies, not a record of what
    traded — a bar cannot know the order its trades arrived in. Modules
    built on it report reduced confidence for exactly that reason.
    """
    pts = ([c.open, c.low, c.high, c.close] if c.close >= c.open
           else [c.open, c.high, c.low, c.close])
    legs = [(pts[i], pts[i + 1]) for i in range(3)]
    travel = sum(abs(b - a) for a, b in legs)
    vol = c.volume or 1.0
    out: dict[float, list[float]] = {}
    if travel <= 0:
        out[quantize(c.close, step)] = [vol / 2.0, vol / 2.0]
        return out
    for a, b in legs:
        if a == b:
            continue
        up = b > a
        lo, hi = (a, b) if up else (b, a)
        buckets = int((hi - lo) / step) + 1
        per = vol * (hi - lo) / travel / buckets
        for k in range(buckets):
            slot = out.setdefault(quantize(lo + k * step, step), [0.0, 0.0])
            slot[1 if up else 0] += per
    return out


def profile_step(candles: list[Candle], tick_sz: float, buckets: int = 240) -> float:
    """Bucket width for a volume profile: the tick grid unless the session's
    range is so wide that it would produce thousands of near-empty rows."""
    if not candles:
        return tick_sz
    rng = max(c.high for c in candles) - min(c.low for c in candles)
    return max(tick_sz, round(rng / buckets / tick_sz) * tick_sz or tick_sz)
