"""Chart levels: user-maintained support/resistance levels in levels.json.

Format:
  {
    "levels": [
      {"price": 23500, "label": "Weekly high", "kind": "resistance"},
      {"price": 23180, "label": "Value area low", "kind": "support"}
    ]
  }

`kind` is free-form ("support", "resistance", "pivot", ...) and only affects
dashboard coloring. Auto-levels (PDH/PDL/VWAP/opening range) are computed
live and merged in by the dashboard — don't duplicate them here.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "levels.json")


@dataclass
class Level:
    price: float
    label: str = ""
    kind: str = "pivot"


DEFAULT_SYMBOL = "NQ=F"


def _parse_items(items) -> list[Level]:
    out = []
    for item in items or []:
        try:
            out.append(
                Level(
                    price=float(item["price"]),
                    label=str(item.get("label", "")),
                    kind=str(item.get("kind", "pivot")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda l: l.price, reverse=True)


def load_levels(symbol: str = DEFAULT_SYMBOL, path: str = DEFAULT_PATH) -> list[Level]:
    """Levels for one symbol. File format is {"symbols": {sym: [...]}};
    a legacy flat {"levels": [...]} is treated as the default symbol's."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        raw = json.load(f)
    if "symbols" in raw:
        return _parse_items(raw["symbols"].get(symbol))
    if symbol == DEFAULT_SYMBOL:
        return _parse_items(raw.get("levels"))
    return []


def save_levels(
    levels: list[Level], symbol: str = DEFAULT_SYMBOL, path: str = DEFAULT_PATH
) -> None:
    """Replace one symbol's levels, preserving other symbols' entries."""
    data: dict = {"symbols": {}}
    if os.path.exists(path):
        try:
            with open(path) as f:
                raw = json.load(f)
            if "symbols" in raw:
                data["symbols"] = raw["symbols"]
            elif raw.get("levels"):
                data["symbols"][DEFAULT_SYMBOL] = raw["levels"]
        except (json.JSONDecodeError, OSError):
            pass
    data["symbols"][symbol] = [asdict(l) for l in levels]
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def compute_auto_levels(sess) -> list[dict]:
    """Computed session levels: PDH/PDL/mid, prev close, classic floor pivots,
    weekly range, overnight high/low, VWAP, opening range. `sess` is an
    nq.data.Session; VWAP and the opening range come from its analysis
    candles (RTH once the cash session is under way)."""
    from .bias import opening_range, session_vwap

    out: list[dict] = []

    def add(price: float | None, label: str) -> None:
        if price:
            out.append({"price": round(price, 2), "label": label, "kind": "auto"})

    add(sess.prev_high, "PDH")
    add(sess.prev_low, "PDL")
    add(sess.prev_close, "Prev close")
    if sess.prev_high and sess.prev_low:
        add((sess.prev_high + sess.prev_low) / 2, "PD mid")
        if sess.prev_close:
            # Classic floor-trader pivots off prior-day H/L/C.
            p = (sess.prev_high + sess.prev_low + sess.prev_close) / 3
            rng = sess.prev_high - sess.prev_low
            add(p, "Pivot P")
            add(2 * p - sess.prev_low, "R1")
            add(2 * p - sess.prev_high, "S1")
            add(p + rng, "R2")
            add(p - rng, "S2")
    add(getattr(sess, "week_high", None), "Week high")
    add(getattr(sess, "week_low", None), "Week low")
    # Overnight range: only once RTH is what's being analysed — before the
    # cash open the overnight *is* the session, and its extremes are just
    # today's high and low.
    if getattr(sess, "analysis_scope", "") == "rth":
        add(getattr(sess, "overnight_high", None), "ON high")
        add(getattr(sess, "overnight_low", None), "ON low")
    candles = getattr(sess, "analysis_candles", sess.candles)
    if candles:
        add(session_vwap(candles), "VWAP")
    orange = opening_range(candles) if candles else None
    if orange:
        add(orange[0], "OR high")
        add(orange[1], "OR low")
    return out
