"""Vercel serverless function: GET /api/signals[?symbol=&demo=1]."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard import build_signals  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        demo = qs.get("demo", ["0"])[0] in ("1", "true")
        symbol = qs.get("symbol", ["NQ=F"])[0][:24]
        try:
            body = json.dumps(build_signals(demo=True if demo else None, symbol=symbol)).encode()
            code = 200
        except Exception as e:
            body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
            code = 503
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "s-maxage=30, stale-while-revalidate=60")
        self.end_headers()
        self.wfile.write(body)
