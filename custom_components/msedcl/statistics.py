"""Energy-Dashboard long-term statistics for the MSEDCL integration (Phase 5).

Design (locked in by the 2026-07-31 live smoke test + hourly probe — PLAN.md):

* Two external statistic series per consumer:
      msedcl:<cno>_grid_import   <- import register / hourly UNITS_IMPORTED
      msedcl:<cno>_grid_export   <- export register / hourly UNITS_EXPORTED
  Two separate non-negative series — never the signed net.

* REGISTERS AS SUMS. The daily payload's READING_* fields are cumulative
  end-of-day meter registers, verified consistent with the per-day UNITS_*
  deltas (register delta over the month == sum of daily units, exactly).
  Using the register itself as the statistic `sum`:
    - idempotency is automatic (re-inserting a day rewrites the same value),
    - late/revised data self-corrects with NO forward recompute,
    - gaps carry forward naturally.

* DAILY ROWS (history). Register snapshots are end-of-IST-day values, but IST
  is UTC+5:30, so IST midnight is an invalid (xx:30) bucket start. Day D's
  register is pinned to 23:30 IST == 18:00 UTC on day D — a valid top-of-hour
  UTC start; the day's consumption renders inside day D. (No DST in IST.)

* HOURLY ROWS (recent window). GetHourlyConsumption works for past days too
  (verified live). A completed day returns 25 PER-SLOT amounts tiling the IST
  day as the AMISP 30-minute block profile aggregated to:
      slot 0        [00:00, 00:30)   half hour
      slots 1..23   [HH:30, HH+1:30) full hours
      slot 24       [23:30, 24:00)   half hour
  Slot boundaries are therefore EXACTLY top-of-hour UTC (00:30 IST == 19:00
  UTC), so hourly buckets align natively — no rendering skew. Each day's
  amounts are accumulated onto the register scale, anchored at the PREVIOUS
  day's end-of-day register (anchor + all 25 slots == that day's register,
  exactly, per the smoke-test identity). The two half-slots around IST
  midnight (slot 24 of day D + slot 0 of day D+1) share one UTC bucket and
  merge; that bucket also supersedes day D's daily row at the same start.
  A day whose anchor register is missing falls back to its daily row.

* SCHEDULE. One run at setup + a configurable interval (OPT_STATS_INTERVAL,
  default hourly), as a background task. First run: STATS_BACKFILL_MONTHS
  months of daily data (GetMonthlyConsumption is dead: endpoint-level 401) +
  hourly for the last STATS_HOURLY_BACKFILL_DAYS days. Later runs: current IST
  month daily (plus previous month near month-start for late revisions) +
  hourly for today and yesterday — 3 requests per run, so the hourly default
  keeps the dashboard within ~an hour of live at a modest request budget.

Dashboard note: these statistics are the Energy-Dashboard source of truth.
Selecting the live meter-reading sensor AS WELL double-counts consumption.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from .api import MahaApiClient, MahaError
from .const import (
    DOMAIN,
    STATS_BACKFILL_MONTHS,
    STATS_HOURLY_BACKFILL_DAYS,
    STATS_HOURLY_DAYS_PER_RUN,
)
from .models import IST, DailyPoint, HourlyPoint

_LOGGER = logging.getLogger(__name__)

# StatisticMetaData grew mean_type (replacing has_mean) in newer HA releases;
# support both so the integration spans the hacs.json minimum version.
try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _META_NO_MEAN: dict[str, Any] = {"mean_type": StatisticMeanType.NONE}
except ImportError:  # pragma: no cover - older HA
    _META_NO_MEAN = {"has_mean": False}

# End-of-day registers on the import/export series.
_IMPORT_REG = lambda p: p.reading_import if p.reading_import is not None else p.reading  # noqa: E731
_EXPORT_REG = lambda p: p.reading_export  # noqa: E731


def _months_back(n: int) -> list[str]:
    """The last `n` IST months as YYYYMM strings, oldest first."""
    now = datetime.now(IST)
    year, month = now.year, now.month
    out: list[str] = []
    for _ in range(n):
        out.append(f"{year}{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(out))


def _days_back(n: int) -> list[str]:
    """The last `n` IST days as YYYYMMDD strings, oldest first, ending today."""
    today = datetime.now(IST)
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n - 1, -1, -1)]


def _prev_day(day: str) -> str:
    return (datetime.strptime(day, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")


def _pin_start(date_str: str) -> datetime | None:
    """Map an IST YYYYMMDD day to its daily-row bucket start (18:00 UTC)."""
    if not date_str or len(date_str) != 8 or not date_str.isdigit():
        return None
    try:
        pinned_ist = datetime(
            int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]),
            23, 30, tzinfo=IST,
        )
    except ValueError:
        return None
    return pinned_ist.astimezone(timezone.utc)


def _slot_bucket(day: str, k: int) -> datetime:
    """UTC statistics bucket for hourly slot `k` of IST day `day`.

    Slot k starts at 00:00 IST (k=0) or (k-1):30 IST (k>=1); the bucket is
    that instant floored to the UTC hour. Slots 1..23 start exactly on a UTC
    hour; the two half-slots (0 and 24) floor into the shared 18:00 UTC bucket
    on consecutive UTC days.
    """
    start_ist = datetime(int(day[:4]), int(day[4:6]), int(day[6:8]), tzinfo=IST)
    if k > 0:
        start_ist += timedelta(hours=k - 1, minutes=30)
    return start_ist.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )


def _slots_valid(points: list[HourlyPoint]) -> bool:
    """Check the 25-slot label pattern: 0, 1..23, 0 (prefix for partial days)."""
    if not points or len(points) > 25:
        return False
    for k, point in enumerate(points):
        expected = 0 if k in (0, 24) else k
        if point.hour != expected:
            return False
    return True


class MsedclStatistics:
    """Inserts external long-term statistics for one consumer."""

    def __init__(
        self, hass: HomeAssistant, title: str, consumer_no: str, client: MahaApiClient
    ) -> None:
        self._hass = hass
        self._title = title
        self._client = client
        self._import_id = f"{DOMAIN}:{consumer_no}_grid_import"
        self._export_id = f"{DOMAIN}:{consumer_no}_grid_export"

    async def async_update(self, _now: datetime | None = None) -> None:
        """Fetch consumption data and upsert statistics. Never raises."""
        try:
            await self._async_update()
        except Exception:  # noqa: BLE001 - scheduled background task
            _LOGGER.exception("Statistics update failed")

    async def _async_update(self) -> None:
        if await self._has_stats(self._import_id):
            # Incremental: current month; near month-start also re-fetch the
            # previous month so late-arriving revisions self-correct.
            months = _months_back(2 if datetime.now(IST).day <= 3 else 1)
            hourly_days = _days_back(STATS_HOURLY_DAYS_PER_RUN)
        else:
            months = _months_back(STATS_BACKFILL_MONTHS)
            hourly_days = _days_back(STATS_HOURLY_BACKFILL_DAYS)
            _LOGGER.info(
                "First statistics run: backfilling %d months daily + %d days hourly",
                len(months), len(hourly_days),
            )

        daily: list[DailyPoint] = []
        for month in months:
            try:
                raw = await self._client.daily_consumption(month)
            except MahaError as err:
                # Old months may simply predate the smart meter — debug, move on.
                _LOGGER.debug("No daily data for %s: %s", month, err)
                continue
            daily.extend(DailyPoint.parse_list(raw))

        # Only completed IST days carry a final end-of-day register.
        today = datetime.now(IST).strftime("%Y%m%d")
        completed = [p for p in daily if p.date_str and p.date_str < today]

        # End-of-day registers by date: daily-row values AND hourly anchors.
        import_reg = {
            p.date_str: v for p in completed if (v := _IMPORT_REG(p)) is not None
        }
        export_reg = {
            p.date_str: v for p in completed if (v := _EXPORT_REG(p)) is not None
        }

        hourly: dict[str, list[HourlyPoint]] = {}
        for day in hourly_days:
            try:
                raw = await self._client.hourly_consumption(day)
            except MahaError as err:
                _LOGGER.debug("No hourly data for %s: %s", day, err)
                continue
            points = HourlyPoint.parse_list(raw)
            if _slots_valid(points):
                hourly[day] = points
            else:
                _LOGGER.debug(
                    "Unexpected hourly slot structure for %s (%d rows) — "
                    "day left at daily resolution", day, len(points),
                )

        now_hour = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        import_rows = _merge(
            _daily_rows(completed, _IMPORT_REG),
            _hourly_rows(hourly, import_reg, lambda p: p.units_import, now_hour),
        )
        export_rows = _merge(
            _daily_rows(completed, _EXPORT_REG),
            _hourly_rows(hourly, export_reg, lambda p: p.units_export, now_hour),
        )

        if import_rows:
            async_add_external_statistics(
                self._hass, self._meta(self._import_id, "Grid import"), import_rows
            )
        if export_rows:
            async_add_external_statistics(
                self._hass, self._meta(self._export_id, "Grid export"), export_rows
            )
        _LOGGER.debug(
            "Inserted statistics: %d import rows, %d export rows (%d hourly days)",
            len(import_rows), len(export_rows), len(hourly),
        )

    def _meta(self, statistic_id: str, label: str) -> dict[str, Any]:
        return {
            "source": DOMAIN,
            "statistic_id": statistic_id,
            "name": f"{self._title} {label}",
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
            "has_sum": True,
            **_META_NO_MEAN,
        }

    async def _has_stats(self, statistic_id: str) -> bool:
        last = await get_instance(self._hass).async_add_executor_job(
            get_last_statistics, self._hass, 1, statistic_id, True, {"sum"}
        )
        return bool(last)


def _daily_rows(
    points: list[DailyPoint],
    value_fn: Callable[[DailyPoint], float | None],
) -> dict[datetime, dict[str, Any]]:
    """One row per completed day: register as both state and sum."""
    rows: dict[datetime, dict[str, Any]] = {}
    for point in points:
        value = value_fn(point)
        start = _pin_start(point.date_str or "")
        if value is None or start is None:
            continue
        rows[start] = {"start": start, "state": value, "sum": value}
    return rows


def _hourly_rows(
    hourly: dict[str, list[HourlyPoint]],
    registers: dict[str, float],
    value_fn: Callable[[HourlyPoint], float | None],
    now_hour: datetime,
) -> dict[datetime, dict[str, Any]]:
    """Hourly buckets: per-slot amounts accumulated onto the register scale.

    Days are processed chronologically, so at the IST-midnight seam slot 24 of
    day D and slot 0 of day D+1 hit the same UTC bucket and the later (larger)
    cumulative wins. Continuity across days is exact: anchor(D) + all 25 slot
    amounts == register(D) == anchor(D+1).
    """
    rows: dict[datetime, dict[str, Any]] = {}
    for day in sorted(hourly):
        anchor = registers.get(_prev_day(day))
        if anchor is None:
            # No register scale to anchor to (gap / very first day of data):
            # leave this day at daily resolution.
            continue
        slots = hourly[day]
        if len(slots) < 25:
            # Partial day (today): the final slot may still be accumulating.
            slots = slots[:-1]
        cumulative = anchor
        for k, point in enumerate(slots):
            cumulative += value_fn(point) or 0.0
            bucket = _slot_bucket(day, k)
            if bucket >= now_hour:
                break
            rows[bucket] = {"start": bucket, "state": cumulative, "sum": cumulative}
    return rows


def _merge(
    daily: dict[datetime, dict[str, Any]],
    hourly: dict[datetime, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay hourly onto daily (hourly wins shared buckets), sorted by start."""
    merged = {**daily, **hourly}
    return [merged[start] for start in sorted(merged)]
