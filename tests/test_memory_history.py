"""
Pins the living-memory bucket cascade in rtk_sf/memory/.

The invariants that matter: nothing is lost on the way down the hierarchy, a
weekly view survives a month boundary (the original design jumped straight from
72 hours to current_month, which made "last week" unanswerable on the 1st), and a
corrupt or absent history file degrades to an empty store instead of raising —
these run inside CLI hooks that must never break a turn.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from rtk_sf.memory.history import HistoryManager
from rtk_sf.memory.time_utils import (
    day_series,
    fiscal_quarter_key,
    fiscal_year_key,
    is_within_days,
    parse_ts,
)


def _utc(year, month, day, hour=12):
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fiscal arithmetic
# ---------------------------------------------------------------------------


def test_japanese_fiscal_year_starts_in_april():
    assert fiscal_year_key(_utc(2026, 3, 31)) == "FY2025"
    assert fiscal_year_key(_utc(2026, 4, 1)) == "FY2026"


def test_fiscal_quarters_follow_the_fiscal_start():
    assert fiscal_quarter_key(_utc(2026, 4, 1)) == "FY2026-Q1"
    assert fiscal_quarter_key(_utc(2026, 9, 29)) == "FY2026-Q2"
    assert fiscal_quarter_key(_utc(2027, 1, 5)) == "FY2026-Q4"


def test_calendar_fiscal_year_is_configurable():
    assert fiscal_year_key(_utc(2026, 3, 31), start_month=1) == "FY2026"
    assert fiscal_quarter_key(_utc(2026, 3, 31), start_month=1) == "FY2026-Q1"


def test_day_series_is_oldest_first_and_inclusive():
    series = day_series(_utc(2026, 9, 29), days=7)
    assert series[0] == "2026-09-23"
    assert series[-1] == "2026-09-29"
    assert len(series) == 7


def test_is_within_days_window():
    now = _utc(2026, 9, 29)
    assert is_within_days(_utc(2026, 9, 24), now, 7)
    assert not is_within_days(_utc(2026, 9, 1), now, 7)


# ---------------------------------------------------------------------------
# Roll-up cascade
# ---------------------------------------------------------------------------


def test_events_cascade_through_every_bucket(tmp_path):
    manager = HistoryManager(tmp_path)
    now = _utc(2026, 9, 29)

    manager.record_turn("today", files=["a.py"], insertions=10, timestamp=now)
    manager.record_turn("five days ago", files=["b.py"], insertions=3, timestamp=now - timedelta(days=5))
    manager.record_turn("45 days ago", files=["c.py"], insertions=7, timestamp=now - timedelta(days=45))
    manager.record_turn("500 days ago", files=["d.py"], insertions=1, timestamp=now - timedelta(days=500))
    manager.roll_up(now=now)

    buckets = manager.store["buckets"]
    assert [e["summary"] for e in buckets["recent_3_days"]] == ["today"]
    assert [s["period"] for s in buckets["current_month"]] == ["2026-09-24"]
    assert "FY2026-Q2" in buckets["fiscal_quarters"]
    assert "FY2025" in buckets["fiscal_years"]


def test_rollup_preserves_totals(tmp_path):
    manager = HistoryManager(tmp_path)
    now = _utc(2026, 9, 29)
    for offset in range(1, 6):
        manager.record_turn(
            f"turn {offset}",
            files=[f"file{offset}.py"],
            insertions=offset,
            deletions=1,
            timestamp=now - timedelta(days=offset + 3),
        )
    manager.roll_up(now=now)

    totals = sum(s["insertions"] for s in manager.store["buckets"]["current_month"])
    assert totals == 1 + 2 + 3 + 4 + 5
    assert sum(s["events"] for s in manager.store["buckets"]["current_month"]) == 5


def test_day_granularity_survives_a_month_boundary(tmp_path):
    """The reason last_7_days exists: on Oct 2, late-September days must remain."""
    manager = HistoryManager(tmp_path)
    for day in (27, 28, 29, 30):
        manager.record_turn(f"sept {day}", files=["x.py"], insertions=1, timestamp=_utc(2026, 9, day))

    manager.roll_up(now=_utc(2026, 10, 2, 9))
    periods = [s["period"] for s in manager.store["buckets"]["current_month"]]
    assert "2026-09-27" in periods
    assert manager.store["buckets"]["fiscal_quarters"] == {}

    # Once the week has fully passed, the days fold into the quarter.
    manager.roll_up(now=_utc(2026, 10, 20, 9))
    assert "FY2026-Q2" in manager.store["buckets"]["fiscal_quarters"]
    assert manager.store["buckets"]["current_month"] == []


# ---------------------------------------------------------------------------
# Weekly series
# ---------------------------------------------------------------------------


def test_week_series_is_zero_filled_and_ordered(tmp_path):
    manager = HistoryManager(tmp_path)
    now = _utc(2026, 9, 29)
    manager.record_turn("today", files=["a.py"], insertions=2, timestamp=now)
    manager.record_turn("four days ago", files=["b.py"], insertions=5, timestamp=now - timedelta(days=4))
    manager.record_turn("three weeks ago", files=["c.py"], timestamp=now - timedelta(days=21))

    rows = manager.week_series(now=now)
    assert len(rows) == 7
    assert [r["period"] for r in rows] == sorted(r["period"] for r in rows)
    assert sum(r["events"] for r in rows) == 2  # the 3-week-old turn is excluded
    quiet = [r for r in rows if r["events"] == 0]
    assert quiet and all(r["insertions"] == 0 for r in quiet)


def test_week_series_merges_live_events_and_day_summaries(tmp_path):
    manager = HistoryManager(tmp_path)
    now = _utc(2026, 9, 29)
    manager.record_turn("live", files=["a.py"], insertions=4, timestamp=now)
    manager.record_turn("rolled up", files=["b.py"], insertions=6, timestamp=now - timedelta(days=5))
    manager.roll_up(now=now)

    rows = {r["period"]: r for r in manager.week_series(now=now)}
    assert rows["2026-09-29"]["insertions"] == 4   # still a live event
    assert rows["2026-09-24"]["insertions"] == 6   # already a day summary


def test_timeline_exposes_the_week_scope(tmp_path):
    manager = HistoryManager(tmp_path)
    manager.record_turn("work", files=["a.py"])
    assert len(manager.timeline("last_7_days")["last_7_days"]) == 7
    assert "last_7_days" in manager.timeline("all")["buckets"]


def test_timeline_rejects_an_unknown_scope(tmp_path):
    with pytest.raises(ValueError):
        HistoryManager(tmp_path).timeline("yesterday")


# ---------------------------------------------------------------------------
# Persistence and resilience
# ---------------------------------------------------------------------------


def test_history_round_trips_through_disk(tmp_path):
    HistoryManager(tmp_path).record_turn("first", files=["a.py"], insertions=1)
    reopened = HistoryManager(tmp_path)
    assert [e["summary"] for e in reopened.store["buckets"]["recent_3_days"]] == ["first"]


def test_unknown_top_level_keys_are_preserved(tmp_path):
    """The post-turn hook stores its git snapshot alongside the buckets."""
    manager = HistoryManager(tmp_path)
    manager.store["snapshot"] = {"head": "abc123", "files": {"a.py": [1, 0]}}
    manager.save()
    assert HistoryManager(tmp_path).store["snapshot"]["head"] == "abc123"


def test_corrupt_history_degrades_to_empty(tmp_path):
    rtk = tmp_path / ".rtk-sf"
    rtk.mkdir()
    (rtk / "history.json").write_text("{not json at all", encoding="utf-8")

    manager = HistoryManager(tmp_path)
    assert manager.store["buckets"]["recent_3_days"] == []
    manager.record_turn("recovered", files=["a.py"])
    assert len(manager.store["buckets"]["recent_3_days"]) == 1


def test_missing_history_is_not_created_by_reading(tmp_path):
    manager = HistoryManager(tmp_path)
    assert not manager.path.exists()
    assert manager.recent_digest() == ""


def test_stored_fiscal_start_month_wins_over_the_default(tmp_path):
    HistoryManager(tmp_path, fiscal_start_month=1).record_turn("calendar year", files=["a.py"])
    assert HistoryManager(tmp_path).fiscal_start_month == 1


# ---------------------------------------------------------------------------
# Digest budget
# ---------------------------------------------------------------------------


def test_recent_digest_respects_its_character_budget(tmp_path):
    manager = HistoryManager(tmp_path)
    for i in range(60):
        manager.record_turn(f"turn {i} with a deliberately long summary line", files=[f"f{i}.py"])

    digest = manager.recent_digest(max_chars=600)
    assert len(digest) <= 620  # budget plus the truncation marker
    assert digest.startswith("rtk-sf memory")
    assert "Hot files:" in digest


def test_recent_digest_excludes_turns_that_have_aged_out(tmp_path):
    """The behaviour that made the ordering test rot, pinned deliberately.

    `recent_digest` rolls up first, so a turn more than 72h behind the clock is
    no longer in `recent_3_days` and cannot appear. That is correct, but it is
    also why a test holding absolute timestamps silently turns into an
    assertion about an empty string as real time moves on.
    """
    manager = HistoryManager(tmp_path)
    now = parse_ts("2026-09-29T10:00:00+00:00")
    manager.record_turn("inside the window", files=["a.py"], timestamp=now - timedelta(hours=2))
    manager.record_turn("aged out", files=["b.py"], timestamp=now - timedelta(days=5))

    digest = manager.recent_digest(now=now)

    assert "inside the window" in digest
    assert "aged out" not in digest


def test_recent_digest_is_empty_once_everything_has_aged_out(tmp_path):
    manager = HistoryManager(tmp_path)
    now = parse_ts("2026-09-29T10:00:00+00:00")
    manager.record_turn("ancient", files=["a.py"], timestamp=now - timedelta(days=10))

    assert manager.recent_digest(now=now) == ""


def test_recent_digest_defaults_to_the_wall_clock(tmp_path):
    """Omitting `now` must keep working — it is how the pre-turn hook calls it."""
    manager = HistoryManager(tmp_path)
    manager.record_turn("just happened", files=["a.py"])

    assert "just happened" in manager.recent_digest()


def test_recent_digest_is_newest_first(tmp_path):
    """Both turns must stay inside the 72h window for ordering to be observable.

    This test used to pin `now` to an absolute date. `recent_digest` rolls up
    before rendering, so once that date fell more than 72h behind the real
    clock both turns aged out, the digest came back empty, and the assertion
    failed with "substring not found" — a failure that says nothing about
    ordering. The clock is injected instead, so the window is fixed relative
    to the timestamps and the test cannot rot.
    """
    manager = HistoryManager(tmp_path)
    now = parse_ts("2026-09-29T10:00:00+00:00")
    manager.record_turn("older", files=["a.py"], timestamp=now - timedelta(hours=2))
    manager.record_turn("newer", files=["b.py"], timestamp=now)

    digest = manager.recent_digest(now=now)
    assert "newer" in digest and "older" in digest
    assert digest.index("newer") < digest.index("older")
