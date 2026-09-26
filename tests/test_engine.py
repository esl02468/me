"""Bias, levels, strategies, confluence, reversal ranking, deflation."""

import math
import unittest

from nq import clock, data, deflate
from nq.backtest import COST_POINTS, Report, Trade, run_all
from nq.bias import compute_bias, opening_range, session_vwap
from nq.confluence import detect, simulate
from nq.levels import compute_auto_levels
from nq.reversal import _merge_coincident, filter_proven, rank_levels

NOW = clock.et_ts(2026, 9, 25, 14, 0)


class BiasAndLevels(unittest.TestCase):
    def setUp(self):
        self.sess = data.demo_session(now=NOW)

    def test_opening_range_is_the_cash_open(self):
        b = compute_bias(self.sess)
        first15 = self.sess.rth[:15]
        self.assertEqual(b.detail["or_high"], round(max(c.high for c in first15), 2))
        self.assertEqual(b.detail["or_low"], round(min(c.low for c in first15), 2))
        self.assertIn(b.label, ("STRONG BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "STRONG BEARISH"))
        self.assertEqual(set(b.components), {"vwap", "ema", "or", "prev", "momentum"})

    def test_opening_range_scales_with_bar_size(self):
        five = data.aggregate_candles(self.sess.rth, 300)
        hi, lo = opening_range(five)
        self.assertEqual((hi, lo), (max(c.high for c in five[:3]), min(c.low for c in five[:3])))
        hourly = data.aggregate_candles(self.sess.rth, 3600)
        self.assertIsNone(opening_range(hourly))

    def test_vwap_anchors_at_the_newest_session(self):
        two = data.demo_session(minutes=780, now=NOW, seed=3)
        last_day = [c for c in two.candles if c.ts >= clock.session_start(two.candles[-1].ts)]
        from nq.bias import vwap
        self.assertAlmostEqual(session_vwap(two.candles), vwap(last_day)[-1])

    def test_auto_levels_include_overnight_and_rth_structure(self):
        labels = [l["label"] for l in compute_auto_levels(self.sess)]
        for want in ("PDH", "PDL", "Prev close", "Pivot P", "R1", "S1", "Week high",
                     "ON high", "ON low", "VWAP", "OR high", "OR low"):
            self.assertIn(want, labels)
        by = {l["label"]: l["price"] for l in compute_auto_levels(self.sess)}
        self.assertEqual(by["ON high"], round(self.sess.overnight_high, 2))

    def test_no_overnight_levels_before_the_open(self):
        overnight = data.annotate_session(
            data.Session("NQ=F", [c for c in self.sess.candles if c.ts < self.sess.rth_open_ts],
                         prev_high=1, prev_low=0, prev_close=0.5))
        labels = [l["label"] for l in compute_auto_levels(overnight)]
        self.assertNotIn("ON high", labels)
        self.assertIn("OR high", labels)  # the Globex opening range, honestly labelled by scope


class Strategies(unittest.TestCase):
    def test_every_strategy_runs_on_rth_bars(self):
        sess = data.demo_session(now=NOW)
        reports = run_all(sess)
        self.assertEqual(len(reports), 34)
        rth_ts = {c.ts for c in sess.rth}
        for r in reports:
            for t in r.trades:
                self.assertIn(t.entry_ts, rth_ts, r.strategy)

    def test_gap_setups_see_the_real_gap(self):
        sess = data.demo_session(now=NOW)
        # Manufacture a big gap: prior close far below the RTH open.
        sess.prev_close = sess.rth[0].open - 200
        names = {r.strategy: r for r in run_all(sess)}
        self.assertEqual(len(names["gap_fade"].trades), 1)
        self.assertEqual(names["gap_fade"].trades[0].side, "short")
        # Signal on the first RTH close, filled at the next bar's open.
        self.assertEqual(names["gap_fade"].trades[0].entry_ts, sess.rth[2].ts)

    def test_trade_points_are_net_of_cost(self):
        t = Trade("long", 0, 100.0, exit_ts=60, exit=102.0)
        self.assertAlmostEqual(t.points, 2.0 - COST_POINTS)
        self.assertAlmostEqual(t.gross_points, 2.0)
        r = Report("x", [t, Trade("short", 0, 100.0, exit_ts=60, exit=101.0)])
        self.assertEqual(len(r.closed), 2)
        self.assertEqual(r.summary()["trades"], 2)
        self.assertEqual(r.summary()["win_rate"], 50.0)


class Confluence(unittest.TestCase):
    def test_engine_fires_sometimes_and_never_before_bar_30(self):
        total = 0
        for seed in range(1, 13):
            sess = data.demo_session(now=NOW, seed=seed)
            bias = compute_bias(sess)
            scored = rank_levels(sess, compute_auto_levels(sess), bias)
            sigs = detect(sess.analysis_candles, filter_proven(scored), bias)
            total += len(sigs)
            rep = simulate(sigs, sess.analysis_candles)
            for s in sigs:
                self.assertGreaterEqual(s.score, 60)
                self.assertTrue(s.reasons)
                if s.exit is not None:
                    self.assertIsNotNone(s.points)
            self.assertEqual(len(rep.trades), sum(1 for s in sigs if not s.open or s.entry is not None))
        self.assertGreater(total, 0)
        self.assertLess(total / 12, 6)

    def test_signals_are_deterministic(self):
        sess = data.demo_session(now=NOW, seed=5)
        bias = compute_bias(sess)
        scored = filter_proven(rank_levels(sess, compute_auto_levels(sess), bias))
        a = detect(sess.analysis_candles, scored, bias)
        b = detect(sess.analysis_candles, scored, bias)
        self.assertEqual([(s.ts, s.side, s.score) for s in a], [(s.ts, s.side, s.score) for s in b])


class Reversal(unittest.TestCase):
    def test_coincident_levels_merge_and_keep_user_priority(self):
        merged = _merge_coincident([
            {"price": 100.0, "label": "PDL", "kind": "auto"},
            {"price": 100.001, "label": "Week low", "kind": "auto"},
            {"price": 100.0, "label": "mine", "kind": "support"},
            {"price": 110.0, "label": "PDH", "kind": "auto"},
        ])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["label"], "PDL / Week low / mine")
        self.assertEqual(merged[0]["kind"], "support")

    def test_ranking_and_filter(self):
        sess = data.demo_session(now=NOW)
        scored = rank_levels(sess, compute_auto_levels(sess), compute_bias(sess))
        self.assertEqual([l.rank for l in scored], list(range(1, len(scored) + 1)))
        for l in scored:
            self.assertTrue(0 <= l.score <= 100)
        kept = filter_proven(scored)
        for l in kept:
            self.assertTrue(l.bounce_rate is None or l.bounce_rate > 0.51)
        self.assertGreaterEqual(len(scored), len(kept))


class Deflate(unittest.TestCase):
    def test_sharpe_and_moments(self):
        self.assertTrue(math.isnan(deflate.sharpe_ratio([1.0])))
        self.assertTrue(math.isnan(deflate.sharpe_ratio([1.0, 1.0, 1.0])))
        self.assertAlmostEqual(deflate.sharpe_ratio([1.0, -1.0, 1.0, -1.0]), 0.0)
        self.assertAlmostEqual(deflate.kurtosis([1, 2, 3, 4, 5, 6, 7, 8, 9, 10] * 50), 1.78, places=1)

    def test_more_trials_raise_the_bar(self):
        v = deflate.null_sharpe_variance(50)
        self.assertLess(deflate.expected_max_sharpe(10, v), deflate.expected_max_sharpe(100, v))
        self.assertEqual(deflate.expected_max_sharpe(1, v), 0.0)

    def test_five_for_five_streak_is_the_trap(self):
        # A perfect five-trade streak sails through the asymptotic formula —
        # which is why the dashboard refuses a verdict below DSR_MIN_TRADES.
        pts = [10.0, 12.0, 9.0, 11.0, 10.5]
        sr = deflate.sharpe_ratio(pts)
        dsr = deflate.deflated_sharpe_ratio(sr, 5, deflate.skewness(pts), deflate.kurtosis(pts), 111)
        self.assertGreater(dsr, 0.95)
        from dashboard import DSR_MIN_TRADES
        self.assertGreaterEqual(DSR_MIN_TRADES, 20)

    def test_noise_does_not_clear(self):
        import random
        rng = random.Random(0)
        pts = [rng.gauss(0.2, 5.0) for _ in range(60)]
        sr = deflate.sharpe_ratio(pts)
        dsr = deflate.deflated_sharpe_ratio(sr, 60, deflate.skewness(pts), deflate.kurtosis(pts), 100)
        self.assertLess(dsr, 0.95)


if __name__ == "__main__":
    unittest.main()
