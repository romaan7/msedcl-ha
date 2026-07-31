"""Setup/teardown tests: coordinator isolation, auth failure, retry, unload."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.msedcl.api import (
    MahaAuthError,
    MahaError,
    MahaNotFound,
    MahaServerError,
)
from custom_components.msedcl.const import (
    CONF_AMISP,
    CONF_BILLING_TYPE,
    CONF_BILLING_UNIT,
    CONF_CATEGORY,
    CONF_CONSUMER_NAME,
    CONF_CONSUMER_NO,
    CONF_HAS_SMART_METER,
    CONF_METER_NUMBER,
    CONF_TARIFF,
    DOMAIN,
)
from tests.conftest import BILL_HISTORY_PAYLOAD, CNO, READING_PAYLOAD

ENTRY_DATA = {
    CONF_USERNAME: "user",
    CONF_PASSWORD: "secret",
    CONF_CONSUMER_NO: CNO,
    CONF_AMISP: "002",
    CONF_BILLING_UNIT: "0000",
    CONF_HAS_SMART_METER: True,
    CONF_METER_NUMBER: "103-MH0000000",
    CONF_CONSUMER_NAME: "Test Consumer",
    CONF_TARIFF: "LT-I Residential 1Ph",
    CONF_CATEGORY: "LT",
    CONF_BILLING_TYPE: "postpaid",
}


def _client(**overrides) -> MagicMock:
    client = MagicMock()
    client.current_reading = AsyncMock(return_value=READING_PAYLOAD)
    client.meter_health = AsyncMock(side_effect=MahaNotFound("no health"))
    client.bill_history = AsyncMock(return_value=BILL_HISTORY_PAYLOAD)
    client.transaction_info = AsyncMock(return_value={})
    for name, value in overrides.items():
        setattr(client, name, value)
    return client


def _stats() -> MagicMock:
    stats = MagicMock()
    stats.async_update = AsyncMock()
    return stats


async def _setup(hass: HomeAssistant, client: MagicMock, data: dict | None = None):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=CNO, data=data or ENTRY_DATA)
    entry.add_to_hass(hass)
    with (
        patch("custom_components.msedcl.MahaApiClient", return_value=client),
        patch("custom_components.msedcl.MsedclStatistics", return_value=_stats()),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _state(hass: HomeAssistant, metric: str):
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{CNO}_{metric}"
    )
    return hass.states.get(entity_id) if entity_id else None


async def test_setup_creates_meter_and_billing_sensors(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _client())
    assert entry.state is ConfigEntryState.LOADED

    assert _state(hass, "meter_reading").state == "3109.786"
    assert _state(hass, "mtd_consumption").state == "58.1"
    assert _state(hass, "latest_bill_amount").state == "-3910.0"
    # health 404'd -> entity intentionally not created
    assert _state(hass, "meter_health") is None


async def test_billing_failure_does_not_kill_meter_sensors(
    hass: HomeAssistant,
) -> None:
    client = _client(bill_history=AsyncMock(side_effect=MahaServerError("500")))
    entry = await _setup(hass, client)

    # setup still succeeds and meter data is live
    assert entry.state is ConfigEntryState.LOADED
    assert _state(hass, "meter_reading").state == "3109.786"
    # billing entities exist but are unavailable
    assert _state(hass, "latest_bill_amount").state == STATE_UNAVAILABLE


async def test_auth_failure_starts_reauth(hass: HomeAssistant) -> None:
    client = _client(current_reading=AsyncMock(side_effect=MahaAuthError("401")))
    entry = await _setup(hass, client)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_api_down_retries_setup(hass: HomeAssistant) -> None:
    client = _client(current_reading=AsyncMock(side_effect=MahaError("net")))
    entry = await _setup(hass, client)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_billing_only_setup_without_smart_meter(hass: HomeAssistant) -> None:
    data = {**ENTRY_DATA, CONF_HAS_SMART_METER: False, CONF_AMISP: None}
    client = _client()
    entry = await _setup(hass, client, data=data)

    assert entry.state is ConfigEntryState.LOADED
    client.current_reading.assert_not_awaited()
    assert _state(hass, "meter_reading") is None
    assert _state(hass, "latest_bill_amount").state == "-3910.0"


async def test_unload(hass: HomeAssistant) -> None:
    entry = await _setup(hass, _client())
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
