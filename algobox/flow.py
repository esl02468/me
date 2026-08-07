"""Order-flow modules: delta, pressure, imbalance, absorption.

These four ask the same question at four resolutions: *who is being
aggressive, and are they getting what they paid for?*

  delta       net aggression over time, and whether it still agrees with price
  pressure    the current buy/sell mix — a right-now reading, not a trend
  imbalance   where on the price ladder one side ran the other over
  absorption  where one side spent size and got nothing for it

Each runs on bars (simple) or on the tape (tick). The bar versions are
proxies — see core.path_volume_split — and report it in `confidence`.
"""

from __future__ import annotations

from .core import (
    SIMPLE, TICK, Context, Event, Module, Reading,
    clamp, mean, path_volume_split, quantize, scale, zscore,
)

MIN_BARS = 25


def bar_delta(c) -> float:
    """A bar's delta proxy: volume signed by where it closed in its range.

    A bar closing on its high is read as buyer-dominated. It is the best a
    single OHLCV bar can do, and it is wrong exactly when it matters most
    — a bar can close on its high *because* a passive seller stopped
    defending, not because buyers were aggressive. That is why the tick
    engine exists.
    """
    rng = c.high - c.low
    v = c.volume or 1.0
    if rng <= 0:
        return v * (1.0 if c.close > c.open else -1.0 if c.close < c.open else 0.0)
    return v * (2.0 * (c.close - c.low) / rng - 1.0)


def _cumulative(vals: list[float]) -> list[float]:
    out, run = [], 0.0
    for v in vals:
        run += v
        out.append(run)
    return out


def _divergence(prices: list[float], cum: list[float], look: int = 40):
    """(side, magnitude 0..1) when price and cumulative delta disagree.

    Compares the recent half-window's extreme against the prior half's:
    a higher price high on a lower delta high means the push was not paid
    for. Returns None when they agree.
    """
    look = min(look, len(prices))
    if look < 12:
        return None
    half = look // 2
    p_old, p_new = prices[-look:-half], prices[-half:]
    c_old, c_new = cum[-look:-half], cum[-half:]
    span = (max(cum[-look:]) - min(cum[-look:])) or 1.0
    if max(p_new) > max(p_old) and max(c_new) < max(c_old):
        return "short", clamp((max(c_old) - max(c_new)) / span, 0.0, 1.0)
    if min(p_new) < min(p_old) and min(c_new) > min(c_old):
        return "long", clamp((min(c_new) - min(c_old)) / span, 0.0, 1.0)
    return None


class Delta(Module):
    key = "delta"
    name = "Cumulative delta"
    blurb = "Net aggressive buying minus selling, and whether it still backs the price."

    def _score(self, ctx: Context, engine: str, deltas: list[float],
               closes: list[float], conf: float, detail_bits: list[str]) -> Reading:
        cum = _cumulative(deltas)
        window = deltas[-15:]
        effort = sum(abs(d) for d in window) or 1.0
        ratio = sum(window) / effort           # -1..1: one-sidedness of recent flow
        score = ratio * 70.0
        strength = abs(ratio) * 70.0
        div = _divergence(closes, cum, look=40)
        events: list[Event] = []
        if div:
            side, mag = div
            sgn = 1.0 if side == "long" else -1.0
            score = clamp(score * 0.45 + sgn * 55.0 * mag, -100, 100)
            strength = clamp(strength + 40.0 * mag, 0, 100)
            what = ("price made a new high on weaker delta" if side == "short"
                    else "price made a new low on stronger delta")
            detail_bits.append(what)
            events.append(Event(ts=int(ctx.candles[-1].ts), kind=self.key, side=side,
                                price=ctx.price, text=f"delta divergence — {what}",
                                weight=round(0.4 + 0.6 * mag, 2)))
        # State reflects the score after divergence has had its say — a
        # panel that reads "SELLERS" next to a positive number is a bug.
        state = "BUYERS" if score > 15 else "SELLERS" if score < -15 else "BALANCED"
        if div:
            state += " · DIVERGENT"
        detail_bits.insert(0, f"{ratio:+.0%} of recent effort was one-sided")
        return self.reading(
            ctx, engine, state=state, score=score, strength=strength,
            detail="; ".join(detail_bits), value=cum[-1], unit="contracts",
            confidence=conf, series=cum[-120:], events=events,
            extra={"session_delta": round(cum[-1], 1),
                   "last_bar_delta": round(deltas[-1], 1)},
        )

    def simple(self, ctx: Context) -> Reading:
        if len(ctx.candles) < MIN_BARS:
            return self.reading(ctx, SIMPLE, confidence=0.0)
        deltas = [bar_delta(c) for c in ctx.candles]
        closes = [c.close for c in ctx.candles]
        return self._score(ctx, SIMPLE, deltas, closes,
                           0.55 if ctx.has_volume else 0.3,
                           ["bar-close proxy, not true aggressor volume"])

    def tick(self, ctx: Context) -> Reading:
        if not ctx.foot or len(ctx.foot) < 8:
            return self.reading(ctx, TICK, confidence=0.0, detail="no tape")
        deltas = [b.delta for b in ctx.foot]
        closes = [b.close for b in ctx.foot]
        return self._score(ctx, TICK, deltas, closes, ctx.tick_confidence,
                           ["true aggressor volume, trade by trade"])


