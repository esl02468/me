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


def load_levels(path: str = DEFAULT_PATH) -> list[Level]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        raw = json.load(f)
    out = []
    for item in raw.get("levels", []):
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


def save_levels(levels: list[Level], path: str = DEFAULT_PATH) -> None:
    with open(path, "w") as f:
        json.dump({"levels": [asdict(l) for l in levels]}, f, indent=2)
        f.write("\n")


def compute_auto_levels(sess) -> list[dict]:
    """Computed session levels: PDH/PDL/mid, prev close, classic floor pivots,
    weekly range, VWAP, opening range. `sess` is an nq.data.Session."""
    from .bias import OPENING_RANGE_BARS, vwap

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
    if sess.candles:
        add(vwap(sess.candles)[-1], "VWAP")
    if len(sess.candles) >= OPENING_RANGE_BARS:
        or_bars = sess.candles[:OPENING_RANGE_BARS]
        add(max(c.high for c in or_bars), "OR high")
        add(min(c.low for c in or_bars), "OR low")
    return out
