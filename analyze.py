#!/usr/bin/env python3
"""Strategy Analyzer: backtest today's 1m NQ session.

Usage:
  python3 analyze.py                      # all strategies on today's data
  python3 analyze.py --strategy orb       # one strategy
  python3 analyze.py --demo               # synthetic session (offline)
  python3 analyze.py --trades             # include per-trade log
  python3 analyze.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from nq.backtest import run_all
from nq.data import get_session


def fmt_ts(ts: int | None) -> str:
    if ts is None:
        return "--:--"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strategy", help="only strategies whose name starts with this")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    ap.add_argument("--trades", action="store_true", help="print per-trade log")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    try:
        sess = get_session(demo=args.demo)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if len(sess.candles) < 30:
        print(
            f"error: only {len(sess.candles)} candles available from source "
            f"'{sess.source}' — not enough to backtest.",
            file=sys.stderr,
        )
        return 1

    reports = run_all(sess)
    if args.strategy:
        reports = [r for r in reports if r.strategy.startswith(args.strategy)]
        if not reports:
            print(f"error: no strategy named like {args.strategy!r}", file=sys.stderr)
            return 1

    if args.as_json:
        print(
            json.dumps(
                {
                    "symbol": sess.symbol,
                    "source": sess.source,
                    "candles": len(sess.candles),
                    "reports": [
                        {**r.summary(), "trade_log": [
                            {
                                "side": t.side,
                                "entry_time": fmt_ts(t.entry_ts),
                                "entry": t.entry,
                                "exit_time": fmt_ts(t.exit_ts),
                                "exit": t.exit,
                                "points": round(t.points, 2),
                                "reason": t.reason,
                            }
                            for t in r.trades
                        ]}
                        for r in reports
                    ],
                },
                indent=2,
            )
        )
        return 0

    print(
        f"\n  Strategy Analyzer — {sess.symbol}  "
        f"({len(sess.candles)} x 1m candles, source: {sess.source})\n"
    )
    header = f"  {'strategy':<28}{'trades':>7}{'win %':>8}{'points':>9}{'NQ $':>10}{'MNQ $':>9}{'PF':>7}{'maxDD':>8}"
    print(header)
    print("  " + "─" * (len(header) - 2))
    for r in reports:
        s = r.summary()
        print(
            f"  {s['strategy']:<28}{s['trades']:>7}{s['win_rate']:>8}{s['total_points']:>9}"
            f"{s['pnl_nq_usd']:>10,.0f}{s['pnl_mnq_usd']:>9,.0f}{s['profit_factor']:>7}"
            f"{s['max_drawdown_points']:>8}"
        )
        if s["open_position"]:
            print(f"  {'':<28}  (still holding a {s['open_position']} at session end)")

    if args.trades:
        for r in reports:
            if not r.trades:
                continue
            print(f"\n  {r.strategy} — trades:")
            for t in r.trades:
                pts = f"{t.points:+.2f}" if t.exit is not None else "open"
                print(
                    f"    {t.side:<6} {fmt_ts(t.entry_ts)} @ {t.entry:,.2f}"
                    f"  →  {fmt_ts(t.exit_ts)} @ {t.exit or 0:,.2f}   {pts} pts  [{t.reason}]"
                )
    print("\n  Fills are next-bar-open, 1 contract, no commissions/slippage. Analysis only.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
