#!/usr/bin/env python3
"""AlgoBox in the terminal.

  python3 -m algobox                      # both engines, side by side
  python3 -m algobox --demo               # synthetic session, no network
  python3 -m algobox --engine tick        # tape only
  python3 -m algobox --ticks trades.csv   # run the tick engines on real ticks
  python3 -m algobox --tf 5m --json       # machine-readable

With no --ticks file (and no ALGOBOX_TICK_FILE), the tick engines run on a
deterministic reconstruction of the bars — see algobox/ticks.py. The panel
always prints which tape it used.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from .core import SIMPLE, TICK, clamp
from .suite import BY_KEY, MODULES, analyze, build_context, compare, run

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
GREEN, RED, YELLOW, CYAN, GREY = ("\033[32m", "\033[31m", "\033[33m",
                                  "\033[36m", "\033[90m")
_color = True


def c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if _color else text


def tone(score: float) -> str:
    return GREEN if score >= 18 else RED if score <= -18 else YELLOW


CELL_W = 40   # state(21) + number(5) + space + gauge(11) + gutter(2)
GAUGE_W = 11


def bar(score: float) -> str:
    """A centred -100..+100 gauge, zero at the bar in the middle."""
    half = GAUGE_W // 2
    filled = int(round(abs(score) / 100 * half))
    if score >= 0:
        return " " * half + "│" + "█" * filled + "·" * (half - filled)
    return "·" * (half - filled) + "█" * filled + "│" + " " * half


def meter(value: float) -> str:
    """A left-to-right 0..100 gauge, for readings that have no side."""
    filled = int(round(clamp(value, 0, 100) / 100 * GAUGE_W))
    return "█" * filled + "·" * (GAUGE_W - filled)


def cell(reading) -> str:
    if not reading or reading.confidence <= 0:
        return c(f"{'—':<21}", GREY) + " " * (CELL_W - 21)
    directional = BY_KEY[reading.key].directional
    if directional:
        body = f"{reading.state[:20]:<21}{reading.score:+5.0f} {bar(reading.score)}"
        colour = tone(reading.score)
    else:
        body = f"{reading.state[:20]:<21}{reading.strength:5.0f} {meter(reading.strength)}"
        colour = CYAN
    return c(f"{body:<{CELL_W - 2}}", colour) + "  "


def hhmm(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def print_panel(ctx, results: dict, events_n: int) -> None:
    price = ctx.price
    src = ctx.tick_source
    src_note = {"file": c("real tick file", GREEN),
                "reconstructed": c("reconstructed from bars", YELLOW),
                "none": c("no tape", GREY)}.get(src, src)
    print()
    print(f"{c('ALGOBOX', BOLD)}  {c(ctx.symbol, CYAN)}  {price:,.2f}"
          f"   ATR {ctx.atr:.2f}   {len(ctx.candles)} bars"
          + (f" · {len(ctx.ticks):,} prints ({src_note})" if ctx.ticks else ""))
    print()

    engines = [e for e in (SIMPLE, TICK) if e in results]
    rule = "─" * (22 + CELL_W * len(engines))
    print(f"  {'':<22}" + "".join(
        c(f"{('SIMPLE (bars)' if e == SIMPLE else 'TICK (tape)'):<{CELL_W}}", BOLD)
        for e in engines))
    print("  " + c(rule, GREY))

    for m in MODULES:
        print(f"  {m.name:<22}" + "".join(cell(results[e].reading(m.key))
                                          for e in engines))

    print("  " + c(rule, GREY))
    # Pad before colouring: ANSI codes would otherwise count toward the width.
    print(f"  {'COMPOSITE':<22}" + "".join(
        c(f"{results[e].label:<21}{results[e].composite:+5.0f} "
          f"{bar(results[e].composite)}", tone(results[e].composite) + BOLD) + "  "
        for e in engines))
    print(f"  {'conviction':<22}" + "".join(
        c(f"{'':<21}{results[e].conviction:5.0f} /100{'':<7}", GREY)
        for e in engines))

    for e in engines:
        if results[e].note:
            print(f"\n  {c('note', GREY)} [{e}] {results[e].note}")

    if len(engines) == 2:
        cmp_ = compare(results[SIMPLE], results[TICK])
        print(f"\n  {c('Agreement', BOLD)} {cmp_['agreement']:.0f}%"
              f"   composite gap {cmp_['composite_gap']:+.0f}"
              f"   — {cmp_['verdict']}")
        if cmp_["flips"]:
            print(f"  {c('SIDE FLIPS', RED + BOLD)} on: {', '.join(cmp_['flips'])}"
                  " — the two engines point opposite ways here")

    # Both engines flag the same things; collapse near-identical prices so
    # the list reads as findings rather than as two overlapping logs.
    merged: dict[tuple, tuple] = {}
    for r in results.values():
        for e in r.events:
            key = (e.kind, e.side, round(e.price))
            if key not in merged or e.weight > merged[key][5]:
                merged[key] = (e.ts, e.kind, e.side, e.price, e.text, e.weight)
    events = sorted(merged.values())[-events_n:]
    if events:
        print(f"\n  {c('Events', BOLD)}")
        for ts, kind, side, price, text, _w in events:
            mark = GREEN if side == "long" else RED if side == "short" else GREY
            print(f"   {c(hhmm(ts), GREY)}  {c(f'{kind:<11}', CYAN)}"
                  f"{c(f'{side:<6}', mark)}{price:>10,.2f}  {text}")
    print()
    print(c("  Analysis, not advice. Reconstructed tape models aggressor side; "
            "it does not observe it.", GREY))
    print()


def main(argv=None) -> int:
    global _color
    ap = argparse.ArgumentParser(prog="algobox", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="NQ=F")
    ap.add_argument("--engine", choices=[SIMPLE, TICK, "both"], default="both")
    ap.add_argument("--tf", default="1m", help="1m / 5m / 15m / 1h / 1d")
    ap.add_argument("--rb", type=float, default=None, help="range bars, in points")
    ap.add_argument("--ticks", default=None, help="path to a tick CSV")
    ap.add_argument("--demo", action="store_true", help="synthetic session, no network")
    ap.add_argument("--json", action="store_true", help="dump raw JSON")
    ap.add_argument("--events", type=int, default=8, help="how many events to list")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args(argv)
    _color = not args.no_color and sys.stdout.isatty()

    demo = True if args.demo else None
    if args.json:
        print(json.dumps(analyze(symbol=args.symbol, demo=demo, engine=args.engine,
                                 tf=args.tf, rb=args.rb, tick_path=args.ticks),
                         indent=2))
        return 0
    try:
        ctx = build_context(symbol=args.symbol, demo=demo, tf=args.tf, rb=args.rb,
                            tick_path=args.ticks, need_ticks=args.engine != SIMPLE)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    engines = [SIMPLE, TICK] if args.engine == "both" else [args.engine]
    print_panel(ctx, {e: run(ctx, e) for e in engines}, args.events)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
