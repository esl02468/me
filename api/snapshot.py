"""Vercel serverless function: GET /api/snapshot[?demo=1]."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dashboard import build_snapshot  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        demo = qs.get("demo", ["0"])[0] in ("1", "true")
        try:
            body = json.dumps(build_snapshot(demo=True if demo else None)).encode()
            code = 200
        except Exception as e:
            body = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
            code = 503
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "s-maxage=10, stale-while-revalidate=30")
        self.end_headers()
        self.wfile.write(body)
