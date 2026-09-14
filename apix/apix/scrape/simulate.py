"""
Tier-B synthetic fare generator.

Every record it emits is tagged ``source_type = 'simulated'`` at creation and
stays tagged through cleaning, the index and the API. Nothing here is ever
presented as observed market data.

Determinism: the generator never uses a shared RNG stream. Each quote seeds its
own `random.Random` from ``sha256(seed | route | carrier | window | travel_date
| cycle_date | source)``, so output depends only on the inputs — not on
iteration order, thread scheduling or how many quotes were produced before it.
Re-running a cycle reproduces byte-identical fares, which is what makes the
backtest reproducible.

Realism modelled (calibrated to publicly-documented Indian domestic dynamics):
  * advance-purchase curve — convex, cheapest around T+30, steep toward T+1
  * festival / holiday surges from festivals.yaml
  * day-of-week effects
  * per-carrier price positioning
  * occasional non-festival demand spikes (so the IQR stage has real outliers)
  * occasional sold-out cells (so imputation has real gaps)
  * itemised base / taxes / UDF / convenience-fee decomposition
"""
from __future__ import annotations

import hashlib
import logging
import random
from datetime import date, timedelta

from apix.config.loader import RouteMeta, load_festivals, load_routes
from apix.config.settings import settings
from apix.db.models import AdvanceWindow, SourceType, TripType
from apix.scrape.base import RawQuote

logger = logging.getLogger("apix.scrape.simulate")

# Advance-purchase multipliers relative to the T+30 anchor (= 1.00).
# The T+1 : T+30 ratio of ~2.9x reproduces the 200–400% spread the problem
# statement describes. T+45 sits slightly above T+30 because carriers open
# inventory at higher buckets before the mid-window trough — hence "cheapest
# around T+21–T+30", not "cheapest at the earliest possible date".
_ADVANCE_CURVE: dict[int, float] = {1: 2.85, 7: 1.62, 15: 1.20, 30: 1.00, 45: 1.07}

# Monday=0 … Sunday=6. Friday and Sunday carry the classic leisure/return peak;
# midweek is the trough.
_DOW_FACTOR: dict[int, float] = {0: 1.06, 1: 0.97, 2: 0.96, 3: 1.00,
                                 4: 1.10, 5: 0.98, 6: 1.08}

# Online travel aggregators modelled as resellers. They quote the same carrier
# inventory but add a portal convenience fee — which is exactly why the
# decomposition columns exist.
_OTA_SOURCES = ("SimulatedOTA1", "SimulatedOTA2")

_SOLD_OUT_RATE = 0.05   # fraction of cells with no inventory -> imputation path
_SPIKE_RATE = 0.04      # non-festival demand spikes -> IQR outlier path


