"""The MSEDCL (Mahavitaran) integration.

Unofficial, reverse-engineered API — see PLAN.md for the design contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .api import MahaApiClient
from .const import (
    CONF_AMISP,
    CONF_BILLING_UNIT,
    CONF_CATEGORY,
    CONF_CONSUMER_NO,
    CONF_HAS_SMART_METER,
    DEFAULT_BILLING_INTERVAL_H,
    DEFAULT_METER_INTERVAL_MIN,
    DEFAULT_STATS_INTERVAL_H,
    OPT_BILLING_INTERVAL,
    OPT_METER_INTERVAL,
    OPT_STATS_INTERVAL,
)
from .coordinator import BillingCoordinator, MeterCoordinator
from .statistics import MsedclStatistics

PLATFORMS: list[Platform] = [Platform.SENSOR]


@dataclass
class MsedclRuntimeData:
    """Runtime objects stored on the config entry."""

    client: MahaApiClient
    meter: MeterCoordinator | None
    billing: BillingCoordinator


type MsedclConfigEntry = ConfigEntry[MsedclRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: MsedclConfigEntry) -> bool:
    """Set up one MSEDCL consumer connection."""
    # Shared HA session is safe here: api.py sets auth/headers per request and
    # never mutates session state.
    client = MahaApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data[CONF_CONSUMER_NO],
        amisp=entry.data.get(CONF_AMISP) or "",
        billing_unit=entry.data.get(CONF_BILLING_UNIT),
        category=entry.data.get(CONF_CATEGORY) or "LT",
    )

    has_smart_meter = bool(
        entry.data.get(CONF_HAS_SMART_METER) and entry.data.get(CONF_AMISP)
    )

    meter: MeterCoordinator | None = None
    if has_smart_meter:
        meter = MeterCoordinator(
            hass,
            entry,
            client,
            "meter",
            timedelta(
                minutes=entry.options.get(
                    OPT_METER_INTERVAL, DEFAULT_METER_INTERVAL_MIN
                )
            ),
        )

    billing = BillingCoordinator(
        hass,
        entry,
        client,
        "billing",
        timedelta(
            hours=entry.options.get(OPT_BILLING_INTERVAL, DEFAULT_BILLING_INTERVAL_H)
        ),
    )

    if meter:
        # Smart-meter data is the core of the integration: fail setup (and let
        # HA retry via ConfigEntryNotReady) if it can't be fetched at boot.
        await meter.async_config_entry_first_refresh()
        # Billing is best-effort by design (partly-verified channel): a failure
        # here must not take down the meter sensors, so plain refresh.
        await billing.async_refresh()
    else:
        # Billing-only setup (hasSmartMeterYN=N): billing must work.
        await billing.async_config_entry_first_refresh()

    entry.runtime_data = MsedclRuntimeData(client=client, meter=meter, billing=billing)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    if meter:
        # Energy-Dashboard statistics: one run now (background — never blocks
        # setup), then on its own slow schedule, decoupled from meter polling.
        stats = MsedclStatistics(
            hass, entry.title, entry.data[CONF_CONSUMER_NO], client
        )
        entry.async_create_background_task(
            hass, stats.async_update(), f"{entry.entry_id}_statistics_initial"
        )
        entry.async_on_unload(
            async_track_time_interval(
                hass,
                stats.async_update,
                timedelta(
                    hours=entry.options.get(
                        OPT_STATS_INTERVAL, DEFAULT_STATS_INTERVAL_H
                    )
                ),
            )
        )

    return True


async def _async_update_listener(hass: HomeAssistant, entry: MsedclConfigEntry) -> None:
    """Reload the entry when options (poll intervals) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: MsedclConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: MsedclConfigEntry) -> bool:
    """Migrate old config entries (stub — entry schema is at version 1)."""
    if entry.version > 1:
        # Downgrade from a future version: refuse.
        return False
    return True
