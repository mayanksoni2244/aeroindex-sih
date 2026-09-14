# RECON_RESULTS.md — Tier-1 carrier reconnaissance

**Date of reconnaissance:** 2026-09-12
**Performed by:** AeroIndex build session, against live public carrier sites
**Client used:** Python 3.13 `httpx` 0.28 and Chromium 1234 via Playwright 1.62
**User agent sent:** `AeroIndex-Research/0.1 (SIH26056 academic prototype; contact: team-scalex@example.edu)`

This file is a deliverable, not a scratch note. It records what was tested, what
was observed, and — as importantly — **what could not be tested from this
environment**. Every verdict below is marked with its evidence class:

- **[DIRECT]** — observed in this session, evidence quoted.
- **[INFERRED]** — deduced from a direct observation.
- **[UNTESTED]** — could not be verified here; stated as untested rather than assumed.

---

## Headline finding

> **One of the five PS-named carriers is both compliance-permitted and
> technically reachable for fare collection: Akasa Air.**
>
> The other four are stopped by one of three distinct causes — a `robots.txt`
> prohibition, an active bot rejection, or total unreachability from this
> network. Full detail per carrier below.

This is 1 of 5 carriers. That is thin, it is below the coverage the brief hoped
for, and per the brief's own instruction ("If reconnaissance shows zero carriers
are viably scrapeable at all, STOP and report this back") this finding is
escalated rather than absorbed. It is not zero, so the pipeline is not abandoned
— but the achieved real-data percentage will be low, and Section 5 of the README
must say so in plain numbers rather than presenting simulated data as the plan.

---

## Per-carrier verdicts

| Carrier | `robots.txt` permits fare path? | Reachable? | Challenge? | Backend data | **Verdict** |
|---|---|---|---|---|---|
| **Akasa Air** | ✅ Yes — no `Disallow` at all | ✅ Yes | ✅ None | ✅ Clean JSON API | **PASS** |
| **Air India Express** | ❌ No — `/flight-availability` disallowed | ✅ Yes | ✅ None | n/a | **FAIL (compliance)** |
| **SpiceJet** | ❌ No — `/api/v1` disallowed | ✅ Yes | ✅ None | JSON API (off-limits) | **FAIL (compliance)** |
| **IndiGo** | ⚠️ Undeterminable — robots.txt unretrievable | ❌ Bot rejection | ⛔ Rejection page | none served | **FAIL (blocked)** |
| **Air India** | ⚠️ Undeterminable — robots.txt unretrievable | ❌ HTTP/2 error | — | none served | **FAIL (unreachable)** |

---

## 1. Akasa Air — PASS

**`robots.txt`** (`https://www.akasaair.com/robots.txt`, HTTP 200, 133 bytes) **[DIRECT]**

```
User-Agent: *
Sitemap: https://www.akasaair.com/sitemap.xml
Sitemap: https://www.akasaair.com/book-flight-tickets/sitemap_index.xml
```

No `Disallow` directive of any kind. Every path is permitted to every agent.
Saved verbatim at `recon/robots_akasa.txt`.

**Page load** **[DIRECT]** — HTTP 200, 432,450 bytes, no challenge markers
(`just a moment`, `cf-chl`, `captcha`, `checking your browser` all absent).
Title resolved correctly to *"Book Flights Online with Akasa Air at affordable
fares."*

**Backend data shape — the brief's central question** **[DIRECT]**

Fare data arrives via **clean backend JSON/XHR**, not obfuscated HTML. Captured
in a real browser session:

```
POST https://prod-bl.qp.akasaair.com/api/ibe/token/generateToken
     body: {"deviceType":"WEB","bookingType":"BOOKING","userType":"LOGIN"}
GET  https://prod-bl.qp.akasaair.com/api/ibe/resources/master-data
GET  https://prod-bl.qp.akasaair.com/api/nsk/v2/resources/markets?ActiveOnly=true
```

This is a conventional IBE (internet booking engine) with a token handshake and
a master-data resource. Confirmed response shapes **[DIRECT]**:

```jsonc
// POST /api/ibe/token/generateToken
// REQ  {"deviceType":"WEB","bookingType":"BOOKING","userType":"LOGIN"}
// RESP
{"data":{"idleTimeoutInMinutes":15,"token":"py9C8iSba3NVOlkFabqPt7RZ…"}}   // 15-min expiry

// GET /api/ibe/resources/master-data
// RESP
{"data":{"cities":{"values":[{"cityCode":"AMD","countryCode":"IN",
                              "name":"Ahmedabad","provinceStateCode":"GJ"}, …]}, …}}

// GET /api/nsk/v2/resources/markets?ActiveOnly=true
// RESP
{"data":[{"locationCode":"AMD","travelLocationCode":"AUH",
          "earliestCheckInFrom":2880,"earliestCheckInTo":2880,
          "latestCheckInFrom":180,"latestCheckInTo":180,
          "includesTaxesAndFees":0,"inActive":false}, …]}
```

Two details here matter for the adapter design:

