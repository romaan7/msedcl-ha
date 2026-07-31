"""Unit tests for statistics.py — bucket geometry, register anchoring, merge.

Uses the real July-30 hourly profile captured live (see conftest) so the
half-slot tiling and register-continuity identities are tested against
actual meter behavior, not synthetic data.
"""

from __future__ import annotations

from datetime import datetime, timezone

from custom_components.msedcl import statistics as st
from custom_components.msedcl.models import DailyPoint, HourlyPoint
from tests.conftest import HOURLY_EXP_20260730, HOURLY_IMP_20260730, hourly_rows

UTC = timezone.utc
FAR_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)

R_IMP_0729 = 3106.307
R_IMP_0730 = R_IMP_0729 + sum(HOURLY_IMP_20260730)
R_EXP_0729 = 6480.884
R_EXP_0730 = R_EXP_0729 + sum(HOURLY_EXP_20260730)
REG_IMP = {"20260729": R_IMP_0729, "20260730": R_IMP_0730}
REG_EXP = {"20260729": R_EXP_0729, "20260730": R_EXP_0730}


def _day30() -> list[HourlyPoint]:
    return HourlyPoint.parse_list(
        hourly_rows("20260730", HOURLY_IMP_20260730, HOURLY_EXP_20260730)
    )


def _day31_partial() -> list[HourlyPoint]:
    return HourlyPoint.parse_list(
        hourly_rows("20260731", [0.064, 0.152] + [0.1] * 14, [0.0] * 16)
    )


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #
def test_months_and_days_helpers():
    months = st._months_back(3)
    assert len(months) == 3 and months == sorted(months)
    days = st._days_back(3)
    assert len(days) == 3 and days == sorted(days)
    assert st._prev_day("20260801") == "20260731"
    assert st._prev_day("20260101") == "20251231"


def test_daily_pin_is_top_of_hour_utc():
    start = st._pin_start("20260701")
    assert start == datetime(2026, 7, 1, 18, 0, tzinfo=UTC)  # 23:30 IST
    assert st._pin_start("bogus") is None
    assert st._pin_start("20260231") is None
    assert st._pin_start("") is None


def test_slot_buckets_align_with_utc_hours():
    # slot 0 (00:00 IST) floors into 18:00 UTC of the previous UTC day
    assert st._slot_bucket("20260730", 0) == datetime(2026, 7, 29, 18, tzinfo=UTC)
    # interior slots start exactly on UTC hours (00:30 IST == 19:00 UTC)
    assert st._slot_bucket("20260730", 1) == datetime(2026, 7, 29, 19, tzinfo=UTC)
    assert st._slot_bucket("20260730", 6) == datetime(2026, 7, 30, 0, tzinfo=UTC)
    # slot 24 (23:30 IST) is 18:00 UTC of the same day...
    assert st._slot_bucket("20260730", 24) == datetime(2026, 7, 30, 18, tzinfo=UTC)
    # ...shared with slot 0 of the NEXT day (the IST-midnight seam)
    assert st._slot_bucket("20260730", 24) == st._slot_bucket("20260731", 0)


def test_slots_valid_patterns():
    assert st._slots_valid(_day30())
    assert st._slots_valid(_day31_partial())          # prefix of the pattern
    assert not st._slots_valid([])
    bad = _day30()
    bad[5].hour = 7
    assert not st._slots_valid(bad)
    assert not st._slots_valid(_day30() + _day30())   # > 25 rows


# --------------------------------------------------------------------------- #
# hourly accumulation on the register scale
# --------------------------------------------------------------------------- #
def test_hourly_rows_register_continuity():
    hourly = {"20260730": _day30(), "20260731": _day31_partial()}
    rows = st._hourly_rows(hourly, REG_IMP, lambda p: p.units_import, FAR_FUTURE)

    # one full day = 25 buckets, all distinct except the seam shared with day+1
    day30_buckets = [k for k in rows if k <= datetime(2026, 7, 30, 18, tzinfo=UTC)]
    assert len(day30_buckets) == 25

    # cumulative before the final half-slot = register - that half-slot
    end = rows[datetime(2026, 7, 30, 17, tzinfo=UTC)]
    assert abs(end["sum"] - (R_IMP_0730 - HOURLY_IMP_20260730[-1])) < 1e-9

    # seam bucket = day's register + next day's first half-slot
    seam = rows[datetime(2026, 7, 30, 18, tzinfo=UTC)]
    assert abs(seam["sum"] - (R_IMP_0730 + 0.064)) < 1e-9

    # partial day (16 rows): last possibly-in-progress slot dropped -> slots
    # 0..14, so the final bucket is slot 14 = 13:30-14:30 IST = 08:00 UTC
    assert max(rows) == datetime(2026, 7, 31, 8, tzinfo=UTC)


def test_hourly_rows_export_series():
    hourly = {"20260730": _day30()}
    rows = st._hourly_rows(hourly, REG_EXP, lambda p: p.units_export, FAR_FUTURE)
    # exports end by slot 23 on this day; register already fully accumulated
    assert abs(rows[datetime(2026, 7, 30, 17, tzinfo=UTC)]["sum"] - R_EXP_0730) < 1e-9


def test_hourly_rows_missing_anchor_skips_day():
    hourly = {"20260728": _day30()}  # no register for 20260727
    assert st._hourly_rows(hourly, REG_IMP, lambda p: p.units_import, FAR_FUTURE) == {}


def test_hourly_rows_now_guard_drops_future_buckets():
    hourly = {"20260730": _day30(), "20260731": _day31_partial()}
    now = datetime(2026, 7, 31, 6, tzinfo=UTC)
    rows = st._hourly_rows(hourly, REG_IMP, lambda p: p.units_import, now)
    assert rows and max(rows) < now


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #
def test_merge_hourly_supersedes_daily_and_stays_monotone():
    daily_pts = DailyPoint.parse_list(
        [
            {"DATE": "20260729", "READING_IMP": str(R_IMP_0729)},
            {"DATE": "20260730", "READING_IMP": str(R_IMP_0730)},
        ]
    )
    daily = st._daily_rows(daily_pts, st._IMPORT_REG)
    hourly = st._hourly_rows(
        {"20260730": _day30(), "20260731": _day31_partial()},
        REG_IMP,
        lambda p: p.units_import,
        FAR_FUTURE,
    )
    merged = st._merge(daily, hourly)

    # daily row for 20260729 (18:00 UTC) superseded by day-30 slot 0
    by_start = {r["start"]: r for r in merged}
    assert abs(
        by_start[datetime(2026, 7, 29, 18, tzinfo=UTC)]["sum"]
        - (R_IMP_0729 + HOURLY_IMP_20260730[0])
    ) < 1e-9

    # sorted and monotone non-decreasing across the daily->hourly transition
    sums = [r["sum"] for r in merged]
    starts = [r["start"] for r in merged]
    assert starts == sorted(starts)
    assert all(b >= a - 1e-9 for a, b in zip(sums, sums[1:]))


def test_daily_rows_skip_missing_values_and_bad_dates():
    pts = DailyPoint.parse_list(
        [
            {"DATE": "20260701", "READING_IMP": "10.0"},
            {"DATE": "garbage", "READING_IMP": "11.0"},
            {"DATE": "20260703"},  # no value
        ]
    )
    rows = st._daily_rows(pts, st._IMPORT_REG)
    assert list(rows.values())[0]["sum"] == 10.0
    assert len(rows) == 1
