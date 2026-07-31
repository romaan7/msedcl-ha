"""Async client for MSEDCL's (Mahavitaran) consumer backend.

Port of src/maha_client.py from requests to aiohttp. Two channels, two auth
schemes (from the official app's HTTP client wiring):

  STANDARD channel  -> {HOST}/App_Requests/
      Sends ONLY Client-Os / Client-Os-Version / Client-Version headers,
      NO Basic auth. Serves contact details, bill history, transactions, etc.

  SMART-METER channel -> {HOST}/consappsmartmeterapi-2.1.0/
      Sends ONLY HTTP Basic auth, NO Client-* headers.
      Serves GetCurrentReading, GetDailyConsumption, etc.

Design rules (see PLAN.md):
  * Never call SignIn — it 500s outside the official app. All reads work with
    stored credentials sent per request; assumption is they are long-lived.
  * Auth/headers are set PER REQUEST, never on the session, so the (possibly
    shared) aiohttp session is never polluted for other users.
  * Retry connection errors + 502/503/504 only, NEVER 500 — a 500 from this
    backend is usually a deterministic "bad params / no data".
  * recent_bill (RecentBill) is dead (returns 400) and deliberately absent.
    Use bill_history / transaction_info instead.
  * Log lines never contain the consumer number or credentials.
  * Date defaults are computed in IST — the API's wall clock — not server-local
    time, so a HA host in any timezone asks for the right IST day/month.

This module must stay importable standalone (aiohttp + stdlib only, no
homeassistant or package-relative imports) so scripts/smoke_test.py can use it
outside HA.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiohttp

try:
    from zoneinfo import ZoneInfo

    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - Windows without tzdata
    IST = timezone(timedelta(hours=5, minutes=30), "IST")

_LOGGER = logging.getLogger(__name__)

HOST = "https://mobileapp.mahadiscom.in"
BASE_STANDARD = f"{HOST}/App_Requests"
BASE_SMART = f"{HOST}/consappsmartmeterapi-2.1.0"

DEFAULT_TIMEOUT = 15.0
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 0.5          # seconds; doubles per attempt
RETRY_STATUS = (502, 503, 504)

DEFAULT_CLIENT_VERSION = "12.40"    # app versionName -> Client-Version header
DEFAULT_CLIENT_OS_VERSION = "13"    # android release -> Client-Os-Version header
DEFAULT_APP_VERSION_CODE = 81       # app versionCode -> `appversion` query param


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class MahaError(Exception):
    """Base class for all client errors."""


class MahaAuthError(MahaError):
    """401/403 — bad or missing credentials (smart-meter channel only)."""


class MahaNotFound(MahaError):
    """404 — no data for that consumer / date / endpoint."""


class MahaServerError(MahaError):
    """5xx — backend threw (often bad params or 'no data' server-side)."""


class MahaResponseError(MahaError):
    """2xx but the body wasn't the JSON we expected."""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class MahaApiClient:
    """Async client over the standard + smart-meter channels.

    Methods return parsed JSON (dict / list) or raw text if the body isn't
    JSON (the standard channel sometimes returns raw scalars). Apply the typed
    models from models.py on top.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        login: str,
        password: str,
        consumer_no: str,
        *,
        amisp: str = "",
        billing_unit: Optional[str] = None,
        category: str = "LT",
        lang: str = "en",
        client_version: str = DEFAULT_CLIENT_VERSION,
        client_os_version: str = DEFAULT_CLIENT_OS_VERSION,
        app_version_code: int = DEFAULT_APP_VERSION_CODE,
        timeout: float = DEFAULT_TIMEOUT,
        host: str = HOST,
    ) -> None:
        self._session = session
        # Basic auth header built by hand: aiohttp's BasicAuth/auth= is
        # deprecated (removed in aiohttp 4), and the app sends exactly
        # base64("login:password") anyway.
        token = base64.b64encode(f"{login}:{password}".encode()).decode()
        self._auth_header = f"Basic {token}"
        self._base_standard = f"{host}/App_Requests"
        self._base_smart = f"{host}/consappsmartmeterapi-2.1.0"
        self.consumer_no = consumer_no
        self.amisp = amisp
        self.billing_unit = billing_unit
        self.category = category
        self.lang = lang
        self.app_version_code = app_version_code
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._std_headers = {
            "Client-Os": "ANDROID",
            "Client-Os-Version": str(client_os_version),
            "Client-Version": str(client_version),
            "Accept": "application/json",
        }

    # ---- helpers ---------------------------------------------------------- #
    def _redact(self, text: str) -> str:
        """Strip the consumer number out of anything that gets logged/raised."""
        if self.consumer_no:
            return text.replace(self.consumer_no, "<cno>")
        return text

    @staticmethod
    def _now_ist() -> datetime:
        return datetime.now(tz=IST)

    def _require(self, **kv: Any) -> None:
        missing = [k for k, v in kv.items() if not v]
        if missing:
            raise MahaError(
                f"Missing required config for this call: {', '.join(missing)}"
            )

    # ---- low-level -------------------------------------------------------- #
    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        # drop None-valued params so we don't send literal "None"
        if params:
            params = {k: str(v) for k, v in params.items() if v is not None}
        safe_url = self._redact(url)
        last_err: Optional[Exception] = None

        for attempt in range(RETRY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(RETRY_BACKOFF * 2 ** (attempt - 1))
            try:
                async with self._session.request(
                    method, url,
                    headers=headers, params=params,
                    timeout=self._timeout,
                ) as resp:
                    status = resp.status
                    if status in RETRY_STATUS and attempt < RETRY_ATTEMPTS - 1:
                        _LOGGER.debug(
                            "Retryable status %s from %s (attempt %d/%d)",
                            status, safe_url, attempt + 1, RETRY_ATTEMPTS,
                        )
                        continue
                    if status in (401, 403):
                        raise MahaAuthError(
                            f"Auth rejected ({status}) for {safe_url}"
                        )
                    if status == 404:
                        raise MahaNotFound(f"Not found (404): {safe_url}")
                    text = await resp.text()
                    if status >= 500:
                        raise MahaServerError(
                            f"Server error ({status}) for {safe_url}. Often bad "
                            f"params/no data. Body: {self._redact(text[:300])!r}"
                        )
                    if status >= 400:
                        raise MahaError(
                            f"Unexpected status {status} for {safe_url}"
                        )
                    text = text.strip()
                    if not text:
                        return None
                    try:
                        return json.loads(text)
                    except ValueError:
                        # standard channel sometimes returns raw scalars/strings
                        return text
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as err:
                last_err = err
                _LOGGER.debug(
                    "Connection error calling %s (attempt %d/%d): %s",
                    safe_url, attempt + 1, RETRY_ATTEMPTS,
                    self._redact(str(err)),
                )
                continue

        raise MahaError(
            f"Network error calling {safe_url}: {self._redact(str(last_err))}"
        )

    async def _get_standard(self, path: str, **params: Any) -> Any:
        return await self._request(
            "GET", f"{self._base_standard}/{path.strip('/')}",
            headers=self._std_headers, params=params,
        )

    async def _get_smart(self, path: str) -> Any:
        return await self._request(
            "GET", f"{self._base_smart}/{path.strip('/')}",
            headers={
                "Accept": "application/json",
                "Authorization": self._auth_header,
            },
        )

    # ===================================================================== #
    # STANDARD channel — read-only account/bill methods (scoped to own cno)
    # ===================================================================== #
    async def contact_details(self) -> Any:
        """getContactDetails — full consumer block + registered mobile/email.

        The setup-validation and auto-discovery source: returns BU, amispCode,
        MeterNumber, hasSmartMeterYN, tariff, circle/division/zone.
        """
        return await self._get_standard("getContactDetails", Con=self.consumer_no)

    async def consumer_info(self) -> Any:
        """GetConInfo — consumer info. Requires billing_unit (BU)."""
        self._require(billing_unit=self.billing_unit)
        return await self._get_standard(
            "GetConInfo", Con=self.consumer_no, BU=self.billing_unit
        )

    async def bill_history(self) -> Any:
        """GetBillHistory — historical bills; IsRecentYn='Y' row = latest."""
        return await self._get_standard(
            "GetBillHistory", consumerno=self.consumer_no, cat=self.category
        )

    async def transaction_info(self) -> Any:
        """Transaction/Info — payment/transaction info (+ consumerInfo block)."""
        return await self._get_standard("Transaction/Info", cno=self.consumer_no)

    async def meter_reading_predata(self) -> Any:
        """SubmitMRNew/GetPreData — self-reading eligibility / last reading."""
        return await self._get_standard(
            "SubmitMRNew/GetPreData", consumerno=self.consumer_no
        )

    async def prepaid_recharge_info(self) -> Any:
        """PrepaidRecharge/Info — prepaid balance / recharge info."""
        return await self._get_standard(
            "PrepaidRecharge/Info",
            cno=self.consumer_no, deviceos="ANDROID",
            appversion=self.app_version_code, lang=self.lang,
        )

    async def startup_messages(self) -> Any:
        """StartupMessages — app banner messages (handy no-cno 200 probe)."""
        return await self._get_standard("StartupMessages", lang=self.lang)

    # ===================================================================== #
    # SMART-METER channel (Basic auth; requires amisp)
    # ===================================================================== #
    async def current_reading(self) -> Any:
        self._require(amisp=self.amisp)
        return await self._get_smart(
            f"{self.amisp}/GetCurrentReading/{self.consumer_no}"
        )

    async def meter_health(self) -> Any:
        self._require(amisp=self.amisp)
        return await self._get_smart(
            f"{self.amisp}/GetMeterHealth/{self.consumer_no}"
        )

    async def daily_consumption(self, month: Optional[str] = None) -> Any:
        """Per-day consumption for a month; `month` = YYYYMM (default: this IST month)."""
        self._require(amisp=self.amisp)
        month = month or self._now_ist().strftime("%Y%m")
        return await self._get_smart(
            f"{self.amisp}/GetDailyConsumption/{self.consumer_no}/{month}"
        )

    async def hourly_consumption(self, day: Optional[str] = None) -> Any:
        """Per-hour consumption for a day; `day` = YYYYMMDD (default: today, IST)."""
        self._require(amisp=self.amisp)
        day = day or self._now_ist().strftime("%Y%m%d")
        return await self._get_smart(
            f"{self.amisp}/GetHourlyConsumption/{self.consumer_no}/{day}"
        )

    async def monthly_consumption(self, year: Optional[str] = None) -> Any:
        """Per-month consumption for a year; `year` = YYYY (default: this IST year)."""
        self._require(amisp=self.amisp)
        year = year or self._now_ist().strftime("%Y")
        return await self._get_smart(
            f"{self.amisp}/GetMonthlyConsumption/{self.consumer_no}/{year}"
        )
