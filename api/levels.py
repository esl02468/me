"""Vercel serverless function: GET /api/levels (read-only in the cloud)."""

import json
import os
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nq.levels import load_levels  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"levels": [asdict(l) for l in load_levels()]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # The cloud filesystem is read-only: levels change via a levels.json commit.
        body = json.dumps(
            {"error": "read-only deployment — edit levels.json in the repo and push"}
        ).encode()
        self.send_response(501)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)
