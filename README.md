# AeroIndex (APIx)

**A real-time airfare price index for India, built to augment the CPI transport basket.**

Smart India Hackathon 2026 · Problem statement **SIH26056** · Ministry of Statistics and Programme Implementation (MoSPI)

---

## What problem this solves

India's CPI collects air-travel prices monthly, by hand, from a small number of
observations. Airfares do not behave like the other things in the basket: the
same seat on the same flight can cost three different amounts on three
consecutive days, and the price a traveller pays depends less on the calendar
month than on how far ahead they booked. A monthly average of a handful of
quotes cannot see any of that.

APIx collects fares daily across a fixed basket of routes and booking horizons,
cleans them, and publishes a set of index series built with standard
index-number methodology. It is designed to sit *alongside* the CPI as a
high-frequency input, not to replace it.

### The differentiator: one pipeline, two audiences

Two institutions need different numbers from the same data, and both are right.

| | **Headline APIx** | **Core APIx** |
|---|---|---|
| Audience | MoSPI / NSO | RBI |
| Festival & demand surges | **kept** | **excluded** (flagged rows dropped) |
| Answers | "What did travellers actually pay?" | "What is the underlying trend?" |

A consumer price index must include the Diwali surge, because travellers really
paid it. A policy read needs to see through it. APIx publishes both, and **the
gap between them is itself the surge measure** — a quantity neither series
gives you alone. The dashboard draws that gap directly, beneath the two series
it comes from.

Crossed with a second, independent axis:

| | **Total fare** | **Base fare** |
|---|---|---|
| Contents | tax-inclusive, all-in | excludes taxes, UDF, fees |
| Use | comparable with CPI (measures what is paid) | isolates carrier pricing from tax policy |

That is **four published series** (`headline|core` × `total|base`), all from one
collection run. A change in GST on air travel moves Total and leaves Base flat —
which tells you the rise was tax policy, not carrier pricing. Collapsing fares
into a single `total_fare` column would throw that distinction away, so the
schema keeps the components separate.

---

## The five principles

These govern the codebase. Where any other instruction conflicts with them,
they win.

1. **Provenance over polish.** Every number carries where it came from. A
   simulated fare is labelled simulated everywhere it surfaces — API, CSV, CLI
   and dashboard. Source tiers (Tier-1 LIVE, Tier-1.5 LIVE_LIMITED, and Tier-2 SIMULATED) are propagated end-to-end.
2. **Compliance first.** `robots.txt` is checked before any Tier-A fetch. No
   CAPTCHA or Cloudflare bypass, no headless-browser evasion, no proxy or IP
   rotation. On a block: log it, stop that source, substitute nothing. Tier-1.5 sources are strictly rate-capped.
3. **Flag, never delete.** Outliers, festival surges and validation rejects are
   marked and retained. A festival spike is the signal, not noise.
4. **One number, used everywhere.** One validation run produces one figure, and
   the README, the dashboard and the CLI all quote that same figure.
5. **Method, not claims.** The prototype demonstrates a method. It does not
   claim outcomes it has not measured, and it does not claim data it has not
   collected.

---

## Architecture & Operational Modes

APIx enforces a strict, **structural** — not merely cosmetic — separation
between two operational environments. The split is enforced in the database
(distinct `basket_version` values, distinct `mode` values on every query
path), not only by a label in the UI.

### 1. Live Index — 100% Real Live Data
- **Data Source**: Real-time production booking path of **Akasa Air (QP)**.
- **Simulated Data**: **0.0%** — zero synthetic or placeholder rows.
- **Basket Version**: `v1-akasa-live-4route`, restricted strictly to the 4 non-stop domestic trunk routes operated by Akasa Air:
  - `DEL-BOM` (weight: 0.3416)
  - `DEL-BLR` (weight: 0.2713)
  - `BOM-BLR` (weight: 0.2225)
  - `DEL-CCU` (weight: 0.1646)
  - *Weights renormalised from DGCA city-pair domestic scheduled traffic shares; sum = 1.0000. These four routes carry 12.09% of national domestic passenger traffic.*
- **Connecting Sectors Excluded**: `MAA-DEL` and `BLR-HYD` are connection-only on Akasa Air. Under matched-model index methodology, connecting flights are excluded to preserve price comparability.
- **Reference Point Validation**: Real-time fare levels are empirically validated against independently published market averages from **Ixigo (December 2024)** as published by *The Indian Express*.
- **Endpoint**: `GET /v1/index?mode=live` | CSV: `GET /v1/index.csv?mode=live`

### 2. Methodology Demonstration Sandbox — Synthetic Stress-Test
- **Purpose**: Demonstrates the full econometric pipeline across a 30-day simulated history across all 6 national DGCA routes before multi-month live history is accumulated.
- **Realistic Festival Surges**: Multipliers calibrated to empirical Indian airline surge behaviour (**1.15×–1.30×** base fare across the seven configured festival/peak windows — see `apix/config/festivals.yaml`).
- **Dual-Series Demonstration**: Demonstrates how **Headline APIx** (MoSPI) captures consumer-paid price surges while **Core APIx** (RBI) strips out temporary holiday volatility.
- **Outlier handling**: Tukey fences (`k=1.5`, minimum 8 quotes per cell) **flag** anomalous cells; they are never deleted. The Dashboard presents these as flags, and Core excludes them while Headline retains them.
- **Endpoint**: `GET /v1/index?mode=sandbox`