class Pressure(Module):
    key = "pressure"
    name = "Aggression"
    blurb = "Right now, which side is paying the spread to get filled."

    WINDOW_BARS = 20

    def _emit(self, ctx: Context, engine: str, ratio: float, near: float,
              conf: float, detail: str, extra: dict) -> Reading:
        score = clamp((ratio - 0.5) * 200.0, -100, 100)
        shift = (near - ratio) * 200.0
        state = ("BUYERS LIFTING" if score > 30 else "MILD BID" if score > 10
                 else "SELLERS HITTING" if score < -30 else "MILD OFFER" if score < -10
                 else "TWO-SIDED")
        if abs(shift) > 25:  # kept to one glyph so the panel column holds
            state += " ▲" if shift > 0 else " ▼"
        return self.reading(
            ctx, engine, state=state, score=score,
            strength=clamp(abs(score) * 0.8 + abs(shift) * 0.3, 0, 100),
            detail=detail, value=round(ratio * 100, 1), unit="% bought",
            confidence=conf, extra={**extra, "shift": round(shift, 1)},
        )

    def simple(self, ctx: Context) -> Reading:
        cs = ctx.candles[-self.WINDOW_BARS:]
        if len(cs) < 8:
            return self.reading(ctx, SIMPLE, confidence=0.0)

        def buy_frac(bars):
            tot = sum(b.volume or 1.0 for b in bars) or 1.0
            bought = sum((b.volume or 1.0) * (
                (b.close - b.low) / (b.high - b.low) if b.high > b.low
                else (1.0 if b.close >= b.open else 0.0)) for b in bars)
            return bought / tot

        ratio, near = buy_frac(cs), buy_frac(cs[-5:])
        return self._emit(ctx, SIMPLE, ratio, near, 0.6 if ctx.has_volume else 0.35,
                          f"{ratio:.0%} of the last {len(cs)} bars' volume closed "
                          "in the upper half of its bar (proxy for lifting the offer)",
                          {"bars": len(cs)})

    def tick(self, ctx: Context) -> Reading:
        ticks = ctx.ticks
        if len(ticks) < 200:
            return self.reading(ctx, TICK, confidence=0.0, detail="no tape")
        span = ctx.bar_seconds * self.WINDOW_BARS
        cutoff = ticks[-1].ts - span
        win = [t for t in ticks if t.ts >= cutoff] or ticks[-500:]
        near = [t for t in win if t.ts >= ticks[-1].ts - span / 4] or win[-200:]

        def frac(ts):
            b = sum(t.size for t in ts if t.side == "B")
            s = sum(t.size for t in ts if t.side == "S")
            return b / (b + s) if (b + s) else 0.5

        ratio, near_ratio = frac(win), frac(near)
        buy_n = sum(1 for t in win if t.side == "B")
        sell_n = len(win) - buy_n
        buy_sz = mean([t.size for t in win if t.side == "B"]) or 0.0
        sell_sz = mean([t.size for t in win if t.side == "S"]) or 0.0
        who = ("buyers are the bigger prints" if buy_sz > sell_sz * 1.15
               else "sellers are the bigger prints" if sell_sz > buy_sz * 1.15
               else "print sizes are even")
        return self._emit(
            ctx, TICK, ratio, near_ratio, ctx.tick_confidence,
            f"{ratio:.0%} of {len(win):,} prints lifted the offer; {who} "
            f"({buy_sz:.1f} vs {sell_sz:.1f} avg)",
            {"prints": len(win), "buy_prints": buy_n, "sell_prints": sell_n,
             "avg_buy_size": round(buy_sz, 2), "avg_sell_size": round(sell_sz, 2)},
        )


