#!/usr/bin/env python3
"""Rank chart levels by reversal likelihood, using today's tape.

Usage:
  python3 rank_levels.py            # live data
  python3 rank_levels.py --demo     # synthetic session (offline)
  python3 rank_levels.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from nq.bias import compute_bias
from nq.data import get_session
from nq.levels import load_levels
from nq.reversal import rank_levels


def _all_levels(sess) -> list[dict]:
    from dashboard import _auto_levels  # same auto-level definitions as the dashboard

    return [
        {"price": l.price, "label": l.label, "kind": l.kind} for l in load_levels()
    ] + _auto_levels(sess)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    try:
        sess = get_session(demo=args.demo)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    bias = compute_bias(sess)
    scored = rank_levels(sess, _all_levels(sess), bias)

    if args.as_json:
        print(json.dumps({
            "symbol": sess.symbol,
            "source": sess.source,
            "price": sess.last,
            "bias": bias.label,
            "levels": [asdict(l) for l in scored],
        }, indent=2))
        return 0

    print(f"\n  Reversal-likelihood ranking — {sess.symbol} @ {sess.last:,.2f}  "
          f"(bias: {bias.label}, source: {sess.source})\n")
    hdr = f"  {'#':>2} {'level':<16}{'price':>11}{'dist':>9}{'touches':>8}{'bounced':>8}{'broke':>6}{'score':>7}  rating"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for l in scored:
        who = "you" if l.user else "auto"
        dist = f"{l.distance:+.0f}" if l.distance is not None else "—"
        print(f"  {l.rank:>2} {l.label[:15]:<16}{l.price:>11,.2f}{dist:>9}"
              f"{l.touches:>8}{l.bounces:>8}{l.breaks:>6}{l.score:>7.1f}  {l.rating} ({who})")
    print("\n  Score = today's bounce rate at the level + level-type prior + confluence"
          "\n  + bias alignment − break penalty. Heuristic confidence, not probability."
          "\n  Untested levels carry a neutral prior — they rank on structure alone.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
