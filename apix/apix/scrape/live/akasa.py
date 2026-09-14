"""
Tier-A live adapter: Akasa Air (QP).

WHY THIS SOURCE
Chosen empirically. On 2026-09-09 we fetched the robots.txt of every carrier
and OTA named in the problem statement and compared them:

    Akasa Air          `User-Agent: *` with NO Disallow rules at all
    SpiceJet           allows `*` but explicitly disallows /api/v1, /public/,
                       /externalBooking  -> backend-JSON interception is
                       *disallowed* there, so we do not do it
    Air India Express  explicitly disallows /flight-availability -> the fare
                       search path itself is off limits
    IndiGo, Air India, MakeMyTrip, Yatra  robots.txt did not respond

Akasa is therefore the only named carrier whose published crawl policy places
no restriction on the booking path. Re-run `apix compliance` to re-check.

HOW IT COLLECTS, AND WHY THIS SHAPE
The previous version of this file deep-linked to
`/booking/select-flight?origin=DEL&...` and then harvested *any* price-shaped
number out of *any* JSON the page happened to fetch. Run against the real site
that URL renders no fares at all, so the harvester fell through to Akasa's
Storyblok CMS payload and filed marketing-banner numbers as live airline
fares: status OK, ten quotes, plausible values, entirely fictional. That is
the worst failure this project can have, because it is invisible from outside.

This version does the opposite. It drives the site's own booking widget the
way a person does — choose origin, choose destination, choose the date from
the calendar, press Search — and reads exactly one endpoint:

    POST /api/ibe/availability/search

captured on 2026-09-12 for DEL-BOM. That response carries the full component
breakdown the dual index needs, and its parts reconcile to the total exactly:

    FarePrice  5945.0          -> base fare        (Core APIx)
    Tax         304.0          -> taxes
    TravelFee   UDF 152, DUDF 89 -> user development fees
    TravelFee   CUTE 75, RCS 50, WFE 350, ASF 236 -> other statutory fees
    ---------------------------------------------------------------
    sum        7201.0  ==  totals.fareTotal        (Headline APIx)

If that endpoint does not appear, or its shape changes, the cycle records
EMPTY_NO_INVENTORY / MALFORMED. It never falls back to reading numbers out of
some other response. A fare is only a fare if it came from the fare endpoint,
attached to a real flight designator.

WHAT THIS ADAPTER WILL AND WILL NOT DO
It identifies itself truthfully, obeys a token bucket, jitters its timing and
backs off exponentially. If Akasa challenges it, the run records
BLOCKED_CAPTCHA and stops for that source. There is no CAPTCHA solving, no
header spoofing, no proxy rotation and no headless-evasion here, and there
must never be: the value of this project to a statistical agency rests on the
data being collectable by lawful means.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any

from apix.config.settings import settings
from apix.db.models import AdvanceWindow, SourceType, TripType
from apix.db.time_utils import advance_window_days, today_ist
from apix.scrape.base import (
    ComplianceDecision,
    FetchOutcome,
    LiveSource,
    RawQuote,
    StatusCode,
    backoff_sleep,
)

logger = logging.getLogger("apix.scrape.live.akasa")

#: The one endpoint whose body we are willing to read fares out of. Anything
#: else the page fetches — CMS content, analytics, fee catalogues — is ignored,
#: which is the whole point (see module docstring).
FARE_ENDPOINT = "/api/ibe/availability/search"

#: Service-charge `type` values, as observed in the live payload.
_TYPE_FARE = "fareprice"
_TYPE_TAX = "tax"
_TYPE_FEE = "travelfee"

#: TravelFee codes that are user development fees rather than general fees.
#: UDF is the airport User Development Fee; DUDF its domestic counterpart.
#: Kept separate because the brief models `udf` as its own component.
_UDF_CODES = {"UDF", "DUDF"}


class AkasaParseError(ValueError):
    """The fare payload did not have the structure this adapter understands."""


# ─────────────────────────────────────────────────────────────────────────────
# Parsing — pure functions, no I/O, so the contract can be unit-tested against
# a recorded payload without a browser.
# ─────────────────────────────────────────────────────────────────────────────
def split_components(service_charges: list[dict]) -> dict[str, float]:
    """
    Split one passenger's `serviceCharges` into the index's fare components.

    Every charge is assigned to exactly one bucket, so the parts always sum to
    the whole. An unrecognised charge type is counted into `convenience_fee`
    rather than dropped: dropping it would silently break the reconciliation
    check in the cleaner, and a fee we cannot classify is still money the
    passenger pays.
    """
    out = {"base_fare": 0.0, "taxes": 0.0, "udf": 0.0, "convenience_fee": 0.0}
    for charge in service_charges:
        if not isinstance(charge, dict):
            continue
        amount = charge.get("amount")
        if not isinstance(amount, (int, float)):
            continue
        ctype = str(charge.get("type") or "").strip().lower()
        code = str(charge.get("code") or "").strip().upper()
        if ctype == _TYPE_FARE:
            out["base_fare"] += float(amount)
        elif ctype == _TYPE_TAX:
            out["taxes"] += float(amount)
        elif ctype == _TYPE_FEE and code in _UDF_CODES:
            out["udf"] += float(amount)
        else:
            out["convenience_fee"] += float(amount)
    return out


def _fare_index(payload: dict) -> dict[str, dict]:
    """Map fareAvailabilityKey -> its components and total."""
    index: dict[str, dict] = {}
    for entry in payload.get("faresAvailable") or []:
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if not isinstance(value, dict):
            continue
        key = value.get("fareAvailabilityKey") or entry.get("key")
        if not key:
            continue

        # An adult passenger's charges. The basket is one adult, one way.
        charges: list[dict] = []
        cabin = None
        for fare in value.get("fares") or []:
            if not isinstance(fare, dict):
                continue
            cabin = cabin or fare.get("productClass")
            for pf in fare.get("passengerFares") or []:
                if isinstance(pf, dict) and str(pf.get("passengerType", "ADT")) == "ADT":
                    charges.extend(pf.get("serviceCharges") or [])
        if not charges:
            continue

        components = split_components(charges)
        totals = value.get("totals") or {}
        published = totals.get("fareTotal")
        computed = round(sum(components.values()), 2)
        index[str(key)] = {
            **components,
            "total_fare": float(published) if isinstance(published, (int, float)) else computed,
            "computed_total": computed,
            "cabin": cabin,
        }
    return index


def parse_availability_search(
    payload: Any,
    origin: str,
    destination: str,
    travel_date: date,
) -> list[dict]:
    """
    Turn one `POST /api/ibe/availability/search` body into priced non-stop offers.

    The response splits the information in two and expects the client to join
    them: `results[].trips[].journeysAvailableByMarket[]` holds the flights
    (designator, segments, and which fare keys apply), while `faresAvailable[]`
    holds the money per fare key. A fare key with no flight is not an offer,
    and a flight with no fare key is not priced — so only the join is usable.

    Raises AkasaParseError if the document is not this endpoint's shape at all.
    Returns [] when the shape is right but the requested market/date has no
    sellable non-stop inventory, which is a real answer, not a failure.
    """
    if not isinstance(payload, dict):
        raise AkasaParseError(f"expected a JSON object, got {type(payload).__name__}")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise AkasaParseError("no `data` object in the response")
    if "faresAvailable" not in data or "results" not in data:
        raise AkasaParseError(
            "not an availability/search body "
            f"(keys: {sorted(data)[:8]})"
        )

    currency = str(data.get("currencyCode") or "INR")
    fares = _fare_index(data)
    if not fares:
        return []

    wanted_market = f"{origin.upper()}|{destination.upper()}"
    offers: list[dict] = []

    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        for trip in result.get("trips") or []:
            if not isinstance(trip, dict):
                continue
            trip_date = _parse_dt(trip.get("date"))
            if trip_date is not None and trip_date.date() != travel_date:
                continue  # a neighbouring day the widget also loaded
            for market in trip.get("journeysAvailableByMarket") or []:
                if not isinstance(market, dict):
                    continue
                if str(market.get("key") or "").upper() != wanted_market:
                    continue
                for journey in market.get("value") or []:
                    offers.extend(
                        _journey_offers(journey, fares, currency, travel_date)
                    )
    return offers


def count_journeys(
    payload: Any, origin: str, destination: str
) -> tuple[int, int]:
    """
    (non-stop, connecting) journey counts for one market. Diagnostic only.

    `parse_availability_search` returning [] answers "no priced non-stop offer",
    which conflates two facts the project has to report differently:

      * Akasa flies the city-pair but only as a connection — a real answer
        about its network, and a correct exclusion under the matched-model rule
        (§3.6: the elementary quote is a non-stop economy one-way).
      * Akasa returns nothing for the market at all — no service.

    Both produce zero rows either way, so this does not affect what is
    collected. It exists so the note attached to the empty cell states which
    one it was, and so the README can say "excluded: sold only as a connection"
    instead of the vaguer "no inventory".
    """
    wanted = f"{origin.upper()}|{destination.upper()}"
    nonstop = connecting = 0
    if not isinstance(payload, dict):
        return 0, 0
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return 0, 0
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        for trip in result.get("trips") or []:
            if not isinstance(trip, dict):
                continue
            for market in trip.get("journeysAvailableByMarket") or []:
                if not isinstance(market, dict):
                    continue
                if str(market.get("key") or "").upper() != wanted:
                    continue
                for journey in market.get("value") or []:
                    if not isinstance(journey, dict):
                        continue
                    if len(journey.get("segments") or []) == 1:
                        nonstop += 1
                    else:
                        connecting += 1
    return nonstop, connecting


def _journey_offers(
    journey: Any, fares: dict[str, dict], currency: str, travel_date: date
) -> list[dict]:
    """Every priced fare on one journey, non-stop only."""
    if not isinstance(journey, dict):
        return []
    designator = journey.get("designator") or {}
    segments = journey.get("segments") or []

    # §3.6: the elementary quote is a non-stop economy one-way. A journey with
    # more than one segment is a connection and is a different product, so it
    # is excluded rather than compared against non-stops.
    if len(segments) != 1:
        return []

    departure = _parse_dt(designator.get("departure"))
    if departure is not None and departure.date() != travel_date:
        return []

    carrier = None
    flight_number = None
    ident = segments[0].get("identifier") if isinstance(segments[0], dict) else None
    if isinstance(ident, dict):
        carrier = ident.get("carrierCode")
        flight_number = ident.get("identifier")

    out: list[dict] = []
    for fare_ref in journey.get("fares") or []:
        if not isinstance(fare_ref, dict):
            continue
        priced = fares.get(str(fare_ref.get("fareAvailabilityKey") or ""))
        if priced is None:
            continue
        # Sold-out fare buckets are still listed; only sellable ones are prices
        # a passenger could actually pay today.
        if not _is_sellable(fare_ref):
            continue
        out.append(
            {
                **priced,
                "currency": currency,
                "carrier": str(carrier or "QP"),
                "flight_number": str(flight_number or "").strip() or None,
                "departure": departure,
            }
        )
    return out


def _is_sellable(fare_ref: dict) -> bool:
    details = fare_ref.get("details") or []
    if not details:
        return True  # no availability block published; do not over-filter
    for detail in details:
        if not isinstance(detail, dict):
            continue
        if str(detail.get("status") or "").lower() != "active":
            continue
        count = detail.get("availableCount")
        if count is None or (isinstance(count, (int, float)) and count > 0):
            return True
    return False


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Challenge detection
# ─────────────────────────────────────────────────────────────────────────────
#: Markers of a real interstitial challenge. Deliberately specific: the earlier
#: keyword list matched ordinary page copy, so a perfectly good Akasa run could
#: be reported as blocked. A challenge is only declared when the page is a
#: challenge page *and* no fare endpoint responded.
_CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "cf_chl_opt",
    "/cdn-cgi/challenge-platform",
    "challenge-error-text",
    "_incapsula_resource",
    "distil_r_captcha",
    "px-captcha",
    "g-recaptcha",
    "h-captcha",
    "captcha-delivery.com",
)


def looks_challenged(html: str, title: str = "") -> bool:
    """
    True only for an actual anti-bot interstitial.

    Checked against markers that vendors emit (Cloudflare's challenge
    platform, Imperva/Incapsula, DataDome, PerimeterX, reCAPTCHA, hCaptcha)
    rather than against words like "verify" or "robot", which appear in normal
    airline copy — "verify your booking", "robotic wheelchair assistance".
    """
    blob = (html or "").lower()
    if any(marker in blob for marker in _CHALLENGE_MARKERS):
        return True
    head = (title or "").strip().lower()
    return head in {
        "just a moment...",
        "attention required! | cloudflare",
        "access denied",
        "pardon our interruption",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Adapter
# ─────────────────────────────────────────────────────────────────────────────
class AkasaAirSource(LiveSource):
    name = "AkasaAir"
    base_url = "https://www.akasaair.com"
    carrier_code = "QP"

    #: Booking entry point. The widget lives on the home page; the search is
    #: performed there rather than by deep link, because the deep link renders
    #: no fares (see module docstring).
    HOME = "https://www.akasaair.com/"

    #: How long to wait for the fare POST after pressing Search. Observed at
    #: 20-60s on a cold context; the site does a good deal of work first.
    FARE_WAIT_S = 75

    def __init__(self) -> None:
        super().__init__()
        self._pw = None
        self._browser = None
        self._context = None
        # Guards the launch below. Without it, concurrent searches racing on a
        # cold adapter each see `_context is None` and each start a Chromium —
        # every one but the last is then leaked, because `close()` only knows
        # about the handles the last writer left behind.
        self._browser_lock = asyncio.Lock()

    # -- compliance ---------------------------------------------------------
    def search_url(self, origin: str, destination: str, travel_date: date) -> str:
        """
        The page actually visited. Compliance is checked against this, not
        against the backend host: the backend call is issued by Akasa's own
        JavaScript on Akasa's own page, which is the only way this adapter
        touches it.
        """
        return self.HOME

    def check_compliance(
        self, origin: str, destination: str, travel_date: date
    ) -> ComplianceDecision:
        return self.robots.check(self.search_url(origin, destination, travel_date))

    # -- browser lifecycle --------------------------------------------------
    async def _ensure_browser(self):
        """
        Start one browser for the whole cycle.

        A cycle is 6 routes x 5 windows; launching Chromium per search cost
        more than the searches themselves, and re-establishing a session on
        every request is also heavier on Akasa than keeping one.

        One browser is shared by every concurrent search, each of which opens
        its own page. That is the cheap direction to parallelise: the cost of a
        search is almost entirely the wait for Akasa's fare POST, not local CPU.
        """
        if self._context is not None:
            return self._context
        async with self._browser_lock:
            # Re-check: another search may have launched it while we waited.
            if self._context is not None:
                return self._context
            from playwright.async_api import async_playwright  # noqa: PLC0415

            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=settings.scraper_user_agent,
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            # Publish only once all three exist, so a failure part-way through
            # cannot leave a half-built browser visible to the next caller.
            self._pw, self._browser, self._context = pw, browser, context
            return self._context

    async def close(self) -> None:
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:  # teardown must never mask a collection result
                pass
        self._pw = self._browser = self._context = None

    # -- collection ---------------------------------------------------------
    async def search(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        cabin: str = "economy",
    ) -> FetchOutcome:
        """Fetch live Akasa fares. Never raises — every failure maps to a status."""
        decision = self.check_compliance(origin, destination, travel_date)
        if not decision.allowed:
            return FetchOutcome.fail(StatusCode.COMPLIANCE_REFUSED, decision.reason)

        try:
            import playwright.async_api  # noqa: F401,PLC0415
        except ImportError:
            return FetchOutcome.fail(
                StatusCode.ADAPTER_UNAVAILABLE,
                "playwright is not installed (`pip install playwright`)",
            )

        cycle_date = today_ist()
        try:
            window = AdvanceWindow.from_days(
                advance_window_days(travel_date, cycle_date)
            )
        except ValueError:
            return FetchOutcome.fail(
                StatusCode.VALIDATION_REJECT,
                f"{travel_date} is not a configured advance window from {cycle_date}",
            )

        last_status, last_note = StatusCode.NETWORK_ERROR, ""
        for attempt in range(settings.scrape_max_retries + 1):
            await self.bucket.acquire()
            try:
                status, note, offers = await self._one_search(
                    origin, destination, travel_date
                )
            except Exception as exc:  # the runner must never crash
                status, note, offers = (
                    StatusCode.NETWORK_ERROR,
                    f"{type(exc).__name__}: {exc}"[:200],
                    [],
                )

            if status is StatusCode.OK:
                quotes = self._to_quotes(
                    offers, origin, destination, travel_date, cycle_date, window
                )
                if quotes:
                    return FetchOutcome.ok(
                        quotes, f"{len(quotes)} live quote(s) from {self.name}"
                    )
                status, note = (
                    StatusCode.MALFORMED,
                    f"{len(offers)} offer(s) parsed but none formed a valid quote",
                )

            # A block means stop. We do not escalate, retry harder, or change
            # identity — that is the constitution's line.
            if status is StatusCode.BLOCKED_CAPTCHA:
                return FetchOutcome.fail(status, note)

            last_status, last_note = status, note
            if attempt < settings.scrape_max_retries:
                await backoff_sleep(attempt)

        return FetchOutcome.fail(last_status, last_note)

    async def _one_search(
        self, origin: str, destination: str, travel_date: date
    ) -> tuple[StatusCode, str, list[dict]]:
        """One widget-driven search. Returns (status, note, offers)."""
        context = await self._ensure_browser()
        page = await context.new_page()
        bodies: list[Any] = []

        async def on_response(response):  # noqa: ANN001
            # Only the fare endpoint. Every other JSON the page fetches is
            # deliberately ignored — that indiscriminate reading is exactly
            # what put CMS banner prices into the database before.
            if FARE_ENDPOINT not in response.request.url:
                return
            if response.request.method != "POST":
                return
            try:
                # `.json()` awaits the body itself. An explicit
                # `response.finished()` here leaves a task pending when the
                # page closes, which surfaces as a noisy "Target closed".
                bodies.append(await response.json())
            except Exception:
                return

        page.on("response", on_response)

        try:
            await page.goto(
                self.HOME, wait_until="domcontentloaded",
                timeout=settings.scrape_timeout_ms,
            )
            await page.wait_for_timeout(5000)
            await self._dismiss_cookie_banner(page)

            if not await self._pick_station(page, "From", origin):
                return StatusCode.MALFORMED, f"origin {origin} not selectable", []
            if not await self._pick_station(page, "To", destination):
                return StatusCode.MALFORMED, f"destination {destination} not selectable", []
            if not await self._pick_date(page, travel_date):
                return (
                    StatusCode.EMPTY_NO_INVENTORY,
                    f"{travel_date} not offered in the calendar for {origin}-{destination}",
                    [],
                )

            submit = await self._find_submit(page)
            if submit is None:
                return StatusCode.MALFORMED, "no 'Search Flights' control on the page", []
            if await submit.is_disabled():
                return (
                    StatusCode.MALFORMED,
                    "search control stayed disabled after filling the widget",
                    [],
                )
            await submit.click()

            # The page issues this POST more than once: an initial call comes
            # back with an empty `data` object before the priced one arrives.
            # So we keep waiting until a body actually yields offers, rather
            # than stopping at the first response and reporting the empty one
            # as malformed.
            offers: list[dict] = []
            parse_errors: list[str] = []
            seen = 0
            for _ in range(self.FARE_WAIT_S):
                await page.wait_for_timeout(1000)
                while seen < len(bodies):
                    body = bodies[seen]
                    seen += 1
                    try:
                        offers.extend(
                            parse_availability_search(
                                body, origin, destination, travel_date
                            )
                        )
                    except AkasaParseError as exc:
                        parse_errors.append(str(exc)[:120])
                if offers:
                    break

            if offers:
                return StatusCode.OK, "", offers

            if not bodies:
                html = await page.content()
                title = await page.title()
                if looks_challenged(html, title):
                    return (
                        StatusCode.BLOCKED_CAPTCHA,
                        "anti-bot challenge served; stopping for this source "
                        "(no bypass attempted)",
                        [],
                    )
                return (
                    StatusCode.TIMEOUT,
                    f"no fare response within {self.FARE_WAIT_S}s",
                    [],
                )

            # Responses arrived but held no sellable non-stop fare. That is a
            # real answer about inventory, not a parsing failure — unless every
            # single body was the wrong shape, which is a contract change.
            if parse_errors and len(parse_errors) == seen:
                return StatusCode.MALFORMED, "; ".join(parse_errors[:2]), []

            # Say WHICH kind of empty. A city-pair Akasa sells only as a
            # connection is excluded by the matched-model rule (§3.6) and will
            # never yield a row however often it is retried; a genuinely empty
            # day might. Reporting both as "no inventory" hid a permanent
            # structural exclusion behind wording that reads as transient, and
            # the coverage number has to be explainable to a reader.
            nonstop = connecting = 0
            for body in bodies:
                n, c = count_journeys(body, origin, destination)
                nonstop += n
                connecting += c
            if connecting and not nonstop:
                return (
                    StatusCode.EMPTY_NO_INVENTORY,
                    f"{origin}-{destination} is sold only as a connection on "
                    f"{travel_date} ({connecting} connecting journey(s), no "
                    "non-stop); excluded as a different product, not retried",
                    [],
                )
            return (
                StatusCode.EMPTY_NO_INVENTORY,
                f"no sellable non-stop {origin}-{destination} fare on {travel_date}",
                [],
            )
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # -- widget driving -----------------------------------------------------
    @staticmethod
    async def _dismiss_cookie_banner(page) -> None:
        for label in ("Accept cookies", "Accept"):
            btn = await page.query_selector(f"button:has-text('{label}')")
            if btn is not None and await btn.is_visible():
                try:
                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(800)
                    return
                except Exception:
                    continue

    @staticmethod
    async def _pick_station(page, field_id: str, code: str) -> bool:
        """
        Type an IATA code and accept the suggestion for that exact station.

        The suggestion list is `ul.p-4`. A generic `ul li` selector matches the
        site navigation instead — the page has dozens of lists — which clicks a
        menu item and clears the field. The match is anchored on the first line
        of the item, which is the code itself, so typing "DEL" cannot select an
        airport that merely mentions Delhi in its description.
        """
        field = await page.query_selector(f"#{field_id}")
        if field is None:
            return False
        await field.click()
        await field.fill("")
        await field.type(code, delay=110)
        try:
            await page.wait_for_selector(
                f"ul.p-4 li:has-text('{code}')", timeout=10000, state="visible"
            )
        except Exception:
            return False

        for option in await page.query_selector_all("ul.p-4 li"):
            if not await option.is_visible():
                continue
            text = (await option.inner_text() or "").strip()
            if text.split("\n")[0].strip().upper() != code.upper():
                continue
            await option.click()
            await page.wait_for_timeout(1200)
            return code.upper() in (await field.input_value() or "").upper()
        return False

    @staticmethod
    async def _pick_date(page, travel_date: date) -> bool:
        """
        Choose the departure date by clicking the calendar, never by typing.

        A typed date does not reach the widget's internal state, which is what
        gates the Search control — that is why an earlier attempt filled every
        field correctly and still found the button disabled.
        """
        field = await page.query_selector("input[name='DepartureDate']")
        if field is None:
            return False
        await field.click()
        await page.wait_for_timeout(2500)

        # aria-labels read "Choose Sunday, October 11th, 2026" (or
        # "Not available ..." for a day with no inventory).
        for _ in range(4):
            for cell in await page.query_selector_all("[aria-label]"):
                label = await cell.get_attribute("aria-label") or ""
                if not label.lower().startswith("choose "):
                    continue
                if _label_matches(label, travel_date):
                    await cell.click()
                    await page.wait_for_timeout(1500)
                    return True
            nxt = await page.query_selector(
                "button[aria-label*='Next'], [aria-label='Next Month']"
            )
            if nxt is None:
                break
            await nxt.click()
            await page.wait_for_timeout(1200)
        return False

    @staticmethod
    async def _find_submit(page):
        for btn in await page.query_selector_all("button"):
            text = (await btn.inner_text() or "").strip().lower()
            if "search flight" in text:
                return btn
        return None

    # -- mapping ------------------------------------------------------------
    def _to_quotes(
        self,
        offers: list[dict],
        origin: str,
        destination: str,
        travel_date: date,
        cycle_date: date,
        window: AdvanceWindow,
    ) -> list[RawQuote]:
        """
        Convert parsed offers into RawQuotes.

        Components are published by the source, so nothing here is invented:
        if the parts do not reconcile to the published total the offer is
        dropped rather than patched, because a quote whose components disagree
        with its total would corrupt the Headline-vs-Core comparison that is
        the whole point of the dual index.
        """
        quotes: list[RawQuote] = []
        for offer in offers:
            total = offer.get("total_fare") or 0.0
            base = offer.get("base_fare") or 0.0
            if total <= 0 or base <= 0:
                continue
            if abs(offer.get("computed_total", total) - total) > 1.0:
                logger.warning(
                    "discarding %s-%s offer: components %.2f != total %.2f",
                    origin, destination, offer.get("computed_total"), total,
                )
                continue
            quotes.append(
                RawQuote(
                    origin=origin,
                    destination=destination,
                    carrier=str(offer.get("carrier") or self.carrier_code)[:8],
                    travel_date=travel_date,
                    advance_window=window,
                    base_fare=round(float(base), 2),
                    taxes=round(float(offer.get("taxes") or 0.0), 2),
                    udf=round(float(offer.get("udf") or 0.0), 2),
                    convenience_fee=round(float(offer.get("convenience_fee") or 0.0), 2),
                    total_fare=round(float(total), 2),
                    source=self.name,
                    source_type=SourceType.LIVE,
                    cycle_date=cycle_date,
                    fare_class="economy",
                    trip_type=TripType.ONE_WAY,
                    is_nonstop=True,
                    currency=str(offer.get("currency") or "INR"),
                )
            )
        # The elementary quote is the cheapest non-stop economy one-way (§3.6).
        quotes.sort(key=lambda q: q.total_fare)
        return quotes[:10]


def _label_matches(label: str, target: date) -> bool:
    """'Choose Sunday, October 11th, 2026' -> does it mean `target`?"""
    import re  # noqa: PLC0415

    text = re.sub(r"^\s*Choose\s+", "", label).strip()
    text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text)
    for fmt in ("%A, %B %d, %Y", "%a, %b %d, %Y"):
        try:
            if datetime.strptime(text, fmt).date() == target:
                return True
        except ValueError:
            continue
    return False
