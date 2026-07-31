"""DataUpdateCoordinators for the MSEDCL integration.

Two coordinators with different natural cadences (PLAN.md Phase 3):

  * MeterCoordinator   — fast (default 30 min): current_reading (+ meter_health
    where supported). Smart-meter channel, Basic auth — 401 here means bad
    credentials and triggers reauth.
  * BillingCoordinator — slow (default 8 h): bill_history + transaction_info.
    Standard channel, no auth. Its failures raise UpdateFailed in isolation and
    must never take down the meter sensors.

Both track consecutive server errors and raise an issue-registry warning after
SERVER_ERRORS_BEFORE_REPAIR cycles: the known API-drift failure mode is a
persistent 500 (not 401), which a reauth flow would never catch.

Jitter note: HA schedules coordinator refreshes relative to entry setup time
with a randomized sub-second offset, so installs don't align on :00 — no extra
jitter machinery is needed here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    MahaApiClient,
    MahaAuthError,
    MahaError,
    MahaNotFound,
    MahaServerError,
)
from .const import (
    DOMAIN,
    ISSUE_API_DRIFT,
    SERVER_ERRORS_BEFORE_REPAIR,
)
from .models import BillHistoryRow, ConsumerInfo, CurrentReading

_LOGGER = logging.getLogger(__name__)


@dataclass
class MeterData:
    """Result of one smart-meter poll."""

    reading: CurrentReading
    health: dict[str, Any] | None


@dataclass
class BillingData:
    """Result of one billing poll."""

    rows: list[BillHistoryRow]
    latest: BillHistoryRow | None
    consumer: ConsumerInfo | None


class MsedclCoordinator[_DataT](DataUpdateCoordinator[_DataT]):
    """Base coordinator: shared client + persistent-5xx repair issue."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: MahaApiClient,
        name: str,
        update_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{name}",
            update_interval=update_interval,
        )
        self.client = client
        self._server_failures = 0
        self._issue_id = f"{ISSUE_API_DRIFT}_{entry.entry_id}_{name}"

    def _record_server_failure(self) -> None:
        self._server_failures += 1
        if self._server_failures == SERVER_ERRORS_BEFORE_REPAIR:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_API_DRIFT,
                translation_placeholders={"coordinator": self.name},
            )

    def _record_success(self) -> None:
        if self._server_failures >= SERVER_ERRORS_BEFORE_REPAIR:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        self._server_failures = 0


class MeterCoordinator(MsedclCoordinator[MeterData]):
    """Polls the smart-meter channel."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # None = not probed yet; False = 404'd once, never ask again.
        self._health_supported: bool | None = None

    async def _async_update_data(self) -> MeterData:
        try:
            raw = await self.client.current_reading()
        except MahaAuthError as err:
            # Basic credentials rejected -> HA reauth flow.
            raise ConfigEntryAuthFailed(str(err)) from err
        except MahaServerError as err:
            self._record_server_failure()
            raise UpdateFailed(f"Smart-meter API error: {err}") from err
        except MahaError as err:
            raise UpdateFailed(f"Smart-meter API unreachable: {err}") from err

        reading = CurrentReading.parse(raw if isinstance(raw, dict) else {})

        health: dict[str, Any] | None = None
        if self._health_supported is not False:
            try:
                raw_health = await self.client.meter_health()
                if isinstance(raw_health, dict):
                    health = raw_health
                self._health_supported = True
            except (MahaNotFound, MahaAuthError):
                # Some meters don't expose health at all (404), and some
                # accounts get an endpoint-level 401 on GetMeterHealth even
                # though the same credentials pass GetCurrentReading (observed
                # live 2026-07-31). Either way: not supported, stop asking.
                self._health_supported = False
            except MahaError as err:
                _LOGGER.debug("meter_health failed (non-fatal): %s", err)

        self._record_success()
        return MeterData(reading=reading, health=health)


class BillingCoordinator(MsedclCoordinator[BillingData]):
    """Polls the standard/billing channel (best-effort by design)."""

    async def _async_update_data(self) -> BillingData:
        try:
            raw_hist = await self.client.bill_history()
        except MahaServerError as err:
            self._record_server_failure()
            raise UpdateFailed(f"Billing API error: {err}") from err
        except MahaError as err:
            # Includes MahaAuthError: the standard channel sends no auth, so a
            # 401 here is API drift, not bad credentials — don't reauth-loop.
            raise UpdateFailed(f"Billing API unreachable: {err}") from err

        rows = BillHistoryRow.parse_list(raw_hist)
        latest = next((r for r in rows if r.is_recent), rows[0] if rows else None)

        consumer: ConsumerInfo | None = None
        try:
            raw_txn = await self.client.transaction_info()
            if isinstance(raw_txn, dict):
                consumer = ConsumerInfo.from_transaction_info(raw_txn)
        except MahaError as err:
            _LOGGER.debug("transaction_info failed (non-fatal): %s", err)

        self._record_success()
        return BillingData(rows=rows, latest=latest, consumer=consumer)
