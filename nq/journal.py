"""Persistent signal journal: the multi-day track record.

Every closed signal the dashboard produces is recorded once in a SQLite
file (journal.db beside the repo, override with NQ_JOURNAL_PATH; set
NQ_JOURNAL=0 to disable). Intraday "proven today" verdicts are honest but
short-lived — this file is what turns them into an accumulating record you
can trust or reject a strategy on.

CLI:
  python3 -m nq.journal            # per-strategy/frame record, last 30 days
  python3 -m nq.journal --days 90
"""

from __future__ import annotations

import os
import sqlite3
import time
from nq.console import use_utf8_stdout

DEFAULT_PATH = os.environ.get(
    "NQ_JOURNAL_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "journal.db"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    symbol TEXT NOT NULL,
    frame TEXT NOT NULL,          -- '1m', '15m', 'R5.0', ...
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_ts INTEGER NOT NULL,
    entry REAL NOT NULL,
    exit_ts INTEGER,
    exit REAL,
    points REAL,                  -- net of costs
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (symbol, frame, strategy, entry_ts)
);
"""


def enabled() -> bool:
    return os.environ.get("NQ_JOURNAL", "1").strip() not in ("0", "false", "no")


def _connect(path: str = DEFAULT_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5)
    conn.execute(_SCHEMA)
    return conn


def record_markers(symbol: str, frame: str, markers: list[dict]) -> int:
    """Insert closed markers not seen before. Returns rows added."""
    if not enabled():
        return 0
    closed = [m for m in markers if m.get("exit") is not None]
    if not closed:
        return 0
    try:
        conn = _connect()
        with conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (symbol, frame, m["strategy"], m["side"], m["ts"], m["price"],
                     m["exit_ts"], m["exit"], m["points"], int(time.time()))
                    for m in closed
                ],
            )
            added = conn.total_changes - before
        conn.close()
        return added
    except sqlite3.OperationalError:
        return 0  # read-only filesystem (e.g. Vercel) — journal is VPS/local only


def stats(days: int = 30, symbol: str | None = None) -> list[dict]:
    """Per (strategy, frame) record over the window, best win rate first."""
    if not enabled() or not os.path.exists(DEFAULT_PATH):
        return []
    since = int(time.time()) - days * 86400
    q = """
        SELECT strategy, frame, symbol, COUNT(*) AS trades,
               SUM(points > 0) AS wins,
               ROUND(SUM(points), 2) AS points,
               COUNT(DISTINCT date(entry_ts, 'unixepoch')) AS days_active
        FROM signals
        WHERE entry_ts >= ? {sym}
        GROUP BY strategy, frame, symbol
        HAVING trades >= 1
    """.format(sym="AND symbol = ?" if symbol else "")
    args = [since] + ([symbol] if symbol else [])
    try:
        conn = _connect()
        rows = conn.execute(q, args).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return []
    out = [
        {
            "strategy": r[0], "frame": r[1], "symbol": r[2], "trades": r[3],
            "wins": r[4] or 0,
            "win_rate": round(100.0 * (r[4] or 0) / r[3], 1),
            "points": r[5] or 0.0,
            "days_active": r[6],
        }
        for r in rows
    ]
    out.sort(key=lambda r: (r["win_rate"], r["trades"]), reverse=True)
    return out


def main() -> int:
    use_utf8_stdout()
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbol")
    args = ap.parse_args()
    rows = stats(args.days, args.symbol)
    if not rows:
        print("journal is empty — it fills as the dashboard runs (VPS/local only).")
        return 0
    hdr = f"  {'strategy':<20}{'frame':<7}{'symbol':<8}{'days':>5}{'trades':>7}{'win %':>7}{'net pts':>9}"
    print(f"\n  Signal journal — last {args.days} days (net of costs)\n")
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for r in rows:
        print(f"  {r['strategy']:<20}{r['frame']:<7}{r['symbol']:<8}{r['days_active']:>5}"
              f"{r['trades']:>7}{r['win_rate']:>7.1f}{r['points']:>9.2f}")
    print("\n  Trust needs weeks of days_active, not one hot session.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