# ------------------------------------------------------------- imbalance

IMBALANCE_RATIO = 3.0   # the classic 3:1 diagonal test
ONEWAY_FRACTION = 0.72  # bar-derived test: share of a price's volume, one way
STACK_MIN = 3           # consecutive imbalanced prices to call it a zone


def _stack(flags: list[tuple[float, str]], step: float, bid_vol: dict, ask_vol: dict):
    """Collapse per-price flags into runs of >= STACK_MIN adjacent prices."""
    zones, run = [], []
    for entry in flags + [(None, None)]:
        if run and (entry[0] is None or entry[1] != run[-1][1]
                    or abs(entry[0] - run[-1][0] - step) > step * 0.5):
            if len(run) >= STACK_MIN:
                vol = sum((ask_vol if run[0][1] == "buy" else bid_vol).get(p, 0.0)
                          for p, _ in run)
                zones.append((run[0][1], run[0][0], run[-1][0], vol))
            run = []
        if entry[0] is not None:
            run.append(entry)
    return zones


def _oneway_zones(bid_vol: dict, ask_vol: dict, step: float,
                  fraction: float, floor: float):
    """Prices that were delivered through in one direction only.

    This is the test the *simple* engine can honestly run. A bar cannot
    tell you who was aggressive at a price, but summing many bars' implied
    paths does tell you whether a price band was only ever crossed going
    up (or only going down). Bands like that never traded two-way — they
    are the bar-data cousin of a stacked imbalance, and price tends to
    react the first time it comes back to them.

    A diagonal 3:1 test is deliberately *not* used here: across an
    aggregated ladder the diagonal pairs prices that never traded with
    each other, so it produces noise dressed up as order flow.
    """
    flags: list[tuple[float, str]] = []
    for p in sorted(set(bid_vol) | set(ask_vol)):
        sell, buy = bid_vol.get(p, 0.0), ask_vol.get(p, 0.0)
        total = sell + buy
        if total < floor:
            continue
        if buy / total >= fraction:
            flags.append((p, "buy"))
        elif sell / total >= fraction:
            flags.append((p, "sell"))
    return _stack(flags, step, bid_vol, ask_vol)


def _diagonal_zones(bid_vol: dict, ask_vol: dict, step: float,
                    ratio: float, min_vol: float):
    """Stacked diagonal imbalances -> [(side, low, high, volume)].

    A footprint compares buyers at a price against sellers one tick below
    (and vice versa) because that is the pair that actually traded with
    each other. `STACK_MIN` consecutive imbalanced prices is the standard
    threshold for "someone with size was here" and those price bands tend
    to be defended when revisited.

    This test is only valid on a real (or reconstructed) per-bar ladder,
    where the diagonal genuinely pairs counterparties — the simple engine
    uses _oneway_zones instead.
    """
    flags: list[tuple[float, str]] = []
    for p in sorted(set(bid_vol) | set(ask_vol)):
        below = quantize(p - step, step)
        above = quantize(p + step, step)
        a, b = ask_vol.get(p, 0.0), bid_vol.get(p, 0.0)
        if a >= min_vol and a >= ratio * max(bid_vol.get(below, 0.0), 1e-9):
            flags.append((p, "buy"))
        elif b >= min_vol and b >= ratio * max(ask_vol.get(above, 0.0), 1e-9):
            flags.append((p, "sell"))
    return _stack(flags, step, bid_vol, ask_vol)