- **`markets` publishes the servable city pairs directly**, with `inActive`
  flags. This answers the route-coverage question statically — the adapter can
  read which of the six basket routes Akasa actually serves instead of probing
  and guessing.
- `includesTaxesAndFees: 0` is a per-market flag that bears directly on the
  Total-vs-Base measure axis and must be read per market rather than assumed.
- **`earliestCheckInFrom` / `latestCheckInFrom` are ONLINE CHECK-IN windows, not
  booking windows** — 2880 and 180 minutes are 48 hours and 3 hours before
  departure, which is when web check-in opens and closes. They say nothing about
  how far ahead a fare can be searched and must **not** be used to bound the
  advance-purchase axis. Recorded explicitly because the field values look like
  a booking horizon at a glance, and reading them that way would wrongly rule
  out the T+30 and T+45 basket cells. The real booking horizon is still
  **[UNTESTED]** — it can only be established by driving the search flow at
  increasing dates.

**The fare-availability endpoint itself was NOT captured.** A real search was
driven (Delhi → Mumbai, `#From`/`#To` filled, date entered, submit clicked) but
the form did not complete — the date control retained `Fri, 11 Sep 2026` rather
than the intended future date and the page never navigated, so no
availability/pricing call fired. The token and master-data calls above are from
that session. Capturing the fare call requires correct date-picker interaction
and is the outstanding next step.

Nothing here requires defeating a challenge: the data is published as plain
JSON to a normal browser.

**Rate behaviour** **[DIRECT, compressed]**

5 sequential GETs with ~20s jittered spacing, all HTTP 200, all returning an
identical 432,450 bytes, latencies 355–437ms, no `Retry-After` header, no
throttling, no block:

```
#1  HTTP 200  432450B  390ms  retry-after=-
#2  HTTP 200  432450B  359ms  retry-after=-
#3  HTTP 200  432450B  355ms  retry-after=-
#4  HTTP 200  432450B  437ms  retry-after=-
#5  HTTP 200  432450B  387ms  retry-after=-
```

> ⚠️ **This is NOT the 10-minute / 5-request protocol the brief specifies.** It
> was compressed to ~80 seconds to keep the session bounded. It is evidence that
> Akasa did not throttle a polite burst; it is **not** evidence about behaviour
> over a longer window. The full protocol still needs running before any
> sustained collection. Stated here rather than quietly presented as equivalent.

---

## 2. Air India Express — FAIL (compliance)

**`robots.txt`** (HTTP 200, 392 bytes) **[DIRECT]** — saved at `recon/robots_aix.txt`

```
User-agent: *
Disallow: /rsm-dashboard
Disallow: /retro-claim
Disallow: /loyalty-addon-packs
Disallow: /tcp-terms
Disallow: /tcp-terms-mb
Disallow: /loyalty-benefits-mb
Disallow: /loyalty-program-mb
Disallow: /growtrees
Disallow: /growtrees-certificate
Disallow: /flight-availability
Disallow: /dam
Disallow: /content/dam
Sitemap: https://www.airindiaexpress.com/sitemap.xml
```

**`/flight-availability` is explicitly disallowed.** That is the fare-search
path itself. This is not a technical obstacle to be worked around — it is a
stated prohibition, and the answer is no.

The site is otherwise perfectly reachable (HTTP 200, no challenge), which is
precisely why this must be recorded as a *compliance* failure and not a
*technical* one. A less careful implementation would scrape it successfully and
be in the wrong.

This matches the existing adapter: `apix/scrape/live/airindiaexpress.py`
documents the same finding from a 2026-09-09 check and implements
`COMPLIANCE_REFUSED`, returning without sending a request. Independent
reconnaissance agrees with the code.

---

## 3. SpiceJet — FAIL (compliance)

**`robots.txt`** (HTTP 200, 608 bytes) **[DIRECT]** — saved at `recon/robots_spicejet.txt`

```
User-agent: *
Disallow:
Disallow: /cgi-bin/
Disallow: https://www.spicejet.com/api/v1
Disallow: https://www.spicejet.com/public/
Disallow: https://www.spicejet.com/externalBooking
```

**Page load** **[DIRECT]** — HTTP 200, no challenge, title resolved
(*"SpiceJet - Flight Booking for Domestic and International…"*). The site works
and serves a JSON API:

```
GET  https://www.spicejet.com/api/v1/search/getStationDetails?getAllCities=true&getPopular=true
GET  https://www.spicejet.com/api/v1/content/homeBanners
GET  https://www.spicejet.com/api/v1/featureconfig/getAllFeatureConfig
POST https://www.spicejet.com/api/v1/token
```

**Every one of those endpoints is disallowed.** `/api/v1` is named explicitly.
The site is technically wide open and ethically closed — the same shape of
finding as Air India Express, and the one most likely to be scraped by accident
by a team that tests reachability but not permission.

---

## 4. IndiGo — FAIL (blocked)

**`robots.txt`** — **could not be retrieved.** `https://www.goindigo.in/robots.txt`
failed with `ReadError` across attempts. Under the fail-closed rule, an
unretrievable `robots.txt` means **not permitted**, so this source is closed on
compliance grounds alone, before any technical question. Saved at
`recon/robots_indigo.txt` with that reasoning recorded.

