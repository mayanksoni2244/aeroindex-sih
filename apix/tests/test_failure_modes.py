"""
Failure modes — spec §2.3, and the ethics line in constitution §2.

A scraper's honesty lives entirely in how it reports failure. Three rules:

  * every distinct failure gets its own StatusCode, so "the airline blocked us"
    and "there were genuinely no seats" can never be read as the same event;
  * a block STOPS that source — no retry escalation, no second identity;
  * a failed fetch yields nothing, and nothing is substituted for it.

The adapters are exercised through fakes and against a recorded copy of the
real payload shape, never the network. A test that needs akasaair.com to answer
is not a test, and hitting a live carrier on every `pytest` run would itself be
impolite.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from apix.db.models import AdvanceWindow, SourceType
from apix.scrape.base import (
    ComplianceDecision,
    FetchOutcome,
    LiveSource,
    StatusCode,
    looks_blocked,
    looks_like_consent_wall,
    parse_inr,
)
from apix.scrape.live import REGISTRY, available_sources, get_live_source
from apix.scrape.live.akasa import (
    AkasaAirSource,
    AkasaParseError,
    count_journeys,
    looks_challenged,
    parse_availability_search,
    split_components,
)
from apix.scrape.runner import collect_live_source, tier_a_plan
from tests.conftest import ANCHOR, make_quote

SPEC_MANDATED = (
    "OK",
    "TIMEOUT",
    "EMPTY_NO_INVENTORY",
    "BLOCKED_CAPTCHA",
    "MALFORMED",
    "RATE_LIMITED",
    "NETWORK_ERROR",
    "VALIDATION_REJECT",
)


# ─────────────────────────────────────────────────────────────────────────────
# The taxonomy itself
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", SPEC_MANDATED)
def test_every_spec_mandated_status_exists(name):
    """
    These strings are persisted in `scrape_runs.status_counts` and read back by
    the coverage panel, so the name and the stored value must not drift apart.
    """
    assert hasattr(StatusCode, name), f"StatusCode.{name} is required by spec §2.3"
    assert StatusCode[name].value == name


def test_the_two_added_statuses_are_distinct_from_the_mandated_ones():
    """
    COMPLIANCE_REFUSED is us declining; BLOCKED_CAPTCHA is them refusing.

    Folding the first into the second would misreport our own ethics gate as a
    hostile block — a reader would conclude the site fought us off when in fact
    we never sent the request.
    """
    added = {StatusCode.COMPLIANCE_REFUSED, StatusCode.ADAPTER_UNAVAILABLE}
    mandated = {StatusCode[n] for n in SPEC_MANDATED}
    assert added.isdisjoint(mandated)
    assert set(StatusCode) == mandated | added, (
        "a new status needs a documented reason in base.py and a line here"
    )


def test_only_ok_counts_as_success():
    assert [s for s in StatusCode if s.is_success] == [StatusCode.OK], (
        "if any failure reports is_success, every coverage percentage is fiction"
    )


# ─────────────────────────────────────────────────────────────────────────────
# FetchOutcome contract
# ─────────────────────────────────────────────────────────────────────────────
def test_an_empty_ok_is_downgraded_to_empty_no_inventory():
    """
    "Success with zero quotes" is a contradiction the constructor refuses.

    Left representable, it would flatter every coverage number: a source that
    returned nothing all day would report 100% OK.
    """
    outcome = FetchOutcome.ok([])
    assert outcome.status is StatusCode.EMPTY_NO_INVENTORY
    assert outcome.quotes == []


@pytest.mark.parametrize("status", [
    StatusCode.TIMEOUT,
    StatusCode.BLOCKED_CAPTCHA,
    StatusCode.MALFORMED,
    StatusCode.COMPLIANCE_REFUSED,
    StatusCode.ADAPTER_UNAVAILABLE,
])
def test_a_failure_carries_no_quotes(status):
    outcome = FetchOutcome.fail(status, "note")
    assert outcome.quotes == [], f"{status.value} must not carry data"
    assert outcome.status is status


def test_a_populated_ok_keeps_its_quotes():
    outcome = FetchOutcome.ok([make_quote()])
    assert outcome.status is StatusCode.OK
    assert len(outcome.quotes) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Block detection exists to stop, not to circumvent
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("html", [
    "<html><body>Please verify you are a human</body></html>",
    "<h1>Attention Required! | Cloudflare</h1>",
    "<p>We detected unusual traffic from your network</p>",
    "<div id='px-captcha'></div>",
    "<title>Access Denied</title>",
])
def test_block_pages_are_recognised(html):
    assert looks_blocked(html) is True


def test_a_normal_results_page_is_not_read_as_a_block():
    assert looks_blocked("<div class='flight-card'>QP 1102 4,599</div>") is False


def test_a_consent_wall_is_malformed_not_empty():
    """
    An interstitial returned with HTTP 200 is a page we failed to get past, not
    a route with no seats. Calling it EMPTY_NO_INVENTORY would quietly convert
    our own failure into a claim about the market.
    """
    wall = "<div>We use cookies. Accept all cookies to continue to site.</div>"
    assert looks_like_consent_wall(wall) is True
    assert looks_like_consent_wall("<div class='flight'>consent</div>") is False, (
        "a results page that happens to contain the word must not trip this"
    )


@pytest.mark.parametrize("marker", [
    "<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1'></script>",
    "<div class='cf-browser-verification'></div>",
    "<div id='px-captcha'></div>",
    "<div class='g-recaptcha' data-sitekey='x'></div>",
    "<script src='https://captcha-delivery.com/c.js'></script>",
    "<iframe src='/_Incapsula_Resource?SWCGHOEL=v'></iframe>",
])
def test_a_real_challenge_page_is_recognised(marker):
    assert looks_challenged(marker) is True


@pytest.mark.parametrize("title", [
    "Just a moment...",
    "Attention Required! | Cloudflare",
    "Access Denied",
])
def test_a_challenge_title_alone_is_enough(title):
    assert looks_challenged("<html></html>", title) is True


@pytest.mark.parametrize("html", [
    "<p>Please verify your booking reference before check-in.</p>",
    "<li>Robotic wheelchair assistance available on request</li>",
    "<h2>Security check-in opens 2 hours before departure</h2>",
    "<div class='flight-card'>QP 1102  DEL-BOM  ₹4,599</div>",
    "<p>Are you a Akasa Rewards member? Verify your account.</p>",
])
def test_ordinary_airline_copy_is_not_read_as_a_challenge(html):
    """
    Phase 2's hardening, stated as a test.

    The previous detector matched bare words like "verify", "robot" and
    "security check" — all of which appear in normal airline prose. A working
    Akasa run would then be filed as BLOCKED_CAPTCHA, which is the worst kind
    of wrong: it under-reports our real coverage *and* falsely accuses the
    carrier of blocking us.
    """
    assert looks_challenged(html) is False


# ─────────────────────────────────────────────────────────────────────────────
# Fare parsing — never guess a number
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("₹ 4,599", 4599.0),
    ("Rs. 4 599.00", 4599.0),
    ("INR 4,599", 4599.0),
    ("२०००", 2000.0),
    ("₹1,23,456", 123456.0),
    (4599, 4599.0),
    (4599.5, 4599.5),
])
def test_displayed_fares_parse_to_numbers(raw, expected):
    assert parse_inr(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["Sold out", "", "—", None, "Call us"])
def test_unparseable_text_returns_none_rather_than_zero(raw):
    """
    None maps to MALFORMED upstream. Returning 0.0 would inject a free flight
    into the index — the single most damaging possible parse bug.
    """
    assert parse_inr(raw) is None


# ─────────────────────────────────────────────────────────────────────────────
# The Akasa fare contract
#
# Shaped after the real `POST /api/ibe/availability/search` body captured on
# 2026-09-12 for DEL-BOM. The numbers are that capture's numbers, and they
# reconcile exactly: 5945 + 304 + (152+89) + (75+50+350+236) = 7201.
# ─────────────────────────────────────────────────────────────────────────────
TRAVEL = ANCHOR + timedelta(days=30)

_REAL_CHARGES = [
    {"type": "FarePrice", "code": "FARE", "amount": 5945.0},
    {"type": "Tax", "code": "GST", "amount": 304.0},
    {"type": "TravelFee", "code": "UDF", "amount": 152.0},
    {"type": "TravelFee", "code": "DUDF", "amount": 89.0},
    {"type": "TravelFee", "code": "CUTE", "amount": 75.0},
    {"type": "TravelFee", "code": "RCS", "amount": 50.0},
    {"type": "TravelFee", "code": "WFE", "amount": 350.0},
    {"type": "TravelFee", "code": "ASF", "amount": 236.0},
]


def _iso(d: date, hour: int = 6) -> str:
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).isoformat()


def availability_payload(
    *,
    travel_date: date = TRAVEL,
    market: str = "DEL|BOM",
    charges: list[dict] | None = None,
    fare_total: float | None = 7201.0,
    segments: int = 1,
    available_count: int = 5,
    status: str = "Active",
    flight: str = "1102",
) -> dict:
    """One well-formed availability/search body, with the knobs each test needs."""
    key = "FARE-KEY-1"
    return {
        "data": {
            "currencyCode": "INR",
            "faresAvailable": [
                {
                    "key": key,
                    "value": {
                        "fareAvailabilityKey": key,
                        "totals": (
                            {} if fare_total is None else {"fareTotal": fare_total}
                        ),
                        "fares": [
                            {
                                "productClass": "EC",
                                "passengerFares": [
                                    {
                                        "passengerType": "ADT",
                                        "serviceCharges": (
                                            _REAL_CHARGES if charges is None else charges
                                        ),
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
            "results": [
                {
                    "trips": [
                        {
                            "date": _iso(travel_date, 0),
                            "journeysAvailableByMarket": [
                                {
                                    "key": market,
                                    "value": [
                                        {
                                            "designator": {
                                                "departure": _iso(travel_date),
                                            },
                                            "segments": [
                                                {
                                                    "identifier": {
                                                        "carrierCode": "QP",
                                                        "identifier": flight,
                                                    }
                                                }
                                                for _ in range(segments)
                                            ],
                                            "fares": [
                                                {
                                                    "fareAvailabilityKey": key,
                                                    "details": [
                                                        {
                                                            "status": status,
                                                            "availableCount": available_count,
                                                        }
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            ],
        }
    }


def test_the_real_payload_shape_yields_a_fully_itemised_offer():
    (offer,) = parse_availability_search(availability_payload(), "DEL", "BOM", TRAVEL)

    assert offer["base_fare"] == pytest.approx(5945.0), "FarePrice is the Core APIx input"
    assert offer["taxes"] == pytest.approx(304.0)
    assert offer["udf"] == pytest.approx(241.0), "UDF 152 + DUDF 89"
    assert offer["convenience_fee"] == pytest.approx(711.0), "CUTE+RCS+WFE+ASF"
    assert offer["total_fare"] == pytest.approx(7201.0), "fareTotal is the Headline input"
    assert offer["carrier"] == "QP"
    assert offer["flight_number"] == "1102"


def test_the_components_reconcile_to_the_published_total():
    """
    The dual index is a *difference* between two series computed off the same
    observation. If base + taxes + fees did not sum to the total, the
    Headline−Core gap would measure a parsing error rather than surge.
    """
    (offer,) = parse_availability_search(availability_payload(), "DEL", "BOM", TRAVEL)
    assert offer["computed_total"] == pytest.approx(offer["total_fare"])


def test_an_unclassified_charge_is_counted_not_dropped():
    """
    A fee code we have never seen must still land in a bucket.

    Dropping it would leave the components short of the total, and the
    reconciliation guard in `_to_quotes` would then throw away a perfectly
    real fare as if it were corrupt.
    """
    charges = _REAL_CHARGES + [{"type": "TravelFee", "code": "NEWFEE", "amount": 99.0}]
    parts = split_components(charges)
    assert sum(parts.values()) == pytest.approx(7201.0 + 99.0)
    assert parts["convenience_fee"] == pytest.approx(711.0 + 99.0)


@pytest.mark.parametrize("payload,why", [
    ({}, "no faresAvailable/results at all"),
    ({"data": {}}, "an empty data object — the site's first, unpriced response"),
    ({"data": {"faresAvailable": []}}, "half the contract"),
    ([], "not an object"),
])
def test_a_body_that_is_not_this_endpoint_raises_rather_than_returning_empty(payload, why):
    """
    MALFORMED and EMPTY_NO_INVENTORY must stay distinguishable.

    Returning [] for a wrong-shaped document would report "no seats on this
    route" when the truth is "we no longer understand the response" — a claim
    about the market made out of a bug in our code.
    """
    with pytest.raises(AkasaParseError):
        parse_availability_search(payload, "DEL", "BOM", TRAVEL)


def test_a_connecting_itinerary_is_excluded_not_compared():
    """
    §3.6: the elementary quote is a non-stop. A one-stop fare is a different
    product; pricing it against non-stops would make the index move when the
    schedule changes rather than when prices do.
    """
    payload = availability_payload(segments=2)
    assert parse_availability_search(payload, "DEL", "BOM", TRAVEL) == []


def test_a_connection_only_market_is_reported_as_such_not_as_no_inventory():
    """
    Two different facts wear the same status, and the coverage number has to be
    explainable.

    This is not hypothetical: on 2026-09-13 Akasa returned journeys for both
    MAA-DEL and BLR-HYD, every one of them two-segment. Those two routes are 10
    of the basket's 30 cells, and "no inventory" reads as a bad day that might
    clear — whereas connection-only is a structural exclusion that will never
    yield a row. `count_journeys` is what lets the note say which it was.
    """
    connection_only = availability_payload(segments=2)
    assert count_journeys(connection_only, "DEL", "BOM") == (0, 1)
    assert parse_availability_search(connection_only, "DEL", "BOM", TRAVEL) == []

    nonstop = availability_payload()
    assert count_journeys(nonstop, "DEL", "BOM") == (1, 0)

    # Another market's connection must not be attributed to this one.
    assert count_journeys(connection_only, "BOM", "BLR") == (0, 0)


@pytest.mark.parametrize("junk", [None, [], "x", {}, {"data": None}])
def test_counting_journeys_never_raises_on_a_junk_body(junk):
    """It runs on the failure path, where the body is least trustworthy."""
    assert count_journeys(junk, "DEL", "BOM") == (0, 0)


def test_a_sold_out_fare_bucket_is_not_an_offer():
    assert parse_availability_search(
        availability_payload(available_count=0), "DEL", "BOM", TRAVEL
    ) == []
    assert parse_availability_search(
        availability_payload(status="Unavailable"), "DEL", "BOM", TRAVEL
    ) == []


def test_another_market_in_the_same_response_is_not_harvested():
    """
    The widget loads neighbouring markets and dates into one response. Reading
    them would file a BOM-BLR fare under DEL-BOM.
    """
    assert parse_availability_search(
        availability_payload(market="BOM|BLR"), "DEL", "BOM", TRAVEL
    ) == []


def test_a_neighbouring_day_in_the_same_response_is_not_harvested():
    payload = availability_payload(travel_date=TRAVEL + timedelta(days=1))
    assert parse_availability_search(payload, "DEL", "BOM", TRAVEL) == []


def test_a_missing_published_total_falls_back_to_the_sum_of_the_parts():
    """Not an invention — the parts are all published; only the roll-up is absent."""
    (offer,) = parse_availability_search(
        availability_payload(fare_total=None), "DEL", "BOM", TRAVEL
    )
    assert offer["total_fare"] == pytest.approx(7201.0)


# ─────────────────────────────────────────────────────────────────────────────
# Offer -> RawQuote
# ─────────────────────────────────────────────────────────────────────────────
def _offers(**kw):
    return parse_availability_search(availability_payload(**kw), "DEL", "BOM", TRAVEL)


def test_a_parsed_offer_becomes_a_live_quote_with_its_components_intact():
    src = AkasaAirSource()
    (q,) = src._to_quotes(_offers(), "DEL", "BOM", TRAVEL, ANCHOR, AdvanceWindow.T30)

    assert q.source_type is SourceType.LIVE
    assert q.source == "AkasaAir"
    assert q.advance_window is AdvanceWindow.T30
    assert q.is_nonstop is True
    assert (q.base_fare, q.taxes, q.udf, q.convenience_fee) == (5945.0, 304.0, 241.0, 711.0)
    assert q.total_fare == 7201.0
    assert q.completeness() == 4, "every component came from the source"


def test_an_offer_whose_parts_disagree_with_its_total_is_dropped_not_patched():
    """
    §3.4 in its strongest form: we do not repair a fare.

    Silently rewriting the total to match the parts (or vice versa) would put a
    number into the index that the airline never published. Dropping the row
    costs one observation and keeps every remaining one true.
    """
    src = AkasaAirSource()
    offers = _offers()
    offers[0]["total_fare"] = 9999.0  # parts still sum to 7201
    assert src._to_quotes(offers, "DEL", "BOM", TRAVEL, ANCHOR, AdvanceWindow.T30) == []


def test_an_offer_with_no_base_fare_is_not_a_quote():
    """A zero base is not a free flight; it is a payload we misread."""
    src = AkasaAirSource()
    offers = _offers()
    offers[0].update(base_fare=0.0, computed_total=7201.0)
    assert src._to_quotes(offers, "DEL", "BOM", TRAVEL, ANCHOR, AdvanceWindow.T30) == []


def test_a_travel_date_outside_a_configured_window_is_rejected_before_fetching():
    """
    A T+3 fare filed under the T+1 bucket would corrupt the within-route
    comparison the Jevons index is built on, and the corruption would be
    invisible — the number would just be wrong. So the window is resolved up
    front and an unconfigured one never reaches the browser.
    """
    src = AkasaAirSource()
    odd = ANCHOR + timedelta(days=3)  # not in routes.yaml advance_windows

    # No compliance call, no browser: the rejection is arithmetic on dates.
    outcome = asyncio.run(src.search("DEL", "BOM", odd))
    assert outcome.status in {
        StatusCode.VALIDATION_REJECT,
        StatusCode.COMPLIANCE_REFUSED,  # offline: robots.txt unreadable -> fails closed
    }
    assert outcome.quotes == []


# ─────────────────────────────────────────────────────────────────────────────
# Runner behaviour under failure — fakes, no network
# ─────────────────────────────────────────────────────────────────────────────
class _FakeSource(LiveSource):
    """A LiveSource that returns a scripted sequence of outcomes."""

    name = "FakeAir"
    base_url = "https://example.invalid"

    def __init__(self, outcomes):
        # Deliberately not calling super().__init__(): that would build a
        # RobotsGate, and this fake must never touch the network.
        self._outcomes = list(outcomes)
        self.calls = 0
        self.closed = False

    def search_url(self, origin, destination, travel_date) -> str:
        return f"{self.base_url}/search?o={origin}&d={destination}&t={travel_date}"

    def check_compliance(self, origin, destination, travel_date) -> ComplianceDecision:
        return ComplianceDecision(True, "fake: allowed", f"{self.base_url}/robots.txt")

    async def search(self, origin, destination, travel_date, cabin="economy"):
        self.calls += 1
        if self._outcomes:
            return self._outcomes.pop(0)
        return FetchOutcome.fail(StatusCode.EMPTY_NO_INVENTORY, "fake exhausted")

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_live(monkeypatch):
    """Install a fake Tier-A adapter over the single-source collection path."""
    import apix.scrape.runner as runner

    def install(outcomes, routes="DEL-BOM"):
        src = _FakeSource(outcomes)
        monkeypatch.setattr(runner, "get_live_source", lambda _name: src)
        monkeypatch.setattr(runner.settings, "live_routes", routes)
        return src

    return install


def _collect(source_key: str = "fake", cycle: date = ANCHOR):
    return asyncio.run(collect_live_source(cycle, source_key))


def test_a_block_stops_the_source_and_does_not_escalate(fake_live):
    """
    Constitution §2: on a block we log it and stop for that source.

    The fake would happily serve more results after the block. The runner must
    not ask — no retry storm, no second attempt with different headers. This
    also pins the arity of the early return: it once returned a 4-tuple where
    the caller unpacks 5, so the one path whose entire job is to fail
    gracefully raised ValueError instead.
    """
    live_quote = make_quote(cycle=ANCHOR, source_type=SourceType.LIVE, source="FakeAir")
    src = fake_live([
        FetchOutcome.ok([live_quote]),
        FetchOutcome.fail(StatusCode.BLOCKED_CAPTCHA, "challenge page"),
        FetchOutcome.ok([make_quote(cycle=ANCHOR, carrier="UK",
                                    source_type=SourceType.LIVE, source="FakeAir")]),
    ])

    quotes, counts, name, source_type, notes = _collect()

    assert src.calls == 2, "the runner kept fetching after a block"
    assert counts[StatusCode.BLOCKED_CAPTCHA.value] == 1
    assert name == "FakeAir"
    assert source_type is SourceType.LIVE
    assert len(quotes) == 1, "only the pre-block quote survives"
    assert any("no bypass attempted" in n for n in notes), (
        "the stop must be recorded in the run notes, not just in a log line"
    )
    assert src.closed is True, "the adapter must be closed even on the early return"


def test_a_compliance_refusal_yields_no_data_and_is_recorded(fake_live):
    """A refusal is a first-class outcome: zero quotes, one honest status."""
    fake_live([
        FetchOutcome.fail(StatusCode.COMPLIANCE_REFUSED, "robots.txt DISALLOWS /search"),
    ])
    quotes, counts, _name, _stype, notes = _collect()

    assert quotes == []
    assert counts[StatusCode.COMPLIANCE_REFUSED.value] >= 1
    assert any("COMPLIANCE_REFUSED" in n for n in notes)


def test_mixed_failures_are_counted_separately(fake_live):
    """The histogram is per-status, so no failure hides inside another."""
    fake_live([
        FetchOutcome.fail(StatusCode.TIMEOUT, "navigation timeout"),
        FetchOutcome.fail(StatusCode.EMPTY_NO_INVENTORY, "no seats"),
        FetchOutcome.fail(StatusCode.MALFORMED, "consent wall"),
        FetchOutcome.fail(StatusCode.RATE_LIMITED, "429"),
        FetchOutcome.fail(StatusCode.NETWORK_ERROR, "dns"),
    ])
    quotes, counts, _name, _stype, notes = _collect()

    assert quotes == []
    assert counts.get(StatusCode.TIMEOUT.value) == 1
    assert counts.get(StatusCode.EMPTY_NO_INVENTORY.value) == 1
    assert counts.get(StatusCode.MALFORMED.value) == 1
    assert len(notes) >= 4, "every non-OK window should leave a note"
    assert StatusCode.OK.value not in counts


def test_an_unknown_adapter_is_adapter_unavailable_not_a_crash():
    """A typo in APIX_LIVE_SOURCES must degrade honestly, not take down the cycle."""
    quotes, counts, name, _stype, notes = _collect("no-such-airline")

    assert quotes == []
    assert counts.get(StatusCode.ADAPTER_UNAVAILABLE.value) == 1
    assert name == "no-such-airline"
    assert any("unknown live source" in n for n in notes)


def test_an_unconfigured_route_is_skipped_with_a_note(fake_live):
    """A route absent from routes.yaml is skipped, and the note names it."""
    src = fake_live([], routes="XXX-YYY")

    quotes, counts, _name, _stype, notes = _collect()

    assert src.calls == 0
    assert quotes == []
    assert counts == {}
    assert any("XXX-YYY" in n and "routes.yaml" in n for n in notes)


# ─────────────────────────────────────────────────────────────────────────────
# Tier-A go / no-go
# ─────────────────────────────────────────────────────────────────────────────
def test_tier_a_is_not_attempted_for_a_past_cycle(monkeypatch):
    """
    You cannot observe yesterday's fares today.

    Attempting it would file today's prices under yesterday's cycle — a
    fabricated observation wearing a real timestamp. The skip is stated so a
    100%-simulated historical cycle explains itself.
    """
    import apix.scrape.runner as runner

    monkeypatch.setattr(runner.settings, "live_enabled", True)
    monkeypatch.setattr(runner, "today_ist", lambda: ANCHOR)

    attempt, reason = tier_a_plan(ANCHOR - timedelta(days=5))

    assert attempt is False
    assert "not today" in reason and "100% simulated" in reason


def test_disabling_tier_a_is_reported_not_silent(monkeypatch):
    import apix.scrape.runner as runner

    monkeypatch.setattr(runner.settings, "live_enabled", False)
    monkeypatch.setattr(runner, "today_ist", lambda: ANCHOR)

    attempt, reason = tier_a_plan(ANCHOR)

    assert attempt is False
    assert "100% simulated" in reason, (
        "a fully simulated cycle must say so at collection time"
    )


def test_an_explicit_live_flag_overrides_the_environment_default(monkeypatch):
    """`apix scrape --live` is an instruction, not a suggestion."""
    import apix.scrape.runner as runner

    monkeypatch.setattr(runner.settings, "live_enabled", False)
    monkeypatch.setattr(runner, "today_ist", lambda: ANCHOR)

    assert tier_a_plan(ANCHOR, live=True) == (True, "")
    assert tier_a_plan(ANCHOR, live=False)[0] is False


# ─────────────────────────────────────────────────────────────────────────────
# Adapter registry
# ─────────────────────────────────────────────────────────────────────────────
def test_the_registry_is_pluggable_and_rejects_unknown_names():
    assert "akasa" in available_sources()
    assert isinstance(get_live_source("akasa"), AkasaAirSource)
    assert isinstance(get_live_source("  AKASA  "), AkasaAirSource), (
        "the key should be case- and whitespace-insensitive"
    )
    with pytest.raises(KeyError) as exc:
        get_live_source("emirates")
    assert "available:" in str(exc.value), "the error must list what is available"


def test_akasa_is_the_only_production_source():
    """
    The one decision this project is least free to get wrong.

    Every other carrier and OTA named in the problem statement is either
    robots-blocked, ToS-prohibited, or too legally ambiguous for a
    government-facing prototype. Adding a key to REGISTRY is the single act
    that would let one of them contribute data, so it is asserted here rather
    than left to review.
    """
    assert available_sources() == ["akasa"], (
        f"unexpected production source(s): {sorted(REGISTRY)}"
    )


@pytest.mark.parametrize("key", available_sources())
def test_every_registered_adapter_implements_the_full_interface(key):
    """
    A half-implemented adapter would fail mid-cycle. Instantiation alone proves
    the ABC is satisfied; `search_url` proves the compliance target is
    derivable without a network call.
    """
    src = get_live_source(key)
    assert src.name and src.name != "unnamed", f"{key} must name itself"
    assert src.base_url.startswith("https://"), f"{key} must use TLS"

    url = src.search_url("DEL", "BOM", ANCHOR + timedelta(days=30))
    assert url.startswith(src.base_url), (
        f"{key}: the compliance check and the fetch must target the same origin"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent collection — the grid is walked in parallel, politely
# ─────────────────────────────────────────────────────────────────────────────
class _SlowSource(_FakeSource):
    """
    A fake that actually yields, so concurrency is observable.

    `_FakeSource.search` returns without awaiting, which means it cannot tell a
    parallel runner from a serial one: every call completes before the next
    begins whatever the scheduler does. This one sleeps, records how many calls
    are in flight at once, and passes each through a real TokenBucket, so the
    tests below can pin both halves of the bargain — overlap the waiting, do
    not raise the request rate.
    """

    name = "SlowAir"

    def __init__(self, outcomes, delay=0.05, bucket=None):
        super().__init__(outcomes)
        self._delay = delay
        self._bucket = bucket
        self.in_flight = 0
        self.peak_in_flight = 0
        self.started: list[float] = []

    async def search(self, origin, destination, travel_date, cabin="economy"):
        if self._bucket is not None:
            await self._bucket.acquire()
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        self.started.append(time.monotonic())
        try:
            await asyncio.sleep(self._delay)
            self.calls += 1
            if self._outcomes:
                return self._outcomes.pop(0)
            return FetchOutcome.fail(StatusCode.EMPTY_NO_INVENTORY, "fake exhausted")
        finally:
            self.in_flight -= 1


def _install(monkeypatch, src, routes="DEL-BOM,DEL-BLR", concurrency=4):
    import apix.scrape.runner as runner

    monkeypatch.setattr(runner, "get_live_source", lambda _name: src)
    monkeypatch.setattr(runner.settings, "live_routes", routes)
    monkeypatch.setattr(runner.settings, "live_concurrency", concurrency)
    return src


def test_the_grid_is_collected_concurrently(monkeypatch):
    """
    The whole point of the change: cells overlap instead of queueing.

    Two routes x five windows is ten cells. Serially that is ten sleeps; in
    parallel the peak in-flight count has to exceed one, and the wall clock has
    to come in well under the serial total. Both are asserted, because either
    one alone can pass for the wrong reason.
    """
    src = _SlowSource([], delay=0.05)
    _install(monkeypatch, src, concurrency=4)

    started = time.monotonic()
    _quotes, counts, _name, _stype, _notes = _collect()
    elapsed = time.monotonic() - started

    assert sum(counts.values()) == 10, "every cell in the grid must be attempted"
    assert src.peak_in_flight > 1, "cells were collected one at a time"
    assert src.peak_in_flight <= 4, "concurrency exceeded APIX_LIVE_CONCURRENCY"
    assert elapsed < 10 * 0.05, (
        f"{elapsed:.2f}s is no better than serial; the fan-out did not happen"
    )


def test_concurrency_never_raises_the_request_rate(monkeypatch):
    """
    Parallelism must buy overlap, not extra traffic.

    Every search still passes the shared TokenBucket, so however many cells are
    in flight, the spacing between requests stays at the configured rate. This
    is the politeness guarantee that makes the fan-out defensible at all: if it
    ever fails, the change has turned into "scrape harder".
    """
    from apix.scrape.base import TokenBucket

    rate = 240  # 0.25s apart — fast enough for a test, still a real gate
    src = _SlowSource([], delay=0.0, bucket=TokenBucket(rate_per_min=rate))
    _install(monkeypatch, src, routes="DEL-BOM", concurrency=5)

    _collect()

    assert len(src.started) == 5
    gaps = [b - a for a, b in zip(src.started, src.started[1:])]
    # The bucket sleeps its interval and then adds 0.2-0.9s of jitter, so the
    # floor asserted here is the interval alone — the jitter only widens it.
    assert all(g >= 60.0 / rate for g in gaps), (
        f"requests were emitted faster than the token bucket allows: {gaps}"
    )


def test_a_block_stops_every_worker_not_just_one(monkeypatch):
    """
    The constitution's stop rule has to survive the fan-out.

    Serially, "return on BLOCKED_CAPTCHA" stopped everything because there was
    only one loop. With four workers in flight, a block seen by one of them
    must stop the other three as well — otherwise the run keeps hitting a host
    that has just said no, which is the exact behaviour the rule forbids.
    """
    src = _SlowSource(
        [FetchOutcome.fail(StatusCode.BLOCKED_CAPTCHA, "challenge page")],
        delay=0.02,
    )
    _install(monkeypatch, src, routes="DEL-BOM,DEL-BLR,BOM-BLR", concurrency=2)

    _quotes, counts, _name, _stype, notes = _collect()

    assert counts[StatusCode.BLOCKED_CAPTCHA.value] == 1
    assert src.calls < 15, (
        f"{src.calls} of 15 cells were fetched after a block; the stop did not "
        "propagate across workers"
    )
    assert any("no bypass attempted" in n for n in notes)
    assert any("not attempted" in n for n in notes), (
        "the run notes must say how many cells were skipped by the stop"
    )
    assert src.closed is True


def test_one_raising_cell_cannot_lose_the_whole_cycle(monkeypatch):
    """
    `search` is contracted never to raise, but the runner must not depend on it.

    Nothing is written to the database until the cycle completes, so an
    exception escaping one cell would discard every observation collected
    before it — on a real run, that is an hour of live fares lost to one bad
    page. The failure must stay inside its own cell.
    """

    class _Exploding(_SlowSource):
        async def search(self, origin, destination, travel_date, cabin="economy"):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("playwright target closed unexpectedly")
            return FetchOutcome.ok(
                [make_quote(cycle=ANCHOR, source_type=SourceType.LIVE,
                            source="SlowAir")]
            )

    src = _Exploding([], delay=0.0)
    _install(monkeypatch, src, routes="DEL-BOM", concurrency=2)

    quotes, counts, _name, _stype, notes = _collect()

    assert counts.get(StatusCode.NETWORK_ERROR.value) == 1, (
        "the raising cell must be recorded as a failed cell, not vanish"
    )
    assert len(quotes) == 4, "the other four cells must still be collected"
    assert any("unhandled RuntimeError" in n for n in notes)
    assert src.closed is True
