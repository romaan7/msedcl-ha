"""
maha_client.py
==============

A client for MSEDCL's (Mahavitaran) consumer backend — the same servers the
official Mahavitaran Android app talks to. Supersedes / expands the earlier
`maha_smartmeter.py`: it now covers the read-only *standard* account endpoints
(consumer info, bills, bill history, contact details, complaints, meter-reading
pre-data, prepaid-recharge info, transaction info) in addition to the
smart-meter endpoints, and exposes a generic passthrough for everything else.

------------------------------------------------------------------------------
TWO CHANNELS, TWO DIFFERENT AUTH SCHEMES (this is the important bit)
------------------------------------------------------------------------------
From the app's HTTP client wiring:

  STANDARD channel  -> base .../App_Requests/
      Sends ONLY these headers, NO Basic auth:
          Client-Os: ANDROID
          Client-Os-Version: <android release, e.g. 13/14>
          Client-Version: <app versionName, e.g. 12.40>
      Serves: GetConInfo, RecentBill, PreviousBill, GetBillHistory,
              getContactDetails, getComplaints, SubmitMRNew/GetPreData, etc.

  SMART-METER channel -> base .../consappsmartmeterapi-2.1.0/
      Sends ONLY HTTP Basic auth (base64("login:password")), NO Client-* headers.
      Serves: {amisp}/GetCurrentReading/{cno}, GetDailyConsumption, etc.

  PAYMENTS channel -> base .../Payments/   (same header set as STANDARD)
      Transaction endpoints. Not wrapped as methods here; reachable via
      `raw("payments", ...)` if you need them.

So each channel gets its own configured requests.Session.

------------------------------------------------------------------------------
SECURITY / SCOPE
------------------------------------------------------------------------------
* Credentials are NEVER hardcoded — supply via env vars or the constructor.
  Keep the password out of source control, shell history, and logs. If it has
  leaked during testing, rotate it.
* The STANDARD endpoints are keyed by consumer number and, per the decompiled
  client, carry no per-user authentication (only the Client-* headers). This
  client is intended for YOUR OWN account: every wrapped method uses the
  consumer number from config. Don't point it at consumer numbers that aren't
  yours.
* The standard endpoints are UNVERIFIED here — only the smart-meter channel was
  confirmed end to end. The header contract matches the app, but the server may
  enforce a session/token not visible in the client, so some calls may 401/500.
  Treat non-smart-meter methods as "best effort from the spec" until you see a
  real 200.

Requires: requests  (pip install requests)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore


log = logging.getLogger("maha_client")

HOST = "https://mobileapp.mahadiscom.in"
BASE_STANDARD = f"{HOST}/App_Requests"
BASE_SMART = f"{HOST}/consappsmartmeterapi-2.1.0"
BASE_PAYMENTS = f"{HOST}/Payments"


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class MahaError(Exception):
    """Base class for all client errors."""


class MahaAuthError(MahaError):
    """401/403 — bad or missing credentials."""


class MahaNotFound(MahaError):
    """404 — no data for that consumer / date / endpoint."""


class MahaServerError(MahaError):
    """5xx — backend threw (often bad params or 'no data' server-side)."""


class MahaResponseError(MahaError):
    """2xx but the body wasn't the JSON we expected."""


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class MahaConfig:
    # identity / auth
    login: str
    password: str
    consumer_no: str                       # 12-digit consumer number
    amisp: str = ""                         # AMISP routing code, e.g. "002" (smart meter only)
    billing_unit: Optional[str] = None      # BU — needed for GetConInfo / getComplaints
    category: str = "LT"                    # `cat` query param (consumer category)
    lang: str = "en"

    # client identification (sent on the STANDARD/PAYMENTS channels)
    client_version: str = "12.40"           # app versionName -> Client-Version header
    client_os_version: str = "13"           # android release -> Client-Os-Version header
    app_version_code: int = 81              # app versionCode -> `appversion` query param where required

    timeout: float = 15.0

    def redacted(self) -> dict:
        d = {k: getattr(self, k) for k in (
            "login", "consumer_no", "amisp", "billing_unit",
            "category", "lang", "client_version", "client_os_version",
            "app_version_code",
        )}
        d["password"] = "***"
        return d


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class MahaClient:
    """
    Unified client over the standard + smart-meter channels.

    Methods return parsed JSON (dict / list of dicts) or raw text if the body
    isn't JSON. Response field names aren't mapped to typed objects yet — inspect
    a real payload, then add dataclasses on top if you want typed models.

    Example
    -------
        c = MahaClient.from_env()
        print(c.recent_bill())          # standard channel
        print(c.current_reading())      # smart-meter channel
        print(c.raw("standard", "GET", "StartupMessages", params={"lang": "en"}))
    """

    def __init__(self, config: MahaConfig):
        self.cfg = config
        self._std = self._make_session(self._standard_headers())
        self._pay = self._make_session(self._standard_headers())
        self._sm = self._make_session({"Accept": "application/json"})
        # smart-meter channel = HTTP Basic auth, no Client-* headers
        self._sm.auth = (config.login, config.password)

    # ---- construction ---------------------------------------------------- #
    @classmethod
    def from_env(cls, prefix: str = "MSEDCL_") -> "MahaClient":
        """
        Required : MSEDCL_LOGIN, MSEDCL_PASSWORD, MSEDCL_CNO
        Optional : MSEDCL_AMISP (smart meter), MSEDCL_BU, MSEDCL_CAT,
                   MSEDCL_LANG, MSEDCL_CLIENT_VERSION, MSEDCL_OS_VERSION,
                   MSEDCL_APP_VERSION_CODE, MSEDCL_TIMEOUT
        """
        g = lambda n, d=None: os.environ.get(prefix + n, d)

        def need(n: str) -> str:
            v = g(n)
            if not v:
                raise MahaError(f"Missing required env var {prefix + n}")
            return v

        cfg = MahaConfig(
            login=need("LOGIN"),
            password=need("PASSWORD"),
            consumer_no=need("CNO"),
            amisp=g("AMISP", "") or "",
            billing_unit=g("BU"),
            category=g("CAT", "LT"),
            lang=g("LANG", "en"),
            client_version=g("CLIENT_VERSION", "12.40"),
            client_os_version=g("OS_VERSION", "13"),
            app_version_code=int(g("APP_VERSION_CODE", "81")),
            timeout=float(g("TIMEOUT", "15")),
        )
        return cls(cfg)

    def _standard_headers(self) -> dict:
        return {
            "Client-Os": "ANDROID",
            "Client-Os-Version": str(self.cfg.client_os_version),
            "Client-Version": str(self.cfg.client_version),
            "Accept": "application/json",
        }

    @staticmethod
    def _make_session(headers: dict) -> requests.Session:
        s = requests.Session()
        retry = Retry(
            total=3, connect=3, read=2, backoff_factor=0.5,
            status_forcelist=(502, 503, 504),   # NOT 500 (often deterministic)
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update(headers)
        return s

    # ---- low-level ------------------------------------------------------- #
    def _request(self, session: requests.Session, base: str, method: str,
                 path: str, params: Optional[dict] = None,
                 data: Optional[dict] = None) -> Any:
        url = f"{base}/{path.strip('/')}"
        # drop None-valued params so we don't send literal "None"
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        log.debug("%s %s params=%s", method, url, params)

        try:
            resp = session.request(method, url, params=params, data=data,
                                   timeout=self.cfg.timeout)
        except requests.RequestException as exc:
            raise MahaError(f"Network error calling {url}: {exc}") from exc

        if resp.status_code in (401, 403):
            raise MahaAuthError(f"Auth rejected ({resp.status_code}) for {url}")
        if resp.status_code == 404:
            raise MahaNotFound(f"Not found (404): {url}")
        if resp.status_code >= 500:
            raise MahaServerError(
                f"Server error ({resp.status_code}) for {url}. "
                f"Often bad params/no data. Body: {resp.text[:300]!r}"
            )
        if not resp.ok:
            raise MahaError(f"Unexpected status {resp.status_code} for {url}")

        text = resp.text.strip()
        if not text:
            return None
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            # standard channel sometimes returns raw scalars/strings
            return text

    def _require(self, **kv) -> None:
        missing = [k for k, v in kv.items() if not v]
        if missing:
            raise MahaError(
                f"Missing required config for this call: {', '.join(missing)}. "
                f"Set it on MahaConfig / via env."
            )

    # ---- generic passthrough (any of the ~140 endpoints) ----------------- #
    def raw(self, channel: str, method: str, path: str,
            params: Optional[dict] = None, data: Optional[dict] = None) -> Any:
        """
        Call any endpoint directly.
          channel : "standard" | "smart" | "payments"
          method  : "GET" | "POST"
          path    : path after the channel base, e.g. "StartupMessages"
                    or "002/GetCurrentReading/0800...". For POST form endpoints
                    pass fields in `data`.
        """
        ch = {
            "standard": (self._std, BASE_STANDARD),
            "smart": (self._sm, BASE_SMART),
            "payments": (self._pay, BASE_PAYMENTS),
        }.get(channel)
        if ch is None:
            raise MahaError(f"Unknown channel {channel!r}")
        session, base = ch
        return self._request(session, base, method.upper(), path, params, data)

    # thin helpers
    def get_standard(self, path: str, **params) -> Any:
        return self._request(self._std, BASE_STANDARD, "GET", path, params)

    def get_smart(self, path: str) -> Any:
        return self._request(self._sm, BASE_SMART, "GET", path)

    # ===================================================================== #
    # STANDARD channel — read-only account/bill methods (scoped to self.cno)
    # ===================================================================== #
    def consumer_info(self) -> Any:
        """GET GetConInfo — consumer info. Requires billing_unit (BU)."""
        self._require(billing_unit=self.cfg.billing_unit)
        return self.get_standard("GetConInfo",
                                 Con=self.cfg.consumer_no, BU=self.cfg.billing_unit)

    def recent_bill(self, lookup_payments: str = "YES") -> Any:
        """GET RecentBill — latest bill (+ optional payment lookup)."""
        return self.get_standard(
            "RecentBill",
            cat=self.cfg.category, cno=self.cfg.consumer_no,
            lookupPaymentsYN=lookup_payments, lang=self.cfg.lang,
        )

    def previous_bill(self, month: str) -> Any:
        """GET PreviousBill — a specific past bill. `month` = mth token."""
        return self.get_standard(
            "PreviousBill",
            cat=self.cfg.category, cno=self.cfg.consumer_no, mth=month,
        )

    def bill_history(self) -> Any:
        """GET GetBillHistory — historical bills."""
        return self.get_standard(
            "GetBillHistory", consumerno=self.cfg.consumer_no, cat=self.cfg.category
        )

    def contact_details(self) -> Any:
        """GET getContactDetails — registered mobile/email on the account."""
        return self.get_standard("getContactDetails", Con=self.cfg.consumer_no)

    def complaints(self) -> Any:
        """GET getComplaints — service requests / complaints. Requires BU."""
        self._require(billing_unit=self.cfg.billing_unit)
        return self.get_standard("getComplaints",
                                 Con=self.cfg.consumer_no, BU=self.cfg.billing_unit)

    def complaint_details(self, service_request_id: str) -> Any:
        """GET getComplaintDetails — detail for one SR id."""
        return self.get_standard("getComplaintDetails",
                                 serviceRequestID=service_request_id)

    def meter_reading_predata(self) -> Any:
        """GET SubmitMRNew/GetPreData — self-reading eligibility / last reading."""
        return self.get_standard("SubmitMRNew/GetPreData",
                                 consumerno=self.cfg.consumer_no)

    def prepaid_recharge_info(self) -> Any:
        """GET PrepaidRecharge/Info — prepaid balance / recharge info."""
        return self.get_standard(
            "PrepaidRecharge/Info",
            cno=self.cfg.consumer_no, deviceos="ANDROID",
            appversion=self.cfg.app_version_code, lang=self.cfg.lang,
        )

    def transaction_info(self) -> Any:
        """GET Transaction/Info — payment/transaction info for the consumer."""
        return self.get_standard("Transaction/Info", cno=self.cfg.consumer_no)

    def startup_messages(self) -> Any:
        """GET StartupMessages — app banner/notice messages (handy 200 check)."""
        return self.get_standard("StartupMessages", lang=self.cfg.lang)

    # ===================================================================== #
    # SMART-METER channel (Basic auth; requires amisp)
    # ===================================================================== #
    def current_reading(self) -> Any:
        self._require(amisp=self.cfg.amisp)
        return self.get_smart(f"{self.cfg.amisp}/GetCurrentReading/{self.cfg.consumer_no}")

    def meter_health(self) -> Any:
        self._require(amisp=self.cfg.amisp)
        return self.get_smart(f"{self.cfg.amisp}/GetMeterHealth/{self.cfg.consumer_no}")

    def daily_consumption(self, month: Optional[str] = None) -> Any:
        """month inferred as YYYYMM (default: this month)."""
        self._require(amisp=self.cfg.amisp)
        month = month or date.today().strftime("%Y%m")
        return self.get_smart(
            f"{self.cfg.amisp}/GetDailyConsumption/{self.cfg.consumer_no}/{month}")

    def hourly_consumption(self, day: Optional[str] = None) -> Any:
        """day inferred as YYYYMMDD (default: today)."""
        self._require(amisp=self.cfg.amisp)
        day = day or date.today().strftime("%Y%m%d")
        return self.get_smart(
            f"{self.cfg.amisp}/GetHourlyConsumption/{self.cfg.consumer_no}/{day}")

    def monthly_consumption(self, year: Optional[str] = None) -> Any:
        """year inferred as YYYY (default: this year)."""
        self._require(amisp=self.cfg.amisp)
        year = year or date.today().strftime("%Y")
        return self.get_smart(
            f"{self.cfg.amisp}/GetMonthlyConsumption/{self.cfg.consumer_no}/{year}")

    # ---- convenience ----------------------------------------------------- #
    def snapshot(self) -> dict:
        """
        Pull the common point-in-time views in one shot. Each section's failure
        is captured rather than aborting the whole call, so you can see which
        endpoints actually respond for your account.
        """
        jobs = {
            "recent_bill": self.recent_bill,
            "current_reading": self.current_reading,
            "meter_health": self.meter_health,
            "prepaid_recharge_info": self.prepaid_recharge_info,
        }
        out: dict[str, Any] = {}
        for name, fn in jobs.items():
            try:
                out[name] = fn()
            except MahaError as exc:
                out[name] = {"error": str(exc)}
        return out


# Backwards-compat alias for anything importing the old name.
MahaSmartMeter = MahaClient


# --------------------------------------------------------------------------- #
# CLI demo
# --------------------------------------------------------------------------- #
def _main() -> int:
    import argparse

    cmds = {
        # standard
        "coninfo": lambda c, a: c.consumer_info(),
        "recent": lambda c, a: c.recent_bill(),
        "previous": lambda c, a: c.previous_bill(a.month),
        "history": lambda c, a: c.bill_history(),
        "contact": lambda c, a: c.contact_details(),
        "complaints": lambda c, a: c.complaints(),
        "mr-predata": lambda c, a: c.meter_reading_predata(),
        "prepaid": lambda c, a: c.prepaid_recharge_info(),
        "txn": lambda c, a: c.transaction_info(),
        "startup": lambda c, a: c.startup_messages(),
        # smart meter
        "current": lambda c, a: c.current_reading(),
        "health": lambda c, a: c.meter_health(),
        "daily": lambda c, a: c.daily_consumption(a.month),
        "hourly": lambda c, a: c.hourly_consumption(a.day),
        "monthly": lambda c, a: c.monthly_consumption(a.year),
        # everything
        "snapshot": lambda c, a: c.snapshot(),
    }

    p = argparse.ArgumentParser(description="MSEDCL account client (own account).")
    p.add_argument("command", choices=sorted(cmds))
    p.add_argument("--month", help="YYYYMM (daily) or mth token (previous)")
    p.add_argument("--day", help="YYYYMMDD (hourly)")
    p.add_argument("--year", help="YYYY (monthly)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        client = MahaClient.from_env()
    except MahaError as exc:
        print(f"Config error: {exc}")
        print("Set at least: MSEDCL_LOGIN, MSEDCL_PASSWORD, MSEDCL_CNO "
              "(+ MSEDCL_AMISP for smart meter, MSEDCL_BU for coninfo/complaints)")
        return 2

    log.info("Config: %s", client.cfg.redacted())
    try:
        result = cmds[args.command](client, args)
    except MahaError as exc:
        print(f"API error: {exc}")
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