**Reachability** **[DIRECT]** — In Chromium, the page returns HTTP 200 but serves
a rejection page. Body length 1,005 bytes, empty `<title>`, zero XHR/fetch
calls, and visible text:

```
Something went wrong

Please contact our customer support team.
```

This is an active block. **No attempt was made to defeat it** — no user-agent
changing, no stealth plugin, no proxy, no retry escalation. Per the rules, this
is logged and the source is dropped.

This also matches the existing note in `apix/scrape/live/akasa.py`, which
records IndiGo serving a bot-detection interstitial from datacentre IPs.

> **Caveat on interpretation.** The rejection may be geo- or network-specific.
> This session runs from a datacentre IP, which is exactly the profile such
> defences target. A residential connection might see IndiGo serve normally.
> That does **not** change the verdict — its `robots.txt` is still unretrievable
> and the fail-closed rule still applies — but it does mean "IndiGo blocks
> everyone" would be an overstatement of what was observed.

---

## 5. Air India — FAIL (unreachable)

**`robots.txt`** — **could not be retrieved.** `https://www.airindia.com/robots.txt`
timed out across attempts (30s and 60s). Fail-closed: not permitted. Saved at
`recon/robots_airindia.txt`.

**Reachability** **[DIRECT]** — in Chromium, navigation fails outright:

```
Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at https://www.airindia.com/
```

The connection is refused at the HTTP/2 layer. No content, no challenge, no
data. Nothing to scrape and no way to determine permission.

---

## Coverage this implies

The basket is **6 routes × 5 booking windows = 30 cells**. With Akasa Air as the
only viable source, and noting that Akasa does not serve all six basket routes
(our own config restricts live collection to `DEL-BOM` for exactly this reason),
the achievable live coverage is a small fraction of the 30-cell grid.

**The honest number is not yet known.** It depends on which of the six routes
Akasa actually serves and whether the fare-availability endpoint can be captured
and driven reliably. That must be measured by running a real collection cycle,
not estimated here. What can be stated now:

- **Carriers passing reconnaissance: 1 of 5** (Akasa Air).
- **Carriers failed on compliance grounds: 2 of 5** (Air India Express,
  SpiceJet) — reachable but prohibited.
- **Carriers failed on technical grounds: 2 of 5** (IndiGo blocked, Air India
  unreachable).
- **Live cell coverage: to be measured, then reported verbatim in README §5.**

---

## Gaps — what this reconnaissance did NOT establish

Recorded plainly so nobody mistakes this file for more than it is:

1. **The Akasa fare-availability endpoint was not captured.** The token and
   master-data calls were observed; the actual availability/pricing call
   requires completing a real search flow in the browser (selecting origin,
   destination, date, submitting). That is the single highest-value next step,
   and until it is done the per-cell live coverage cannot be stated.
2. **The 10-minute rate protocol was compressed to ~80 seconds.** Longer-window
   behaviour is untested.
3. **Only homepages and search entry points were exercised.** No booking was
   completed. No payment path was touched — deliberately.
4. **Reconnaissance ran once, from one network.** A different egress IP could
   change the IndiGo and Air India results entirely.
5. **The brief asked for headed/debug-mode browsing.** This ran headless, because
   no interactive display is available in this environment. Headless is not an
   evasion technique here — the same user agent and no stealth measures were
   used — but it is a deviation from the instruction and is stated as such.
6. **Two carriers (Air India, IndiGo) were never *permitted* or *denied* by
   robots.txt — permission was simply undeterminable**, which the fail-closed
   rule resolves as denied.

---

## Consequence for the build

Per the brief: *"If reconnaissance shows zero carriers are viably scrapeable at
all, STOP and report this back before writing the rest of the pipeline."*

The result is not zero — it is one — so the pipeline is not abandoned. But the
brief also says Tier-2 fallback is *"never a default; it is a documented
exception per cell."* With one viable carrier, Tier-2 would cover the large
majority of cells, which is materially the same problem the brief was written to
prevent.

**This needs a human decision before the adapters are built out**, because the
options have different costs and the wrong one wastes the remaining time:

- **(a) Accept thin live coverage.** Build the Akasa adapter properly, collect
  what its routes allow, and report a low but honest live percentage. Defensible
  and entirely within the ethics rules.
- **(b) Widen the basket to routes Akasa serves.** More live cells, but the
  basket would then be chosen by data availability rather than by DGCA traffic
  share — a methodological compromise that would need stating loudly.
- **(c) Negotiated access.** The correct long-term answer (GDS or carrier data
  under agreement), and not achievable within a hackathon.
- **(d) Re-test from a different network.** Cheap, and might recover IndiGo.
  Does not help Air India Express or SpiceJet, which fail on compliance.

My recommendation is **(a) plus (d)**: build the Akasa adapter for real, measure
the true coverage, and re-run reconnaissance once from a residential connection
to see whether IndiGo is recoverable. That keeps every claim honest and does not
compromise the basket definition.

**No adapter code has been written or changed pending this decision.**
