#!/usr/bin/env python3
"""Signal engine + trade copier for Tradovate accounts.

Modes:
  python3 -m trader.engine --paper          # DEFAULT: log what it WOULD do
  python3 -m trader.engine --sim            # send orders to Tradovate DEMO
  python3 -m trader.engine --sim --live-ack # live env, ONLY with
                                            #   TRADOVATE_LIVE=YES_I_UNDERSTAND set

How it trades:
  Every poll it rebuilds today's session, runs the configured strategy from
  the analyzer library, and if the strategy's LAST bar produced a fresh
  entry signal, it places a bracket order (ATR target/stop) on EVERY
  account listed in config.json whose risk rules allow it — that is the
  trade copier. Accounts whose PropRules refuse the order are skipped with
  the reason logged. Hard limits force a flatten.

Paper mode needs no credentials and is the required first step: let it run
against the live feed for days and read the log before ever pointing it at
the demo API, and let the demo run for weeks before considering live.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from nq.backtest import STRATEGIES, backtest_level_bounce, session_level_prices  # noqa: E402
from nq.bias import atr  # noqa: E402
from nq.data import get_session  # noqa: E402
from nq.news import in_blackout  # noqa: E402
from nq.notify import push as notify_push  # noqa: E402
from nq.strategies import extended_strategies  # noqa: E402
from trader.risk import AccountState, PropRules, RiskManager  # noqa: E402
from trader.tradovate import TradovateClient, TradovateError  # noqa: E402

POINT_VALUE = {"NQ": 20.0, "MNQ": 2.0}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def latest_signal(session, strategy: str):
    """Run the named strategy; return its trade opened on the LAST bar, if any."""
    if strategy in STRATEGIES:
        report = STRATEGIES[strategy](session.candles)
    elif strategy == "level_bounce":
        report = backtest_level_bounce(session.candles, session_level_prices(session))
    else:
        match = [r for r in extended_strategies(
            session.candles, session.prev_close, session_level_prices(session)[:9])
            if r.strategy.split("(")[0] == strategy]
        if not match:
            raise SystemExit(f"unknown strategy {strategy!r}")
        report = match[0]
    if not report.trades:
        return None
    last = report.trades[-1]
    last_ts = session.candles[-1].ts
    # Fresh = entered on the most recent bar and still open.
    if last.exit is None and last.entry_ts >= last_ts - 90:
        return last
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    ap.add_argument("--paper", action="store_true", help="log only, no orders (default)")
    ap.add_argument("--sim", action="store_true", help="send orders to Tradovate demo")
    ap.add_argument("--demo-data", action="store_true", help="synthetic market data")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"config not found: {cfg_path}\n"
              f"copy trader/config.example.json to trader/config.json and fill it in.",
              file=sys.stderr)
        return 1
    cfg = json.loads(cfg_path.read_text())
    paper = not args.sim
    strategy = cfg.get("strategy", "pivot_bounce")
    symbol = cfg.get("symbol", "MNQZ5")
    qty = int(cfg.get("qty", 1))
    poll = float(cfg.get("poll_seconds", 20))

    managers = {
        acc["name"]: RiskManager(PropRules.from_dict(acc.get("rules", {})), AccountState())
        for acc in cfg.get("accounts", [])
    }
    client = None if paper else TradovateClient(cfg["tradovate"])
    if client is not None:
        client.authenticate()
        log(f"authenticated against {client.base}")

    mode = "PAPER (log only)" if paper else f"SIM ({client.base})"
    log(f"engine up: strategy={strategy} symbol={symbol} qty={qty} accounts={list(managers)} [{mode}]")
    log("reminder: check each prop firm's automation & copy-trading policy before enabling it there.")

    last_entry_ts = 0
    while True:
        try:
            sess = get_session(demo=True if args.demo_data else None)
            blackout = in_blackout()
            if blackout:
                log(f"news blackout ({blackout}) — no new entries")
            sig = None if blackout else latest_signal(sess, strategy)
            a = atr(sess.candles) or 5.0
            if sig and sig.entry_ts != last_entry_ts:
                last_entry_ts = sig.entry_ts
                side = "Buy" if sig.side == "long" else "Sell"
                tp = sig.entry + (0.8 * a if sig.side == "long" else -0.8 * a)
                sl = sig.entry - (1.6 * a if sig.side == "long" else -1.6 * a)
                log(f"SIGNAL {strategy}: {sig.side} @ ~{sig.entry:.2f} tp {tp:.2f} sl {sl:.2f}")
                # Trade copy: same order to every account whose rules allow it.
                for acc in cfg.get("accounts", []):
                    name = acc["name"]
                    refusal = managers[name].pre_trade_check(qty)
                    if refusal:
                        log(f"  {name}: SKIPPED — {refusal}")
                        continue
                    if paper:
                        log(f"  {name}: would place {side} {qty} {symbol} bracket")
                    else:
                        try:
                            out = client.place_bracket(acc["account_id"], symbol, qty, side, tp, sl)
                            log(f"  {name}: order sent -> {out}")
                            notify_push(f"{name}: {side} {qty} {symbol} @ ~{sig.entry:.2f}", title="Order sent")
                        except TradovateError as e:
                            log(f"  {name}: ORDER FAILED — {e}")
            for acc in cfg.get("accounts", []):
                name = acc["name"]
                if managers[name].must_flatten():
                    if paper:
                        log(f"  {name}: would flatten ({managers[name].state.halted or 'flatten_by'})")
                    else:
                        try:
                            client.flatten(acc["account_id"], symbol)
                        except TradovateError as e:
                            log(f"  {name}: flatten failed — {e}")
        except RuntimeError as e:
            log(f"data error: {e}")
        except KeyboardInterrupt:
            log("stopped by user")
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main())
