import json
import unittest
from unittest import mock

from nq import clock, data
from nq.data import Candle, Session


def bars(start_ts, n, price=23_000.0, step=60):
    out = []
    for i in range(n):
        p = price + i * 0.5
        out.append(Candle(start_ts + i * step, p, p + 2, p - 2, p + 1, 100))
    return out


class RthSlicing(unittest.TestCase):
    def setUp(self):
        self.open_ts = clock.et_ts(2026, 9, 24, 18, 0)   # Thu 18:00 ET
        self.rth_ts = clock.et_ts(2026, 9, 25, 9, 30)    # Fri 09:30 ET
        self.close_ts = clock.et_ts(2026, 9, 25, 17, 0)

    def full_day(self):
        n = (self.close_ts - self.open_ts) // 60
        return bars(self.open_ts, n)

    def test_full_globex_day_splits_at_the_cash_open(self):
        sess = data.annotate_session(Session("NQ=F", self.full_day(), mode="rth"))
        self.assertEqual(len(sess.candles), 1380)
        self.assertEqual(sess.session_start_ts, self.open_ts)
        self.assertEqual(sess.rth_start_ts, self.rth_ts)
        self.assertEqual(len(sess.rth), 390)
        self.assertEqual(sess.rth[0].ts, self.rth_ts)
        self.assertEqual(sess.rth[-1].ts, clock.et_ts(2026, 9, 25, 15, 59))
        self.assertEqual(sess.analysis_scope, "rth")
        self.assertEqual(sess.analysis_start_ts, self.rth_ts)
        self.assertIsNotNone(sess.overnight_high)
        self.assertLess(sess.overnight_high, max(c.high for c in sess.rth))

    def test_before_the_open_analysis_is_the_globex_day(self):
        overnight = bars(self.open_ts, 900)  # 18:00 -> 08:59
        sess = data.annotate_session(Session("NQ=F", overnight, mode="rth"))
        self.assertEqual(sess.rth, [])
        self.assertIsNone(sess.rth_start_ts)
        self.assertEqual(sess.analysis_scope, "globex")
        self.assertEqual(len(sess.analysis_candles), 900)

    def test_first_minutes_of_rth_are_still_too_thin(self):
        sess = data.annotate_session(Session("NQ=F", bars(self.open_ts, 930 + 10), mode="rth"))
        self.assertEqual(len(sess.rth), 10)
        self.assertEqual(sess.analysis_scope, "globex")
        sess = data.annotate_session(Session("NQ=F", bars(self.open_ts, 930 + 15), mode="rth"))
        self.assertEqual(sess.analysis_scope, "rth")
        self.assertEqual(len(sess.analysis_candles), 15)

    def test_globex_mode_never_slices(self):
        sess = data.annotate_session(Session("NQ=F", self.full_day(), mode="globex"))
        self.assertEqual(sess.analysis_scope, "globex")
        self.assertEqual(len(sess.analysis_candles), 1380)

    def test_hourly_bars_keep_the_bar_holding_the_open(self):
        hourly = bars(self.open_ts, 23, step=3600)  # 18:00 .. 16:00
        kept = data.rth_filter(hourly)
        mods = [clock.et_minutes(c.ts) for c in kept]
        self.assertEqual(mods, [9 * 60, 10 * 60, 11 * 60, 12 * 60, 13 * 60, 14 * 60, 15 * 60])

    def test_daily_bars_are_never_filtered(self):
        daily = bars(self.open_ts, 30, step=86400)
        self.assertEqual(len(data.rth_filter(daily)), 30)
        self.assertEqual(Session("NQ=F", daily).analysis_scope, "daily")

    def test_current_session_slice_drops_the_prior_day(self):
        two_days = bars(self.open_ts - 86400, 1380) + self.full_day()
        cur = data.current_session_slice(two_days)
        self.assertEqual(cur[0].ts, self.open_ts)
        self.assertEqual(len(cur), 1380)

    def test_weekend_reads_fridays_session(self):
        friday = self.full_day()
        cur = data.current_session_slice(friday)
        sess = data.annotate_session(Session("NQ=F", cur))
        self.assertEqual(sess.session_start_ts, self.open_ts)
        self.assertEqual(sess.rth_open_ts, self.rth_ts)


