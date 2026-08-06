"""Vercel serverless function: GET /api/candles?tf=1m|5m|15m|1h|1d or ?rb=<pts>."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nq.data import get_chart_candles  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        tf = qs.get("tf", ["1m"])[0]
        rb = qs.get("rb", [None])[0]
        demo = qs.get("demo", ["0"])[0] in ("1", "true")
        try:
            candles = get_chart_candles(tf, float(rb) if rb else None,
                                        demo=True if demo else None)
            body = json.dumps({
                "tf": tf, "rb": float(rb) if rb else None,
                "candles": [
                    {"t": c.ts, "o": c.open, "h": c.high, "l": c.low, "c": c.close, "v": c.volume}
                    for c in candles
                ],
            }).encode()
            code = 200
        except Exception as e:
            body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
            code = 503
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "s-maxage=15, stale-while-revalidate=30")
        self.end_headers()
        self.wfile.write(body)
