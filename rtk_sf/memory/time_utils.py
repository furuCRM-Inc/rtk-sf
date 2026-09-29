"""
time_utils.py — fiscal-aware time bucketing for the rtk-sf living memory.

The history file keeps four hierarchical buckets. This module owns the pure
date arithmetic that decides which bucket a timestamp belongs to:

    recent_3_days    < ROLLUP_HOURS old            (full-detail events)
    current_month    same calendar month as now,   (per-day summaries)
                     or inside the trailing week
    last_7_days      derived per-day series over the trailing week
    fiscal_quarters  current fiscal year           (per-quarter summaries)
    fiscal_years     everything older              (per-year summaries)

Fiscal year start defaults to April (Japanese convention: FY2026 runs
2026-04-01 → 2027-03-31). Pass a different ``start_month`` for calendar-year
or US-style fiscal years.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Events older than this are rolled up out of recent_3_days.
ROLLUP_HOURS = 72

# Day summaries are kept at day granularity for at least this long, so a
# weekly view still exists after a month boundary rolls the month up.
WEEK_DAYS = 7

# Japanese fiscal year: April 1 → March 31.
DEFAULT_FISCAL_START_MONTH = 4


def utcnow() -> datetime:
    """Current time, timezone-aware, UTC."""
    return datetime.now(timezone.utc)


def format_ts(dt: datetime) -> str:
    """Serialize a datetime as an ISO-8601 UTC string."""
    return _as_utc(dt).isoformat()


def parse_ts(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning None if it is unusable."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _as_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


def _as_utc(dt: datetime) -> datetime:
    """Attach UTC to a naive datetime, or convert an aware one to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def day_key(dt: datetime) -> str:
    """Bucket key for a single day, e.g. '2026-09-29'."""
    return _as_utc(dt).strftime("%Y-%m-%d")


def month_key(dt: datetime) -> str:
    """Bucket key for a calendar month, e.g. '2026-09'."""
    return _as_utc(dt).strftime("%Y-%m")


def fiscal_year_number(dt: datetime, start_month: int = DEFAULT_FISCAL_START_MONTH) -> int:
    """
    Fiscal year a date falls in.

    With start_month=4, 2026-03-31 is FY2025 and 2026-04-01 is FY2026.
    """
    dt = _as_utc(dt)
    return dt.year if dt.month >= start_month else dt.year - 1


def fiscal_year_key(dt: datetime, start_month: int = DEFAULT_FISCAL_START_MONTH) -> str:
    """Bucket key for a fiscal year, e.g. 'FY2026'."""
    return f"FY{fiscal_year_number(dt, start_month)}"


def fiscal_quarter_number(dt: datetime, start_month: int = DEFAULT_FISCAL_START_MONTH) -> int:
    """Quarter index (1-4) within the fiscal year."""
    dt = _as_utc(dt)
    months_in = (dt.month - start_month) % 12
    return months_in // 3 + 1


def fiscal_quarter_key(dt: datetime, start_month: int = DEFAULT_FISCAL_START_MONTH) -> str:
    """Bucket key for a fiscal quarter, e.g. 'FY2026-Q2'."""
    return f"{fiscal_year_key(dt, start_month)}-Q{fiscal_quarter_number(dt, start_month)}"


def is_recent(dt: datetime, now: datetime | None = None, hours: int = ROLLUP_HOURS) -> bool:
    """True while a timestamp is still inside the full-detail window."""
    now = now or utcnow()
    return _as_utc(dt) >= _as_utc(now) - timedelta(hours=hours)


def is_within_days(dt: datetime, now: datetime | None = None, days: int = 7) -> bool:
    """True while a timestamp is inside a trailing window of whole days."""
    now = now or utcnow()
    return _as_utc(dt) >= _as_utc(now) - timedelta(days=days)


def day_series(now: datetime | None = None, days: int = 7) -> list[str]:
    """Day keys for a trailing window, oldest first — the x-axis of a week view."""
    now = _as_utc(now or utcnow())
    return [day_key(now - timedelta(days=offset)) for offset in range(days - 1, -1, -1)]


def is_current_month(dt: datetime, now: datetime | None = None) -> bool:
    """True when a timestamp falls in the same calendar month as ``now``."""
    now = now or utcnow()
    return month_key(dt) == month_key(now)


def is_current_fiscal_year(
    dt: datetime,
    now: datetime | None = None,
    start_month: int = DEFAULT_FISCAL_START_MONTH,
) -> bool:
    """True when a timestamp falls in the same fiscal year as ``now``."""
    now = now or utcnow()
    return fiscal_year_number(dt, start_month) == fiscal_year_number(now, start_month)
