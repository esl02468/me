import unittest
from datetime import datetime, timezone

from nq import clock


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp()


class DstRule(unittest.TestCase):
    def test_transitions_2026(self):
        # DST 2026: starts Sun Mar 8 02:00 EST (07:00 UTC), ends Sun Nov 1 02:00 EDT (06:00 UTC).
        self.assertEqual(clock.eastern_offset_hours(utc(2026, 3, 8, 6, 59)), -5)
        self.assertEqual(clock.eastern_offset_hours(utc(2026, 3, 8, 7, 0)), -4)
        self.assertEqual(clock.eastern_offset_hours(utc(2026, 11, 1, 5, 59)), -4)
        self.assertEqual(clock.eastern_offset_hours(utc(2026, 11, 1, 6, 0)), -5)

    def test_month_guess_was_wrong_in_early_march(self):
        # March 5 is standard time; the old "EDT Apr-Oct, EST otherwise" rule
        # got that right by luck but put March 20 an hour off.
        self.assertEqual(clock.eastern_offset_hours(utc(2026, 3, 20, 12)), -4)
        self.assertEqual(clock.eastern_offset_hours(utc(2026, 11, 3, 12)), -5)

    def test_fallback_matches_zoneinfo(self):
        if clock._EASTERN is None:
            self.skipTest("no tz database here; the fallback is the only path")
        for ts in range(int(utc(2024, 1, 1)), int(utc(2028, 1, 1)), 7 * 3600 + 601):
            a = clock.eastern(ts)
            b = clock._fallback_local(ts, -5)
            self.assertEqual((a.year, a.month, a.day, a.hour, a.minute),
                             (b.year, b.month, b.day, b.hour, b.minute), ts)
            a = clock.central(ts)
            b = clock._fallback_local(ts, -6)
            self.assertEqual((a.hour, a.minute), (b.hour, b.minute), ts)

    def test_et_datetime_fallback(self):
        saved = clock._EASTERN
        try:
            clock._EASTERN = None
            self.assertEqual(clock.et_ts(2026, 9, 25, 9, 30), int(utc(2026, 9, 25, 13, 30)))
            self.assertEqual(clock.et_ts(2026, 12, 9, 14, 0), int(utc(2026, 12, 9, 19, 0)))
        finally:
            clock._EASTERN = saved


class Sessions(unittest.TestCase):
    def test_globex_boundary(self):
        # Friday 09:29 ET belongs to the session that opened Thursday 18:00 ET.
        ts = clock.et_ts(2026, 9, 25, 9, 29)
        self.assertEqual(clock.session_start(ts), clock.et_ts(2026, 9, 24, 18, 0))
        self.assertEqual(clock.trade_date(ts).isoformat(), "2026-09-25")
        self.assertFalse(clock.in_rth(ts))
        self.assertTrue(clock.in_rth(ts + 60))

    def test_sunday_evening_is_monday(self):
        ts = clock.et_ts(2026, 9, 27, 19, 0)  # Sunday 19:00 ET
        self.assertEqual(clock.trade_date(ts).isoformat(), "2026-09-28")
        self.assertEqual(clock.rth_open_ts(ts), clock.et_ts(2026, 9, 28, 9, 30))
        self.assertEqual(clock.rth_close_ts(ts), clock.et_ts(2026, 9, 28, 16, 0))

    def test_settlement_hour_belongs_to_the_day_that_ended(self):
        ts = clock.et_ts(2026, 9, 25, 17, 30)
        self.assertEqual(clock.session_start(ts), clock.et_ts(2026, 9, 24, 18, 0))
        self.assertFalse(clock.in_rth(clock.et_ts(2026, 9, 25, 16, 0)))
        self.assertTrue(clock.in_rth(clock.et_ts(2026, 9, 25, 15, 59)))

    def test_central_clock(self):
        self.assertEqual(clock.central_hhmm(utc(2026, 9, 25, 20, 56)), (15, 56))
        self.assertEqual(clock.central_hhmm(utc(2026, 12, 9, 21, 56)), (15, 56))


if __name__ == "__main__":
    unittest.main()
