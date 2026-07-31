"""Unit tests for models.py — parsing of live payload shapes."""

from __future__ import annotations

from datetime import timedelta

from custom_components.msedcl.models import (
    BillHistoryRow,
    ConsumerInfo,
    CurrentReading,
    DailyPoint,
    HourlyPoint,
    MonthlyPoint,
)
from tests.conftest import (
    BILL_HISTORY_PAYLOAD,
    CNO,
    CONTACT_PAYLOAD,
    HOURLY_IMP_20260730,
    HOURLY_EXP_20260730,
    READING_PAYLOAD,
    hourly_rows,
)


def test_consumer_info_from_contact_details():
    info = ConsumerInfo.from_contact_details(CONTACT_PAYLOAD)
    assert info.consumer_no == CNO
    assert info.bu == "0000"
    assert info.amisp_code == "002"
    assert info.has_smart_meter is True
    assert info.solar_rt is True
    assert info.billing_type == "postpaid"
    assert info.mobile == "0000000000"
    assert info.connected_load_kw == 5.0


def test_consumer_info_empty_payload():
    info = ConsumerInfo.from_contact_details({})
    assert info.consumer_no is None
    assert info.has_smart_meter is None


def test_current_reading_parse_ist_aware():
    cr = CurrentReading.parse(READING_PAYLOAD)
    assert cr.reading == 3109.786
    assert cr.mtd_consumption == 58.1
    assert cr.current_balance == 0.0
    assert cr.reading_dt is not None
    assert cr.reading_dt.tzinfo is not None
    assert cr.reading_dt.utcoffset() == timedelta(hours=5, minutes=30)
    assert (cr.reading_dt.year, cr.reading_dt.hour) == (2026, 10)


def test_current_reading_tolerates_garbage():
    cr = CurrentReading.parse({"READING": "not-a-number", "READING_DATE_TIME": "xx"})
    assert cr.reading is None
    assert cr.reading_dt is None
    cr = CurrentReading.parse({})
    assert cr.reading is None


def test_daily_point_key_variants():
    p = DailyPoint.parse(
        {"DATE": "20260701", "READING_IMP": "1.5", "READING_EXP": "2.5"}
    )
    assert p.reading_import == 1.5 and p.reading_export == 2.5
    p = DailyPoint.parse(
        {"DATE": "20260701", "READING_IMPORT": "1.5", "READING_EXPORT": "2.5"}
    )
    assert p.reading_import == 1.5 and p.reading_export == 2.5


def test_hourly_point_parse():
    points = HourlyPoint.parse_list(
        hourly_rows("20260730", HOURLY_IMP_20260730, HOURLY_EXP_20260730)
    )
    assert len(points) == 25
    assert points[0].hour == 0 and points[24].hour == 0
    assert points[1].hour == 1 and points[23].hour == 23
    assert points[0].units_import == 0.063
    assert points[15].units_export == 1.476
    assert abs(sum(p.units_import for p in points) - 2.080) < 1e-9


def test_bill_history_latest_flag_and_fallback():
    latest = BillHistoryRow.latest(BILL_HISTORY_PAYLOAD)
    assert latest.bill_month == "JUL-2026"
    assert latest.current_bill == -3910.0  # negative = solar credit
    # no IsRecentYn flag -> first row
    rows = [dict(r, IsRecentYn="N") for r in BILL_HISTORY_PAYLOAD]
    assert BillHistoryRow.latest(rows).bill_month == "JUL-2026"
    assert BillHistoryRow.latest([]) is None
    assert BillHistoryRow.latest(None) is None


def test_monthly_point_parse():
    m = MonthlyPoint.parse(
        {"YEAR": "2026", "MONTH": "7", "UNITS_CONS": "56.08", "READING_NET": "-3378.4"}
    )
    assert (m.year, m.month) == (2026, 7)
    assert m.units_consumed == 56.08
    assert m.reading_net == -3378.4