class YahooParsing(unittest.TestCase):
    def test_error_payload_raises_value_error_not_type_error(self):
        payload = {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data"}}}
        with self.assertRaises(ValueError) as cm:
            data._parse_yahoo(payload)
        self.assertIn("No data", str(cm.exception))

    def test_null_bars_are_skipped(self):
        payload = {"chart": {"result": [{
            "meta": {"chartPreviousClose": 100.0},
            "timestamp": [1, 2, 3],
            "indicators": {"quote": [{
                "open": [1.0, None, 3.0], "high": [2.0, 2.0, 4.0],
                "low": [0.5, 0.5, 2.5], "close": [1.5, 1.5, 3.5], "volume": [10, None, 30],
            }]},
        }]}}
        candles, meta = data._parse_yahoo(payload)
        self.assertEqual([c.ts for c in candles], [1, 3])
        self.assertEqual(candles[1].volume, 30)
        self.assertEqual(meta["chartPreviousClose"], 100.0)

    def test_retry_on_429_then_success(self):
        calls = []

        class Err(Exception):
            pass

        import urllib.error

        def fake_get(url, timeout=15.0):
            calls.append(url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(url, 429, "Too Many", {}, None)
            return b'{"ok": true}'

        with mock.patch.object(data, "_http_get", fake_get), mock.patch.object(data.time, "sleep"):
            self.assertEqual(data._http_get_retry("u"), b'{"ok": true}')
        self.assertEqual(len(calls), 2)

    def test_no_retry_on_404(self):
        import urllib.error

        def fake_get(url, timeout=15.0):
            raise urllib.error.HTTPError(url, 404, "nope", {}, None)

        with mock.patch.object(data, "_http_get", fake_get), mock.patch.object(data.time, "sleep") as sl:
            with self.assertRaises(urllib.error.HTTPError):
                data._http_get_retry("u")
            sl.assert_not_called()


class DailyContext(unittest.TestCase):
    def test_prev_day_is_the_bar_before_the_session_open(self):
        session_open = clock.et_ts(2026, 9, 27, 18, 0)  # Sunday evening -> Monday
        # Yahoo-style: daily bars stamped at their own session open.
        daily = [Candle(clock.et_ts(2026, 9, d - 1, 18, 0), 1, 2 + d, 0.5, 1.5 + d) for d in (23, 24, 25)]
        daily.append(Candle(session_open, 9, 9, 9, 9))  # today's live bar
        ctx = data._context_from_daily(daily, session_open)
        self.assertEqual(ctx["prev_close"], 1.5 + 25)  # Friday, not today
        self.assertEqual(ctx["week_high"], 2 + 25)

    def test_midnight_stamped_bars(self):
        session_open = clock.et_ts(2026, 9, 24, 18, 0)  # Friday's session
        daily = [Candle(clock.et_ts(2026, 9, d, 0, 0), 1, d, 0.5, d) for d in (22, 23, 24, 25)]
        ctx = data._context_from_daily(daily, session_open)
        self.assertEqual(ctx["prev_close"], 24)

    def test_stooq_csv(self):
        text = "Date,Open,High,Low,Close,Volume\n2026-09-24,1,5,0.5,4,100\n2026-09-25,4,6,3,5,N/D\nbad line\n"
        with mock.patch.object(data, "_http_get", lambda url, timeout=15.0: text.encode()):
            rows = data.fetch_stooq_daily("nq.f")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].close, 5)
        self.assertEqual(rows[1].ts, clock.et_ts(2026, 9, 25, 0, 0))

    def test_stooq_symbol_map(self):
        self.assertEqual(data.stooq_symbol("MNQ=F"), "nq.f")
        self.assertEqual(data.stooq_symbol("es=f"), "es.f")
        self.assertIsNone(data.stooq_symbol("AAPL"))


