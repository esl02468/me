"""AlgoBox tests: python3 -m unittest discover tests

The contract these lock down, in order of importance:

  1. Reconstruction never invents direction. Aggressor side is a pure
     function of the bar's path, so the same OHLCV always produces the
     same delta — a tick reading that changed sign between runs would be
     worse than no reading at all.
  2. Every module, under both engines, returns a well-formed Reading on
     any input, including inputs far too short to say anything about.
  3. The detectors actually fire on the things they claim to detect.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace

from algobox import suite
from algobox.core import Context, SIMPLE, TICK, path_volume_split, tick_size
from algobox.flow import _divergence, bar_delta
from algobox.structure import _value_area
from algobox.ticks import (
    build_footprint, get_ticks, load_tick_file, reconstruct, volume_at_price,
)
from nq.data import Candle, demo_session
from nq.reversal import LevelScore

SYM = "NQ=F"


def ctx_for(session, need_ticks=True) -> Context:
    return suite.build_context(session=session, demo=True, need_ticks=need_ticks)


def bars(specs, start=1_700_000_000, span=60):
    """[(o, h, l, c, v)] -> candles on a clean minute grid."""
    return [Candle(ts=start + i * span, open=o, high=h, low=l, close=c, volume=v)
            for i, (o, h, l, c, v) in enumerate(specs)]


class TestReconstruction(unittest.TestCase):
    def setUp(self):
        self.candles = demo_session(seed=42).candles

    def test_is_deterministic(self):
        a, b = reconstruct(self.candles, SYM), reconstruct(self.candles, SYM)
        self.assertEqual(len(a), len(b))
        self.assertEqual([(t.price, t.size, t.side) for t in a],
                         [(t.price, t.size, t.side) for t in b])

    def test_is_content_addressed_not_time_addressed(self):
        """Shifting every timestamp must not change a single print's
        price, size, or side — otherwise readings drift as the clock does."""
        shifted = [replace(c, ts=c.ts + 37) for c in self.candles]
        a, b = reconstruct(self.candles, SYM), reconstruct(shifted, SYM)
        self.assertEqual([(t.price, t.size, t.side) for t in a],
                         [(t.price, t.size, t.side) for t in b])

    def test_appending_bars_never_rewrites_history(self):
        head = reconstruct(self.candles[:200], SYM)
        full = reconstruct(self.candles, SYM)
        self.assertEqual([(t.price, t.size, t.side) for t in head],
                         [(t.price, t.size, t.side) for t in full[:len(head)]])

    def test_preserves_what_the_bar_proves(self):
        for c in self.candles[:40]:
            ticks = reconstruct([c], SYM)
            self.assertTrue(ticks)
            self.assertTrue(all(c.low - 1e-9 <= t.price <= c.high + 1e-9 for t in ticks),
                            "a print escaped the bar's range")
            self.assertAlmostEqual(ticks[-1].price, c.close, places=6,
                                   msg="the bar's last print must be its close")
            # Volume is preserved to per-print rounding, not exactly.
            self.assertLess(abs(sum(t.size for t in ticks) - c.volume),
                            max(len(ticks), c.volume * 0.05))

    def test_side_follows_the_path_and_nothing_else(self):
        """A bar that opened on its low and closed on its high can only be
        reconstructed as buyer-dominated. No seed may change that."""
        up = bars([(100.0, 110.0, 100.0, 110.0, 500)])
        ticks = reconstruct(up, SYM)
        delta = sum(t.signed for t in ticks)
        self.assertGreater(delta, 0)
        down = bars([(110.0, 110.0, 100.0, 100.0, 500)])
        self.assertLess(sum(t.signed for t in reconstruct(down, SYM)), 0)

    def test_flat_bar_does_not_crash(self):
        ticks = reconstruct(bars([(100.0, 100.0, 100.0, 100.0, 50)]), SYM)
        self.assertTrue(ticks)
        self.assertTrue(all(t.price == 100.0 for t in ticks))

    def test_missing_volume_still_produces_a_tape(self):
        ticks = reconstruct(bars([(100.0, 104.0, 99.0, 103.0, 0)]), SYM)
        self.assertGreaterEqual(len(ticks), 4)


class TestTickFile(unittest.TestCase):
    def _write(self, text: str) -> str:
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        fh.write(text)
        fh.close()
        return fh.name

    def test_reads_explicit_sides(self):
        path = self._write(
            "timestamp,price,size,side\n"
            "1700000000,100.25,3,buy\n"
            "1700000001,100.00,7,sell\n")
        ticks = load_tick_file(path)
        self.assertEqual([t.side for t in ticks], ["B", "S"])
        self.assertEqual([t.size for t in ticks], [3.0, 7.0])

    def test_classifies_from_bid_ask(self):
        path = self._write(
            "ts,price,size,bid,ask\n"
            "1700000000,100.25,1,100.00,100.25\n"
            "1700000001,100.00,1,100.00,100.25\n")
        self.assertEqual([t.side for t in load_tick_file(path)], ["B", "S"])

    def test_falls_back_to_the_tick_rule(self):
        path = self._write("ts,price,size\n1,100.0,1\n2,100.5,1\n3,100.5,1\n4,100.0,1\n")
        self.assertEqual([t.side for t in load_tick_file(path)], ["?", "B", "B", "S"])

    def test_millisecond_timestamps_are_rescaled(self):
        path = self._write("ts,price\n1700000000000,100.0\n")
        self.assertAlmostEqual(load_tick_file(path)[0].ts, 1_700_000_000.0)

    def test_a_real_file_beats_reconstruction(self):
        path = self._write("ts,price,size,side\n1700000000,100.0,5,buy\n"
                           "1700000030,100.5,5,buy\n")
        ticks, source = get_ticks(bars([(100.0, 101.0, 99.0, 100.5, 10)]), SYM, path)
        self.assertEqual(source, "file")
        self.assertEqual(len(ticks), 2)

    def test_missing_file_falls_back_without_raising(self):
        ticks, source = get_ticks(bars([(100.0, 101.0, 99.0, 100.5, 10)]),
                                  SYM, "/nonexistent/ticks.csv")
        self.assertEqual(source, "reconstructed")
        self.assertTrue(ticks)


class TestFootprint(unittest.TestCase):
    def test_splits_by_side_and_price(self):
        candles = bars([(100.0, 102.0, 99.0, 101.0, 400)])
        ticks = reconstruct(candles, SYM)
        foot = build_footprint(ticks, 60, 0.25, origin=int(candles[0].ts))
        self.assertEqual(len(foot), 1)
        b = foot[0]
        self.assertAlmostEqual(b.volume, sum(t.size for t in ticks))
        self.assertAlmostEqual(
            b.delta, sum(b.ask_vol.values()) - sum(b.bid_vol.values()), places=6)
        self.assertEqual(b.trades, len(ticks))

    def test_origin_aligns_buckets_to_the_candle_grid(self):
        """Bars whose timestamps are not minute-aligned must still get one
        footprint per candle."""
        candles = bars([(100.0, 101.0, 99.5, 100.5, 300),
                        (100.5, 101.5, 100.0, 101.0, 300)], start=1_700_000_037)
        ticks = reconstruct(candles, SYM)
        foot = build_footprint(ticks, 60, 0.25, origin=int(candles[0].ts))
        self.assertEqual(len(foot), 2)
        self.assertEqual([b.ts for b in foot], [c.ts for c in candles])

    def test_volume_at_price_totals(self):
        ticks = reconstruct(bars([(100.0, 102.0, 99.0, 101.0, 400)]), SYM)
        vap = volume_at_price(ticks, 0.25)
        self.assertAlmostEqual(sum(sum(v) for v in vap.values()),
                               sum(t.size for t in ticks), places=6)


class TestBarDerivedHelpers(unittest.TestCase):
    def test_path_split_conserves_volume(self):
        c = bars([(100.0, 103.0, 99.0, 102.0, 600)])[0]
        split = path_volume_split(c, 0.25)
        self.assertAlmostEqual(sum(sum(v) for v in split.values()), 600.0, places=6)

    def test_path_split_of_a_pure_up_bar_is_mostly_buying(self):
        c = bars([(100.0, 104.0, 100.0, 104.0, 500)])[0]
        split = path_volume_split(c, 0.25)
        buy = sum(v[1] for v in split.values())
        self.assertGreater(buy, sum(v[0] for v in split.values()))

    def test_bar_delta_signs(self):
        self.assertGreater(bar_delta(bars([(100.0, 101.0, 100.0, 101.0, 10)])[0]), 0)
        self.assertLess(bar_delta(bars([(101.0, 101.0, 100.0, 100.0, 10)])[0]), 0)

    def test_value_area_brackets_the_point_of_control(self):
        hist = {100.0: 1, 100.25: 5, 100.5: 40, 100.75: 6, 101.0: 2}
        poc, val, vah = _value_area(hist, 0.70)
        self.assertEqual(poc, 100.5)
        self.assertLessEqual(val, poc)
        self.assertGreaterEqual(vah, poc)

    def test_divergence_detects_a_lower_delta_high(self):
        price = [float(p) for p in range(40)]                  # price keeps rising
        # Delta peaks in the first half and never gets back there.
        cum = [float(v) for v in list(range(20)) + list(range(15, -5, -1))]
        found = _divergence(price, cum, look=40)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "short")

    def test_no_divergence_when_they_agree(self):
        price = [float(i) for i in range(40)]
        self.assertIsNone(_divergence(price, list(price), look=40))

    def test_tick_size_lookup(self):
        self.assertEqual(tick_size("NQ=F"), 0.25)
        self.assertEqual(tick_size("YM=F"), 1.0)
        self.assertEqual(tick_size("AAPL"), 0.01)
        self.assertEqual(tick_size("ZZ=F"), 0.25)  # unknown future


class TestModuleContract(unittest.TestCase):
    """Whatever a module concludes, the shape of the conclusion is fixed."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = ctx_for(demo_session(seed=42))

    def test_every_module_both_engines_stays_in_range(self):
        for m in suite.MODULES:
            for engine in (SIMPLE, TICK):
                with self.subTest(module=m.key, engine=engine):
                    r = m.run(self.ctx, engine)
                    self.assertEqual(r.key, m.key)
                    self.assertEqual(r.engine, engine)
                    self.assertGreaterEqual(r.score, -100)
                    self.assertLessEqual(r.score, 100)
                    self.assertGreaterEqual(r.strength, 0)
                    self.assertLessEqual(r.strength, 100)
                    self.assertGreaterEqual(r.confidence, 0)
                    self.assertLessEqual(r.confidence, 1)
                    self.assertTrue(r.state and r.detail)
                    json.dumps(r.to_dict())  # must survive the API boundary

    def test_short_series_degrades_instead_of_raising(self):
        tiny = replace(demo_session(seed=42), candles=demo_session(seed=42).candles[:5])
        ctx = ctx_for(tiny)
        for m in suite.MODULES:
            for engine in (SIMPLE, TICK):
                with self.subTest(module=m.key, engine=engine):
                    r = m.run(ctx, engine)
                    self.assertEqual(r.confidence, 0.0)

    def test_reconstructed_tape_is_never_full_confidence(self):
        self.assertEqual(self.ctx.tick_source, "reconstructed")
        for m in suite.MODULES:
            r = m.run(self.ctx, TICK)
            self.assertLess(r.confidence, 1.0)

    # Per module, the words that may only appear beside a positive score
    # and the words that may only appear beside a negative one. Absorption
    # is the one to watch: "SELLERS ABSORBED" is bullish, because the
    # sellers are the side that lost.
    POLARITY = {
        "trend": (("UP",), ("DOWN",)),
        "delta": (("BUYERS",), ("SELLERS",)),
        "pressure": (("LIFTING", "BID"), ("HITTING", "OFFER")),
        "imbalance": (("BUY STACKS",), ("SELL STACKS",)),
        "absorption": (("SELLERS ABSORBED",), ("BUYERS ABSORBED",)),
        "sweep": (("LOWS SWEPT",), ("HIGHS SWEPT",)),
        "zones": (("ABOVE VALUE",), ("BELOW VALUE",)),
    }

    def test_state_never_contradicts_the_score(self):
        for m in suite.MODULES:
            if not m.directional:
                continue
            bullish, bearish = self.POLARITY[m.key]
            for engine in (SIMPLE, TICK):
                r = m.run(self.ctx, engine)
                with self.subTest(module=m.key, engine=engine, state=r.state):
                    if r.score > 20:
                        for word in bearish:
                            self.assertNotIn(word, r.state)
                    if r.score < -20:
                        for word in bullish:
                            self.assertNotIn(word, r.state)


