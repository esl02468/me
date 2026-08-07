"""Vercel serverless function: GET /api/algobox[?symbol=&tf=&rb=&engine=&demo=1].

`engine` is both (default) / simple / tick. The tick engines read a real
tick file when ALGOBOX_TICK_FILE is set in the deployment environment;
otherwise they run on a deterministic reconstruction of the same bars and
say so in `tick_source`.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard import build_algobox  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        demo = qs.get("demo", ["0"])[0] in ("1", "true")
        symbol = qs.get("symbol", ["NQ=F"])[0][:24]
        tf = qs.get("tf", ["1m"])[0]
        rb = qs.get("rb", [None])[0]
        engine = qs.get("engine", ["both"])[0]
        if engine not in ("both", "simple", "tick"):
            engine = "both"
        try:
            body = json.dumps(build_algobox(
                demo=True if demo else None, symbol=symbol, tf=tf,
                rb=float(rb) if rb else None, engine=engine)).encode()
            code = 200
        except Exception as e:
            body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
            code = 503
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "s-maxage=20, stale-while-revalidate=60")
        self.end_headers()
        self.wfile.write(body)