class GetSessionFallbacks(unittest.TestCase):
    def test_yahoo_error_payload_falls_through_to_stooq_quote(self):
        with mock.patch.object(data, "fetch_yahoo_intraday", side_effect=ValueError("yahoo: no result")), \
             mock.patch.object(data, "fetch_stooq_quote", return_value=123.5) as sq:
            sess = data.get_session("MNQ=F", demo=False)
        self.assertEqual(sess.source, "stooq")
        self.assertEqual(sess.last, 123.5)
        sq.assert_called_once_with("nq.f")

    def test_everything_down_is_a_runtime_error(self):
        with mock.patch.object(data, "fetch_yahoo_intraday", side_effect=OSError("net")), \
             mock.patch.object(data, "fetch_stooq_quote", return_value=None):
            with self.assertRaises(RuntimeError):
                data.get_session(demo=False)

    def test_context_falls_back_to_stooq_daily(self):
        sess = data.annotate_session(Session("NQ=F", bars(clock.et_ts(2026, 9, 24, 18, 0), 100), source="yahoo"))
        with mock.patch.object(data, "fetch_yahoo_intraday", return_value=sess), \
             mock.patch.object(data, "fetch_yahoo_daily_context", side_effect=OSError("429")), \
             mock.patch.object(data, "fetch_stooq_daily_context", return_value={"prev_close": 7, "prev_high": 9, "prev_low": 5}):
            out = data.get_session(demo=False)
        self.assertEqual(out.prev_close, 7)
        self.assertEqual(out.prev_high, 9)

    def test_chart_upstream_failure_is_a_runtime_error(self):
        with mock.patch.object(data, "fetch_yahoo_session", side_effect=ValueError("yahoo: no result")):
            with self.assertRaises(RuntimeError):
                data.get_chart_candles("5m", demo=False)
        with self.assertRaises(ValueError):
            data.get_chart_candles("bogus", demo=True)


class Demo(unittest.TestCase):
    NOW = clock.et_ts(2026, 9, 25, 14, 0)  # Friday afternoon

    def test_demo_is_a_full_globex_day_with_rth(self):
        s = data.demo_session(now=self.NOW)
        self.assertEqual(len(s.candles), 1380)
        self.assertEqual(s.candles[0].ts, clock.et_ts(2026, 9, 24, 18, 0))
        self.assertEqual(len(s.rth), 390)
        self.assertEqual(s.analysis_scope, "rth")
        self.assertIsNotNone(s.prev_high)
        self.assertGreater(s.week_high, s.prev_low)

    def test_demo_is_deterministic(self):
        a = data.demo_session(now=self.NOW).candles
        b = data.demo_session(now=self.NOW).candles
        self.assertEqual(a, b)
        c = data.demo_session(now=self.NOW, seed=2).candles
        self.assertNotEqual(a, c)

    def test_demo_backs_up_to_a_finished_session_early_in_the_day(self):
        early = clock.et_ts(2026, 9, 25, 7, 0)  # Friday 07:00 ET -> Thursday's session
        s = data.demo_session(now=early)
        self.assertEqual(clock.trade_date(s.candles[-1].ts).isoformat(), "2026-09-24")
        sunday = clock.et_ts(2026, 9, 27, 20, 0)
        s = data.demo_session(now=sunday)
        self.assertEqual(clock.trade_date(s.candles[-1].ts).isoformat(), "2026-09-25")

    def test_multi_day_demo_is_contiguous(self):
        s = data.demo_session(minutes=390 * 5, now=self.NOW, seed=11)
        ts = [c.ts for c in s.candles]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(len(set(clock.trade_date(t) for t in ts)), 5)
        agg = data.aggregate_candles(s.candles, 300)
        self.assertEqual(len(agg), len(s.candles) // 5)

    def test_bar_volatility_profile_is_thinner_overnight(self):
        s = data.demo_session(now=self.NOW)
        rng = lambda cs: sum(c.high - c.low for c in cs) / len(cs)
        overnight = [c for c in s.candles if c.ts < s.rth_open_ts]
        self.assertLess(rng(overnight), rng(s.rth) * 0.6)


class RangeBars(unittest.TestCase):
    def test_every_closed_bar_spans_exactly_the_range(self):
        s = data.demo_session(now=Demo.NOW)
        rb = data.build_range_bars(s.candles, 5.0)
        self.assertGreater(len(rb), 50)
        for b in rb[:-1]:
            self.assertAlmostEqual(b.high - b.low, 5.0, places=2)
            self.assertLessEqual(b.low, min(b.open, b.close))
            self.assertGreaterEqual(b.high, max(b.open, b.close))
        self.assertEqual(rb[-1].close, s.candles[-1].close)


if __name__ == "__main__":
    unittest.main()
