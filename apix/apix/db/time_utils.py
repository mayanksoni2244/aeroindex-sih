"""
Time handling.

Constitution / edge case §11.24: every timestamp in APIx is stored and compared
in **IST (Asia/Kolkata, UTC+05:30)**. India does not observe DST, so IST is a
fixed offset and there is no ambiguous-hour problem — but we still pin it
explicitly rather than relying on the host machine's locale, because a demo
laptop in another timezone must produce identical results.

The advance-purchase window is derived from *dates* (travel_date minus the
scrape cycle date), never from a raw timestamp subtraction, so a scrape running
at 23:55 IST cannot silently shift a T+7 into a T+6.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def now_ist() -> datetime:
    """Current wall-clock time in IST (timezone-aware)."""
    return datetime.now(IST)


def today_ist() -> date:
    """Current calendar date in IST."""
    return now_ist().date()


def to_ist(dt: datetime) -> datetime:
    """Coerce any datetime to IST. Naive datetimes are assumed to already be IST."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def advance_window_days(travel_date: date, cycle_date: date) -> int:
    """
    Days between the scrape cycle date and the travel date.

    Negative values mean the travel date is in the past (a stale queue entry),
    which callers must skip with a logged reason — edge case §11.25.
    """
    return (travel_date - cycle_date).days
