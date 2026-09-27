"""Dashboard builders and the HTTP handler, on demo data, plus journal,
news guard and risk rails."""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

os.environ.setdefault("NQ_JOURNAL", "0")

import dashboard  # noqa: E402
from nq import clock, journal, news  # noqa: E402
from trader.risk import AccountState, PropRules, RiskManager  # noqa: E402


class Builders(unittest.TestCase):
    def test_snapshot_carries_session_structure(self):
        snap = dashboard.build_snapshot(demo=True)
        self.assertEqual(snap["source"], "demo")
        s = snap["session"]
        self.assertEqual(s["mode"], "rth")
        self.assertEqual(s["scope"], "rth")
        self.assertEqual(s["analysis_bars"], 390)
        self.assertEqual(s["bars"], 1380)
        self.assertEqual(s["analysis_start_ts"], s["rth_open_ts"] if s["rth_open_ts"] else None)
        self.assertEqual(len(snap["candles"]), 1380)
        labels = {l["label"] for l in snap["auto_levels"]}
        self.assertIn("ON high", labels)
        self.assertIn("OR high", labels)
        self.assertTrue(all("bounce_rate" in r for r in snap["reversal"]))
        json.dumps(snap)  # serialisable

    def test_backtest_reports_rth_bar_count(self):
        bt = dashboard.build_backtest(demo=True)
        self.assertEqual(bt["candles"], 390)
        self.assertEqual(len(bt["reports"]), 34)

    def test_signals_every_frame(self):
        for tf, rb in (("1m", None), ("5m", None), ("15m", None), ("1h", None), ("1d", None), ("1m", 5.0)):
            out = dashboard.build_signals(demo=True, tf=tf, rb=rb)
            self.assertEqual(out["engine"], "confluence", (tf, rb))
            self.assertLessEqual(len(out["markers"]), dashboard.MAX_MARKERS)
            for m in out["markers"]:
                self.assertIn(m["side"], ("long", "short"))
                self.assertGreaterEqual(m["score"], 60)
            json.dumps(out)

    def test_edge_report_refuses_verdicts_on_thin_samples(self):
        edge = dashboard.build_edge_bar(demo=True)
        self.assertGreater(edge["n_trials"], 50)
        self.assertEqual(edge["dsr_min_trades"], dashboard.DSR_MIN_TRADES)
        for row in edge["rows"]:
            if row["trades"] < dashboard.DSR_MIN_TRADES:
                self.assertFalse(row["enough"])
                self.assertFalse(row["clears"])
            if row["clears"]:
                self.assertGreaterEqual(row["trades"], dashboard.DSR_MIN_TRADES)
                self.assertGreaterEqual(row["dsr"], dashboard.DSR_THRESHOLD)
        self.assertEqual(edge["survivors"], sum(1 for r in edge["rows"] if r["clears"]))
        # Rows with a verdict sort above rows without one.
        flags = [r["enough"] for r in edge["rows"]]
        self.assertEqual(flags, sorted(flags, reverse=True))


