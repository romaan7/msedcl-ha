"""Typed models for MSEDCL API responses.

Ported from src/parse.py — field names are mapped to the ACTUAL JSON observed
from live responses. The API returns many numbers as strings ("3103.567", "0")
and mixes key cases (READING vs reading); parsers coerce defensively and never
raise on a missing optional field.

All API timestamps are IST with no timezone suffix. Parsed datetimes are made
tz-aware (Asia/Kolkata) here so Home Assistant never sees a naive datetime.

This module must stay importable standalone (stdlib only, no homeassistant or
package-relative imports) so scripts/smoke_test.py can use it outside HA.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - Windows without tzdata
    IST = timezone(timedelta(hours=5, minutes=30), "IST")


# --------------------------------------------------------------------------- #
# coercion helpers
# --------------------------------------------------------------------------- #
def _f(v: Any) -> Optional[float]:
    """Coerce to float; tolerate strings, blanks, None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _yn(v: Any) -> Optional[bool]:
    s = _s(v)
    if s is None:
        return None
    return s.upper() in ("Y", "YES", "TRUE", "1")


def _get(d: Any, *names: str) -> Any:
    """First present key among `names` (handles case/label variants)."""
    if not isinstance(d, dict):
        return None
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return None


# --------------------------------------------------------------------------- #
# Consumer identity (from contact_details / transaction_info)
# --------------------------------------------------------------------------- #
@dataclass
class ConsumerInfo:
    consumer_no: Optional[str] = None
    name: Optional[str] = None
    bu: Optional[str] = None
    bu_name: Optional[str] = None
    category: Optional[str] = None          # LT / HT
    meter_number: Optional[str] = None
    connected_load_kw: Optional[float] = None
    sanctioned_load_kw: Optional[float] = None
    circle_code: Optional[str] = None
    circle_name: Optional[str] = None
    division_name: Optional[str] = None
    zone_name: Optional[str] = None
    pc: Optional[str] = None
    dtc_code: Optional[str] = None
    dtc_name: Optional[str] = None
    has_smart_meter: Optional[bool] = None
    amisp_code: Optional[str] = None
    amisp: Optional[str] = None
    billing_type: Optional[str] = None      # postpaid / prepaid
    solar_rt: Optional[bool] = None
    tariff_name: Optional[str] = None
    bill_month: Optional[str] = None        # YYMM, e.g. "2607"
    address_lines: tuple = ()
    mobile: Optional[str] = None            # only present on contact_details
    email: Optional[str] = None
    uid: Optional[str] = None

    @classmethod
    def parse(cls, consumer: dict, outer: Optional[dict] = None) -> "ConsumerInfo":
        """`consumer` is the inner consumer object; `outer` optionally carries
        MobileNo/EmailId/Uid that sit alongside it (contact_details)."""
        c = consumer or {}
        o = outer or {}
        addr = tuple(
            x for x in (
                _s(_get(c, "addressLine1")),
                _s(_get(c, "addressLine2")),
                _s(_get(c, "addressLine3")),
            ) if x
        )
        return cls(
            consumer_no=_s(_get(c, "ConNumber", "consumerNumber", "ConsumerNumber")),
            name=_s(_get(c, "ConName", "consumerName")),
            bu=_s(_get(c, "BU", "billingUnit")),
            bu_name=_s(_get(c, "BUName")),
            category=_s(_get(c, "consumerCategory")),
            meter_number=_s(_get(c, "MeterNumber")),
            connected_load_kw=_f(_get(c, "connectedLoad")),
            sanctioned_load_kw=_f(_get(c, "sanctionedLoadKw")),
            circle_code=_s(_get(c, "circleCode")),
            circle_name=_s(_get(c, "circleName")),
            division_name=_s(_get(c, "divisionName")),
            zone_name=_s(_get(c, "zoneName")),
            pc=_s(_get(c, "pc")),
            dtc_code=_s(_get(c, "DTCCode")),
            dtc_name=_s(_get(c, "DTCName")),
            has_smart_meter=_yn(_get(c, "hasSmartMeterYN")),
            amisp_code=_s(_get(c, "amispCode")),
            amisp=_s(_get(c, "amisp")),
            billing_type=_s(_get(c, "billingType")),
            solar_rt=_yn(_get(c, "solarRtYN")),
            tariff_name=_s(_get(c, "tariffName")),
            bill_month=_s(_get(c, "billMonth")),
            address_lines=addr,
            mobile=_s(_get(o, "MobileNo")),
            email=_s(_get(o, "EmailId")),
            uid=_s(_get(o, "Uid")),
        )

    @classmethod
    def from_contact_details(cls, resp: dict) -> "ConsumerInfo":
        return cls.parse(_get(resp, "Consumer") or {}, outer=resp)

    @classmethod
    def from_transaction_info(cls, resp: dict) -> "ConsumerInfo":
        return cls.parse(_get(resp, "consumerInfo") or {})


