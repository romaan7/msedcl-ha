"""Shared test scaffolding.

Two execution modes:

* CI / full dev env — homeassistant is installed: nothing is stubbed, the
  real package (including custom_components/msedcl/__init__.py) imports
  normally, and tests/components run against a live test hass.
* Local / lightweight — homeassistant is NOT installed: the handful of HA
  modules that api.py / models.py / statistics.py touch are stubbed, and
  `custom_components.msedcl` is registered as a bare package so importing
  submodules does NOT execute the real __init__.py (which needs full HA).
  tests/components skips itself via importorskip.

Sample payloads below mirror live responses (smoke tests of 2026-07-31),
sanitized of real PII.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


try:
    import homeassistant  # noqa: F401

    HA_AVAILABLE = True
except ImportError:
    HA_AVAILABLE = False
    _stub("homeassistant")
    _stub("homeassistant.components")
    _stub("homeassistant.components.recorder", get_instance=lambda hass: None)
    _stub(
        "homeassistant.components.recorder.statistics",
        async_add_external_statistics=lambda *a, **k: None,
        get_last_statistics=lambda *a, **k: {},
    )
    _stub(
        "homeassistant.components.recorder.models",
        StatisticMeanType=type("StatisticMeanType", (), {"NONE": 0}),
    )
    _stub(
        "homeassistant.const",
        UnitOfEnergy=type("UnitOfEnergy", (), {"KILO_WATT_HOUR": "kWh"}),
    )
    _stub("homeassistant.core", HomeAssistant=object)
    # Bare package parents: submodule imports work, real __init__.py doesn't run.
    cc = _stub("custom_components")
    cc.__path__ = [str(ROOT / "custom_components")]
    pkg = _stub("custom_components.msedcl")
    pkg.__path__ = [str(ROOT / "custom_components" / "msedcl")]


# --------------------------------------------------------------------------- #
# Sample payloads (live shapes, sanitized)
# --------------------------------------------------------------------------- #
CNO = "080010000000"

CONTACT_PAYLOAD = {
    "MobileNo": "0000000000",
    "EmailId": "x@example.com",
    "Uid": "000000000000",
    "Consumer": {
        "ConName": "TEST CONSUMER",
        "ConNumber": CNO,
        "BU": "0000",
        "BUName": "SAMPLE CITY (U-1)",
        "consumerCategory": "LT",
        "MeterNumber": "103-MH0000000",
        "connectedLoad": "5",
        "hasSmartMeterYN": "Y",
        "amispCode": "002",
        "amisp": "M/s NCC",
        "billingType": "postpaid",
        "solarRtYN": "Y",
        "tariffName": "LT-I  Residential 1Ph",
    },
}

READING_PAYLOAD = {
    "METER_NUMBER": "MH0000000",
    "READING_DATE_TIME": "20260731101057",
    "READING": "3109.786",
    "MTD_CONSUMPTION": "58.1",
    "MD": "0.0",
    "CURRENT_BALANCE": "0",
    "PREV_MTH_READING": "3051.686",
}

BILL_HISTORY_PAYLOAD = [
    {
        "BillMonth": "JUL-2026",
        "billDate": "19-Jul-26",
        "Consumption": "0",
        "Status": "LIVE",
        "CurrentBill": "-3910",
        "IsRecentYn": "Y",
    },
    {
        "BillMonth": "JUN-2026",
        "billDate": "18-Jun-26",
        "Consumption": "0",
        "Status": "LIVE",
        "CurrentBill": "-4050",
        "IsRecentYn": "N",
    },
]

# Real July-30 hourly profile (solar consumer: imports overnight, exports midday).
HOURLY_IMP_20260730 = [
    0.063, 0.149, 0.128, 0.145, 0.196, 0.156, 0.153, 0.095, 0.014, 0.006,
    0, 0, 0, 0, 0, 0, 0, 0, 0.032, 0.128, 0.191, 0.196, 0.192, 0.167, 0.069,
]
HOURLY_EXP_20260730 = [
    0, 0, 0, 0, 0, 0, 0, 0, 0.044, 0.143, 0.343, 0.437, 0.141, 0.343, 0.754,
    1.476, 1.105, 0.95, 0.189, 0.001, 0, 0, 0, 0, 0,
]


def hourly_rows(day: str, imports: list[float], exports: list[float]) -> list[dict]:
    """Build raw GetHourlyConsumption rows with the live 0,1..23,0 label shape."""
    return [
        {
            "DATE": day,
            "HOUR": f"{(0 if k in (0, 24) else k):02d}",
            "MINUTE": "00",
            "READING": str(imports[k]),
            "UNITS_IMPORTED": str(imports[k]),
            "UNITS_EXPORTED": str(exports[k]),
            "UNITS_NET": str(imports[k] - exports[k]),
        }
        for k in range(len(imports))
    ]


@pytest.fixture
def contact_payload() -> dict:
    return CONTACT_PAYLOAD


@pytest.fixture
def reading_payload() -> dict:
    return READING_PAYLOAD


@pytest.fixture
def bill_history_payload() -> list[dict]:
    return BILL_HISTORY_PAYLOAD
