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
from nq.reversal import filter_proven, rank_levels


def _all_levels(sess) -> list[dict]:
    from dashboard import _auto_levels  # same auto-level definitions as the dashboard

    return [
        {"price": l.price, "label": l.label, "kind": l.kind} for l in load_levels()
    ] + _auto_levels(sess)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--all", action="store_true", dest="show_all",
                    help="include levels proven wrong (default: hide tested levels ≤51%% right)")
    ap.add_argument("--min-rate", type=float, default=0.51,
                    help="bounce-rate cutoff for the proven filter (default 0.51)")
    args = ap.parse_args()

    try:
        sess = get_session(demo=args.demo)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    bias = compute_bias(sess)
    scored = rank_levels(sess, _all_levels(sess), bias)
    hidden = 0
    if not args.show_all:
        proven = filter_proven(scored, args.min_rate)
        hidden = len(scored) - len(proven)
        scored = proven

    if args.as_json:
        print(json.dumps({
            "symbol": sess.symbol,
            "source": sess.source,
            "price": sess.last,
            "bias": bias.label,
            "filtered": not args.show_all,
            "hidden": hidden,
            "levels": [{**asdict(l), "bounce_rate": l.bounce_rate} for l in scored],
        }, indent=2))
        return 0

    print(f"\n  Reversal-likelihood ranking — {sess.symbol} @ {sess.last:,.2f}  "
          f"(bias: {bias.label}, source: {sess.source})\n")
    hdr = (f"  {'#':>2} {'level':<16}{'price':>11}{'dist':>9}{'touches':>8}"
           f"{'bounced':>8}{'broke':>6}{'right%':>8}{'score':>7}  rating")
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    if not scored:
        print("  (no level was right more than "
              f"{args.min_rate:.0%} of the time today — try --all)")
    for l in scored:
        who = "you" if l.user else "auto"
        dist = f"{l.distance:+.0f}" if l.distance is not None else "—"
        rate = f"{l.bounce_rate:.0%}" if l.bounce_rate is not None else "n/a"
        print(f"  {l.rank:>2} {l.label[:15]:<16}{l.price:>11,.2f}{dist:>9}"
              f"{l.touches:>8}{l.bounces:>8}{l.breaks:>6}{rate:>8}{l.score:>7.1f}  {l.rating} ({who})")
    if hidden:
        print(f"\n  {hidden} level(s) hidden: tested and ≤ {args.min_rate:.0%} right today "
              "(--all to show). Untested levels stay — they're where the next reversal can happen.")
    print("\n  right% = today's bounces/touches at the level. Score adds level-type prior,"
          "\n  confluence, bias alignment, and break penalty. Heuristic, not probability.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
