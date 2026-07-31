"""
maha_smartmeter.py
==================

A small, dependency-light client for MSEDCL's (Mahavitaran) consumer
smart-meter API — the same `consappsmartmeterapi-2.1.0` backend the official
Mahavitaran Android app talks to.

Discovered contract (from the app's Retrofit interface + HTTP client):

    Base URL : https://mobileapp.mahadiscom.in/consappsmartmeterapi-2.1.0/
    Auth     : HTTP Basic — base64("<login>:<password>")
               (the app decrypts a stored password and sends it as Basic auth;
                plain requests-Basic auth reproduces this exactly)
    Headers  : the smart-meter client sends ONLY the Authorization header —
               no Client-Os / Client-Version headers (unlike the login/standard
               endpoints). So we deliberately don't send them here.

    Endpoints (all GET, path params only):
        {amisp}/GetCurrentReading/{cno}
        {amisp}/GetMeterHealth/{cno}
        {amisp}/GetDailyConsumption/{cno}/{month}     # month format inferred: YYYYMM
        {amisp}/GetHourlyConsumption/{cno}/{day}      # day   format inferred: YYYYMMDD
        {amisp}/GetMonthlyConsumption/{cno}/{year}    # year  format inferred: YYYY

    {amisp} is the AMISP routing code for the consumer (e.g. "002" = NCC).
    It is per-consumer and comes from the login/account payload; here it's config.

------------------------------------------------------------------------------
SECURITY
------------------------------------------------------------------------------
Credentials are NEVER hardcoded in this file. Supply them via environment
variables (see `MahaSmartMeter.from_env`) or pass them to the constructor from
your own secret store. This is your own account's data; keep the password out
of source control, shell history, and logs. (If your password has been pasted
into terminals/chats during testing, rotate it.)

Requires: requests  (pip install requests)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter

try:
    # urllib3 v2 and v1 expose Retry at slightly different paths
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore


log = logging.getLogger("maha_smartmeter")


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
    """5xx — backend threw (often means bad params or 'no data' server-side)."""


class MahaResponseError(MahaError):
    """2xx but the body wasn't the JSON we expected."""


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MahaConfig:
    login: str
    password: str
    amisp: str          # AMISP routing code, e.g. "002"
    consumer_no: str    # 12-digit consumer number
    base_url: str = "https://mobileapp.mahadiscom.in/consappsmartmeterapi-2.1.0"
    timeout: float = 15.0

    def redacted(self) -> dict:
        """Safe-to-log view (no secrets)."""
        return {
            "login": self.login,
            "amisp": self.amisp,
            "consumer_no": self.consumer_no,
            "base_url": self.base_url,
            "password": "***",
        }


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class MahaSmartMeter:
    """
    Thin, typed wrapper over the smart-meter endpoints.

    Every method returns the parsed JSON (usually a dict or a list of dicts).
    Field names inside those payloads aren't mapped to typed objects yet,
    because they weren't fully known at build time — inspect one response, then
    add dataclasses in `parse.py` on top of this if you want typed models.

    Example
    -------
        client = MahaSmartMeter.from_env()          # reads MSEDCL_* env vars
        print(client.current_reading())
        print(client.monthly_consumption("2026"))
    """

    def __init__(self, config: MahaConfig, session: Optional[requests.Session] = None):
        self.cfg = config
        self.session = session or self._build_session()
        # requests handles Basic auth: base64("login:password") in Authorization
        self.session.auth = (config.login, config.password)

    # ---- construction helpers -------------------------------------------- #
    @classmethod
    def from_env(cls, prefix: str = "MSEDCL_") -> "MahaSmartMeter":
        """
        Build from environment variables:
            MSEDCL_LOGIN, MSEDCL_PASSWORD, MSEDCL_AMISP, MSEDCL_CNO
            (optional) MSEDCL_BASE_URL, MSEDCL_TIMEOUT
        """
        def need(name: str) -> str:
            val = os.environ.get(prefix + name)
            if not val:
                raise MahaError(f"Missing required env var {prefix + name}")
            return val

        cfg = MahaConfig(
            login=need("LOGIN"),
            password=need("PASSWORD"),
            amisp=need("AMISP"),
            consumer_no=need("CNO"),
            base_url=os.environ.get(prefix + "BASE_URL",
                                    MahaConfig.base_url),
            timeout=float(os.environ.get(prefix + "TIMEOUT", "15")),
        )
        return cls(cfg)

    @staticmethod
    def _build_session() -> requests.Session:
        s = requests.Session()
        # Retry only transient failures. Note: a 500 from this API is often a
        # deterministic "bad params / no data", so we do NOT retry 500 — only
        # gateway/connection issues — to avoid hammering the backend.
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=0.5,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({"Accept": "application/json"})
        return s

    # ---- low-level request ----------------------------------------------- #
    def _get(self, *path_parts: str) -> Any:
        """
        GET {base}/{amisp}/{path_parts...} and return parsed JSON.

        Raises the appropriate MahaError subclass on failure.
        """
        path = "/".join(str(p).strip("/") for p in path_parts)
        url = f"{self.cfg.base_url}/{self.cfg.amisp}/{path}"
        log.debug("GET %s", url)

        try:
            resp = self.session.get(url, timeout=self.cfg.timeout)
        except requests.RequestException as exc:
            raise MahaError(f"Network error calling {url}: {exc}") from exc

        if resp.status_code in (401, 403):
            raise MahaAuthError(
                f"Auth rejected ({resp.status_code}). Check login/password."
            )
        if resp.status_code == 404:
            raise MahaNotFound(f"Not found (404): {url}")
        if resp.status_code >= 500:
            raise MahaServerError(
                f"Server error ({resp.status_code}) for {url}. "
                f"Often means bad params or no data. Body: {resp.text[:300]!r}"
            )
        if not resp.ok:
            raise MahaError(f"Unexpected status {resp.status_code} for {url}")

        # Success — parse JSON, but keep the raw text if it isn't JSON.
        text = resp.text.strip()
        if not text:
            return None
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            raise MahaResponseError(
                f"Expected JSON from {url} but got: {text[:300]!r}"
            )

    # ---- endpoints -------------------------------------------------------- #
    def current_reading(self) -> Any:
        """Latest meter reading + timestamp (SmartMeterCurrentReading)."""
        return self._get("GetCurrentReading", self.cfg.consumer_no)

    def meter_health(self) -> Any:
        """Meter health / status (SmartMeterHealth)."""
        return self._get("GetMeterHealth", self.cfg.consumer_no)

    def daily_consumption(self, month: Optional[str] = None) -> Any:
        """
        Per-day consumption for a month.
        `month` format is inferred as YYYYMM (e.g. '202607'); defaults to the
        current month. If you get a 500/404, try the alternate format your
        capture showed (e.g. '2026-07').
        """
        month = month or date.today().strftime("%Y%m")
        return self._get("GetDailyConsumption", self.cfg.consumer_no, month)

    def hourly_consumption(self, day: Optional[str] = None) -> Any:
        """
        Per-hour consumption for a day.
        `day` format inferred as YYYYMMDD (e.g. '20260727'); defaults to today.
        """
        day = day or date.today().strftime("%Y%m%d")
        return self._get("GetHourlyConsumption", self.cfg.consumer_no, day)

    def monthly_consumption(self, year: Optional[str] = None) -> Any:
        """
        Per-month consumption for a year.
        `year` format inferred as YYYY (e.g. '2026'); defaults to current year.
        """
        year = year or date.today().strftime("%Y")
        return self._get("GetMonthlyConsumption", self.cfg.consumer_no, year)

    # convenience: everything at once
    def snapshot(self) -> dict:
        """
        Pull the 'point-in-time' views in one call. Consumption history is left
        out here since it needs date params — call those explicitly.
        Failures per-section are captured rather than aborting the whole thing.
        """
        out: dict[str, Any] = {}
        for name, fn in (("current_reading", self.current_reading),
                         ("meter_health", self.meter_health)):
            try:
                out[name] = fn()
            except MahaError as exc:
                out[name] = {"error": str(exc)}
        return out


