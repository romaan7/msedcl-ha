"""Config-flow tests: happy path, auto-discovery, error mapping, reauth, options."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.msedcl.api import MahaAuthError, MahaError
from custom_components.msedcl.const import (
    CONF_AMISP,
    CONF_BILLING_UNIT,
    CONF_CONSUMER_NO,
    CONF_HAS_SMART_METER,
    DOMAIN,
    OPT_BILLING_INTERVAL,
    OPT_METER_INTERVAL,
    OPT_STATS_INTERVAL,
)
from tests.conftest import CNO, CONTACT_PAYLOAD, READING_PAYLOAD

USER_INPUT = {CONF_USERNAME: "user", CONF_PASSWORD: "secret", CONF_CONSUMER_NO: CNO}


def _mock_client(contact=CONTACT_PAYLOAD, reading=READING_PAYLOAD):
    client = AsyncMock()
    client.contact_details = AsyncMock(return_value=contact)
    client.current_reading = AsyncMock(return_value=reading)
    return client


async def _start_user_flow(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_discovers_and_creates_entry(hass: HomeAssistant) -> None:
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM

    client = _mock_client()
    with (
        patch(
            "custom_components.msedcl.config_flow.MahaApiClient",
            return_value=client,
        ),
        patch("custom_components.msedcl.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "TEST CONSUMER"
    data = result["data"]
    assert data[CONF_CONSUMER_NO] == CNO
    assert data[CONF_AMISP] == "002"          # discovered
    assert data[CONF_BILLING_UNIT] == "0000"  # discovered
    assert data[CONF_HAS_SMART_METER] is True
    # credentials were validated on the smart channel
    client.current_reading.assert_awaited_once()


async def test_user_flow_no_smart_meter_skips_credential_probe(
    hass: HomeAssistant,
) -> None:
    contact = {
        **CONTACT_PAYLOAD,
        "Consumer": {**CONTACT_PAYLOAD["Consumer"], "hasSmartMeterYN": "N"},
    }
    client = _mock_client(contact=contact)
    result = await _start_user_flow(hass)
    with (
        patch(
            "custom_components.msedcl.config_flow.MahaApiClient",
            return_value=client,
        ),
        patch("custom_components.msedcl.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HAS_SMART_METER] is False
    client.current_reading.assert_not_awaited()


@pytest.mark.parametrize(
    ("exc", "error_key", "error_field"),
    [
        (MahaAuthError("nope"), "invalid_auth", "base"),
        (MahaError("down"), "cannot_connect", "base"),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant, exc: Exception, error_key: str, error_field: str
) -> None:
    client = _mock_client()
    if isinstance(exc, MahaAuthError):
        client.current_reading.side_effect = exc
    else:
        client.contact_details.side_effect = exc

    result = await _start_user_flow(hass)
    with patch(
        "custom_components.msedcl.config_flow.MahaApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {error_field: error_key}


async def test_user_flow_rejects_malformed_consumer_no(hass: HomeAssistant) -> None:
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_CONSUMER_NO: "12345"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CONSUMER_NO: "invalid_consumer_no"}


async def test_user_flow_unknown_consumer(hass: HomeAssistant) -> None:
    client = _mock_client(contact={"Consumer": {}})
    result = await _start_user_flow(hass)
    with patch(
        "custom_components.msedcl.config_flow.MahaApiClient", return_value=client
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CONSUMER_NO: "invalid_consumer_no"}


async def test_user_flow_aborts_on_duplicate(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=CNO, data={}).add_to_hass(hass)
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow_updates_credentials(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CNO,
        data={**USER_INPUT, CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1

    client = _mock_client()
    with (
        patch(
            "custom_components.msedcl.config_flow.MahaApiClient",
            return_value=client,
        ),
        patch("custom_components.msedcl.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"],
            {CONF_USERNAME: "user", CONF_PASSWORD: "new-secret"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"


async def test_options_flow_roundtrip(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=CNO, data=USER_INPUT)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {OPT_METER_INTERVAL: 60, OPT_BILLING_INTERVAL: 12, OPT_STATS_INTERVAL: 2},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[OPT_METER_INTERVAL] == 60
    assert entry.options[OPT_STATS_INTERVAL] == 2