class Imbalance(Module):
    key = "imbalance"
    name = "Stacked imbalance"
    blurb = "Prices where one side ran over the other by 3:1 or more."

    def _emit(self, ctx: Context, engine: str, zones, conf: float, note: str) -> Reading:
        # The zones that matter are the ones price can reach, not the ones
        # that happen to sit lowest on the ladder.
        recent = sorted(zones, key=lambda z: abs((z[1] + z[2]) / 2 - ctx.price))[:6]
        buys = [z for z in recent if z[0] == "buy"]
        sells = [z for z in recent if z[0] == "sell"]
        bv, sv = sum(z[3] for z in buys), sum(z[3] for z in sells)
        net = (bv - sv) / ((bv + sv) or 1.0)
        # One lone stack is a hint; five stacked the same way is a wall.
        # Without this, `net` saturates at ±1 whenever the nearby zones
        # happen to share a side and every reading pins to ±85.
        score = clamp(net * scale(len(recent), 1, 5, 35, 85), -100, 100)
        state = ("BUY STACKS" if score > 20 else "SELL STACKS" if score < -20
                 else "BOTH SIDES" if recent else "NONE")
        events = [
            Event(ts=int(ctx.candles[-1].ts), kind=self.key,
                  side="long" if z[0] == "buy" else "short",
                  price=(z[1] + z[2]) / 2,
                  text=f"{z[0]} imbalance stacked {z[1]:.2f}–{z[2]:.2f} "
                       f"({z[3]:,.0f} contracts)",
                  weight=clamp(z[3] / ((bv + sv) or 1.0), 0.2, 1.0))
            for z in recent[-4:]
        ]
        detail = (f"{len(buys)} buy / {len(sells)} sell stacks near price "
                  f"(of {len(zones)} in the window); {note}" if recent
                  else f"no {STACK_MIN}-deep stacks in the window; {note}")
        return self.reading(
            ctx, engine, state=state, score=score,
            strength=clamp(len(recent) * 15.0 + abs(net) * 40.0, 0, 100),
            detail=detail, value=round(net * 100, 1), unit="% net stacked",
            confidence=conf, events=events,
            extra={"zones": [{"side": z[0], "low": round(z[1], 2),
                              "high": round(z[2], 2), "volume": round(z[3], 1)}
                             for z in recent]},
        )

    def simple(self, ctx: Context) -> Reading:
        cs = ctx.candles[-30:]
        if len(cs) < 8:
            return self.reading(ctx, SIMPLE, confidence=0.0)
        step = ctx.tick_sz
        # One bar's implied ladder is too smooth to be informative — every
        # price it crossed gets roughly equal volume. Stacking 30 bars into
        # a single ladder makes the genuinely one-way prices stand out:
        # bands that were only ever delivered through in one direction.
        bid_vol: dict[float, float] = {}
        ask_vol: dict[float, float] = {}
        for c in cs:
            for p, (sell, buy) in path_volume_split(c, step).items():
                bid_vol[p] = bid_vol.get(p, 0.0) + sell
                ask_vol[p] = ask_vol.get(p, 0.0) + buy
        totals = [bid_vol.get(p, 0.0) + ask_vol.get(p, 0.0)
                  for p in set(bid_vol) | set(ask_vol)]
        zones = _oneway_zones(bid_vol, ask_vol, step, ONEWAY_FRACTION,
                              mean(totals) * 0.4)
        return self._emit(
            ctx, SIMPLE, zones, 0.4,
            f"price bands crossed one way only across {len(cs)} bars — "
            "the bar-data cousin of a stacked imbalance, not the ladder itself")

    def tick(self, ctx: Context) -> Reading:
        if not ctx.foot:
            return self.reading(ctx, TICK, confidence=0.0, detail="no tape")
        zones = []
        for b in ctx.foot[-30:]:
            if not b.prices:
                continue
            floor = max(1.0, mean([b.total_at(p) for p in b.prices]) * 1.2)
            zones += _diagonal_zones(b.bid_vol, b.ask_vol, ctx.tick_sz,
                                     IMBALANCE_RATIO, floor)
        return self._emit(ctx, TICK, zones, ctx.tick_confidence,
                          "measured off the price ladder")


# ------------------------------------------------------------ absorption

HEAVY = 1.5  # threshold on `_heat`


def _heat(volume: float, baseline) -> tuple[float, float]:
    """(heat, multiple) — how heavy this bar is against recent volume.

    A z-score alone is not enough: when the recent bars all carried the
    same volume the standard deviation is zero, the z-score collapses to
    0, and a bar ten times the size of its neighbours reads as ordinary.
    Falling back on the plain multiple keeps the detector honest on flat
    or synthetic baselines, and the multiple is the more intuitive number
    to show a trader anyway.
    """
    base = list(baseline)
    avg = mean(base) or 1.0
    multiple = volume / avg
    return max(zscore(volume, base), (multiple - 1.0) * 1.5), multiple