class Handler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dashboard._demo = True
        dashboard._token = "s3cret"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        dashboard._demo = False
        dashboard._token = ""

    def get(self, path, headers=None, method="GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_token_required(self):
        self.assertEqual(self.get("/api/snapshot")[0], 401)
        self.assertEqual(self.get("/api/snapshot?token=wrong")[0], 401)
        self.assertEqual(self.get("/api/snapshot?token=s3cret")[0], 200)
        self.assertEqual(self.get("/api/snapshot", headers={"X-NQ-Token": "s3cret"})[0], 200)
        self.assertEqual(self.get("/?token=s3cret")[0], 200)

    def test_head_and_health(self):
        self.assertEqual(self.get("/?token=s3cret", method="HEAD")[0], 200)
        self.assertEqual(self.get("/", method="HEAD")[0], 401)
        code, body = self.get("/healthz?token=s3cret")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_bad_timeframe_is_a_400_not_a_500(self):
        code, body = self.get("/api/candles?tf=bogus&token=s3cret")
        self.assertEqual(code, 400)
        self.assertIn("unknown timeframe", json.loads(body)["error"])
        self.assertEqual(self.get("/api/journal?days=abc&token=s3cret")[0], 400)

    def test_every_route(self):
        for path in ("/api/snapshot", "/api/backtest", "/api/signals?tf=15m", "/api/signals?rb=2",
                     "/api/candles?tf=1h", "/api/candles?rb=5", "/api/edge", "/api/journal", "/api/levels"):
            sep = "&" if "?" in path else "?"
            code, body = self.get(f"{path}{sep}token=s3cret")
            self.assertEqual(code, 200, path)
            self.assertNotIn("error", json.loads(body), path)
        self.assertEqual(self.get("/nope?token=s3cret")[0], 404)


class Journal(unittest.TestCase):
    def test_round_trip_and_dedupe(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "j.db")
            with mock.patch.object(journal, "DEFAULT_PATH", path), \
                 mock.patch.dict(os.environ, {"NQ_JOURNAL": "1"}):
                m = [{"strategy": "confluence", "side": "long", "ts": 1_790_000_000, "price": 1.0,
                      "exit_ts": 1_790_000_600, "exit": 2.0, "points": 0.25},
                     {"strategy": "confluence", "side": "short", "ts": 1_790_000_060, "price": 1.0,
                      "exit_ts": None, "exit": None, "points": None}]
                self.assertEqual(journal.record_markers("NQ=F", "1m", m), 1)
                self.assertEqual(journal.record_markers("NQ=F", "1m", m), 0)
                rows = journal.stats(days=10_000)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["trades"], 1)
                self.assertEqual(rows[0]["win_rate"], 100.0)


class NewsGuard(unittest.TestCase):
    def test_nfp_first_friday_exact_eastern_time(self):
        ev = news._nfp_events(datetime(2026, 10, 15, tzinfo=timezone.utc))
        self.assertEqual([e[0].isoformat() for e in ev],
                         ["2026-10-02T08:30:00-04:00", "2026-11-06T08:30:00-05:00"])

    def test_blackout_window(self):
        release = clock.et_ts(2026, 11, 6, 8, 30)
        self.assertEqual(news.in_blackout(release - 4 * 60), "NFP (jobs report)")
        self.assertEqual(news.in_blackout(release + 9 * 60), "NFP (jobs report)")
        self.assertIsNone(news.in_blackout(release + 11 * 60))
        self.assertIsNone(news.in_blackout(release - 6 * 60))


class RiskRails(unittest.TestCase):
    def test_flatten_by_is_chicago_time(self):
        rm = RiskManager(PropRules(allow_automation=True, flatten_by="15:55"))
        before = datetime(2026, 9, 25, 20, 54, tzinfo=timezone.utc).timestamp()  # 15:54 CT
        after = before + 120
        self.assertIsNone(rm.pre_trade_check(1, now=before))
        self.assertFalse(rm.must_flatten(now=before))
        self.assertIn("flatten_by", rm.pre_trade_check(1, now=after))
        self.assertTrue(rm.must_flatten(now=after))

    def test_loss_limits_halt(self):
        rm = RiskManager(PropRules(allow_automation=True, daily_loss_limit=500, flatten_by=None), AccountState())
        rm.record_fill(-600)
        self.assertEqual(rm.pre_trade_check(1), "daily loss limit")
        self.assertTrue(rm.must_flatten())
        rm2 = RiskManager(PropRules(allow_automation=False, flatten_by=None))
        self.assertIn("automation", rm2.pre_trade_check(1))
        rm3 = RiskManager(PropRules(allow_automation=True, max_contracts=1, flatten_by=None))
        self.assertIn("max_contracts", rm3.pre_trade_check(2))


class EngineFreshness(unittest.TestCase):
    def test_stale_last_bar_signal_is_not_fresh(self):
        from nq import data
        from trader.engine import latest_signal
        sess = data.demo_session(now=clock.et_ts(2026, 9, 25, 14, 0))
        last_ts = sess.analysis_candles[-1].ts
        # Whatever the strategy says, a bar hours old is never an order.
        self.assertIsNone(latest_signal(sess, "pivot_bounce", now=last_ts + 3 * 3600))


if __name__ == "__main__":
    unittest.main()
