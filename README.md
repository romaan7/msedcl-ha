# MSEDCL (Mahavitaran) for Home Assistant

Unofficial Home Assistant integration for MSEDCL (Maharashtra State Electricity
Distribution Co. Ltd. / Mahavitaran) consumers: live smart-meter readings and
billing information from the same backend the official Mahavitaran Android app
uses.

> **Disclaimer:** this project is not affiliated with, endorsed by, or
> supported by MSEDCL. It talks to an unofficial, reverse-engineered API that
> can change or break at any time without notice. Use it only with your own
> consumer account, at your own risk. Default polling intervals are
> deliberately conservative — please keep them that way.

## Features

- **Smart-meter sensors** (30-minute polling; the data itself is ~15 min old
  server-side): meter reading (kWh), month-to-date consumption, maximum
  demand, balance, last reading time, meter health.
- **Billing sensors** (8-hour polling): latest bill amount and consumption,
  with bill month/date/receipt attributes. Billing endpoints are best-effort —
  if MSEDCL breaks them, the meter sensors keep working.
- **Auto-discovery**: enter only your app login, password, and 12-digit
  consumer number. Billing unit, AMISP code, meter number, and smart-meter
  capability are discovered automatically.
- **Consumers without a smart meter** get a billing-only setup.
- **Energy-Dashboard statistics**: separate grid import/export series (solar /
  net-metered consumers get both). True **hourly** resolution for the last ~7
  days and ongoing; older history is backfilled ~12 months at daily
  resolution. Refreshed hourly by default (configurable 1–24 h), so the
  dashboard trails reality by at most about an hour.

## Installation

### HACS (recommended)

1. HACS → Integrations → ⋮ → Custom repositories → add this repo (category
   "Integration").
2. Install **MSEDCL Mahavitaran**, restart Home Assistant.
3. Settings → Devices & services → Add integration → **MSEDCL Mahavitaran**.

### Manual

Copy `custom_components/msedcl/` into your HA `config/custom_components/`
directory and restart.

## Configuration

All via the UI. You need:

| Field | Where to find it |
|---|---|
| Login | Your Mahavitaran app username |
| Password | Your Mahavitaran app password |
| Consumer number | 12 digits, on any bill |

Poll intervals can be tuned later via the integration's **Configure** dialog.

## Energy Dashboard

In **Settings → Dashboards → Energy**, select:

- Grid consumption → **"&lt;your name&gt; Grid import"** (statistic `msedcl:<consumer>_grid_import`)
- Return to grid → **"&lt;your name&gt; Grid export"** (statistic `msedcl:<consumer>_grid_export`, solar consumers only)

Do **not** additionally select the live *Meter reading* sensor — that would
double-count consumption. The statistics are backfilled ~12 months deep on
first setup: the last ~7 days (and everything going forward) at true hourly
resolution — the meter's native 30-minute blocks align exactly with the
dashboard's hour buckets — and older history at daily granularity, where each
day's total lands at 23:30 IST.

Note: this API exposes grid import/export only. **Solar production is not
available** — pair your inverter's own integration for that.

## Development

- The design contract lives in [PLAN.md](PLAN.md).
- `scripts/smoke_test.py` exercises the async client standalone (no HA needed)
  and answers the Phase-3 blocker questions against your own account:

  ```bash
  pip install aiohttp
  export MSEDCL_LOGIN='...' MSEDCL_PASSWORD='...' MSEDCL_CNO='...'
  python scripts/smoke_test.py
  ```

- Reference sync client and payload notes: [src/](src/).