class Absorption(Module):
    key = "absorption"
    name = "Absorption"
    blurb = "Size spent, no ground gained — the passive side is winning."

    def _emit(self, ctx: Context, engine: str, events: list[Event],
              eff_res: float, conf: float, detail: str, extra: dict) -> Reading:
        recent = events[-4:]
        net = sum((1 if e.side == "long" else -1) * e.weight for e in recent)
        score = clamp(net * 45.0, -100, 100)
        state = ("BUYERS ABSORBED" if score < -20 else "SELLERS ABSORBED" if score > 20
                 else "MIXED" if recent else "NONE")
        return self.reading(
            ctx, engine, state=state, score=score,
            strength=clamp(len(recent) * 20.0 + abs(net) * 25.0, 0, 100),
            detail=detail, value=round(eff_res, 1), unit="contracts/point",
            confidence=conf, events=recent, extra=extra,
        )

    def simple(self, ctx: Context) -> Reading:
        cs = ctx.candles
        if len(cs) < MIN_BARS:
            return self.reading(ctx, SIMPLE, confidence=0.0)
        vols = [c.volume or 1.0 for c in cs]
        events: list[Event] = []
        for i in range(20, len(cs)):
            c = cs[i]
            heat, multiple = _heat(vols[i], vols[i - 20:i])
            rng = c.high - c.low
            if heat < HEAVY or rng > 0.75 * ctx.atr:
                continue  # not heavy, or it actually went somewhere
            d = bar_delta(c)
            if abs(d) < vols[i] * 0.15:
                continue  # two-sided churn, not one side being denied
            side = "short" if d > 0 else "long"  # the aggressor got nothing
            who = "buyers" if d > 0 else "sellers"
            events.append(Event(
                ts=int(c.ts), kind=self.key, side=side, price=c.close,
                text=f"{who} spent {vols[i]:,.0f} contracts ({multiple:.1f}x "
                     f"recent) in a {rng:.2f}-point bar and were absorbed",
                weight=clamp(scale(heat, HEAVY, 4.0, 0.35, 1.0), 0.3, 1.0)))
        moved = abs(cs[-1].close - cs[-20].close) or ctx.tick_sz
        eff_res = sum(vols[-20:]) / moved
        return self._emit(
            ctx, SIMPLE, events, eff_res, 0.45 if ctx.has_volume else 0.2,
            (events[-1].text if events else
             "no heavy-volume/no-progress bars in the session"),
            {"detected": len(events)})

    def tick(self, ctx: Context) -> Reading:
        if not ctx.foot or len(ctx.foot) < 8:
            return self.reading(ctx, TICK, confidence=0.0, detail="no tape")
        bars = ctx.foot
        vols = [b.volume for b in bars]
        events: list[Event] = []
        for i in range(10, len(bars)):
            b = bars[i]
            if not b.prices:
                continue
            # Same effort/no-result gate as the simple engine, deliberately:
            # the comparison between the two is only meaningful if they are
            # looking at the same bars. What the ladder adds is *where* the
            # size went and *who* paid for it.
            rng = b.high - b.low
            heat, multiple = _heat(b.volume, vols[max(0, i - 20):i])
            if heat < HEAVY or rng > 0.75 * ctx.atr:
                continue
            per_price = mean([b.total_at(p) for p in b.prices]) or 1.0
            hot = max(b.prices, key=b.total_at)
            if b.total_at(hot) < 2.2 * per_price:
                continue  # size was spread out, not parked at one price
            ask_at, bid_at = b.ask_vol.get(hot, 0.0), b.bid_vol.get(hot, 0.0)
            lopsided = max(ask_at, bid_at) / max(1e-9, ask_at + bid_at)
            if lopsided < 0.58:
                continue  # both sides traded there — a fight, not absorption
            aggressor_up = ask_at > bid_at
            # Absorbed = the aggressor never got price away from that price.
            beyond = (b.high - hot) if aggressor_up else (hot - b.low)
            if beyond > max(2.5 * ctx.tick_sz, 0.4 * rng):
                continue
            side = "short" if aggressor_up else "long"
            who = "buyers" if aggressor_up else "sellers"
            events.append(Event(
                ts=b.ts, kind=self.key, side=side, price=hot,
                text=f"{who} paid for {max(ask_at, bid_at):,.0f} contracts at "
                     f"{hot:.2f} ({lopsided:.0%} one-sided, bar ran "
                     f"{multiple:.1f}x recent) and moved it "
                     f"{beyond / ctx.tick_sz:.0f} ticks",
                weight=clamp(scale(heat, HEAVY, 4.0, 0.4, 1.0) * lopsided, 0.3, 1.0)))
        recent = bars[-20:]
        moved = abs(recent[-1].close - recent[0].open) or ctx.tick_sz
        eff_res = sum(b.volume for b in recent) / moved
        return self._emit(
            ctx, TICK, events, eff_res, ctx.tick_confidence,
            (events[-1].text if events else
             f"no price took absorbing size; {eff_res:,.0f} contracts per point moved"),
            {"detected": len(events),
             "effort_result": round(eff_res, 1)})


MODULES = [Delta(), Pressure(), Imbalance(), Absorption()]