def _rng(*parts: object) -> random.Random:
    """Deterministic RNG for one quote, independent of call order."""
    payload = "|".join(str(p) for p in (settings.sim_seed, *parts))
    digest = hashlib.sha256(payload.encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _advance_multiplier(days: int) -> float:
    """Interpolate the advance curve so non-anchor windows still behave sanely."""
    if days in _ADVANCE_CURVE:
        return _ADVANCE_CURVE[days]
    anchors = sorted(_ADVANCE_CURVE)
    if days <= anchors[0]:
        return _ADVANCE_CURVE[anchors[0]]
    if days >= anchors[-1]:
        return _ADVANCE_CURVE[anchors[-1]]
    lo = max(a for a in anchors if a <= days)
    hi = min(a for a in anchors if a >= days)
    span = hi - lo
    frac = 0.0 if span == 0 else (days - lo) / span
    return _ADVANCE_CURVE[lo] + frac * (_ADVANCE_CURVE[hi] - _ADVANCE_CURVE[lo])


def festival_multiplier(travel_date: date) -> tuple[float, str | None]:
    """Surge multiplier for a travel date, plus the festival name if any."""
    fest = load_festivals().surge_for(travel_date)
    return (fest.multiplier, fest.name) if fest else (1.0, None)


def is_sold_out(route: str, carrier: str, window_days: int,
                travel_date: date, cycle_date: date) -> bool:
    """Deterministic 'no inventory' decision for a cell (edge case §11.12)."""
    r = _rng("soldout", route, carrier, window_days, travel_date, cycle_date)
    return r.random() < _SOLD_OUT_RATE


def simulate_quote(
    route_meta: RouteMeta,
    carrier_code: str,
    carrier_multiplier: float,
    window_days: int,
    cycle_date: date,
    source: str,
    is_ota: bool,
) -> RawQuote:
    """Generate one deterministic simulated fare."""
    routes_cfg = load_routes()
    comp = routes_cfg.fare_components
    travel_date = cycle_date + timedelta(days=window_days)

    r = _rng("fare", route_meta.route, carrier_code, window_days,
             travel_date, cycle_date, source)

    anchor = route_meta.sim_base_fare * carrier_multiplier
    adv = _advance_multiplier(window_days)
    dow = _DOW_FACTOR[travel_date.weekday()]
    fest, _fest_name = festival_multiplier(travel_date)

    # Bounded idiosyncratic noise: fare buckets move in steps, not smoothly.
    noise = 1.0 + r.uniform(-0.08, 0.08)

    # Rare demand spike, applied ONLY outside festival windows. A festival is
    # already a modelled demand event with its own multiplier; stacking a second
    # random surge on top compounds to fares no real market produces (a 1.85x
    # Diwali factor times a 1.9x spike is 3.5x, which pushed DEL-CCU T+1 past
    # ₹66,000 and tripped the parsing-error guard — a genuine fare being thrown
    # away as if it were a typo). Keeping the two disjoint also keeps the
    # downstream distinction clean: spikes outside festival windows are what the
    # IQR stage should find, festival surges are what it should attribute.
    # The draws happen unconditionally so the RNG stream position stays fixed
    # regardless of the calendar, preserving determinism.
    spike_draw = r.random()
    spike_size = r.uniform(1.30, 1.90)
    spike = spike_size if (spike_draw < _SPIKE_RATE and fest == 1.0) else 1.0

    base = anchor * adv * dow * fest * noise * spike
    base = round(base, 2)

    taxes = round(base * comp.taxes_pct_of_base, 2)
    udf = round(comp.udf_flat_inr, 2)
    convenience = round(comp.convenience_fee_inr, 2) if is_ota else 0.0
    total = round(base + taxes + udf + convenience, 2)

    return RawQuote(
        origin=route_meta.origin,
        destination=route_meta.destination,
        carrier=carrier_code,
        travel_date=travel_date,
        advance_window=AdvanceWindow.from_days(window_days),
        base_fare=base,
        taxes=taxes,
        udf=udf,
        convenience_fee=convenience,
        total_fare=total,
        source=source,
        # NOT negotiable, and NOT a default worth "simplifying": a synthetic
        # fare is tagged simulated at the moment it is created, and that tag
        # is what every downstream provenance count, the live/simulated split
        # on the dashboard, the upsert guard that stops synthetic data burying
        # an observation, and the "NO LIVE DATA" warning all read. This line
        # once said SourceType.LIVE, which silently reported a 100%-synthetic
        # cycle as 100% real.
        source_type=SourceType.SIMULATED,
        cycle_date=cycle_date,
        fare_class="economy",
        trip_type=TripType.ONE_WAY,
        is_nonstop=True,
        currency="INR",
    )


def simulate_cycle(
    cycle_date: date,
    skip_keys: set[tuple[str, str, int, date]] | None = None,
    routes: list[str] | None = None,
) -> tuple[list[RawQuote], dict[str, int]]:
    """
    Generate a full simulated cycle for the basket.

    `skip_keys` holds ``(route, carrier, window_days, travel_date)`` cells that
    Tier-A already covered with genuine live data. Tier-B yields to Tier-A for
    those cells so a real observation is never displaced by a synthetic one and
    the two are never double-counted.

    Returns the quotes plus a status tally for the scrape_runs ledger.
    """
    skip_keys = skip_keys or set()
    routes_cfg = load_routes()
    wanted = routes or routes_cfg.route_codes
    quotes: list[RawQuote] = []
    tally: dict[str, int] = {}

    def bump(status: str) -> None:
        tally[status] = tally.get(status, 0) + 1

    for route_code in wanted:
        meta = routes_cfg.by_route(route_code)
        for window_days in routes_cfg.advance_windows:
            travel_date = cycle_date + timedelta(days=window_days)
            for carrier in routes_cfg.carriers:
                key = (route_code, carrier.code, window_days, travel_date)
                if key in skip_keys:
                    continue

                if is_sold_out(route_code, carrier.code, window_days,
                               travel_date, cycle_date):
                    bump("EMPTY_NO_INVENTORY")
                    continue

                # Airline-direct quote.
                quotes.append(
                    simulate_quote(meta, carrier.code, carrier.price_multiplier,
                                   window_days, cycle_date,
                                   source=f"Simulator:{carrier.code}", is_ota=False)
                )
                bump("OK")

                # A subset of cells is also offered through an OTA, which adds a
                # convenience fee. These collide with the airline-direct row on
                # the dedupe key by design, and the cleaning stage keeps the more
                # completely itemised record (spec §3.1).
                r = _rng("ota", route_code, carrier.code, window_days, travel_date)
                if r.random() < 0.45:
                    ota = _OTA_SOURCES[int(r.random() * len(_OTA_SOURCES))]
                    quotes.append(
                        simulate_quote(meta, carrier.code, carrier.price_multiplier,
                                       window_days, cycle_date,
                                       source=f"Simulator:{ota}", is_ota=True)
                    )
                    bump("OK")

    logger.info(
        "Tier-B simulated cycle %s: %d quotes across %d routes (%d cells sold out)",
        cycle_date, len(quotes), len(wanted), tally.get("EMPTY_NO_INVENTORY", 0),
    )
    return quotes, tally
