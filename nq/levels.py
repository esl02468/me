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
