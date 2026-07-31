"""Sensor entities for the MSEDCL integration (PLAN.md Phase 4).

Energy Dashboard note: the external statistics inserted by statistics.py
(msedcl:<cno>_grid_import / _grid_export) are the dashboard source of truth —
the live meter-reading sensor is informational. Selecting both double-counts.

Phase-3 gate RESOLVED (live smoke test, 2026-07-31): READING is the IMPORT
register (identical to READING_IMPORT, strictly monotonic over a full month on
a solar/net-metered account), so state_class=total_increasing is safe for the
live reading sensor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MsedclConfigEntry
from .const import (
    CONF_CONSUMER_NAME,
    CONF_CONSUMER_NO,
    CONF_METER_NUMBER,
    CONF_TARIFF,
    DOMAIN,
)
from .coordinator import BillingCoordinator, BillingData, MeterCoordinator, MeterData
from .models import IST, _get, _s


def _month_start_ist() -> datetime:
    """Start of the current month in IST — last_reset for the MTD sensor.

    Approximates the billing month with the calendar month; the true billing
    cycle boundary isn't exposed by the API.
    """
    now = datetime.now(IST)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _health_status(data: MeterData) -> StateType:
    """Best-effort status string from the (loosely specified) health payload."""
    if not data.health:
        return None
    return (
        _s(_get(data.health, "METER_STATUS", "MeterStatus", "STATUS", "Status",
                "RELAY_STATUS", "RelayStatus"))
        or "reported"
    )


@dataclass(frozen=True, kw_only=True)
class MsedclMeterSensorDescription(SensorEntityDescription):
    """Sensor backed by the MeterCoordinator."""

    value_fn: Callable[[MeterData], StateType | datetime]
    # Entity is only created when this returns True for the first fetch
    # (e.g. no balance field on postpaid, no health payload on some meters).
    exists_fn: Callable[[MeterData], bool] = lambda _: True
    attrs_fn: Callable[[MeterData], dict[str, Any] | None] = lambda _: None
    last_reset_fn: Callable[[], datetime] | None = None


@dataclass(frozen=True, kw_only=True)
class MsedclBillingSensorDescription(SensorEntityDescription):
    """Sensor backed by the BillingCoordinator."""

    value_fn: Callable[[BillingData], StateType]
    attrs_fn: Callable[[BillingData], dict[str, Any] | None] = lambda _: None


METER_SENSORS: tuple[MsedclMeterSensorDescription, ...] = (
    MsedclMeterSensorDescription(
        key="meter_reading",
        translation_key="meter_reading",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,  # Phase-3 gate, see module docstring
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda d: d.reading.reading,
    ),
    MsedclMeterSensorDescription(
        key="mtd_consumption",
        translation_key="mtd_consumption",
        device_class=SensorDeviceClass.ENERGY,
        # Resets monthly: TOTAL + last_reset, never bare TOTAL (PLAN.md [v3]).
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value_fn=lambda d: d.reading.mtd_consumption,
        last_reset_fn=_month_start_ist,
    ),
    MsedclMeterSensorDescription(
        key="max_demand",
        translation_key="max_demand",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        value_fn=lambda d: d.reading.md,
        exists_fn=lambda d: d.reading.md is not None,
    ),
    MsedclMeterSensorDescription(
        key="current_balance",
        translation_key="current_balance",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="INR",
        value_fn=lambda d: d.reading.current_balance,
        exists_fn=lambda d: d.reading.current_balance is not None,
    ),
    MsedclMeterSensorDescription(
        key="last_reading_time",
        translation_key="last_reading_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.reading.reading_dt,
    ),
    MsedclMeterSensorDescription(
        key="meter_health",
        translation_key="meter_health",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_health_status,
        exists_fn=lambda d: d.health is not None,
        attrs_fn=lambda d: d.health,
    ),
)

BILLING_SENSORS: tuple[MsedclBillingSensorDescription, ...] = (
    MsedclBillingSensorDescription(
        key="latest_bill_amount",
        translation_key="latest_bill_amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="INR",
        value_fn=lambda d: d.latest.current_bill if d.latest else None,
        attrs_fn=lambda d: {
            "bill_month": d.latest.bill_month,
            "bill_date": d.latest.bill_date,
            "status": d.latest.status,
            "last_receipt_amount": d.latest.last_receipt_amount,
            "last_receipt_date": d.latest.last_receipt_date,
        }
        if d.latest
        else None,
    ),
    MsedclBillingSensorDescription(
        key="latest_bill_consumption",
        translation_key="latest_bill_consumption",
        device_class=SensorDeviceClass.ENERGY,
        # No state_class on purpose (deviates from the PLAN table): this is a
        # per-bill figure, and TOTAL would count month-over-month differences
        # as consumption in long-term statistics.
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: d.latest.consumption if d.latest else None,
        attrs_fn=lambda d: {"bill_month": d.latest.bill_month} if d.latest else None,
    ),
)


def _device_info(entry: MsedclConfigEntry) -> DeviceInfo:
    """One HA device per consumer number."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_CONSUMER_NO])},
        name=entry.data.get(CONF_CONSUMER_NAME) or entry.title,
        manufacturer="MSEDCL (Mahavitaran)",
        model=entry.data.get(CONF_TARIFF) or "Consumer connection",
        serial_number=entry.data.get(CONF_METER_NUMBER),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MsedclConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    data = entry.runtime_data
    entities: list[SensorEntity] = []

    if data.meter and data.meter.data:
        entities.extend(
            MsedclMeterSensor(data.meter, entry, desc)
            for desc in METER_SENSORS
            if desc.exists_fn(data.meter.data)
        )

    # Billing entities are always created; if the (best-effort) billing channel
    # was down at setup they sit unavailable until it recovers.
    entities.extend(
        MsedclBillingSensor(data.billing, entry, desc) for desc in BILLING_SENSORS
    )

    async_add_entities(entities)


class MsedclMeterSensor(CoordinatorEntity[MeterCoordinator], SensorEntity):
    """Sensor fed by the smart-meter coordinator."""

    entity_description: MsedclMeterSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MeterCoordinator,
        entry: MsedclConfigEntry,
        description: MsedclMeterSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.data[CONF_CONSUMER_NO]}_{description.key}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> StateType | datetime:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def last_reset(self) -> datetime | None:
        if self.entity_description.last_reset_fn is None:
            return None
        return self.entity_description.last_reset_fn()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


class MsedclBillingSensor(CoordinatorEntity[BillingCoordinator], SensorEntity):
    """Sensor fed by the billing coordinator."""

    entity_description: MsedclBillingSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BillingCoordinator,
        entry: MsedclConfigEntry,
        description: MsedclBillingSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.data[CONF_CONSUMER_NO]}_{description.key}"
        self._attr_device_info = _device_info(entry)

    @property
    def native_value(self) -> StateType:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
