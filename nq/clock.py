"""Exchange time without third-party packages.

Everything in this toolkit that cares about *when* — the RTH open, the
Globex session boundary, news blackouts, the trader's flatten-by cutoff —
needs US Eastern or Central time with the real daylight-saving rule, not
the month-based guess the first version shipped with (which put the first
two weeks of March and the first week of November an hour off).

`zoneinfo` is used when the platform has a tz database. Windows Python has
none unless the `tzdata` wheel is installed, and this project promises a
stdlib-only deploy, so the US rule (second Sunday of March 02:00 local to
first Sunday of November 02:00 local, in force since 2007) is implemented
as the fallback. Both paths give identical results for any date after 2007.
"""

from __future__ import annotations

import calendar
import time
from datetime import datetime, timedelta, timezone

try:  # tz database present (Linux, macOS, Windows + tzdata)
    from zoneinfo import ZoneInfo

    _EASTERN = ZoneInfo("America/New_York")
    _CENTRAL = ZoneInfo("America/Chicago")
    # Probe: a bad or missing database raises here, not later at 09:29.
    datetime(2026, 3, 8, 12, tzinfo=_EASTERN).utcoffset()
except Exception:  # ZoneInfoNotFoundError, ImportError, corrupt tzdata
    _EASTERN = _CENTRAL = None

_STD_OFFSET_ET = -5   # hours; EST
_STD_OFFSET_CT = -6   # CST

# Regular trading hours for CME equity index futures, in Eastern time.
RTH_OPEN_MIN = 9 * 60 + 30     # 09:30 ET
RTH_CLOSE_MIN = 16 * 60        # 16:00 ET (cash close; 16:00-17:00 is post)
GLOBEX_OPEN_MIN = 18 * 60      # 18:00 ET — the CME trade date rolls here


def _nth_sunday(year: int, month: int, n: int) -> int:
    """Day-of-month of the n-th Sunday."""
    first = calendar.weekday(year, month, 1)  # Mon=0 .. Sun=6
    first_sunday = 1 + (6 - first) % 7
    return first_sunday + 7 * (n - 1)


def us_dst_active(utc_dt: datetime) -> bool:
    """US daylight-saving rule (post-2007) evaluated against a UTC instant.

    DST starts 02:00 local standard time on the second Sunday of March
    (07:00 UTC for Eastern) and ends 02:00 local daylight time on the first
    Sunday of November (06:00 UTC for Eastern). Central is one hour later in
    UTC terms; the one-hour window where the two differ is handled by the
    callers below passing the right standard offset.
    """
    return _us_dst_active(utc_dt, _STD_OFFSET_ET)


def _us_dst_active(utc_dt: datetime, std_offset_hours: int) -> bool:
    y = utc_dt.year
    start = datetime(y, 3, _nth_sunday(y, 3, 2), 2, tzinfo=timezone.utc) - timedelta(hours=std_offset_hours)
    end = datetime(y, 11, _nth_sunday(y, 11, 1), 2, tzinfo=timezone.utc) - timedelta(hours=std_offset_hours + 1)
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return start <= utc_dt < end


def _fallback_local(ts: float, std_offset_hours: int) -> datetime:
    utc_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    offset = std_offset_hours + (1 if _us_dst_active(utc_dt, std_offset_hours) else 0)
    return utc_dt.astimezone(timezone(timedelta(hours=offset)))


def eastern(ts: float) -> datetime:
    """Aware datetime in US Eastern time for a unix timestamp."""
    if _EASTERN is not None:
        return datetime.fromtimestamp(ts, tz=_EASTERN)
    return _fallback_local(ts, _STD_OFFSET_ET)


def central(ts: float) -> datetime:
    """Aware datetime in US Central (CME) time for a unix timestamp."""
    if _CENTRAL is not None:
        return datetime.fromtimestamp(ts, tz=_CENTRAL)
    return _fallback_local(ts, _STD_OFFSET_CT)


def eastern_offset_hours(ts: float) -> int:
    """UTC offset of Eastern time at `ts` (-4 in summer, -5 in winter)."""
    return int(eastern(ts).utcoffset().total_seconds() // 3600)


def et_minutes(ts: float) -> int:
    """Minutes since midnight, Eastern time."""
    d = eastern(ts)
    return d.hour * 60 + d.minute


def et_date(ts: float):
    """Calendar date in Eastern time."""
    return eastern(ts).date()


def et_datetime(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """An aware Eastern-time datetime built from wall-clock components."""
    if _EASTERN is not None:
        return datetime(year, month, day, hour, minute, tzinfo=_EASTERN)
    # Fallback: resolve the offset from the instant assuming standard time,
    # then correct if that instant is in DST. Around the 02:00 transitions
    # this picks the post-transition offset, which is what matters for
    # 08:30 releases and 09:30 opens.
    guess = datetime(year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=_STD_OFFSET_ET)))
    if _us_dst_active(guess.astimezone(timezone.utc), _STD_OFFSET_ET):
        return datetime(year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=_STD_OFFSET_ET + 1)))
    return guess


def et_ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(et_datetime(year, month, day, hour, minute).timestamp())


def in_rth(ts: float) -> bool:
    """True when the bar starting at `ts` is inside RTH (09:30-16:00 ET),
    any weekday. Weekends can't have bars so are not special-cased."""
    return RTH_OPEN_MIN <= et_minutes(ts) < RTH_CLOSE_MIN


def session_start(ts: float) -> int:
    """Globex open (18:00 ET) that begins the CME trade date containing `ts`.

    A bar at 17:30 ET falls in the closed hour and is attributed to the
    session that just ended, which is what a chart of "today" wants.
    """
    d = eastern(ts)
    if d.hour * 60 + d.minute < GLOBEX_OPEN_MIN:
        d = d - timedelta(days=1)
    return et_ts(d.year, d.month, d.day, 18, 0)


def trade_date(ts: float):
    """The CME trade date for `ts`: the Eastern calendar day the session's
    RTH falls on (Sunday 18:00 onward is Monday's trade date)."""
    return et_date(session_start(ts) + 12 * 3600)


def rth_open_ts(ts: float) -> int:
    """09:30 ET on the trade date of `ts`."""
    d = trade_date(ts)
    return et_ts(d.year, d.month, d.day, 9, 30)


def rth_close_ts(ts: float) -> int:
    d = trade_date(ts)
    return et_ts(d.year, d.month, d.day, 16, 0)


def central_hhmm(ts: float | None = None) -> tuple[int, int]:
    """(hour, minute) in Chicago time — the clock prop-firm cutoffs run on."""
    d = central(ts if ts is not None else time.time())
    return d.hour, d.minute
