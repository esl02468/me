#!/usr/bin/env python3
"""NQ live dashboard server (stdlib only — no pip installs).

Serves static/dashboard.html plus a small JSON API, proxying market data
server-side so the browser never hits CORS walls:

  GET  /               dashboard UI
  GET  /api/snapshot   candles + price + bias + auto levels + user levels
  GET  /api/backtest   strategy analyzer results for today's session
  GET  /api/levels     user levels from levels.json
  POST /api/levels     replace user levels (dashboard editor)

Usage:
  python3 dashboard.py                 # live data on http://localhost:8787
  python3 dashboard.py --demo         # synthetic data (offline)
  python3 dashboard.py --port 9000
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from nq.backtest import run_all
from nq.bias import OPENING_RANGE_BARS, compute_bias, vwap
from nq.data import Session, get_session
from nq.levels import Level, load_levels, save_levels
from nq.reversal import rank_levels

ROOT = Path(__file__).parent
STATIC = ROOT / "static"
CACHE_TTL = 10.0  # seconds between upstream fetches

_demo = False
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, builder):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = builder()
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)
    return value


def _auto_levels(sess: Session) -> list[dict]:
    """Computed session levels: PDH/PDL, prev close, VWAP, opening range."""
    out = []
    if sess.prev_high:
        out.append({"price": round(sess.prev_high, 2), "label": "PDH", "kind": "auto"})
    if sess.prev_low:
        out.append({"price": round(sess.prev_low, 2), "label": "PDL", "kind": "auto"})
    if sess.prev_close:
        out.append({"price": round(sess.prev_close, 2), "label": "Prev close", "kind": "auto"})
    if sess.candles:
        out.append({"price": round(vwap(sess.candles)[-1], 2), "label": "VWAP", "kind": "auto"})
    if len(sess.candles) >= OPENING_RANGE_BARS:
        or_bars = sess.candles[:OPENING_RANGE_BARS]
        out.append({"price": round(max(c.high for c in or_bars), 2), "label": "OR high", "kind": "auto"})
        out.append({"price": round(min(c.low for c in or_bars), 2), "label": "OR low", "kind": "auto"})
    return out


def build_snapshot() -> dict:
    sess: Session = get_session(demo=True if _demo else None)
    bias = compute_bias(sess)
    auto = _auto_levels(sess)
    user = [asdict(l) for l in load_levels()]
    reversal = [
        {
            "price": r.price, "label": r.label, "user": r.user, "rank": r.rank,
            "score": r.score, "rating": r.rating, "touches": r.touches,
            "bounces": r.bounces, "breaks": r.breaks, "distance": r.distance,
        }
        for r in rank_levels(sess, user + auto, bias)
    ]
    return {
        "symbol": sess.symbol,
        "source": sess.source,
        "ts": int(time.time()),
        "price": sess.last,
        "prev_close": sess.prev_close,
        "candles": [
            {"t": c.ts, "o": c.open, "h": c.high, "l": c.low, "c": c.close, "v": c.volume}
            for c in sess.candles
        ],
        "bias": {
            "score": bias.score,
            "label": bias.label,
            "components": bias.components,
            "detail": bias.detail,
        },
        "auto_levels": auto,
        "user_levels": user,
        "reversal": reversal,
    }


def build_backtest() -> dict:
    sess: Session = get_session(demo=True if _demo else None)
    reports = run_all(sess)
    return {
        "symbol": sess.symbol,
        "source": sess.source,
        "candles": len(sess.candles),
        "reports": [r.summary() for r in reports],
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path in ("/", "/index.html"):
                self._send(200, (STATIC / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/snapshot":
                self._json(_cached("snapshot", CACHE_TTL, build_snapshot))
            elif self.path == "/api/backtest":
                self._json(_cached("backtest", 30.0, build_backtest))
            elif self.path == "/api/levels":
                self._json({"levels": [asdict(l) for l in load_levels()]})
            else:
                self._json({"error": "not found"}, 404)
        except RuntimeError as e:
            self._json({"error": str(e)}, 503)
        except Exception as e:  # keep the server alive on upstream hiccups
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/levels":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = json.loads(self.rfile.read(length) or b"{}")
            levels = [
                Level(price=float(i["price"]), label=str(i.get("label", "")), kind=str(i.get("kind", "pivot")))
                for i in raw.get("levels", [])
            ]
            save_levels(levels)
            self._json({"ok": True, "count": len(levels)})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            self._json({"error": f"bad levels payload: {e}"}, 400)

    def log_message(self, fmt: str, *args) -> None:
        pass  # quiet


def main() -> None:
    global _demo
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    args = ap.parse_args()
    _demo = args.demo

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    mode = "DEMO (synthetic)" if _demo else "LIVE (yahoo → stooq fallback)"
    print(f"NQ dashboard on http://localhost:{args.port}  [{mode}]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