# --------------------------------------------------------------------------- #
# CLI demo
# --------------------------------------------------------------------------- #
def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Query the MSEDCL smart-meter API for your own consumer."
    )
    parser.add_argument(
        "command",
        choices=["current", "health", "daily", "hourly", "monthly", "snapshot"],
        help="which reading to fetch",
    )
    parser.add_argument("--month", help="YYYYMM for 'daily' (default: this month)")
    parser.add_argument("--day", help="YYYYMMDD for 'hourly' (default: today)")
    parser.add_argument("--year", help="YYYY for 'monthly' (default: this year)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        client = MahaSmartMeter.from_env()
    except MahaError as exc:
        print(f"Config error: {exc}", flush=True)
        print("Set: MSEDCL_LOGIN, MSEDCL_PASSWORD, MSEDCL_AMISP, MSEDCL_CNO")
        return 2

    log.info("Config: %s", client.cfg.redacted())

    try:
        if args.command == "current":
            result = client.current_reading()
        elif args.command == "health":
            result = client.meter_health()
        elif args.command == "daily":
            result = client.daily_consumption(args.month)
        elif args.command == "hourly":
            result = client.hourly_consumption(args.day)
        elif args.command == "monthly":
            result = client.monthly_consumption(args.year)
        else:  # snapshot
            result = client.snapshot()
    except MahaError as exc:
        print(f"API error: {exc}")
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