class TestDetectors(unittest.TestCase):
    """The detectors must fire on the setups they are named after."""

    def _ctx(self, candles, levels=()):
        ctx = Context(symbol=SYM, candles=candles, atr=2.0, tick_sz=0.25,
                      bar_seconds=60, levels=list(levels))
        ctx.ticks, ctx.tick_source = get_ticks(candles, SYM)
        ctx.tick_confidence = 0.6
        ctx.foot = build_footprint(ctx.ticks, 60, 0.25, origin=int(candles[0].ts))
        return ctx

    def test_absorption_fires_on_effort_without_result(self):
        specs = [(100.0, 101.0, 99.0, 100.0, 300) for _ in range(25)]
        # Heavy volume, tiny range, closing on its high: buyers spent and
        # got nothing -> read as sellers holding, i.e. a short-side warning.
        specs.append((100.0, 100.4, 99.9, 100.4, 4000))
        ctx = self._ctx(bars(specs))
        r = suite.BY_KEY["absorption"].simple(ctx)
        self.assertTrue(r.events, "no absorption flagged on an effort/no-result bar")
        self.assertEqual(r.events[-1].side, "short")

    def test_absorption_ignores_heavy_volume_that_went_somewhere(self):
        specs = [(100.0, 101.0, 99.0, 100.0, 300) for _ in range(25)]
        specs.append((100.0, 108.0, 100.0, 108.0, 4000))  # paid for and got 8 points
        r = suite.BY_KEY["absorption"].simple(self._ctx(bars(specs)))
        self.assertFalse(r.events)

    def test_sweep_fires_on_a_pierce_and_reclaim(self):
        level = LevelScore(price=110.0, label="PDH", kind="PDH", user=False, score=80.0)
        specs = [(100.0, 101.0, 99.0, 100.0, 300) for _ in range(30)]
        specs.append((100.0, 109.0, 100.0, 108.0, 300))     # approach from below
        specs.append((108.0, 113.0, 107.0, 107.5, 900))     # through 110 and back
        ctx = self._ctx(bars(specs), [level])
        ctx.atr = 4.0
        r = suite.BY_KEY["sweep"].simple(ctx)
        self.assertTrue(r.events, "a level was pierced and reclaimed but not flagged")
        self.assertEqual(r.events[-1].side, "short")
        self.assertLess(r.score, 0)

    def test_sweep_ignores_a_clean_break(self):
        level = LevelScore(price=110.0, label="PDH", kind="PDH", user=False, score=80.0)
        specs = [(100.0, 101.0, 99.0, 100.0, 300) for _ in range(30)]
        specs.append((100.0, 109.0, 100.0, 108.0, 300))
        specs.append((108.0, 113.0, 107.0, 112.5, 900))     # closed through, held
        ctx = self._ctx(bars(specs), [level])
        ctx.atr = 4.0
        self.assertFalse(suite.BY_KEY["sweep"].simple(ctx).events)

    def test_speed_reads_a_burst_as_fast(self):
        specs = [(100.0, 101.0, 99.0, 100.0, 300) for _ in range(30)]
        specs += [(100.0, 101.0, 99.0, 100.0, 2400) for _ in range(3)]
        r = suite.BY_KEY["speed"].simple(self._ctx(bars(specs)))
        self.assertIn(r.state, ("FAST", "VIOLENT"))
        self.assertEqual(r.score, 0.0, "the speedometer must not vote on direction")

    def test_speed_reads_a_lull_as_slow(self):
        specs = [(100.0, 101.0, 99.0, 100.0, 300) for _ in range(30)]
        specs += [(100.0, 100.2, 99.8, 100.0, 40) for _ in range(3)]
        r = suite.BY_KEY["speed"].simple(self._ctx(bars(specs)))
        self.assertIn(r.state, ("DEAD", "SLOW"))


