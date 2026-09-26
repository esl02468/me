#!/usr/bin/env python3
"""Real-time NQ price fetcher + bias indicator (terminal).

Usage:
  python3 fetch_nq.py            # one snapshot
  python3 fetch_nq.py --watch    # refresh every 15s (Ctrl-C to stop)
  python3 fetch_nq.py --demo     # synthetic data (offline testing)
  python3 fetch_nq.py --json     # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from nq.bias import compute_bias
from nq.data import get_session
from nq.levels import load_levels
from nq.console import use_utf8_stdout

ARROWS = {1: "▲", 0: "·", -1: "▼"}


def snapshot(demo: bool, as_json: bool) -> None:
    sess = get_session(demo=demo)
    bias = compute_bias(sess)
    price = sess.last

    if as_json:
        print(
            json.dumps(
                {
                    "symbol": sess.symbol,
                    "source": sess.source,
                    "price": price,
                    "bias": bias.label,
                    "score": bias.score,
                    "components": bias.components,
                    "detail": bias.detail,
                    "ts": int(time.time()),
                },
                indent=2,
            )
        )
        return

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    chg = ""
    if sess.prev_close and price:
        d = price - sess.prev_close
        chg = f"  {d:+.2f} ({d / sess.prev_close * 100:+.2f}%)"
    print(f"\n  {sess.symbol}  {price:,.2f}{chg}   [{sess.source}] {now}")
    print(f"  BIAS: {bias.label}  (score {bias.score:+d}/5)")
    names = {"vwap": "VWAP", "ema": "EMA 9/21", "or": "Open range", "prev": "Prev close", "momentum": "Momentum"}
    for key, vote in bias.components.items():
        extra = ""
        if key == "vwap" and bias.detail.get("vwap"):
            extra = f"  ({bias.detail['vwap']:,.2f})"
        elif key == "ema" and bias.detail.get("ema9"):
            extra = f"  ({bias.detail['ema9']:,.2f} / {bias.detail['ema21']:,.2f})"
        elif key == "or" and bias.detail.get("or_high"):
            extra = f"  ({bias.detail['or_low']:,.2f} – {bias.detail['or_high']:,.2f})"
        elif key == "prev" and bias.detail.get("prev_close"):
            extra = f"  ({bias.detail['prev_close']:,.2f})"
        elif key == "momentum" and bias.detail.get("momentum_pts") is not None:
            extra = f"  ({bias.detail['momentum_pts']:+.2f} pts/10m)"
        print(f"    {ARROWS[vote]} {names[key]:<11}{extra}")

    levels = load_levels()
    if levels and price:
        above = [l for l in levels if l.price >= price][-2:]
        below = [l for l in levels if l.price < price][:2]
        if above or below:
            print("  LEVELS:")
            for l in above:
                print(f"    ─ {l.price:,.2f}  {l.label}  (+{l.price - price:.2f})")
            print(f"    ▸ {price:,.2f}  ← price")
            for l in below:
                print(f"    ─ {l.price:,.2f}  {l.label}  ({l.price - price:.2f})")
    print()


def main() -> int:
    use_utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--watch", action="store_true", help="refresh every N seconds")
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    try:
        while True:
            snapshot(args.demo, args.as_json)
            if not args.watch:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
