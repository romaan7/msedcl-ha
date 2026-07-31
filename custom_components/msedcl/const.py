"""Constants for the MSEDCL (Mahavitaran) integration."""

from __future__ import annotations

DOMAIN = "msedcl"

# Config entry data keys (credentials use HA's CONF_USERNAME / CONF_PASSWORD).
CONF_CONSUMER_NO = "consumer_no"
# Discovered at setup from contact_details and stored on the entry:
CONF_AMISP = "amisp"
CONF_BILLING_UNIT = "billing_unit"
CONF_HAS_SMART_METER = "has_smart_meter"
CONF_METER_NUMBER = "meter_number"
CONF_CONSUMER_NAME = "consumer_name"
CONF_TARIFF = "tariff"
CONF_CATEGORY = "category"
CONF_BILLING_TYPE = "billing_type"

# Options
OPT_METER_INTERVAL = "meter_scan_interval"      # minutes
OPT_BILLING_INTERVAL = "billing_scan_interval"  # hours
OPT_STATS_INTERVAL = "stats_scan_interval"      # hours

# Data is ~15 min old server-side; polling faster than 30 min gains nothing,
# and this is a production utility backend — be a considerate API citizen.
DEFAULT_METER_INTERVAL_MIN = 30
MIN_METER_INTERVAL_MIN = 15
MAX_METER_INTERVAL_MIN = 720

DEFAULT_BILLING_INTERVAL_H = 8
MIN_BILLING_INTERVAL_H = 3
MAX_BILLING_INTERVAL_H = 24

# Persistent-5xx repair issue: reauth can't catch API drift (the known failure
# mode is 500, not 401 — see PLAN.md assumption 2), so after this many
# consecutive server-error cycles we raise an issue-registry warning instead.
SERVER_ERRORS_BEFORE_REPAIR = 6
ISSUE_API_DRIFT = "api_drift"

# Energy-Dashboard statistics (statistics.py). GetMonthlyConsumption 401s
# (endpoint-level ACL, observed live), so the initial backfill iterates
# GetDailyConsumption month by month — one request per month, once.
STATS_BACKFILL_MONTHS = 12

# Statistics refresh: each incremental run costs 3 requests (current-month
# daily + today/yesterday hourly). The hourly default keeps the Energy
# Dashboard within ~an hour of live at ~72 requests/day — modest next to the
# official app's usage, and tunable via the options flow.
DEFAULT_STATS_INTERVAL_H = 1
MIN_STATS_INTERVAL_H = 1
MAX_STATS_INTERVAL_H = 24

# Hourly resolution costs one GetHourlyConsumption request PER DAY, so the
# hourly window is kept small: recent days get true hourly buckets, older
# history stays daily. Deeper hourly backfill would hammer the API for
# little dashboard value.
STATS_HOURLY_BACKFILL_DAYS = 7   # first run
STATS_HOURLY_DAYS_PER_RUN = 2    # each 12-hourly run: today + yesterday