class TestSuite(unittest.TestCase):
    def test_analyze_is_json_serialisable_and_complete(self):
        out = suite.analyze(symbol=SYM, demo=True, engine="both")
        for key in ("symbol", "price", "tick_source", "bars", "ticks",
                    SIMPLE, TICK, "comparison"):
            self.assertIn(key, out)
        self.assertEqual(len(out[SIMPLE]["modules"]), len(suite.MODULES))
        self.assertEqual(len(out[TICK]["modules"]), len(suite.MODULES))
        json.dumps(out)

    def test_single_engine_skips_the_tape(self):
        out = suite.analyze(symbol=SYM, demo=True, engine=SIMPLE)
        self.assertNotIn(TICK, out)
        self.assertEqual(out["ticks"], 0)

    def test_repeat_runs_agree_exactly(self):
        a = suite.run(ctx_for(demo_session(seed=42)), TICK)
        b = suite.run(ctx_for(demo_session(seed=42)), TICK)
        self.assertEqual(a.composite, b.composite)
        self.assertEqual([r.score for r in a.readings], [r.score for r in b.readings])

    def test_comparison_of_a_result_with_itself_is_total_agreement(self):
        r = suite.run(ctx_for(demo_session(seed=42)), SIMPLE)
        cmp_ = suite.compare(r, r)
        self.assertEqual(cmp_["agreement"], 100.0)
        self.assertEqual(cmp_["flips"], [])
        self.assertEqual(cmp_["composite_gap"], 0.0)

    def test_dead_tape_damps_conviction(self):
        quiet = [(100.0, 100.05, 99.95, 100.0, 5) for _ in range(60)]
        ctx = Context(symbol=SYM, candles=bars(quiet), atr=1.0, tick_sz=0.25,
                      bar_seconds=60)
        ctx.ticks, ctx.tick_source = get_ticks(ctx.candles, SYM)
        ctx.tick_confidence = 0.6
        ctx.foot = build_footprint(ctx.ticks, 60, 0.25, origin=int(ctx.candles[0].ts))
        result = suite.run(ctx, SIMPLE)
        self.assertLess(result.conviction, 40)

    def test_composite_labels_track_the_score(self):
        self.assertEqual(suite.label_for(60), "STRONG LONG")
        self.assertEqual(suite.label_for(25), "LONG")
        self.assertEqual(suite.label_for(0), "NEUTRAL")
        self.assertEqual(suite.label_for(-25), "SHORT")
        self.assertEqual(suite.label_for(-60), "STRONG SHORT")

    def test_every_module_key_carries_a_weight(self):
        for m in suite.MODULES:
            if m.directional:
                self.assertIn(m.key, suite.WEIGHTS)


class TestCli(unittest.TestCase):
    def test_json_mode_runs(self):
        from algobox.__main__ import main
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["--demo", "--json", "--engine", "simple"]), 0)
        self.assertIn("composite", json.loads(buf.getvalue())[SIMPLE])

    def test_panel_mode_runs(self):
        from algobox.__main__ import main
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(main(["--demo", "--no-color"]), 0)
        out = buf.getvalue()
        self.assertIn("ALGOBOX", out)
        self.assertIn("COMPOSITE", out)


if __name__ == "__main__":
    unittest.main()
