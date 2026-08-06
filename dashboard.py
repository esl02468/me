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
import os
import threading
import time
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from nq.backtest import run_all
from nq.bias import compute_bias
from nq.data import Session, get_chart_candles, get_session, nowcast_from_qqq
from nq.levels import Level, compute_auto_levels, load_levels, save_levels
from nq.confluence import FIRE_THRESHOLD, detect as confluence_detect, simulate as confluence_simulate
from nq.journal import record_markers, stats as journal_stats
from nq.news import in_blackout, upcoming as news_upcoming
from nq.notify import enabled as notify_enabled, push as notify_push
from nq.reversal import filter_proven, rank_levels

ROOT = Path(__file__).parent
INDEX = ROOT / "index.html"
CACHE_TTL = 10.0  # seconds between upstream fetches

_demo = False
_token = ""  # non-empty -> every request must carry it (?token= or X-NQ-Token)
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


_auto_levels = compute_auto_levels

MAX_MARKERS = 15


def build_snapshot(demo: bool | None = None, symbol: str = "NQ=F") -> dict:
    if demo is None:
        demo = True if _demo else None
    sess: Session = get_session(symbol=symbol, demo=demo)
    bias = compute_bias(sess)
    if sess.source == "yahoo":
        est = nowcast_from_qqq(sess)
    elif sess.source == "demo" and sess.last:
        est = {"price": round(sess.last + 1.75, 2), "basis": "demo nowcast"}
    else:
        est = None
    auto = _auto_levels(sess)
    user = [asdict(l) for l in load_levels(sess.symbol)]
    reversal = [
        {
            "price": r.price, "label": r.label, "user": r.user, "rank": r.rank,
            "score": r.score, "rating": r.rating, "touches": r.touches,
            "bounces": r.bounces, "breaks": r.breaks, "distance": r.distance,
            "bounce_rate": r.bounce_rate,
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
        "live_estimate": est,
        "news": {
            "blackout": in_blackout(),
            "upcoming": news_upcoming(hours=12)[:3],
        },
    }


def build_backtest(demo: bool | None = None, symbol: str = "NQ=F") -> dict:
    if demo is None:
        demo = True if _demo else None
    sess: Session = get_session(symbol=symbol, demo=demo)
    reports = run_all(sess)
    return {
        "symbol": sess.symbol,
        "source": sess.source,
        "candles": len(sess.candles),
        "reports": [r.summary() for r in reports],
    }


def build_signals(
    demo: bool | None = None, symbol: str = "NQ=F",
    tf: str = "1m", rb: float | None = None,
) -> dict:
    """Confluence reversal signals for the DISPLAYED series.

    One engine, one explainable score: each bar near a still-credible
    ranked level is scored on level quality, VWAP stretch, wick rejection,
    RSI-2 exhaustion, volume, and momentum deceleration; a signal fires at
    score >= FIRE_THRESHOLD with a freight-train veto and opposing-bias
    damping. Outcomes are simulated with a symmetric 1.2 ATR bracket net
    of costs, so the composite carries an honest record.
    """
    if demo is None:
        demo = True if _demo else None
    sess: Session = get_session(symbol=symbol, demo=demo)
    bias = compute_bias(sess)
    if rb or tf != "1m":
        sess = replace(sess, candles=get_chart_candles(tf, rb, symbol=symbol, demo=demo))
    user = [asdict(l) for l in load_levels(sess.symbol)]
    scored = rank_levels(sess, user + _auto_levels(sess), bias)
    credible = filter_proven(scored)  # drop levels proven wrong on this series
    signals = confluence_detect(sess.candles, credible, bias)
    report = confluence_simulate(signals, sess.candles)
    closed = report.closed
    markers = [
        {
            "strategy": "confluence",
            "score": sig.score,
            "reason": ", ".join(sig.reasons),
            "level": sig.level_label,
            "side": sig.side,
            "ts": sig.entry_ts or sig.ts,
            "price": sig.entry if sig.entry is not None else sig.price,
            "exit_ts": sig.exit_ts,
            "exit": sig.exit,
            "points": sig.points,
            "open": sig.open,
        }
        for sig in signals
    ]
    markers.sort(key=lambda m: m["ts"])
    frame = f"R{rb}" if rb else tf
    record_markers(sess.symbol, frame, markers)
    _push_fresh_signals(sess.symbol, frame, markers)
    markers = markers[-MAX_MARKERS:]
    return {
        "tf": tf,
        "rb": rb,
        "symbol": sess.symbol,
        "engine": "confluence",
        "threshold": FIRE_THRESHOLD,
        "strategies": [
            {
                "name": "confluence",
                "win_rate": round(report.win_rate * 100, 1),
                "trades": len(closed),
                "points": round(report.total_points, 2),
            }
        ] if closed else [],
        "unproven_fallback": False,
        "markers": markers,
    }


_pushed: set = set()


def _push_fresh_signals(symbol: str, frame: str, markers: list[dict]) -> None:
    """Phone-push signals entered on (or right at) the newest bar, once each."""
    if not notify_enabled() or not markers:
        return
    newest = markers[-1]
    key = (symbol, frame, newest["strategy"], newest["ts"])
    if key in _pushed or not newest.get("open"):
        return
    if time.time() - newest["ts"] > 180:  # stale — not actionable
        return
    _pushed.add(key)
    notify_push(
        f"{symbol} {frame}: {newest['strategy']} {newest['side'].upper()} @ {newest['price']:.2f}",
        title="Signal", priority="high",
    )


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

    def _authorized(self) -> bool:
        if not _token:
            return True
        qs = parse_qs(urlparse(self.path).query)
        supplied = qs.get("token", [""])[0] or self.headers.get("X-NQ-Token", "")
        return supplied == _token

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if not self._authorized():
            self._json({"error": "unauthorized — append ?token=<your token>"}, 401)
            return
        try:
            qs = parse_qs(urlparse(self.path).query)
            symbol = qs.get("symbol", ["NQ=F"])[0][:24]
            if route in ("/", "/index.html"):
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            elif route == "/api/snapshot":
                self._json(_cached(f"snapshot:{symbol}", CACHE_TTL,
                                   lambda: build_snapshot(symbol=symbol)))
            elif route == "/api/backtest":
                self._json(_cached(f"backtest:{symbol}", 30.0,
                                   lambda: build_backtest(symbol=symbol)))
            elif route == "/api/signals":
                tf = qs.get("tf", ["1m"])[0]
                rb = qs.get("rb", [None])[0]
                rb_val = float(rb) if rb else None
                self._json(_cached(f"signals:{symbol}:{tf}:{rb_val}", 30.0,
                                   lambda: build_signals(symbol=symbol, tf=tf, rb=rb_val)))
            elif route == "/api/candles":
                tf = qs.get("tf", ["1m"])[0]
                rb = qs.get("rb", [None])[0]
                rb_val = float(rb) if rb else None
                key = f"candles:{symbol}:{tf}:{rb_val}"
                self._json(_cached(key, CACHE_TTL, lambda: {
                    "tf": tf, "rb": rb_val,
                    "candles": [
                        {"t": c.ts, "o": c.open, "h": c.high, "l": c.low, "c": c.close, "v": c.volume}
                        for c in get_chart_candles(tf, rb_val, symbol=symbol,
                                                   demo=True if _demo else None)
                    ],
                }))
            elif route == "/api/journal":
                days = int(qs.get("days", ["30"])[0])
                self._json({"days": days, "rows": journal_stats(days)})
            elif route == "/api/levels":
                self._json({"levels": [asdict(l) for l in load_levels(symbol)]})
            else:
                self._json({"error": "not found"}, 404)
        except RuntimeError as e:
            self._json({"error": str(e)}, 503)
        except Exception as e:  # keep the server alive on upstream hiccups
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json({"error": "unauthorized — append ?token=<your token>"}, 401)
            return
        if urlparse(self.path).path != "/api/levels":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = json.loads(self.rfile.read(length) or b"{}")
            levels = [
                Level(price=float(i["price"]), label=str(i.get("label", "")), kind=str(i.get("kind", "pivot")))
                for i in raw.get("levels", [])
            ]
            symbol = parse_qs(urlparse(self.path).query).get("symbol", ["NQ=F"])[0][:24]
            save_levels(levels, symbol)
            self._json({"ok": True, "count": len(levels)})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            self._json({"error": f"bad levels payload: {e}"}, 400)

    def log_message(self, fmt: str, *args) -> None:
        pass  # quiet


def main() -> None:
    global _demo, _token
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; 0.0.0.0 for remote access (set --token!)")
    ap.add_argument("--token", default=os.environ.get("NQ_TOKEN", ""),
                    help="require this token on every request (?token= or X-NQ-Token)")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    args = ap.parse_args()
    _demo = args.demo
    _token = args.token
    if args.host not in ("127.0.0.1", "localhost") and not _token:
        print("WARNING: binding to a public interface without --token — "
              "anyone who finds the port can view the dashboard.")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    mode = "DEMO (synthetic)" if _demo else "LIVE (yahoo → stooq fallback)"
    tok = f"/?token={_token}" if _token else "/"
    print(f"NQ dashboard on http://{args.host}:{args.port}{tok}  [{mode}]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
