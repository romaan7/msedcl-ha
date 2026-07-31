# MSEDCL → Home Assistant integration — implementation plan (v3)

A HACS-installable custom component `custom_components/msedcl/` that surfaces an
MSEDCL (Mahavitaran) consumer connection in Home Assistant: live smart-meter
readings, billing info, and Energy-Dashboard statistics with solar net metering.

This is the merged single source of truth: the reviewed v2 plan (same bones —
coordinator split, Opower-style statistics backfill, "billing failure must not
kill meter sensors", hardest feature built last) plus the gap fixes from the
follow-up review. Gap fixes are marked **[v3]** so they're easy to spot.

---

## What's in the repo (the data layer, already done)

- **`maha_client.py`** — sync client, two channels:
  - **smart-meter** channel — Basic auth, no `Client-*` headers → **verified working** (`current_reading`, `daily/monthly_consumption`).
  - **standard/billing** channel — `Client-*` headers, no auth → **partly verified** (`contact_details`, `bill_history`, `transaction_info`, `meter_reading_predata` all return live data; `recent_bill` returns **400** — dead, see Phase 6).
- **`parse.py`** — typed models mapped to **real** live payload field names, with tolerant coercion. Reused nearly verbatim in HA.
- **`example_usage.py`** — driver; demonstrates auto-discovering the billing unit and consumer block.

---

## Assumptions & risks — read before building

1. **Unofficial, reverse-engineered API.** MSEDCL can change endpoints, params, or auth with no notice and silently break every install. `recent_bill` already drifted (400). Build for graceful degradation, not correctness-forever.
2. **The integration never calls `SignIn`.** `SignIn` 500s outside the official app (anti-tamper detection), but every read endpoint we need works with plain Basic auth + `Client-*` headers and no login call. So: rely purely on stored `login`/`password` sent per request. **Explicit assumption: Basic credentials are long-lived.** If read calls ever start returning **500** (not 401), this assumption has failed — and a reauth-on-401 flow will *not* catch that, because the failure mode is 5xx, not 401.
3. **All timestamps are IST with no tz suffix** (`READING_DATE_TIME=20260728151020`, `DATE=20260701`, bill dates). HA statistics are UTC-anchored. This is the single most likely silent bug — mishandling it shifts everything by 5:30 and smears the Energy Dashboard across day boundaries.
   **[v3] IST is UTC+5:30 — a half-hour offset.** IST midnight = **18:30 UTC**, which is NOT a valid statistics bucket start: `async_add_external_statistics` requires `start` aligned to the top of the hour in UTC. Daily totals must be pinned to a real hour boundary (18:00 or 19:00 UTC, i.e. 23:30 or 00:30 IST) — never naively "IST midnight converted to UTC".
4. **Solar net metering.** Payloads carry import, export, and *signed net* (often negative). HA's energy model wants **two separate non-negative series** (grid-in vs grid-out), not signed net. Mapping onto that model is the real design task of the backfill.
   **[v3] `READING` register identity is unverified.** For a solar consumer, if `current_reading.READING` is the *net* register it can **decrease**, and `state_class=total_increasing` interprets any decrease as a meter reset — silently inflating long-term statistics. Verify against live data **before Phase 3 exits** (blocker, not an open question).
5. **Per-consumer AMISP + multi-consumer logins.** `amispCode` is per consumer number; one login can have several consumers. Setup must handle multiple connections and consumers with **no smart meter** (`hasSmartMeterYN=N`).
6. **Be a considerate API citizen.** It's a production utility backend. Conservative default poll intervals + jitter; never retry a deterministic 500 in a tight loop.
7. **ToS / privacy.** Own account + modest polling = low risk. For any public release, state plainly it's unofficial and users hit MSEDCL at their own risk.

---

## Architecture

```
custom_components/msedcl/
├── __init__.py            # setup entry, create client + coordinators, forward platforms
├── manifest.json          # domain, iot_class=cloud_polling, version (no external deps)
├── api.py                 # async client (port of maha_client.py)
├── models.py              # parse.py, lifted over
├── coordinator.py         # MeterCoordinator (fast) + BillingCoordinator (slow)
├── config_flow.py         # setup + options + reauth; auto-discovery via contact_details
├── sensor.py              # entity definitions
├── statistics.py          # Energy-Dashboard long-term statistics backfill
├── diagnostics.py         # redacted dump
├── repairs.py             # [v3] issue-registry helpers for persistent 5xx
├── const.py               # DOMAIN, defaults, keys
├── strings.json / translations/en.json
└── (tests live outside, see Phase 7)
```

**Data flow:** config entry (creds + discovered consumer meta) → `api.py` (two channels) → `models.py` (typed) → coordinators (two cadences) → `sensor.py` entities; `statistics.py` runs on its own daily schedule (see Phase 5) to backfill history into HA's stats engine.

---

## Phases

### Phase 1 — Async client port

Port `maha_client.py` from `requests` to `aiohttp`. Not a mechanical swap:

- **Session:** do **not** attach auth/headers to HA's shared session — the two channels use *different* per-channel headers/auth and would leak into other integrations. Set auth + headers **per request** and keep the session stateless (use `async_get_clientsession` safely that way).
- **Basic auth:** `aiohttp.BasicAuth(login, password)` passed per request on the smart-meter channel only.
- **Retry:** no `aiohttp` equivalent of `Retry`/`HTTPAdapter` — hand-roll exponential backoff. Carry over the sync client's rule: retry **connection errors + 502/503/504 only, never 500** (500 here is usually deterministic bad-params/no-data).
- **Error mapping:** rewrite `_request`'s status→exception logic around `aiohttp` errors; keep the typed exceptions (`MahaAuthError`, `MahaNotFound`, `MahaServerError`, …).
- **[v3] Log redaction:** debug lines must not log raw URLs — they contain the consumer number, and "enable debug logging and paste it" is the first thing issue reporters are asked to do. Mask the consumer number (and any creds) in every log statement in `api.py`.
- **`models.py`:** lifts over from `parse.py` unchanged (pure functions, no I/O).

**Exit criteria:** a standalone async smoke script fetches `current_reading` and `contact_details` successfully.

### Phase 2 — Config flow (setup, options, reauth)

- **User inputs (minimal):** `login`, `password`, `consumer_no`. Everything else is discovered.
- **Validation + auto-discovery:** call `contact_details(consumer_no)` during setup — it returns the full consumer block (`BU`, `amispCode`/`amisp`, `MeterNumber`, `hasSmartMeterYN`, tariff, circle/division/zone, mobile/email). One call = setup validation **and** device-metadata source. Store discovered fields in the config entry.
- **Multi-consumer:** several connections = one config entry / device each; re-run the flow per consumer.
- **No-smart-meter degradation:** `hasSmartMeterYN=N` → set up **billing-only** (no smart-meter coordinator/entities), as a first-class path, not an error.
- **Reauth:** `async_step_reauth` for a credentials prompt on `401`. Document that the known failure mode was `500`, which reauth won't catch — persistent 5xx is handled by a repair issue (Phase 3), not a reauth loop.
- **`unique_id`:** config entry `unique_id` = consumer number (prevents duplicate setup).
- **[v3] Config entry versioning:** set `VERSION = 1` and stub `async_migrate_entry` now — cheap today, painful to retrofit once users have entries with discovered metadata stored in them.
- **[v3] Options must take effect:** register an `update_listener` that reloads the entry (or mutates `update_interval`) when options change — otherwise interval tuning silently does nothing until restart.

### Phase 3 — Coordinators + one live sensor (prove it end-to-end)

Build the two-tier polling and get **exactly one** energy sensor live in HA **before** attempting the backfill.

- **`MeterCoordinator`** — fast, default **30 min** (data is ~15-min-old; faster gains nothing). Fetches `current_reading` (+ `meter_health` if present).
- **`BillingCoordinator`** — slow, default **6–12 h**. Fetches `bill_history` + `transaction_info`. Its failures raise `UpdateFailed` in isolation and **must never take down the meter sensors** (separate coordinator = separate availability).
- **[v3] Startup:** `await coordinator.async_config_entry_first_refresh()` in `async_setup_entry` so a down API at boot raises `ConfigEntryNotReady` and HA retries setup automatically.
- **Jitter:** don't align polls to `:00`; add per-install startup jitter.
- **Resilience:** on repeated 5xx, back off, mark entities unavailable via `UpdateFailed`, cap retries.
  **[v3] Repair issue:** after N consecutive 5xx cycles (e.g. 6), raise `issue_registry.async_create_issue` telling the user the API contract may have changed (per Assumption 2, reauth won't fire for this); clear it on the next success.
- **First sensor:** the meter reading (kWh), `device_class=energy`, `state_class=total_increasing`. Confirm it appears and updates.
- **[v3] Phase-gate (blocker):** before this phase exits, verify from live data (a) what register `READING` is for a solar consumer — if it can decrease, it must NOT be `total_increasing` (use plain sensor or `total` with care); and (b) whether `READING_*` (cumulative) or `UNITS_*` (per-period) fields are authoritative for the Phase-5 sums. Both answers gate the backfill design.

**Exit criteria:** one live, updating energy sensor on one HA device, **and** the two [v3] blocker questions answered from live payloads.

### Phase 4 — Full sensor set

One HA **device** per consumer (name by consumer/meter number; `ConsumerInfo` supplies model=meter number, hw=tariff, suggested area=circle/division). Entities:

| Entity | Source | `device_class` / `state_class` | unit | Notes |
|---|---|---|---|---|
| Meter reading | `current_reading.reading` | energy / total_increasing* | kWh | *only if verified monotonic (Phase 3 gate) |
| MTD consumption | `current_reading.mtd_consumption` | energy / total **+ `last_reset`** | kWh | **[v3]** resets monthly — `total` without `last_reset` counts the reset as negative consumption; set `last_reset` = start of billing month (IST) |
| Max demand (MD) | `current_reading.md` | power / measurement | kW | |
| Current balance | `current_reading.current_balance` | monetary | INR | prepaid-style |
| Last reading time | `current_reading.reading_dt` | timestamp | — | diagnostic; **IST→aware datetime**, never naive |
| Latest bill amount | `BillHistoryRow.latest().current_bill` | monetary | INR | negative = credit |
| Latest bill consumption | `BillHistoryRow.latest().consumption` | energy / total | kWh | |
| Latest bill month/date | `BillHistoryRow.latest()` | — | — | attributes |
| Meter health | `meter_health` | — | — | optional — 404s on some meters; omit entity if absent, don't leave permanently "unavailable" |
| Consumer meta (AMISP, meter no., tariff, BU) | `ConsumerInfo` | — | — | device attributes / diagnostic sensors |

- **`unique_id` per entity:** `f"{consumer_no}_{metric}"`. Device identifier: consumer number. Mandatory — wrong keys duplicate entities on restart and block renaming.
- **[v3] Modern entity naming:** `_attr_has_entity_name = True` + `translation_key` per entity — required direction for current HA, free to do from the start.
- **Currency:** monetary sensors need `native_unit_of_measurement="INR"`.

### Phase 5 — Energy Dashboard statistics backfill (the hard part)

Insert long-term statistics from the consumption endpoints, Opower-style. Budget most of the project's effort and *all* the subtlety here.

- **API:** `recorder.statistics.async_add_external_statistics`, `statistic_id`s `msedcl:<consumer>_grid_import` / `_grid_export` (source prefix must equal the domain).
- **[v3] The dashboard source of truth is the external statistics, not the live sensor.** Both are selectable in the Energy Dashboard; a user who adds both double-counts consumption. Document prominently (README + entity description): pick `msedcl:<cno>_grid_import`/`_export` in the Energy Dashboard; the live reading sensor is informational.
- **Timezone (critical):** consumption `DATE`/period fields are **IST**. Convert IST→UTC before bucketing.
  **[v3] Hour alignment:** stats `start` values must be top-of-hour UTC. IST midnight = 18:30 UTC → invalid. Pin daily totals to 18:00 UTC (23:30 IST) or 19:00 UTC (00:30 IST) — pick one, document it, test the day boundary.
- **Net metering → two non-negative series:** import fields → **grid import**, export fields → **grid export/return**, as two separate monotonic `sum` series. Never feed HA the signed net. Which fields are authoritative (`READING_*` vs `UNITS_*`) was answered at the Phase-3 gate.
- **Monotonic sums:** statistics want cumulative `sum`, not per-period deltas. Compute running totals; seed the starting sum sensibly (if `READING_*` cumulative registers are authoritative, they can *be* the sum baseline).
- **Granularity mismatch:** finest *history* is **daily** (hourly only for the current day). Decide single-hour vs spread-across-24 placement — document the choice (interacts with the hour-alignment rule above).
- **Idempotency + gaps:** re-running a backfill over the same day must not double-count (external statistics upsert on `(statistic_id, start)` — align starts exactly). Missing days (meter offline) must not reset the running sum — carry forward.
- **[v3] Late/revised data corrupts all subsequent sums.** Upserting one revised row is not enough: every cumulative `sum` after it becomes wrong. On each run, re-fetch a trailing window (e.g. 7 days); if any value differs from what was inserted, **recompute and re-insert every row from the earliest changed point forward**. This is the classic bug in Opower-style integrations — test it explicitly.
- **[v3] Trigger:** `statistics.py` runs as a dedicated daily task (scheduled at a jittered off-peak time) plus a one-time deep backfill on first setup (~30 days daily + 12 months monthly). It does NOT run on every coordinator cycle.
- **Sequencing:** only start once Phase 3's single live sensor is trusted.

### Phase 6 — Polish & packaging

- **Options flow:** tune both poll intervals post-setup (wired to the Phase-2 `update_listener`).
- **Diagnostics with redaction:** downloadable dump, but redact **credentials *and* PII** — `MobileNo`, `EmailId`, `Uid`, `amispCode`, address lines, and consumer number all appear in payloads and get pasted into GitHub issues.
- **Translations:** `strings.json` + `translations/en.json` for config-flow labels/errors + entity `translation_key`s.
- **HACS packaging:** `hacs.json`, `manifest.json` (version, `iot_class: cloud_polling`, `integration_type: device`, code owners), repo topics.
- **[v3] CI:** GitHub Actions running `hassfest` validation + the HACS validation action — effectively mandatory for a HACS release; catches manifest/translation errors early.
- **README disclaimer (user-facing):** unofficial, reverse-engineered, may break without notice, own account only, not affiliated with MSEDCL; conservative default intervals.
  **[v3] Set expectations:** the dashboard shows grid import/export only — **solar production is not available** from this API (needs the inverter's own integration). Say so, or "solar shows zero" issues will follow. Also: which statistic ids to select in the Energy Dashboard (see Phase 5).
- **`recent_bill` is dead — don't use it.** Billing sources are `bill_history` (`IsRecentYn="Y"` row) and `transaction_info`/`contact_details`. Stated here so nobody re-wires the 400.

### Phase 7 — Tests

- **`pytest-homeassistant-custom-component`.**
- **Fixtures from real shapes:** reuse the payloads already encoded in `parse.py`'s self-test (contact_details, current_reading, daily/monthly, bill_history).
- **Priority coverage:**
  - config-flow happy path + auto-discovery; **no-smart-meter** degradation; reauth on 401
  - billing coordinator failing **without** killing meter sensors; `ConfigEntryNotReady` at boot; repair issue raised on persistent 5xx and cleared on recovery
  - **timezone/day-boundary** statistics conversion incl. **[v3] top-of-hour alignment** (IST midnight ≠ valid bucket)
  - **backfill idempotency** (double-run no double-count); **[v3] revised-history recompute** (change day N-2, assert all later sums corrected); **gap** handling (carry forward, no reset)
  - **[v3]** MTD `last_reset` behavior across a month rollover
  - monetary INR units; `unique_id` stability across reload; options change actually reschedules polling

---

## Cross-cutting checklist

- [ ] Per-request auth/headers, not on a shared session
- [ ] Async retry: 502/503/504 + connection only, **never 500**; capped backoff → `UpdateFailed`
- [ ] IST→UTC everywhere timestamps meet HA statistics
- [ ] **[v3]** Stats `start` = top-of-hour UTC (IST is +5:30 — midnight lands at :30)
- [ ] Solar: two non-negative import/export series, not signed net
- [ ] **[v3]** External stats = dashboard source; live sensor informational; documented to avoid double-count
- [ ] **[v3]** Verify `READING` register monotonicity before trusting `total_increasing` (Phase-3 gate)
- [ ] **[v3]** MTD sensor: `total` + `last_reset`, not bare `total`
- [ ] **[v3]** Revised history → recompute sums forward from earliest change
- [ ] **[v3]** `async_config_entry_first_refresh` + `ConfigEntryNotReady`; options `update_listener`; entry `VERSION`/migration stub
- [ ] **[v3]** Repair issue on persistent 5xx (reauth won't catch it); clear on recovery
- [ ] **[v3]** Statistics task: daily jittered schedule + setup backfill, not per-coordinator-cycle
- [ ] Monetary sensors → `INR`
- [ ] `unique_id`: entity = `{consumer}_{metric}`, device = consumer no.; **[v3]** `has_entity_name` + translation keys
- [ ] Multi-consumer setup; per-consumer AMISP
- [ ] `hasSmartMeterYN=N` → billing-only path (not error)
- [ ] `meter_health` optional/absent on 404
- [ ] Never call `SignIn`; document Basic-creds-long-lived assumption + 5xx caveat
- [ ] Poll jitter; conservative defaults; options flow to tune
- [ ] Diagnostics redact creds **and** PII; **[v3]** debug logs redact consumer number too
- [ ] README: unofficial-API disclaimer; **[v3]** no-solar-production expectation; which stats to pick in the dashboard
- [ ] **[v3]** CI: hassfest + HACS validation actions
- [ ] Use `bill_history`/`transaction_info`; `recent_bill` is 400/dead

---

## Live findings — smoke test, 2026-07-31 (Phase-3 blockers RESOLVED)

Run against a postpaid solar (net-metered) account with `scripts/smoke_test.py`:

1. **`READING` is the IMPORT register** — identical to `READING_IMPORT` on all
   30 daily points and strictly monotonic. `state_class=total_increasing` on
   the live reading sensor is safe. (Signed `READING_NET` was negative and
   non-monotonic, as predicted — never feed it to HA.)
2. **Registers are authoritative and self-consistent** — import register delta
   over the month (56.080) equals the sum of daily `UNITS_*` deltas (56.080)
   exactly. `UNITS(day N) = READING(day N) − READING(day N−1)`, i.e. READING is
   the **end-of-day** register. Consequence, implemented in `statistics.py`:
   **registers ARE the statistics sums.** Idempotency is automatic, and the
   [v3] "recompute sums forward after a revision" problem vanishes — sums are
   absolute register values, not our accumulations.
3. **Endpoint-level 401s with valid credentials**: `GetMeterHealth` and
   `GetMonthlyConsumption` return 401 while the same Basic credentials pass
   `GetCurrentReading` / `GetDailyConsumption`. Not a credential failure —
   don't reauth. Handled: meter-health disables itself permanently on 401/404;
   the ~12-month backfill iterates `GetDailyConsumption` per month (one request
   per month, once) instead of the monthly endpoint.
4. **Hour pin decided**: day D's end-of-day register lands at 23:30 IST ==
   18:00 UTC on day D (valid top-of-hour UTC start; consumption renders inside
   day D; IST has no DST so the mapping is constant).
5. **Hourly endpoint works — including past days** (second probe, 2026-07-31):
   `GetHourlyConsumption` returns per-slot amounts (sum == daily UNITS,
   exactly), with different field names from the daily payload
   (`UNITS_IMPORTED`/`UNITS_EXPORTED`). A completed day has **25 rows** tiling
   the IST day as the AMISP 30-minute block profile aggregated to
   half-hour `[00:00–00:30)` + 23 full hours `[HH:30–HH+1:30)` + half-hour
   `[23:30–24:00)` — verified numerically (half-slots ≈ half of neighboring
   full slots; labels run 0, 1..23, 0). Slot boundaries are therefore
   **exactly top-of-hour UTC**, so hourly statistics buckets align natively
   with no rendering skew. Implemented in `statistics.py`: hourly for the last
   7 days (first run) / today+yesterday (each run), per-slot amounts
   accumulated onto the register scale anchored at the previous day's
   end-of-day register (anchor + 25 slots == day's register, exactly); the
   two half-slots around IST midnight share one UTC bucket and supersede that
   day's daily row. Older history stays daily (hourly costs one request per
   day). Q3 (daily-total placement) from the open questions is thereby moot
   for recent data.

## Open questions

1. Do Basic credentials stay valid indefinitely, or does something server-side expire without a periodic `SignIn`? (Watch for read calls starting to 5xx — repair issue covers detection.)
2. `recent_bill`'s real params — worth recovering, or is `bill_history` sufficient forever? (Currently: sufficient.)
3. Prepaid vs postpaid divergence — this account is postpaid with solar; a prepaid smart meter may expose balance/recharge fields differently. Confirm before claiming prepaid support.
4. `READING` monotonicity is verified over one month; keep an eye on multi-month behavior (meter swaps reset registers — `total_increasing` handles that correctly, but the statistics sums would need a re-baseline if a swap ever happens).
