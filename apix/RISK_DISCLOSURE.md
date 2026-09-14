# APIx Risk Disclosure and Tier Mapping

This document explains the provenance and legal risk mapping for the sources integrated into APIx.

## Source Tier Architecture

APIx enforces a strict stratification of data sources based on compliance and legal risk.

### Tier-1: Full Compliance (LIVE)
- **Status:** Integrated (`SourceType.LIVE`)
- **Criteria:** The source's `robots.txt` explicitly allows or does not disallow the paths accessed, AND their Terms of Service do not explicitly prohibit automated access, scraping, or bots.
- **Sources:**
  - **Akasa Air (QP):** No Disallow rules on the flight availability paths. ToS does not contain specific anti-scraping prohibitions that prevent fair use for this research project.

### Tier-1.5: Rate-Capped / Limited Scope (LIVE_LIMITED)
- **Status:** Integrated (`SourceType.LIVE_LIMITED`)
- **Criteria:** The source's `robots.txt` permits crawling of flight search endpoints, but their Terms of Service contain standard boilerplate language prohibiting automated access or commercial scraping. To balance the academic mandate of this prototype against these prohibitions, access is strictly rate-capped (default 30 requests/day/source) and performed transparently.
- **Sources:**
  - **Yatra:** `robots.txt` permits the endpoint. ToS explicitly prohibits "use [of] any 'robot', 'spider' or other automatic device".
  - **MakeMyTrip:** `robots.txt` permits the endpoint. ToS prohibits automated access and use for "commercial purpose".

### Tier-2: Simulated (SIMULATED)
- **Status:** Integrated (`SourceType.SIMULATED`)
- **Criteria:** Used when real live data is unavailable or when the underlying source explicitly prohibits scraping both in their `robots.txt` and Terms of Service (e.g. Skyscanner, Kayak, Ixigo).
- **Function:** A deterministic engine that generates statistically realistic but entirely fake fare quotes to ensure the pipeline and UI can be tested without breaking compliance rules.

## Excluded Sources
The following sources were evaluated but permanently excluded from live integration due to explicit restrictions in both `robots.txt` and their Terms of Service:
- **Skyscanner**
- **Kayak**
- **Ixigo**
- **Air India Express** (explicitly disallows `/flight-availability` in `robots.txt`)

*No code in APIx attempts to bypass these restrictions.*

## Provenance Enforcement
Every quote in the `fare_quotes` table explicitly carries its `source_type`. The cleaning and indexing pipeline propagates these flags through to the final `IndexPoint`, which carries a precise breakdown of `live_pct`, `live_limited_pct`, `simulated_pct`, and `imputed_pct`. 

The API and the UI dashboard surface these percentages prominently, ensuring that consumers are never misled into treating a predominantly simulated or rate-capped index as a full market snapshot.
