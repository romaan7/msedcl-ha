"""Diagnostics for the MSEDCL integration.

Redacts credentials AND PII: mobile, email, UID, address lines, consumer
number, meter number and name all appear in API payloads and diagnostics get
pasted into GitHub issues (PLAN.md Phase 6).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import MsedclConfigEntry
from .const import (
    CONF_CONSUMER_NAME,
    CONF_CONSUMER_NO,
    CONF_METER_NUMBER,
)

TO_REDACT = {
    # entry data
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_CONSUMER_NO,
    CONF_CONSUMER_NAME,
    CONF_METER_NUMBER,
    # model fields
    "consumer_no",
    "name",
    "meter_number",
    "mobile",
    "email",
    "uid",
    "address_lines",
    # raw payload keys (CurrentReading.raw / meter_health dict)
    "METER_NUMBER",
    "MeterNumber",
    "ConNumber",
    "ConName",
    "MobileNo",
    "EmailId",
    "Uid",
    "CONSUMER_NUMBER",
    "addressLine1",
    "addressLine2",
    "addressLine3",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MsedclConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    data = entry.runtime_data

    meter: dict[str, Any] | None = None
    if data.meter is not None:
        meter = {
            "last_update_success": data.meter.last_update_success,
            "data": asdict(data.meter.data) if data.meter.data else None,
        }

    billing = {
        "last_update_success": data.billing.last_update_success,
        "data": asdict(data.billing.data) if data.billing.data else None,
    }

    return async_redact_data(
        {
            "entry_data": dict(entry.data),
            "options": dict(entry.options),
            "meter": meter,
            "billing": billing,
        },
        TO_REDACT,
    )
