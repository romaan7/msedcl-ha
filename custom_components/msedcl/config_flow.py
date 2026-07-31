"""Config flow for the MSEDCL (Mahavitaran) integration.

User enters only login / password / consumer number. Everything else — BU,
amispCode, hasSmartMeterYN, meter number, tariff — is auto-discovered from a
single contact_details call (PLAN.md Phase 2), which doubles as validation of
the standard channel.

The standard channel carries NO authentication, so contact_details cannot
validate the Basic credentials. For consumers with a smart meter we therefore
also probe current_reading (the smart channel is the only place credentials
are checked). For billing-only consumers the credentials are stored but unused
until MSEDCL exposes something that needs them.
"""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MahaApiClient, MahaAuthError, MahaError, MahaNotFound
from .const import (
    CONF_AMISP,
    CONF_BILLING_TYPE,
    CONF_BILLING_UNIT,
    CONF_CATEGORY,
    CONF_CONSUMER_NAME,
    CONF_CONSUMER_NO,
    CONF_HAS_SMART_METER,
    CONF_METER_NUMBER,
    CONF_TARIFF,
    DEFAULT_BILLING_INTERVAL_H,
    DEFAULT_METER_INTERVAL_MIN,
    DEFAULT_STATS_INTERVAL_H,
    DOMAIN,
    MAX_BILLING_INTERVAL_H,
    MAX_METER_INTERVAL_MIN,
    MAX_STATS_INTERVAL_H,
    MIN_BILLING_INTERVAL_H,
    MIN_METER_INTERVAL_MIN,
    MIN_STATS_INTERVAL_H,
    OPT_BILLING_INTERVAL,
    OPT_METER_INTERVAL,
    OPT_STATS_INTERVAL,
)
from .models import ConsumerInfo

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_CONSUMER_NO): str,
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _async_validate(
    hass: HomeAssistant, username: str, password: str, consumer_no: str
) -> dict[str, Any]:
    """Validate input and return the discovered entry data."""
    client = MahaApiClient(
        async_get_clientsession(hass), username, password, consumer_no
    )

    raw = await client.contact_details()
    if not isinstance(raw, dict):
        raise MahaNotFound("contact_details returned no consumer block")
    info = ConsumerInfo.from_contact_details(raw)
    if not (info.consumer_no or info.name or info.bu):
        # Standard channel answered, but knows nothing about this number.
        raise MahaNotFound("unknown consumer number")

    has_smart_meter = bool(info.has_smart_meter and info.amisp_code)
    if has_smart_meter:
        # The only credential check available: Basic auth on the smart channel.
        client.amisp = info.amisp_code
        await client.current_reading()  # raises MahaAuthError on bad creds

    return {
        CONF_USERNAME: username,
        CONF_PASSWORD: password,
        CONF_CONSUMER_NO: consumer_no,
        CONF_AMISP: info.amisp_code,
        CONF_BILLING_UNIT: info.bu,
        CONF_HAS_SMART_METER: has_smart_meter,
        CONF_METER_NUMBER: info.meter_number,
        CONF_CONSUMER_NAME: info.name,
        CONF_TARIFF: info.tariff_name,
        CONF_CATEGORY: info.category or "LT",
        CONF_BILLING_TYPE: info.billing_type,
    }


class MsedclConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle setup and reauth."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initial setup: credentials + consumer number."""
        errors: dict[str, str] = {}
        if user_input is not None:
            consumer_no = user_input[CONF_CONSUMER_NO].strip()
            if not re.fullmatch(r"\d{12}", consumer_no):
                errors[CONF_CONSUMER_NO] = "invalid_consumer_no"
            else:
                await self.async_set_unique_id(consumer_no)
                self._abort_if_unique_id_configured()
                try:
                    data = await _async_validate(
                        self.hass,
                        user_input[CONF_USERNAME].strip(),
                        user_input[CONF_PASSWORD],
                        consumer_no,
                    )
                except MahaAuthError:
                    errors["base"] = "invalid_auth"
                except MahaNotFound:
                    errors[CONF_CONSUMER_NO] = "invalid_consumer_no"
                except MahaError:
                    errors["base"] = "cannot_connect"
                else:
                    title = data.get(CONF_CONSUMER_NAME) or f"MSEDCL {consumer_no}"
                    return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Credentials were rejected (401 on the smart channel)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt for new credentials and revalidate."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = await _async_validate(
                    self.hass,
                    user_input[CONF_USERNAME].strip(),
                    user_input[CONF_PASSWORD],
                    entry.data[CONF_CONSUMER_NO],
                )
            except MahaAuthError:
                errors["base"] = "invalid_auth"
            except MahaError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_REAUTH_SCHEMA, {CONF_USERNAME: entry.data.get(CONF_USERNAME)}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> "MsedclOptionsFlow":
        """Return the options flow handler."""
        return MsedclOptionsFlow()


class MsedclOptionsFlow(OptionsFlow):
    """Post-setup tuning of the two poll intervals."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_METER_INTERVAL,
                    default=options.get(
                        OPT_METER_INTERVAL, DEFAULT_METER_INTERVAL_MIN
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_METER_INTERVAL_MIN, max=MAX_METER_INTERVAL_MIN),
                ),
                vol.Required(
                    OPT_BILLING_INTERVAL,
                    default=options.get(
                        OPT_BILLING_INTERVAL, DEFAULT_BILLING_INTERVAL_H
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_BILLING_INTERVAL_H, max=MAX_BILLING_INTERVAL_H),
                ),
                vol.Required(
                    OPT_STATS_INTERVAL,
                    default=options.get(OPT_STATS_INTERVAL, DEFAULT_STATS_INTERVAL_H),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_STATS_INTERVAL_H, max=MAX_STATS_INTERVAL_H),
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