# --------------------------------------------------------------------------- #
# Smart-meter current reading
# --------------------------------------------------------------------------- #
@dataclass
class CurrentReading:
    meter_number: Optional[str] = None
    reading: Optional[float] = None
    reading_dt: Optional[datetime] = None
    md: Optional[float] = None
    current_balance: Optional[float] = None
    mtd_consumption: Optional[float] = None
    prev_month_reading: Optional[float] = None
    raw: Optional[dict] = None

    @classmethod
    def parse(cls, d: dict) -> "CurrentReading":
        d = d or {}
        return cls(
            meter_number=_s(_get(d, "METER_NUMBER")),
            reading=_f(_get(d, "READING")),
            reading_dt=_parse_dt(_get(d, "READING_DATE_TIME")),
            md=_f(_get(d, "MD")),
            current_balance=_f(_get(d, "CURRENT_BALANCE")),
            mtd_consumption=_f(_get(d, "MTD_CONSUMPTION")),
            prev_month_reading=_f(_get(d, "PREV_MTH_READING")),
            raw=d,
        )


def _parse_dt(v: Any) -> Optional[datetime]:
    """READING_DATE_TIME looks like 'YYYYMMDDHHMMSS' (e.g. 20260728151020).

    The API emits IST wall-clock time with no suffix; attach Asia/Kolkata so
    downstream consumers (HA timestamp sensors, statistics) get an aware dt.
    """
    s = _s(v)
    if not s:
        return None
    for fmt in ("%Y%m%d%H%M%S", "%Y%m%d%H%M", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Consumption points (daily / monthly)
# --------------------------------------------------------------------------- #
@dataclass
class DailyPoint:
    date_str: Optional[str] = None          # YYYYMMDD (IST)
    day_name: Optional[str] = None
    reading: Optional[float] = None
    reading_import: Optional[float] = None
    reading_export: Optional[float] = None
    reading_net: Optional[float] = None
    recharge_value: Optional[float] = None
    units_import: Optional[float] = None
    units_export: Optional[float] = None
    units_net: Optional[float] = None

    @classmethod
    def parse(cls, d: dict) -> "DailyPoint":
        d = d or {}
        return cls(
            date_str=_s(_get(d, "DATE")),
            day_name=_s(_get(d, "NAME_OF_DAY")),
            reading=_f(_get(d, "READING")),
            reading_import=_f(_get(d, "READING_IMP", "READING_IMPORT")),
            reading_export=_f(_get(d, "READING_EXP", "READING_EXPORT")),
            reading_net=_f(_get(d, "READING_NET")),
            recharge_value=_f(_get(d, "RECHARGE_VALUE")),
            units_import=_f(_get(d, "UNITS_CONSUMED_IMPORT")),
            units_export=_f(_get(d, "UNITS_EXPORT")),
            units_net=_f(_get(d, "UNITS_CONSUMED_NET")),
        )

    @classmethod
    def parse_list(cls, items: Any) -> list["DailyPoint"]:
        return [cls.parse(x) for x in (items or []) if isinstance(x, dict)]


@dataclass
class HourlyPoint:
    """One slot from GetHourlyConsumption (observed live 2026-07-31).

    A completed day has 25 rows tiling the IST day as the AMISP 30-minute
    block profile aggregated to: [00:00-00:30), 23 x [HH:30-HH+1:30), and
    [23:30-24:00). Values are PER-SLOT amounts (they sum to the daily UNITS
    exactly), and field names differ from the daily payload
    (UNITS_IMPORTED / UNITS_EXPORTED, not UNITS_CONSUMED_IMPORT / UNITS_EXPORT).
    """

    date_str: Optional[str] = None          # YYYYMMDD (IST)
    hour: Optional[int] = None              # slot label; 0, 1..23, 0
    minute: Optional[int] = None
    reading: Optional[float] = None         # mirrors UNITS_IMPORTED, not a register
    units_import: Optional[float] = None
    units_export: Optional[float] = None
    units_net: Optional[float] = None

    @classmethod
    def parse(cls, d: dict) -> "HourlyPoint":
        d = d or {}
        return cls(
            date_str=_s(_get(d, "DATE")),
            hour=_i(_get(d, "HOUR")),
            minute=_i(_get(d, "MINUTE")),
            reading=_f(_get(d, "READING")),
            units_import=_f(_get(d, "UNITS_IMPORTED")),
            units_export=_f(_get(d, "UNITS_EXPORTED")),
            units_net=_f(_get(d, "UNITS_NET")),
        )

    @classmethod
    def parse_list(cls, items: Any) -> list["HourlyPoint"]:
        return [cls.parse(x) for x in (items or []) if isinstance(x, dict)]


@dataclass
class MonthlyPoint:
    year: Optional[int] = None
    month: Optional[int] = None
    reading: Optional[float] = None
    reading_import: Optional[float] = None
    reading_export: Optional[float] = None
    reading_net: Optional[float] = None
    recharge_value: Optional[float] = None
    units_consumed: Optional[float] = None
    units_import: Optional[float] = None
    units_export: Optional[float] = None
    units_net: Optional[float] = None

    @classmethod
    def parse(cls, d: dict) -> "MonthlyPoint":
        d = d or {}
        return cls(
            year=_i(_get(d, "YEAR")),
            month=_i(_get(d, "MONTH")),
            reading=_f(_get(d, "READING")),
            reading_import=_f(_get(d, "READING_IMPORT")),
            reading_export=_f(_get(d, "READING_EXPORT")),
            reading_net=_f(_get(d, "READING_NET")),
            recharge_value=_f(_get(d, "RECHARGE_VALUE")),
            units_consumed=_f(_get(d, "UNITS_CONS")),
            units_import=_f(_get(d, "UNITS_CONSUMED_IMPORT")),
            units_export=_f(_get(d, "UNITS_EXPORT")),
            units_net=_f(_get(d, "UNITS_CONSUMED_NET")),
        )

    @classmethod
    def parse_list(cls, items: Any) -> list["MonthlyPoint"]:
        return [cls.parse(x) for x in (items or []) if isinstance(x, dict)]


# --------------------------------------------------------------------------- #
# Bill history row  (from bill_history — the practical "recent bill" source)
# --------------------------------------------------------------------------- #
@dataclass
class BillHistoryRow:
    bill_month: Optional[str] = None        # "JUL-2026"
    bill_date: Optional[str] = None         # "19-Jul-26"
    consumption: Optional[float] = None
    status: Optional[str] = None            # LIVE
    current_bill: Optional[float] = None     # negative => credit balance
    last_receipt_amount: Optional[float] = None
    last_receipt_date: Optional[str] = None
    is_recent: Optional[bool] = None

    @classmethod
    def parse(cls, d: dict) -> "BillHistoryRow":
        d = d or {}
        return cls(
            bill_month=_s(_get(d, "BillMonth")),
            bill_date=_s(_get(d, "billDate")),
            consumption=_f(_get(d, "Consumption")),
            status=_s(_get(d, "Status")),
            current_bill=_f(_get(d, "CurrentBill")),
            last_receipt_amount=_f(_get(d, "LastReceiptAmount")),
            last_receipt_date=_s(_get(d, "LastReceiptDate")),
            is_recent=_yn(_get(d, "IsRecentYn")),
        )

    @classmethod
    def parse_list(cls, items: Any) -> list["BillHistoryRow"]:
        return [cls.parse(x) for x in (items or []) if isinstance(x, dict)]

    @classmethod
    def latest(cls, items: Any) -> Optional["BillHistoryRow"]:
        """The row flagged IsRecentYn='Y' (falls back to the first row)."""
        rows = cls.parse_list(items)
        for r in rows:
            if r.is_recent:
                return r
        return rows[0] if rows else None


# --------------------------------------------------------------------------- #
# self-test against the shapes observed live
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    contact = {
        "MobileNo": "0000000000", "EmailId": "x@y.com", "Uid": "000000000000",
        "Consumer": {
            "ConName": "TEST CONSUMER", "ConNumber": "000000000000", "BU": "0000",
            "BUName": "SAMPLE CITY (U-1)", "consumerCategory": "LT",
            "MeterNumber": "103-MH0000000", "connectedLoad": "5",
            "hasSmartMeterYN": "Y", "amispCode": "002", "amisp": "M/s NCC",
            "billingType": "postpaid", "solarRtYN": "Y", "pc": "4",
            "addressLine1": "PLOT 1", "addressLine2": "SAMPLE CITY",
            "tariffName": "LT-I  Residential 1Ph",
        },
    }
    info = ConsumerInfo.from_contact_details(contact)
    assert info.bu == "0000" and info.amisp_code == "002" and info.has_smart_meter
    assert info.mobile == "0000000000"

    cr = CurrentReading.parse({
        "METER_NUMBER": "MH0000000", "READING_DATE_TIME": "20260728151020",
        "READING": "3103.567", "MTD_CONSUMPTION": "52.291", "CURRENT_BALANCE": "0",
    })
    assert cr.reading == 3103.567 and cr.reading_dt.year == 2026
    assert cr.reading_dt.tzinfo is not None, "reading_dt must be tz-aware (IST)"
    assert cr.reading_dt.utcoffset() == timedelta(hours=5, minutes=30)

    row = BillHistoryRow.latest([
        {"BillMonth": "JUL-2026", "CurrentBill": "-3910", "IsRecentYn": "Y"},
        {"BillMonth": "JUN-2026", "CurrentBill": "-4050", "IsRecentYn": "N"},
    ])
    assert row.bill_month == "JUL-2026" and row.current_bill == -3910.0

    print("models.py self-test OK")
