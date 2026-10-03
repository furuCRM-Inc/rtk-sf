"""
history.py — the rtk-sf living memory store (.rtk-sf/history.json).

Keeps a hierarchical, self-compacting record of what happened in a project so
an agent can answer "what have we been doing?" without re-reading git log or
replaying a transcript.

Bucket cascade (each level is an aggregate of the one before it):

    recent_3_days   full-detail turn events, < 72h old
    current_month   one summary per day, for the current calendar month — a day
                    inside the trailing week is kept here even after the month
                    turns over, so a weekly view never loses granularity
    last_7_days     derived read-only per-day series over the trailing week
    fiscal_quarters one summary per quarter, for the current fiscal year
    fiscal_years    one summary per fiscal year, for everything older

`record_turn()` appends an event and then rolls up, so the file compacts itself
on write and never grows without bound. All reads/writes are defensive: a
missing or corrupt history file yields an empty store rather than an exception,
because this runs inside CLI hooks that must never break a turn.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from rtk_sf.memory.time_utils import (
    DEFAULT_FISCAL_START_MONTH,
    WEEK_DAYS,
    day_key,
    day_series,
    fiscal_quarter_key,
    fiscal_year_key,
    format_ts,
    is_current_fiscal_year,
    is_current_month,
    is_recent,
    is_within_days,
    parse_ts,
    utcnow,
)

logger = logging.getLogger(__name__)

HISTORY_FILE = "history.json"
SCHEMA_VERSION = 1

# Caps that keep the file small enough to load into a hook on every turn.
MAX_RECENT_EVENTS = 400
MAX_TOP_FILES = 10
MAX_HIGHLIGHTS = 8

SCOPES = (
    "recent_3_days",
    "last_7_days",
    "current_month",
    "fiscal_quarters",
    "fiscal_years",
    "all",
)


def _empty_store(fiscal_start_month: int) -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "fiscal_year_start_month": fiscal_start_month,
        "updated_at": None,
        "buckets": {
            "recent_3_days": [],
            "current_month": [],
            "fiscal_quarters": {},
            "fiscal_years": {},
        },
    }


def _blank_summary(period: str) -> dict[str, Any]:
    return {
        "period": period,
        "events": 0,
        "insertions": 0,
        "deletions": 0,
        "files_touched": 0,
        "top_files": [],
        "highlights": [],
    }


class HistoryManager:
    """Read/write access to a project's `.rtk-sf/history.json`."""

    def __init__(
        self,
        project_root: str | Path = ".",
        fiscal_start_month: int = DEFAULT_FISCAL_START_MONTH,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.rtk_dir = self.project_root / ".rtk-sf"
        self.path = self.rtk_dir / HISTORY_FILE
        self._fiscal_start_month = fiscal_start_month
        self._store: dict[str, Any] | None = None

    # -- persistence --------------------------------------------------------

    @property
    def fiscal_start_month(self) -> int:
        """
        Fiscal year start month, as recorded in the file when one exists.

        Reading this loads the store, so a caller never sees the constructor
        default for a project whose history was written with another setting.
        """
        if self._store is None:
            self._store = self.load()
        return self._fiscal_start_month

    @fiscal_start_month.setter
    def fiscal_start_month(self, value: int) -> None:
        self._fiscal_start_month = value

    @property
    def store(self) -> dict[str, Any]:
        if self._store is None:
            self._store = self.load()
        return self._store

    def load(self) -> dict[str, Any]:
        """Load the history file, falling back to an empty store."""
        if not self.path.exists():
            return _empty_store(self._fiscal_start_month)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Unreadable history file %s (%s) — starting fresh", self.path, exc)
            return _empty_store(self._fiscal_start_month)
        if not isinstance(data, dict) or "buckets" not in data:
            return _empty_store(self._fiscal_start_month)

        # Honour a fiscal start month already recorded in the file so buckets
        # stay consistent with how they were originally computed.
        stored_start = data.get("fiscal_year_start_month")
        if isinstance(stored_start, int) and 1 <= stored_start <= 12:
            self._fiscal_start_month = stored_start

        buckets = data.setdefault("buckets", {})
        buckets.setdefault("recent_3_days", [])
        buckets.setdefault("current_month", [])
        buckets.setdefault("fiscal_quarters", {})
        buckets.setdefault("fiscal_years", {})
        return data

    def save(self) -> Path:
        """Write the store atomically so a killed hook cannot truncate it."""
        store = self.store
        store["version"] = SCHEMA_VERSION
        store["fiscal_year_start_month"] = self._fiscal_start_month
        store["updated_at"] = format_ts(utcnow())

        self.rtk_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(store, ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.rtk_dir), prefix=".history-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, self.path)
        except OSError:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return self.path

    # -- writing ------------------------------------------------------------

    def record_turn(
        self,
        summary: str,
        files: list[str] | None = None,
        insertions: int = 0,
        deletions: int = 0,
        commit: str | None = None,
        tools: list[str] | None = None,
        session_id: str | None = None,
        kind: str = "turn",
        timestamp: Any = None,
    ) -> dict[str, Any]:
        """
        Append one event to `recent_3_days`, roll up, and persist.

        Returns the recorded event.
        """
        ts = timestamp or utcnow()
        event: dict[str, Any] = {
            "ts": format_ts(ts),
            "kind": kind,
            "summary": (summary or "").strip()[:500],
            "files": list(dict.fromkeys(files or []))[:40],
            "insertions": int(insertions),
            "deletions": int(deletions),
        }
        if commit:
            event["commit"] = commit
        if tools:
            event["tools"] = list(dict.fromkeys(tools))[:20]
        if session_id:
            event["session"] = session_id

        recent = self.store["buckets"]["recent_3_days"]
        recent.append(event)
        if len(recent) > MAX_RECENT_EVENTS:
            del recent[: len(recent) - MAX_RECENT_EVENTS]

        self.roll_up(now=ts)
        self.save()
        return event

    # -- roll-up cascade ----------------------------------------------------

    def roll_up(self, now: Any = None) -> dict[str, int]:
        """
        Cascade aged entries down the bucket hierarchy.

        Returns counts of entries moved at each level.
        """
        now = now or utcnow()
        buckets = self.store["buckets"]
        moved = {"events_to_days": 0, "days_to_quarters": 0, "quarters_to_years": 0}

        # recent_3_days → current_month (per-day summaries)
        kept: list[dict[str, Any]] = []
        aged: list[dict[str, Any]] = []
        for event in buckets["recent_3_days"]:
            ts = parse_ts(event.get("ts", ""))
            if ts is None or is_recent(ts, now):
                kept.append(event)
            else:
                aged.append(event)
        buckets["recent_3_days"] = kept

        day_summaries = {s.get("period"): s for s in buckets["current_month"]}
        for event in aged:
            ts = parse_ts(event.get("ts", ""))
            if ts is None:
                continue
            period = day_key(ts)
            summary = day_summaries.setdefault(period, _blank_summary(period))
            _merge_event(summary, event)
            moved["events_to_days"] += 1
        buckets["current_month"] = sorted(
            day_summaries.values(), key=lambda s: str(s.get("period", ""))
        )

        # current_month → fiscal_quarters (anything outside this calendar month
        # AND outside the trailing week — a week that straddles a month boundary
        # keeps its per-day rows so `last_7_days` stays answerable)
        still_current: list[dict[str, Any]] = []
        for summary in buckets["current_month"]:
            ts = parse_ts(str(summary.get("period", "")))
            if ts is None or is_current_month(ts, now) or is_within_days(ts, now, WEEK_DAYS):
                still_current.append(summary)
                continue
            qkey = fiscal_quarter_key(ts, self.fiscal_start_month)
            target = buckets["fiscal_quarters"].setdefault(qkey, _blank_summary(qkey))
            _merge_summary(target, summary)
            moved["days_to_quarters"] += 1
        buckets["current_month"] = still_current

        # fiscal_quarters → fiscal_years (anything outside this fiscal year)
        for qkey in list(buckets["fiscal_quarters"]):
            ts = _quarter_start(qkey, self.fiscal_start_month)
            if ts is None or is_current_fiscal_year(ts, now, self.fiscal_start_month):
                continue
            fykey = fiscal_year_key(ts, self.fiscal_start_month)
            target = buckets["fiscal_years"].setdefault(fykey, _blank_summary(fykey))
            _merge_summary(target, buckets["fiscal_quarters"].pop(qkey))
            moved["quarters_to_years"] += 1

        return moved

    # -- reading ------------------------------------------------------------

    def timeline(self, scope: str = "recent_3_days") -> dict[str, Any]:
        """
        Return one bucket (or all of them) as a JSON-serializable dict.

        Valid scopes: recent_3_days, current_month, fiscal_quarters,
        fiscal_years, all.
        """
        if scope not in SCOPES:
            raise ValueError(f"Unknown scope '{scope}'. Valid: {list(SCOPES)}")

        self.roll_up()
        buckets = self.store["buckets"]
        meta = {
            "project": self.project_root.name,
            "fiscal_year_start_month": self.fiscal_start_month,
            "updated_at": self.store.get("updated_at"),
        }
        if scope == "all":
            return {
                "scope": "all",
                **meta,
                "buckets": {**buckets, "last_7_days": self.week_series()},
            }
        if scope == "last_7_days":
            return {"scope": scope, **meta, scope: self.week_series()}
        return {"scope": scope, **meta, scope: buckets[scope]}

    def week_series(self, now: Any = None, days: int = WEEK_DAYS) -> list[dict[str, Any]]:
        """
        Per-day time series over the trailing week, oldest first.

        Derived rather than stored: it merges the rolled-up day summaries with
        the still-live `recent_3_days` events so the newest days are included,
        and zero-fills quiet days so the series is plottable without gaps.
        """
        now = now or utcnow()
        series = {key: _blank_summary(key) for key in day_series(now, days)}

        for summary in self.store["buckets"]["current_month"]:
            period = str(summary.get("period", ""))
            if period in series:
                _merge_summary(series[period], summary)
                series[period].pop("periods", None)

        for event in self.store["buckets"]["recent_3_days"]:
            ts = parse_ts(event.get("ts", ""))
            if ts is None:
                continue
            period = day_key(ts)
            if period in series:
                _merge_event(series[period], event)

        return [series[key] for key in day_series(now, days)]

    def recent_digest(self, max_chars: int = 600, now: Any = None) -> str:
        """
        A compact plain-text digest of `recent_3_days` for prompt injection.

        Budgeted by characters (~4 chars/token) so the caller can keep the
        injection under a token ceiling.

        `now` overrides the clock used for the roll-up, the same way `roll_up`
        and `week_series` already allow. Without it the method reads the wall
        clock, which makes any test that supplies absolute timestamps depend on
        the date it runs on.
        """
        self.roll_up(now)
        events = self.store["buckets"]["recent_3_days"]
        if not events:
            return ""

        files = Counter()
        lines: list[str] = []
        for event in reversed(events):  # newest first
            files.update(event.get("files", []))
            ts = parse_ts(event.get("ts", ""))
            stamp = ts.strftime("%m-%d %H:%M") if ts else "?"
            summary = event.get("summary", "").replace("\n", " ").strip()
            if summary:
                lines.append(f"- {stamp} {summary}")

        head = f"rtk-sf memory — last 72h ({len(events)} turn(s))"
        top = ", ".join(name for name, _ in files.most_common(5))
        body: list[str] = [head]
        if top:
            body.append(f"Hot files: {top}")

        out = "\n".join(body)
        for line in lines:
            if len(out) + len(line) + 1 > max_chars:
                out += "\n- …"
                break
            out += "\n" + line
        return out


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _merge_event(summary: dict[str, Any], event: dict[str, Any]) -> None:
    """Fold one full-detail event into a period summary."""
    summary["events"] = summary.get("events", 0) + 1
    summary["insertions"] = summary.get("insertions", 0) + int(event.get("insertions", 0))
    summary["deletions"] = summary.get("deletions", 0) + int(event.get("deletions", 0))

    counts = Counter({f["name"]: f["count"] for f in summary.get("top_files", [])})
    counts.update(event.get("files", []))
    summary["top_files"] = [
        {"name": name, "count": count} for name, count in counts.most_common(MAX_TOP_FILES)
    ]
    summary["files_touched"] = len(counts)

    text = (event.get("summary") or "").strip()
    if text:
        highlights = summary.setdefault("highlights", [])
        if text not in highlights:
            highlights.insert(0, text)
            del highlights[MAX_HIGHLIGHTS:]


def _merge_summary(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Fold a finer-grained summary into a coarser one."""
    target["events"] = target.get("events", 0) + int(source.get("events", 0))
    target["insertions"] = target.get("insertions", 0) + int(source.get("insertions", 0))
    target["deletions"] = target.get("deletions", 0) + int(source.get("deletions", 0))

    counts = Counter({f["name"]: f["count"] for f in target.get("top_files", [])})
    counts.update({f["name"]: f["count"] for f in source.get("top_files", [])})
    target["top_files"] = [
        {"name": name, "count": count} for name, count in counts.most_common(MAX_TOP_FILES)
    ]
    target["files_touched"] = len(counts)

    highlights = target.setdefault("highlights", [])
    for text in source.get("highlights", []):
        if text not in highlights:
            highlights.append(text)
    del highlights[MAX_HIGHLIGHTS:]

    periods = target.setdefault("periods", [])
    period = source.get("period")
    if period and period not in periods:
        periods.append(period)


def _quarter_start(quarter_key: str, start_month: int) -> Any:
    """
    Resolve a 'FY2026-Q2' key back to a datetime inside that quarter.

    Used to decide whether a quarter still belongs to the current fiscal year.
    """
    from datetime import datetime, timezone

    try:
        fy_part, q_part = quarter_key.split("-Q")
        fy = int(fy_part.replace("FY", ""))
        quarter = int(q_part)
    except (ValueError, AttributeError):
        return None

    month_offset = (quarter - 1) * 3
    month = (start_month - 1 + month_offset) % 12 + 1
    year = fy + (start_month - 1 + month_offset) // 12
    return datetime(year, month, 1, tzinfo=timezone.utc)
