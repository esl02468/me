"""Scheduled-news guard.

High-impact releases (CPI, FOMC, NFP...) routinely steamroll intraday
reversal setups, and several prop firms require being flat through them.
This module answers two questions: are we inside a blackout window now,
and what's coming up?

Event sources:
  1. news.json in the repo root — you maintain the dated list:
       {"events": [{"time": "2026-08-12T08:30:00-04:00", "label": "CPI"}]}
  2. A built-in recurring rule for NFP (first Friday, 08:30 ET).

Windows default to 5 minutes before through 10 minutes after the release
(NQ_NEWS_BEFORE_MIN / NQ_NEWS_AFTER_MIN override). All comparisons in UTC;
event times carry their own offsets. This is a guard, not a calendar feed —
keep news.json current from your economic calendar of choice.
"""

from __future__ import annotations

import calendar as _cal
import json
import os
import time
from datetime import datetime, timedelta, timezone

NEWS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "news.json")
BEFORE_MIN = float(os.environ.get("NQ_NEWS_BEFORE_MIN", "5"))
AFTER_MIN = float(os.environ.get("NQ_NEWS_AFTER_MIN", "10"))

# US Eastern offset by month (approximation: EDT Mar-Oct, EST otherwise).
def _et_offset(month: int) -> int:
    return -4 if 3 < month < 11 else -5


def _nfp_events(now: datetime) -> list[tuple[datetime, str]]:
    """First Friday of this and next month, 08:30 ET."""
    out = []
    for add in (0, 1):
        year = now.year + (now.month + add - 1) // 12
        month = (now.month + add - 1) % 12 + 1
        first_friday = next(
            d for d in range(1, 8)
            if _cal.weekday(year, month, d) == _cal.FRIDAY
        )
        dt = datetime(year, month, first_friday, 8, 30,
                      tzinfo=timezone(timedelta(hours=_et_offset(month))))
        out.append((dt, "NFP (jobs report)"))
    return out


def _file_events() -> list[tuple[datetime, str]]:
    if not os.path.exists(NEWS_PATH):
        return []
    try:
        with open(NEWS_PATH) as f:
            raw = json.load(f)
        out = []
        for e in raw.get("events", []):
            out.append((datetime.fromisoformat(e["time"]), str(e.get("label", "news"))))
        return out
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return []


def upcoming(hours: float = 24.0) -> list[dict]:
    """Events within the next `hours`, soonest first."""
    now = datetime.now(timezone.utc)
    events = _file_events() + _nfp_events(now)
    out = []
    for dt, label in events:
        delta = (dt - now).total_seconds()
        if -AFTER_MIN * 60 <= delta <= hours * 3600:
            out.append({"ts": int(dt.timestamp()), "label": label,
                        "minutes_away": round(delta / 60, 1)})
    out.sort(key=lambda e: e["ts"])
    return out


def in_blackout(now_ts: float | None = None) -> str | None:
    """Label of the release we're inside the window of, else None."""
    now = now_ts or time.time()
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    for dt, label in _file_events() + _nfp_events(now_dt):
        start = dt.timestamp() - BEFORE_MIN * 60
        end = dt.timestamp() + AFTER_MIN * 60
        if start <= now <= end:
            return label
    return None