### Living separately, by construction

| Property | Live Index | Sandbox |
|---|---|---|
| `basket_version` | `v1-akasa-live-4route` (4 routes) | `v1-dgca-fy2022-23` (6 routes) |
| Real observations | 100% of contributing quotes | a small minority — real Akasa quotes land on the 4 shared routes, and Tier-B yields those cells rather than overwriting them |
| Simulated observations | **none, ever** | the overwhelming majority (the demonstration's point) |
| Lineage exposed | per point, and in the CSV | per point, and in the CSV |
| Survives a "rebuild everything" | yes — regenerated from live quotes | yes — regenerated from the seed |

The sandbox basket is the six-route DGCA basket, and the four live Akasa routes
are a subset of it — so real observations *do* enter the sandbox series wherever
they exist, and Tier-B deliberately yields those cells instead of generating a
rival fare for them. That is the honest arrangement: the sandbox shows the full
pipeline over a route set that includes whatever reality is available, and its
provenance block states the exact split (currently ~0.4% live / 95% simulated /
5% imputed, counts published alongside the percentages). What the sandbox must
never do is *pass itself off* as live, and it does not.

The Live Index is the strict environment: it is built from
`v1-akasa-live-4route` only, and any simulated row is excluded from it by
construction, not by convention.

Because the two live under different `basket_version` values, they occupy
different rows in `index_value` under the unique constraint
`uq_index_point (index_date, frequency, series, measure, basket_version)`. A
sandbox row can never overwrite a live row. `tests/test_live_isolation.py`
pins this, and `tests/test_blend_guard.py` makes blending the two without
exposing the split a test failure rather than a judgement call.

---

## Quickstart

Requires Python 3.13 and Node 22.

```bash
cd apix && python -m venv .venv && ./.venv/Scripts/pip install -e ".[dev]"
```

Build a complete demo database — init, DGCA placeholder reference, 45 cycles of
simulated history, and all four index series:

```bash
./.venv/Scripts/python -m apix.cli demo --days 45
```

Serve the API on `127.0.0.1:8000`:

```bash
./.venv/Scripts/python -m apix.cli serve
```

Then the dashboard, in a second terminal:

```bash
cd apix/web && npm install && npm run dev
```

The dashboard is at `http://localhost:5173` and proxies `/v1` to the API, so
the browser stays on one origin and there is no CORS to configure.

Run the tests:

```bash
cd apix && ./.venv/Scripts/python -m pytest -q
```

Check what the Tier-A sources permit before collecting anything:

```bash
./.venv/Scripts/python -m apix.cli compliance
```

---

## Index methodology

### Two levels

**Elementary (within a route) — Jevons.** The geometric mean of price relatives,
computed in log space:

```
I_route = exp( (1/n) · Σ_cells log(p_current / p_base) )
```

A *cell* is a (carrier, advance-purchase window) pair — for example
(`6E`, `T+7`). Jevons is the right choice here because cells on one route differ
in level by large factors: a full-service carrier at T+1 against a low-cost
carrier at T+45. A formula sensitive to those levels would weight expensive
cells more heavily, and that weighting would come from nowhere. Jevons is
*commensurable* — invariant to the units each cell is quoted in — and Dutot is
not. `test_dutot_and_jevons_disagree_on_a_witness_that_cannot_be_filtered_away`
pins the counter-example: one cell denominated in paise instead of rupees moves
Dutot from a 50% rise to a 1% rise, while Jevons reads √2 either way.

Dutot is available via `APIX_ELEMENTARY_FORMULA=dutot` for comparison. Carli is
deliberately **not** offered: it fails time reversal and circularity.

**Upper (across routes) — Laspeyres**, fixed basket, DGCA passenger-traffic
weights:

```
APIx_t = 100 × Σ_r (w_r · I_r,t) / Σ_r w_r
```

The denominator renormalises over *covered* routes, so a route that returned no
data on a given day does not silently count as zero inflation.

### Matched-model principle

Only cells present in **both** the base and the current period contribute. A
carrier entering or leaving a route is not a price change, and
`test_route_entry_does_not_move_the_index` holds the line on that. Unmatched
cells are dropped from the comparison and reported in `cells_matched`, not
assumed unchanged.

### Weights

Source: DGCA, *City Pair Wise Scheduled Domestic Passenger Traffic Statistics*,
FY2022-23. Grand total domestic passengers in that publication: **136,028,656**.

`national_share_pct` is the city-pair's share of that total. `weight` is that
share renormalised across the six basket routes so the weights sum to 1.0, which
is what Laspeyres requires:

```
basket_sum = 4.13 + 3.28 + 2.69 + 1.99 + 1.50 + 1.32 = 14.91
weight_r   = national_share_pct_r / 14.91
```

| Route | City pair | National share | Weight |
|---|---|---:|---:|
| DEL-BOM | Delhi–Mumbai | 4.13% | 0.2770 |
| DEL-BLR | Delhi–Bengaluru | 3.28% | 0.2200 |
| BOM-BLR | Mumbai–Bengaluru | 2.69% | 0.1804 |
| DEL-CCU | Delhi–Kolkata | 1.99% | 0.1335 |
| MAA-DEL | Chennai–Delhi | 1.50% | 0.1006 |
| BLR-HYD | Bengaluru–Hyderabad | 1.32% | 0.0885 |
| | | **14.91%** | **1.0000** |

The six routes cover 14.91% of Indian domestic passenger traffic. The weights
are not hardcoded in Python — they live in `apix/config/basket.yaml`, are loaded
into a `basket_config` table, and the arithmetic above is re-derived from the
published shares by `test_weights_match_the_published_dgca_shares`.

The **live** basket is a subset: the same published shares, renormalised across
the four Akasa non-stop routes only (`load_live_basket`, `config/loader.py`).
Same shares, same rule, different denominator — so the live and sandbox baskets
cannot drift apart in method even though they cover different route sets.

**Basket versioning:** a published basket version is never edited in place. A
new version gets a new `basket_version` and the old one gets an `effective_to`
date; the index chain-links across versions so history stays continuous.

### Axioms the index satisfies

`tests/test_properties.py` asserts these as Hypothesis property tests (21
generated properties at 100 examples each, plus one fixed witness) rather than
describing them in prose:

| Axiom | Meaning |
|---|---|
| Identity | Unchanged prices give exactly 100 |
| Proportionality | All prices ×k gives index ×k |
| Time reversal | I(0→1) · I(1→0) = 1 |
| Circularity | I(0→1) · I(1→2) = I(0→2) |
| Commensurability | Re-denominating one cell changes nothing |
| Mean value | The index lies within the range of its own relatives |

No ordering between Jevons and Dutot is asserted, because none holds in general.

### DGCA monthly data is never an index input

DGCA's monthly averages are used **only** as an ex-post backtest yardstick.
Using monthly data as a daily index input would be a methodological error, and
the separation is enforced by `tests/test_dgca_separation.py` (12 tests) rather
than left to convention.

---

## Data collection

### Two tiers, always distinguishable

**Tier-A (live).** A real Playwright adapter against the Akasa Air booking
path, behind a compliance gate. Akasa is the **only** registered live source,
and that is deliberate — see `apix/scrape/live/__init__.py` for the
reconnaissance result on every other carrier and OTA named in the problem
statement. `AirIndiaExpressSource` remains importable as *evidence* (a real
carrier whose `robots.txt` disallows the fare path), used by the compliance
tests to prove the gate refuses **before** a request is sent. It is
deliberately absent from `REGISTRY`.

**Tier-B (simulated).** A deterministic, seeded fare generator. It exists so the
pipeline can be exercised and demonstrated end-to-end without depending on
whether a scrape succeeded today. It is **labelled simulated at every layer** —
`source_type` on the row, in the lineage JSON, in the API response, on the
dashboard, in the CSV export.

**Tier-1.5 (LIVE_LIMITED)** exists as a provenance category for
rate-capped/summary-level sources. Nothing currently writes it; the taxonomy
is carried so a future negotiated source does not require a schema change.

### Compliance

`RobotsGate` fetches and parses `robots.txt` before any Tier-A request and
**fails closed** — if the gate cannot determine that a path is allowed, the
fetch does not happen. A 404 on `robots.txt` means no restrictions are published
(RFC 9309), which is the one case where absence is permission.

There is no CAPTCHA solver, no Cloudflare bypass, no stealth plugin, no proxy
rotation, and no user-agent randomisation in this codebase. The scraper
identifies itself honestly via `APIX_SCRAPER_USER_AGENT`. A block is recorded as
`BLOCKED_CAPTCHA` and collection stops for that source.

### Politeness primitives

Three mechanisms, each with one job:

- `TokenBucket` (`scrape/base.py`) — the **rate ceiling**, shared across every
  concurrent request in a cycle. `APIX_SCRAPE_RATE_PER_MIN` sets the refill
  rate; a 0.2–0.9 s jitter is added so requests never arrive on a fixed beat.
- `backoff_sleep(attempt, base=1.5, cap=20.0)` — **retry spacing**, capped
  exponential with jitter. A struggling host is backed away from, never hammered.
- `asyncio.Semaphore(APIX_LIVE_CONCURRENCY)` — **in-flight ceiling**, so a
  wide route × window grid cannot open more sockets than the host deserves.

Concurrency widens the *shape* of a cycle; it never raises the *rate*. A
45-cell grid finishes faster than the same grid run serially, and still issues
the same number of requests per minute.

### Status taxonomy

Every collection attempt resolves to exactly one `StatusCode`, so a degraded run
is legible instead of invisible:

| Status | Meaning |
|---|---|
| `OK` | Fares returned and parsed |
| `EMPTY_NO_INVENTORY` | Route genuinely had no seats |
| `COMPLIANCE_REFUSED` | `robots.txt` disallows it; **no request was sent** |
| `ADAPTER_UNAVAILABLE` | No adapter implemented for that source yet |
| `BLOCKED_CAPTCHA` | Challenge page served; stopped, no bypass attempted |
| `RATE_LIMITED` | Asked to slow down; backed off |
| `TIMEOUT` | No response in time |
| `NETWORK_ERROR` | Request failed before a response |
| `MALFORMED` | Response arrived, unreadable as fares |
| `VALIDATION_REJECT` | Parsed quote failed a sanity check, not stored as a fare |

`COMPLIANCE_REFUSED` is styled as success on the dashboard, because it is the
system working correctly.

### Confidence grading

Every published index point carries a **support grade** — 0–100, banded
`HIGH` / `MODERATE` / `LOW` / `INDICATIVE` — computed in
`index/confidence.py` from four weighted components:

| Component | Weight | Measures |
|---|---:|---|
| Observation | 0.40 | Real matched cells against a target basket |
| Provenance | 0.30 | Share of contributing quotes that are live rather than simulated or imputed |
| Coverage | 0.20 | Basket weight actually covered |
| Breadth | 0.10 | Evenness of observations across routes (normalised Shannon entropy) |

This is a **support measure, not a confidence interval.** It says how much real
evidence stands behind a number; it does not say how likely that number is to be
right. The dashboard states this in the panel itself. The weights are published
on the wire (`ConfidenceOut.weights`) so the panel's arithmetic and the
engine's arithmetic are provably the same — a dashboard printing "40%" beside a
component the engine weights at 0.30 would be two documents disagreeing about
one number.

The point of the grade is to turn an honest limitation into a legible one: a
small basket produces grades of `INDICATIVE` and `MODERATE` in the sandbox and
`HIGH` in the live window, and a reader can see *which day they are looking at*
rather than trusting a single headline figure.

### Cleaning pipeline

**Dedupe happens at collection time**, not in the cleaning pass: `scrape/runner.py`
collapses identical itinerary cells to the most complete record and upserts on
`dedupe_hash`, which carries a unique index. Re-running a cycle is therefore a
no-op rather than a doubling.

The cleaning pass itself runs in strict order:
**reset → validate → outlier-flag → impute → normalize.**

The reset stage clears *derived* state so a re-run recomputes instead of
compounding. It deletes imputed rows — those are synthetic fillers this pipeline
created, not observations, so regenerating them is correct — and only ever
un-flags observed rows, never removes them.

Nothing observed is deleted at any stage. **Two outlier rules are kept
deliberately apart:**

- `is_high_demand_outlier` — the **calendar** flag. True inside a configured
  festival/peak window (`config/festivals.yaml`).
- `is_outlier` — the **statistical** flag. Tukey fence per cell (`k=1.5`,
  minimum 8 quotes), fitted on non-festival, non-imputed rows only, so a real
  festival surge is never mistaken for statistical noise.

Headline keeps every row. Core excludes both flags. Because the two flags are
separate columns, the dashboard can say *which* rule removed a day rather than
just "outlier".

Rows imputed by carry-forward (max age `APIX_IMPUTE_MAX_AGE_DAYS=3`) are counted
as imputed regardless of what they were carried forward from — an imputed row
never launders itself into the live count.

---

## Live vs Simulated — what is actually real today

Stated plainly, because a prototype that overstates its own coverage is worth
less than one that states its limits.

**Live today:**

| Route | Live source | Basket weight (live basket) |
|---|---|---:|
| DEL-BOM | Akasa Air | 0.3416 |
| DEL-BLR | Akasa Air | 0.2713 |
| BOM-BLR | Akasa Air | 0.2225 |
| DEL-CCU | Akasa Air | 0.1646 |

That is **4 of 6 basket routes**, carrying **100% of the live basket** but
**12.09% of national domestic passenger traffic**. The live index is built from
these four routes only; no other route contributes to it under any circumstance.

**Simulated:**

| Where | What | Why |
|---|---|---|
| Sandbox mode (`?mode=sandbox`) | 6-route basket, 30 days | Methodology demonstration only. Real Akasa observations on the 4 shared routes are kept and labelled; everything else is generated |
| `tests/` fixtures | generated cycles | Reproducible assertions |
| DGCA reference table | 42 `SYNTHETIC-PLACEHOLDER` rows | Awaiting transcribed published figures |

**How to tell which you are looking at at any moment:**

1. The **sticky banner** at the top of the dashboard is non-dismissible and
   names the current mode in words.
2. Every index point carries a `data_lineage` block whose percentages sum to
   100, and every chart dot is filled by its dominant source.
3. `GET /v1/health` returns a `warnings` array that includes an explicit
   no-live-data warning when the live window is empty.
4. The `.csv` export carries `live_pct`, `simulated_pct` and `imputed_pct` as
   columns, plus the confidence grade — so a number cannot leave the system
   stripped of its caveats.

**What is NOT claimed:** that the sandbox figures describe the Indian market.
They describe the behaviour of the pipeline. The only figures in this repository
that describe real prices are the live Akasa observations and the Ixigo
December 2024 reference benchmarks.

---

## Repository layout — what each file does

> **Line numbers are a snapshot, not a permanent index.** The ranges below were
> accurate at the time of writing and *will* drift as the code changes. Treat
> them as a pointer to where to start reading, and use your editor's symbol
> search for the current position. Every range names a symbol, so searching the
> symbol name is always correct.

### `apix/scrape/` — getting fares in

| File / symbol | Lines | What it does |
|---|---:|---|
| `base.py` | 365 | Foundations shared by every adapter. |
| · `class StatusCode` | 43–73 | The 10-member outcome taxonomy above. One status per attempt. |
| · `class RawQuote` | 80–135 | One observed fare before cleaning. Always carries its provenance; never constructed without a `source_type`. |
| · `class FetchOutcome` | 139–155 | One fetch attempt: exactly one status, plus any quotes it produced. |
| · `class TokenBucket` | 168–185 | Async token bucket. The shared rate ceiling. |
| · `backoff_sleep` | 188–191 | Capped exponential backoff with jitter. |
| · `class RobotsGate` | 197–252 | Fetches and evaluates `robots.txt`. **Fails closed.** |
| · `parse_inr` | 262–282 | Displayed fare → clean numeric INR. Handles the currency strings real sites emit. |
| · `looks_blocked` / `looks_like_consent_wall` | 301–314 | Block-page and consent-interstitial detection. |
| · `class LiveSource` | 320–365 | The interface every Tier-A adapter implements. Adding a source = one subclass + one `REGISTRY` line. |
| `live/akasa.py` | 847 | **The only registered live source.** |
| · `split_components` | 102–130 | Splits Akasa's service-charge array into base fare and each tax/fee component — the split that makes the Headline/Core *and* Total/Base axes possible. |
| · `parse_availability_search` | 171–228 | The real parse: availability JSON → `RawQuote`s. |
| · `count_journeys` / `_journey_offers` | 230–325 | Journey and offer enumeration; handles multi-leg payloads without double-counting. |
| · `looks_challenged` | 371–393 | Challenge-page detection specific to this host. |
| · `class AkasaAirSource` | 395–847 | The adapter. |
| · `· search` | 483–550 | Entry point: run the grid for one route/window set. |
| · `· _one_search` | 552–688 | One browser-driven search with its own retry and status handling. |
| · `· _pick_station` / `_pick_date` / `_find_submit` | 701–778 | Form interaction helpers. |
| · `· _to_quotes` | 779–834 | Adapter-specific `RawQuote` construction. |
| `live/__init__.py` | 78 | The registry, and the written record of *why the other sources are absent*. |
| `live/airindiaexpress.py` | 105 | Excluded adapter, importable as evidence. Refuses at the gate before any request. |
| `simulate.py` | 234 | The deterministic Tier-2 generator. Output is always tagged `simulated`. |
| · `simulate_quote` | 95–167 | One deterministic fare: advance curve × carrier tier × day-of-week × festival. |
| · `simulate_cycle` | 170–234 | A full cycle across the basket. |
| `runner.py` | 550 | Orchestrates one collection cycle. |
| · `class CycleReport` | 55–94 | What one cycle actually did. Drives the CLI summary and `/v1/coverage`. |
| · `dedupe` | 118–131 | Collapses quotes sharing a `dedupe_hash`. |
| · `upsert_quotes` | 164–240 | Idempotent write keyed on `dedupe_hash`. Re-running a cycle is a no-op. |
| · `collect_live_source` | 270–388 | The concurrent grid: `asyncio.gather` + semaphore, rate still governed by the shared `TokenBucket`. A block propagates through a shared `asyncio.Event` so one refusal stops the whole source rather than racing. |
| · `tier_a_plan` | 394–417 | Whether Tier-A runs this cycle — and an explanation when it does not. |
| · `run_cycle` | 420–518 | One full cycle, persisted. |
| · `count_quotes` | 521–550 | Provenance headcount: one key per `SourceType` plus `imputed`. |

### `apix/clean/` — making them comparable

| File / symbol | Lines | What it does |
|---|---:|---|
| `pipeline.py` | 487 | The cleaning pass, in strict order and idempotent. |
| · `class CleanReport` | 60–91 | Counts per stage, so a run's effect is inspectable. |
| · `_reset_cycle` | 97–137 | Clears *derived* state. Deletes only imputed rows it created; un-flags observed rows, never removes them. |
| · `validate` | 143–189 | Sanity bounds and component reconciliation (does base + components equal total?). |
| · `_fence` | 195–199 | Tukey fence: `Q1 − k·IQR`, `Q3 + k·IQR`, inclusive quartiles. |
| · `flag_outliers` | 202–319 | The two flags, kept apart: calendar (`is_high_demand_outlier`) and statistical (`is_outlier`). Fences fitted on non-festival, non-imputed rows. |
| · `impute_missing` | 325–420 | Carry-forward, capped at `APIX_IMPUTE_MAX_AGE_DAYS`. Marked `is_imputed`, never laundered. |
| · `normalize` | 439–449 | Rounding discipline so downstream equality comparisons are exact. |
| · `clean_cycle` | 455–487 | Runs the whole pass for one cycle. |

### `apix/index/` — turning them into a number

| File / symbol | Lines | What it does |
|---|---:|---|
| `construct.py` | 644 | The index arithmetic. |
| · `class RouteRelative` | 91–103 | One route's elementary relative against base. |
| · `class IndexPoint` | 107–127 | A constructed point before persistence. |
| · `_price` / `_eligible` / `_cells` | 133–185 | Which fare aggregate is being indexed (`total` vs `base`), and which quotes may enter the series. |
| · `elementary_relative` | 191–245 | **Jevons** (log-space geometric mean) or Dutot, matched-model. |
| · `_lineage` | 251–305 | Builds the provenance block. Percentages sum to 100 by construction. |
| · `compute_point` | 327–418 | One point: Jevons within routes, Laspeyres across them. |
| · `resolve_base_period` | 424–441 | Earliest cycle with cleaned data, unless pinned. |
| · `build_series` | 472–525 | All four series over a range, optionally persisted. |
| · `weekly_from_daily` | 528–604 | Daily → ISO week by geometric mean. |
| · `chain_link` | 610–644 | Splices a re-based series onto its predecessor at an overlap period. |
| `confidence.py` | 244 | The support grade. |
| · `class Confidence` | 71–96 | A graded statement. Carries `components`, `weights` and `reasons`. |
| · `_evenness` | 99–123 | Normalised Shannon entropy of per-route cell counts — the breadth component. |
| · `grade_point` | 126–244 | Grades one point from its lineage, and always gives a reason. |
| `backtest.py` | 303 | Ex-post check against DGCA monthly averages. |
| · `pearson` / `mape` | 130–149 | Correlation and error. Both return `None` rather than a misleading zero when undefined. |
| · `run_backtest` | 239–303 | Overlapping months only; produces the single `reportable` flag the CLI, API and dashboard all read. |
| `reference_validation.py` | 133 | **Reference Point Validation** — live route averages against independently published market benchmarks. |
| · `run_reference_validation` | 54–133 | Point-deviation check against `external_benchmark_avg_fare`. |

### `apix/api/` — publishing them

| File / symbol | Lines | What it does |
|---|---:|---|
| `main.py` | 1097 | FastAPI app. Read-only `GET` endpoints, API-key auth. |
| · `require_api_key` | 134–142 | Constant-time key check. No-op when `APIX_API_KEY` is blank. |
| · `_provenance_block` | 148–188 | **The one place** a live/simulated/imputed split becomes a published block. It once had four hand-rolled copies; three had dropped the label. |
| · `_aggregate_provenance` / `_quote_provenance` | 191–259 | The same block from index points, or straight from fare rows. |
| · `_resolve_mode` / `_points_for_mode` | 301–373 | `mode` → `basket_version`. The structural Live/Sandbox boundary on the read path. |
| · `_route_cell_counts` | 385–399 | Per-route counts feeding the confidence breadth component. |
| · `health` | 420–483 | Liveness plus an honest self-description, including the no-live-data warning. |
| · `get_index` | 492–571 | The series, with per-point and window-level provenance and confidence. |
| · `get_index_csv` | 599–651 | CSV export: provenance **and** confidence columns travel with every row. |
| · `get_routes` | 660–713 | The basket, its weights, and how much data each route holds. |
| · `get_coverage` | 722–815 | What the collector actually managed, per cycle, including every failure. |
| · `get_reference_validation` | 824–854 | Live averages vs external benchmarks. |
| · `get_backtest` | 864–894 | Ex-post DGCA comparison. |
| · `get_elasticity_curve` | 903–981 | Advance-purchase curve for one route. |
| · `get_heatmap` | 1034–1097 | Route × advance-window medians, with provenance per cell. |
| `schemas.py` | 276 | Pydantic response models. `LineageOut` (17–37), `ConfidenceOut` (40–60), `IndexPointOut` (63–75), and one model per endpoint. Every data response carries provenance. |

### `apix/db/` — the schema

| File / symbol | Lines | What it does |
|---|---:|---|
| `models.py` | 327 | SQLAlchemy 2.x schema. |
| · `class SourceType` | 46–56 | `LIVE` / `LIVE_LIMITED` / `SIMULATED`. The tag that must survive every stage. |
| · `class Series` | 108–116 | `headline` / `core`. |
| · `class Measure` | 119–139 | `total` / `base` — a **separate axis** from `Series`. |
| · `class FareQuote` | 171–229 | Fare components as separate columns, never one `total_fare`. `source_type` and `is_imputed` are first-class. |
| · `class IndexValue` | 302–327 | Carries the `uq_index_point` constraint that keeps live and sandbox baskets in separate rows. |
| · `class BasketConfigRow` | 281–296 | Weights as data, not literals. |
| `session.py` | 70 | Engine and session factory; `session_scope` commits or rolls back as a unit. |
| `time_utils.py` | 45 | IST-aware dates. A "cycle date" is an Asia/Kolkata calendar day, not a UTC timestamp. |

### `apix/config/` — everything tunable, as data

| File / symbol | Lines | What it does |
|---|---:|---|
| `settings.py` | 122 | Pydantic-settings. Every knob is an `APIX_*` variable with a working default. |
| `loader.py` | 270 | Reads YAML into typed config. |
| · `load_basket` | 202–219 | Six-route DGCA basket. |
| · `load_live_basket` | 233–263 | The same published shares renormalised across the four Akasa non-stop routes. One rule, two denominators. |
| · `load_festivals` | 223–230 | The festival/peak windows that drive the calendar flag. |
| `basket.yaml` | — | Six routes with DGCA shares, their published provenance, and the versioning rule. |
| `festivals.yaml` | — | Seven festival/peak windows with multipliers (Holi, Eid al-Fitr, summer vacation, Dussehra, Diwali, Christmas/New Year, and Holi of the following year). |
| `routes.yaml` | — | Airport codes, cities, distances. |
| `cli.py` | 570 | Every operation as a subcommand: `init`, `compliance`, `scrape`, `clean`, `index`, `backtest`, `seed-dgca`, `generate-history`, `status`, `serve`, `demo`. The main entry point and the Windows-friendly stand-in for a Makefile. |

### `web/src/` — the dashboard

| File / symbol | Lines | What it does |
|---|---:|---|
| `App.jsx` | 626 | Layout, fetching, and the mode/series/measure pickers. `class`-free; `Card` (60–78) is the shared panel. Fetches the counterpart series alongside the selected one so the Headline−Core gap can be drawn without a second request. |
| `api.js` | 109 | The single place the browser talks to the API. `downloadCsv` (63–109) sends the key as a header and hands back a blob, because a plain `<a href>` cannot set a header and would 401. |
| `components/SurgeGap.jsx` | 204 | **The differentiator, drawn.** `GapTooltip` (39–67) and `SurgeGap` (69–204). Plots Headline−Core only on days where both exist; a day with no counterpart is omitted, never zero-filled. |
| `components/Confidence.jsx` | 309 | The support grade. `gradeStyle` (77–85) → Tailwind classes; `ConfidenceBadge` (87–112) is a real `<button>`, not a `<span>`, so it is keyboard-reachable; `ComponentRow` (114–160) shows each component's percentage *and* its contribution in points out of 100; `ConfidencePanel` (162–279); `GRADE_COLOURS` (304–309). |
| `components/IndexChart.jsx` | 336 | The trend line. `dominantSource` (30–54) picks the dot's fill; `ProvenanceDot` (56–95) encodes **two channels** — fill = data source, ring = confidence grade; `ProvenanceLegend` (278–336). Clicking a dot selects that day. |
| `components/Provenance.jsx` | 162 | `GlobalProvenanceBanner` (41–57, sticky and non-dismissible), `ProvenanceBadge` (82–108, renders the API's `label` verbatim), `ProvenanceBar` (110–154), `PROVENANCE_COLOURS` (156–162). |
| `components/CoveragePanel.jsx` | 278 | What the collector managed, including every way it failed. Each `StatusCode` gets a plain-English meaning and a tone. |
| `components/Heatmap.jsx` | 233 | Route × advance-window movement against base. An unmatched cell is hatched grey, never 0% — a missing comparison and an unchanged price look nothing alike. |
| `components/ElasticityCurve.jsx` | 146 | Median fare by booking horizon, indexed to T+30. The T+30 anchor is drawn distinctly so its 1.00× is not misread as a finding. |
| `components/BacktestPanel.jsx` | 181 | The backtest. When `reportable` is false the figures are struck through and stamped *inside the same box as the caveat*, so a cropped screenshot carries the mark with it. |
| `components/ReferenceValidationPanel.jsx` | 170 | Live route averages against independently published benchmarks. |
| `vite.config.js` | — | Dev proxy `/v1` → `APIX_API_URL` (default `http://127.0.0.1:8000`), keeping the browser on one origin so there is no CORS to configure. |
| `tailwind.config.js` | — | Provenance-named colours (`live`, `simulated`, `imputed`, `surge`) so the encoding cannot drift between components. |

### `tests/` — 266 tests

| File | Tests | What it pins |
|---|---:|---|
| `test_failure_modes.py` | 87 | Every way collection and cleaning can fail, and what each must do. |
| `test_api.py` | 46 | The API contract: auth, response shape, provenance on every data endpoint, honesty under emptiness, and a static check that the `"SIMULATED DATA"` badge has exactly one definition in the source. |
| `test_compliance.py` | 21 | `robots.txt` handling, fail-closed behaviour, block detection, and that `AirIndiaExpressSource` is *not* registered. |
| `test_properties.py` | 21 | Index-number axioms as Hypothesis properties, plus the Dutot commensurability counter-example. |
| `test_clean.py` | 18 | Stage order, flag-not-delete, festival-aware fences. |
| `test_idempotency.py` | 18 | Re-running a cycle or a rebuild changes nothing. |
| `test_confidence.py` | 17 | Grading bands, sub-score bounds, reason-always-present, and that the published weights reproduce the published score. |
| `test_index.py` | 14 | Golden values, base period exactly 100, weight arithmetic, matched-model, both axes. |
| `test_dgca_separation.py` | 12 | DGCA monthly data cannot reach the daily index path. |
| `test_blend_guard.py` | 8 | Live and simulated are never blended without the split being exposed. |
| `test_live_isolation.py` | 4 | The live basket version never mixes with the sandbox basket version. |

### `scripts/` and root

| File | What it does |
|---|---|
| `scripts/seed_basket.py` | Loads `basket.yaml` and `routes.yaml` into the database. |
| `scripts/seed_dgca.py` | Loads real DGCA figures from CSV (with a required citation), or generates clearly-stamped `SYNTHETIC-PLACEHOLDER` rows. |
| `scripts/seed_external_benchmark.py` | Loads the Ixigo December 2024 reference benchmarks used by Reference Point Validation. |
| `scripts/generate_history.py` | Builds N cycles of simulated back-history for demonstration. |

---

## API

Ten read-only `GET` endpoints. `test_the_api_is_read_only` asserts that no
path exposes any other verb. Authentication is an `X-API-Key` header on every
data endpoint; `/v1/health` is open so a monitor can reach it.

| Endpoint | Returns |
|---|---|
| `GET /v1/health` | Instance state, counts, and `warnings` — including the no-live-data warning |
| `GET /v1/index` | An index series: `?mode=live\|sandbox&series=headline\|core&measure=total\|base`, with per-point and window-level confidence |
| `GET /v1/index.csv` | The same series as CSV, with provenance **and** confidence columns |
| `GET /v1/sandbox/index` | Sandbox index — the explicit, separately-named route to the demonstration environment |
| `GET /v1/routes` | The basket, with weights and per-route quote counts |
| `GET /v1/coverage` | What the latest cycle collected, and every way it failed |
| `GET /v1/reference-validation` | Live averages vs independently published benchmarks |
| `GET /v1/backtest` | APIx vs DGCA, with `reportable` and its reasons |
| `GET /v1/heatmap` | Route × advance-window movement against base |
| `GET /v1/routes/{route}/curve` | Advance-purchase elasticity for one route |

Every data response carries a provenance block with `live_count`,
`simulated_count`, `imputed_count`, their percentages, and a human-readable
`label`. That block is built in exactly one function, `_provenance_block` —
there were once four hand-rolled copies and three had quietly dropped the label,
which is why `test_the_badge_string_has_exactly_one_definition` now counts the
literal in the source.

### A note on the dashboard's API key

The key reaches the browser bundle, and a key in a browser bundle is not access
control — anyone with devtools can read it. It is there because the API requires
the header and the dashboard is a demonstration client, not because it protects
anything. A real deployment puts this behind a server-side session and never
lets the key reach the page. The comment at the top of `web/src/api.js` says the
same thing in the same words.

---

## Configuration

All settings are `APIX_*` environment variables; see `apix/.env.example` for the
annotated list. The ones that change behaviour materially:

| Variable | Default | Notes |
|---|---|---|
| `APIX_DATABASE_URL` | `sqlite:///apix.db` | Postgres supported |
| `APIX_API_KEY` | `dev-nso-rbi-key` | Dev default; the dashboard's default matches |
| `APIX_CORS_ORIGINS` | `http://localhost:5173,…` | Only relevant if you bypass the Vite proxy |
| `APIX_LIVE_ENABLED` | `True` | Master switch for Tier-A |
| `APIX_LIVE_SOURCES` | `akasa` | Which adapter(s) to use. `akasa` is the only registered value. |
| `APIX_LIVE_ROUTES` | all six basket routes | Narrow this to be politer; the live basket uses only the four Akasa non-stop routes regardless |
| `APIX_LIVE_CONCURRENCY` | `4` | In-flight ceiling. Widens a cycle; does not raise the rate. |
| `APIX_SCRAPER_USER_AGENT` | `AeroIndex-Research/0.1 …` | Identifies the scraper honestly. Never spoofed. |
| `APIX_SCRAPE_RATE_PER_MIN` | `6` | One request every ten seconds |
| `APIX_SCRAPE_MAX_RETRIES` | `2` | Retries per fetch, with capped backoff |
| `APIX_SCRAPE_TIMEOUT_MS` | `30000` | Per-request timeout |
| `APIX_SIM_SEED` | `20260909` | Tier-B determinism |
| `APIX_IMPUTE_MAX_AGE_DAYS` | `3` | Carry-forward limit |
| `APIX_OUTLIER_IQR_K` | `1.5` | Tukey fence multiplier |
| `APIX_MIN_QUOTES_FOR_IQR` | `8` | Below this, no outlier call is made |
| `APIX_ELEMENTARY_FORMULA` | `jevons` | Or `dutot`; an unknown value raises |
| `APIX_MIN_ROUTES_FOR_INDEX` | `3` | Below this the point is still published but flagged `low_coverage`, and the dashboard rings it red |

---

## Two documented deviations from the pitch deck

The submitted PPT describes two behaviours that conflict with the five
principles. The principles win, and the deviations are recorded here rather than
quietly resolved in code.

**1. The PPT says hard-blocked sources "fall back to DGCA/OGD data."** This
codebase does not do that. Substituting a different dataset for a blocked live
fetch would either put monthly data into a daily index path (a methodological
error) or present substituted data as if it were the intended observation
(principle 1). A block is recorded as a block, and the affected route simply has
no observation for that cycle. `tests/test_dgca_separation.py` enforces this.

**2. The PPT says the IQR filter "removes" one-day spikes.** This codebase
flags them and keeps them. Deleting festival surges would destroy the Headline
series' whole purpose — travellers really paid those fares — and would erase the
Headline-minus-Core surge measure. The fences are also fitted on non-festival
data only, so a genuine surge is never classified as noise (principle 3).

---

## Run Instructions

Set up once:

```bash
cd apix && python -m venv .venv && ./.venv/Scripts/pip install -e ".[dev]"
```

Check what is permitted before collecting anything:

```bash
./.venv/Scripts/python -m apix.cli compliance
```

Run one live collection cycle against Akasa, then clean and index it:

```bash
./.venv/Scripts/python -m apix.cli scrape --live
```

```bash
./.venv/Scripts/python -m apix.cli clean && ./.venv/Scripts/python -m apix.cli index
```

```bash
./.venv/Scripts/python -m apix.cli status
```

Serve the API, and the dashboard in a second terminal:

```bash
./.venv/Scripts/python -m apix.cli serve
```

```bash
cd apix/web && npm install && npm run dev
```

`APIX_LIVE_ROUTES` accepts a comma-separated subset of the basket if you want a
narrower, politer cycle. There is no flag that raises the request rate above
`APIX_SCRAPE_RATE_PER_MIN`, and none that disables the compliance gate.

---

## What would make this production-grade

Honest roadmap, not a claim of completion.

- **Real live data at scale.** The adapter works but covers four routes on one
  carrier. Broad coverage needs many more compliant adapters or, properly,
  negotiated data access.
- **Official GDS or airline data agreements.** The right long-term source is
  data shared under agreement rather than scraped. That requires institutional
  negotiation; nothing here should be read as implying such an arrangement
  exists or is arranged.
- **Real DGCA reference series.** Replace the placeholder rows with transcribed
  published figures, at which point the backtest becomes reportable.
- **Chain-linking across basket revisions**, exercised over a real revision.
- **Seat-class and ancillary coverage** beyond economy base/total.
- **Alembic migrations** for schema evolution; the schema is currently created
  directly from the models.
- **Operational monitoring** — collection-success alerting, not just a ledger.

---

## Licence and scope

A hackathon prototype, built for evaluation. The airline names and route codes
are factual references. No airline endorses or is affiliated with this project.
