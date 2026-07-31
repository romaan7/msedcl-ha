#!/usr/bin/env python3
"""Phase-1 exit-criterion smoke test + Phase-3 blocker analysis.

Runs the async client standalone (no Home Assistant needed) against your own
MSEDCL account and:

  1. Fetches contact_details  -> validates the standard channel + discovers
     BU / amispCode / hasSmartMeterYN / meter number (what the config flow does).
  2. Fetches current_reading  -> validates Basic credentials (smart channel).
  3. Fetches meter_health     -> optional; 404 means "not supported".
  4. Fetches daily + monthly consumption and ANALYZES them to answer the two
     Phase-3 blockers from PLAN.md:
       Q1: is READING monotonic (safe for state_class=total_increasing),
           or can it decrease (net register on a solar meter)?
       Q2: are READING_* (cumulative) or UNITS_* (per-period) fields the
           authoritative source for statistics sums?
  5. Fetches bill_history     -> latest bill row.

Usage (Git Bash):
    pip install aiohttp
    export MSEDCL_LOGIN='...'
    export MSEDCL_PASSWORD='...'      # single-quote if it has special chars
    export MSEDCL_CNO='000000000000'
    python scripts/smoke_test.py

Only GET requests to your own account. Nothing is written to disk.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Import api.py / models.py directly (bypassing the HA package __init__).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "msedcl"))

import aiohttp  # noqa: E402

from api import (  # noqa: E402
    MahaApiClient, MahaAuthError, MahaError, MahaNotFound, MahaServerError,
)
from models import (  # noqa: E402
    BillHistoryRow, ConsumerInfo, CurrentReading, DailyPoint, MonthlyPoint,
)

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def _mask(v, keep: int = 3):
    if not v:
        return v
    s = str(v)
    return s[:keep] + "*" * max(0, len(s) - keep)


def _status(label: str, state: str, detail: str = "") -> None:
    print(f"[{state:^4}] {label}" + (f" — {detail}" if detail else ""))


async def _try(label: str, coro):
    """Run one call; classify the outcome; never abort the whole run."""
    try:
        result = await coro
        _status(label, PASS)
        return result
    except MahaAuthError as e:
        _status(label, FAIL, f"AUTH: {e}")
    except MahaNotFound as e:
        _status(label, SKIP, f"404 (endpoint/data absent): {e}")
    except MahaServerError as e:
        _status(label, FAIL, f"SERVER: {e}")
    except MahaError as e:
        _status(label, FAIL, str(e))
    return None


def _monotonic(values: list[float]) -> bool:
    return all(b >= a for a, b in zip(values, values[1:]))


def analyze_daily(points: list[DailyPoint]) -> None:
    """Answer the two Phase-3 blockers from a month of daily points."""
    print("\n--- Phase-3 blocker analysis (from daily consumption) ---")
    if not points:
        print("No daily points returned — analysis impossible for this month.")
        return

    p0 = points[0]
    present = {
        "READING": [p.reading for p in points if p.reading is not None],
        "READING_IMPORT": [p.reading_import for p in points if p.reading_import is not None],
        "READING_EXPORT": [p.reading_export for p in points if p.reading_export is not None],
        "READING_NET": [p.reading_net for p in points if p.reading_net is not None],
        "UNITS_CONSUMED_IMPORT": [p.units_import for p in points if p.units_import is not None],
        "UNITS_EXPORT": [p.units_export for p in points if p.units_export is not None],
        "UNITS_CONSUMED_NET": [p.units_net for p in points if p.units_net is not None],
    }
    print(f"{len(points)} daily points; fields present:")
    for name, vals in present.items():
        if vals:
            print(f"  {name:>22}: n={len(vals):>2}  "
                  f"first={vals[0]:.3f}  last={vals[-1]:.3f}  "
                  f"monotonic={_monotonic(vals)}")
        else:
            print(f"  {name:>22}: absent")

    # Q1 — is the headline READING register safe for total_increasing?
    readings = present["READING"]
    if readings:
        if _monotonic(readings):
            print("\nQ1: READING is monotonic over this month -> looks safe for "
                  "state_class=total_increasing (verify over >1 month).")
        else:
            print("\nQ1: *** READING DECREASES *** -> it is a net register; "
                  "do NOT use total_increasing for the live reading sensor.")
    else:
        print("\nQ1: READING absent from daily points — check current_reading "
              "value over time instead.")

    # Q2 — cumulative registers vs per-period units.
    imp_reg, imp_units = present["READING_IMPORT"], present["UNITS_CONSUMED_IMPORT"]
    if len(imp_reg) >= 2 and len(imp_units) >= 2:
        reg_delta = imp_reg[-1] - imp_reg[0]
        units_sum = sum(imp_units[1:])  # deltas attributable to days after day 0
        print(f"Q2: import register delta over month = {reg_delta:.3f}, "
              f"sum of daily UNITS after day 0 = {units_sum:.3f} "
              f"(close = consistent; prefer registers as the sum baseline).")
    elif imp_reg:
        print("Q2: only cumulative READING_* registers present -> use registers.")
    elif imp_units:
        print("Q2: only per-period UNITS_* present -> sum units into the stats.")
    else:
        print("Q2: neither import field present — inspect raw payload below.")

    print("\nSample first daily point (raw-ish):")
    print(json.dumps({k: getattr(p0, k) for k in (
        "date_str", "reading", "reading_import", "reading_export",
        "reading_net", "units_import", "units_export", "units_net")}, indent=2))


async def main() -> int:
    login = os.environ.get("MSEDCL_LOGIN")
    password = os.environ.get("MSEDCL_PASSWORD")
    cno = os.environ.get("MSEDCL_CNO")
    amisp = os.environ.get("MSEDCL_AMISP", "")
    if not (login and password and cno):
        print("Set MSEDCL_LOGIN, MSEDCL_PASSWORD, MSEDCL_CNO "
              "(MSEDCL_AMISP optional — auto-discovered).")
        return 2

    async with aiohttp.ClientSession() as session:
        client = MahaApiClient(session, login, password, cno, amisp=amisp)

        # 1) standard channel + discovery
        raw = await _try("contact_details (standard channel + discovery)",
                         client.contact_details())
        info = None
        if isinstance(raw, dict):
            info = ConsumerInfo.from_contact_details(raw)
            print(f"       name={_mask(info.name)}  BU={info.bu}  "
                  f"amispCode={info.amisp_code}  smartMeter={info.has_smart_meter}  "
                  f"solar={info.solar_rt}  billing={info.billing_type}  "
                  f"meter={_mask(info.meter_number, 4)}  tariff={info.tariff_name}")
            if not client.amisp and info.amisp_code:
                client.amisp = info.amisp_code
                print(f"       discovered amisp={info.amisp_code} -> "
                      "using it for smart-meter calls")

        if not client.amisp:
            print("\nNo AMISP (no smart meter?) — skipping smart-meter calls.")
        else:
            # 2) smart channel = the only place Basic creds are validated
            raw_cr = await _try("current_reading (validates Basic credentials)",
                                client.current_reading())
            if isinstance(raw_cr, dict):
                cr = CurrentReading.parse(raw_cr)
                print(f"       reading={cr.reading} kWh  at={cr.reading_dt}  "
                      f"MTD={cr.mtd_consumption}  MD={cr.md}  "
                      f"balance={cr.current_balance}")

            # 3) optional endpoint
            await _try("meter_health (optional)", client.meter_health())

            # 4) consumption history -> blocker analysis
            raw_daily = await _try("daily_consumption (this month)",
                                   client.daily_consumption())
            analyze_daily(DailyPoint.parse_list(raw_daily))

            raw_monthly = await _try("monthly_consumption (this year)",
                                     client.monthly_consumption())
            months = MonthlyPoint.parse_list(raw_monthly)
            if months:
                m = months[-1]
                print(f"       {len(months)} monthly points; latest: "
                      f"{m.year}-{m.month:02d} units_net={m.units_net} "
                      f"import={m.units_import} export={m.units_export}")

        # 5) billing
        raw_bills = await _try("bill_history", client.bill_history())
        latest = BillHistoryRow.latest(raw_bills)
        if latest:
            print(f"       latest bill: {latest.bill_month}  "
                  f"amount={latest.current_bill}  "
                  f"consumption={latest.consumption} kWh  status={latest.status}")

    print("\nSmoke test complete. PASS on contact_details + current_reading "
          "= Phase 1 exit criterion met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
