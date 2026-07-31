#!/usr/bin/env python3
"""
example_usage.py
================

A worked example of using `maha_client.py`.

It shows:
  1. Building the client (from env vars, or explicitly with a password prompt).
  2. A connectivity probe on the standard channel.
  3. Fetching the latest bill, and auto-discovering the billing unit (BU) from it.
  4. Using that BU to unlock the BU-gated calls (consumer info, complaints).
  5. Smart-meter reads (current reading, meter health, consumption history).
  6. The generic passthrough for endpoints that don't have a wrapper.

Run it:
    export MSEDCL_LOGIN=your-login
    export MSEDCL_PASSWORD='your-password'     # single-quote if it has a #
    export MSEDCL_CNO=000000000000
    export MSEDCL_AMISP=002                     # for smart-meter calls
    # BU is optional — this script discovers it from the bill automatically
    python example_usage.py

Nothing secret is written to disk. Keep your password out of source control.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from maha_client import (
    MahaClient,
    MahaConfig,
    MahaError,
    MahaAuthError,
    MahaNotFound,
    MahaServerError,
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def show(label: str, value: Any) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False))


def try_call(label: str, fn):
    """Run one endpoint call, print the result, and never let one failure
    abort the whole demo. Returns the result (or None on failure)."""
    try:
        result = fn()
        show(label, result)
        return result
    except MahaAuthError as e:
        print(f"\n[{label}] AUTH FAILED: {e}")
    except MahaNotFound as e:
        print(f"\n[{label}] NOT FOUND: {e}")
    except MahaServerError as e:
        print(f"\n[{label}] SERVER ERROR: {e}")
    except MahaError as e:
        print(f"\n[{label}] ERROR: {e}")
    return None


def dig(obj: Any, *keys: str) -> Optional[Any]:
    """Safely walk nested dict keys; return None if any hop is missing."""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def find_billing_unit(recent_bill: Any) -> Optional[str]:
    """
    The BU lives on the bill payload. RecentBillHTTPIN wraps an EnergyBill
    under "bill", whose field is "billingUnit". We check a couple of shapes in
    case the JSON differs slightly from the decompiled DTO.
    """
    for path in (("bill", "billingUnit"), ("billingUnit",), ("bill", "billing_unit")):
        v = dig(recent_bill, *path)
        if v:
            return str(v)
    return None


# --------------------------------------------------------------------------- #
# client construction
# --------------------------------------------------------------------------- #
def build_client_from_env() -> MahaClient:
    """Recommended path — reads MSEDCL_* environment variables."""
    return MahaClient.from_env()


def build_client_explicitly() -> MahaClient:
    """
    Alternative — build the config in code. The password is prompted at runtime
    with getpass so it never sits in the source file or your shell history.
    """
    import getpass

    cfg = MahaConfig(
        login="your-login",
        password=getpass.getpass("MSEDCL password: "),
        consumer_no="000000000000",
        amisp="002",              # smart-meter routing code (from your account)
        # billing_unit left None on purpose — discovered from the bill below
        category="LT",
        lang="en",
        client_version="12.40",   # app versionName
        client_os_version="13",
        app_version_code=81,      # app versionCode
    )
    return MahaClient(cfg)


# --------------------------------------------------------------------------- #
# the demo
# --------------------------------------------------------------------------- #
def main() -> int:
    try:
        client = build_client_explicitly()
    except MahaError as e:
        print(f"Config error: {e}")
        print("Set at least MSEDCL_LOGIN, MSEDCL_PASSWORD, MSEDCL_CNO "
              "(and MSEDCL_AMISP for smart-meter calls).")
        print("Or switch main() to build_client_explicitly() for a prompt.")
        return 2

    print("Config:", client.cfg.redacted())

    # 1) Connectivity probe. StartupMessages takes no consumer number, so a 200
    #    here means the standard channel + Client-* headers are accepted.
    try_call("startup_messages (connectivity probe)", client.startup_messages)

    # 2) Latest bill.
    bill = try_call("recent_bill", client.recent_bill)

    # 3) Auto-discover BU from the bill and set it on the live config, so the
    #    BU-gated calls below work without you having to know it up front.
    if bill is not None and not client.cfg.billing_unit:
        bu = find_billing_unit(bill)
        if bu:
            client.cfg.billing_unit = bu
            print(f"\n[info] discovered billing_unit (BU) = {bu} from the bill")
        else:
            print("\n[info] couldn't find billingUnit in the bill payload; "
                  "BU-gated calls will be skipped. Inspect the recent_bill "
                  "output above and set MahaConfig.billing_unit manually.")

    # 4) BU-gated standard calls (only if we have a BU).
    if client.cfg.billing_unit:
        try_call("consumer_info (GetConInfo)", client.consumer_info)
        try_call("complaints", client.complaints)

    # other standard reads that only need the consumer number
    try_call("bill_history", client.bill_history)
    try_call("contact_details", client.contact_details)
    try_call("meter_reading_predata", client.meter_reading_predata)
    try_call("transaction_info", client.transaction_info)

    # 5) Smart-meter channel (Basic auth). Needs amisp configured.
    if client.cfg.amisp:
        try_call("current_reading", client.current_reading)
        try_call("meter_health", client.meter_health)
        try_call("monthly_consumption (this year)", client.monthly_consumption)
        # explicit period example:
        try_call("daily_consumption (202607)",
                 lambda: client.daily_consumption("202607"))
    else:
        print("\n[info] MSEDCL_AMISP not set — skipping smart-meter calls.")

    # 6) Generic passthrough — reach any endpoint without a wrapper.
    #    Example: the same StartupMessages call, done manually.
    try_call('raw("standard", "GET", "StartupMessages")',
             lambda: client.raw("standard", "GET", "StartupMessages",
                                params={"lang": client.cfg.lang}))

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
