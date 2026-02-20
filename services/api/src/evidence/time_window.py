from __future__ import annotations

from datetime import datetime, timedelta, timezone


def clamp_time_window(
    *,
    start: datetime,
    end: datetime,
    max_minutes: int,
) -> tuple[datetime, datetime, bool]:
    """
    Clamp a time window to at most max_minutes. Returns (start, end, clamped?).

    Assumes timezone-aware datetimes.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start/end must be timezone-aware")

    if end <= start:
        raise ValueError("end must be after start")

    max_delta = timedelta(minutes=max_minutes)
    actual = end - start

    if actual <= max_delta:
        return start, end, False

    # Keep the most recent window (common for incident debugging)
    new_start = end - max_delta
    return new_start, end, True


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
