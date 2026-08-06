"""Vercel serverless function: GET /api/journal?days=30 (empty in the cloud —
the journal lives where the server runs 24/7, i.e. the VPS)."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nq.journal import stats  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        days = int(parse_qs(urlparse(self.path).query).get("days", ["30"])[0])
        body = json.dumps({"days": days, "rows": stats(days)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
