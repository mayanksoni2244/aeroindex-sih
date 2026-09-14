"""
Cleaning-pipeline invariants.

The one rule every test here defends: NOTHING IS EVER DELETED. Validation
failures, statistical outliers and festival surges are all flagged in place.
A pipeline that quietly drops rows produces a smoother index and a false one.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from apix.clean.pipeline import clean_cycle
from apix.config.loader import load_festivals, load_routes
from apix.config.settings import settings
from apix.db.models import AdvanceWindow, FareQuote, QuoteStatus, SourceType
from apix.db.session import session_scope
from tests.conftest import ANCHOR, fetch, make_quote, store


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────
def test_validation_rejects_are_kept_not_deleted():
    """A rejected row stays in the table, flagged. Deleting it would hide it."""
    meta = load_routes().by_route("DEL-BOM")
    store([
        make_quote(carrier="6E", base=5000),
        make_quote(carrier="UK", base=meta.sanity_max * 3, taxes=0, udf=0, fee=0),
    ])
    clean_cycle(ANCHOR)

    rows = fetch(ANCHOR)
    assert len(rows) == 2, "a rejected quote must remain in the database"
    rejected = [r for r in rows if r.status is QuoteStatus.VALIDATION_REJECT]
    assert len(rejected) == 1
    assert rejected[0].carrier == "UK"


def test_non_inr_currency_is_rejected_not_converted():
    """No FX guessing. A USD row is rejected, and says so."""
    store([make_quote(carrier="6E", currency="USD", base=80, taxes=10, udf=5, fee=5)])
    report = clean_cycle(ANCHOR)
    assert report.currency_rejects == 1
    assert fetch(ANCHOR)[0].status is QuoteStatus.VALIDATION_REJECT


def test_component_mismatch_is_flagged_but_row_survives():
    """The total is still a valid observation; only the decomposition is suspect."""
    store([make_quote(carrier="6E", base=5000, taxes=500, udf=150, fee=100, total=9999)])
    report = clean_cycle(ANCHOR)
    row = fetch(ANCHOR)[0]
    assert report.component_mismatches == 1
    assert row.is_component_mismatch is True
    assert row.status is QuoteStatus.OK, "a mismatch must not reject the observation"


def test_sanity_bounds_are_wide_enough_for_a_real_surge():
    """
    A 3x festival fare must pass validation.

    This is the regression guard for a bug that actually occurred: bounds tight
    enough to reject a genuine Diwali fare turn the validator into a censor.
    """
    meta = load_routes().by_route("DEL-BOM")
    surge = meta.sim_base_fare * 3.0
    assert surge <= meta.sanity_max, (
        f"sanity_max {meta.sanity_max} would reject a 3x surge ({surge:.0f}) on "
        "DEL-BOM. Widen the bound; do not let validation silently delete surges."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Outliers — two independent rules
# ─────────────────────────────────────────────────────────────────────────────
def _festival_cycle() -> date:
    """A cycle date whose T+30 travel date lands inside a configured festival."""
    fests = load_festivals().festivals
    assert fests, "festivals.yaml must define at least one window"
    mid = fests[0].start + (fests[0].end - fests[0].start) / 2
    return mid - timedelta(days=AdvanceWindow.T30.days)


def build_history(
    end_cycle: date,
    days: int,
    carrier: str = "6E",
    base: float = 5000.0,
    jitter: float = 25.0,
    clean: bool = True,
) -> date:
    """
    Populate one (route, window, carrier) cell across consecutive cycles.

    The Tukey fence is fitted per cell on its own trailing history, so a test
    that wants to exercise it has to give the cell a history — a single cycle
    of many carriers gives you many groups of one, and every one of them is
    correctly skipped as too small.

    Returns the first cycle date written.
    """
    start = end_cycle - timedelta(days=days)
    for i in range(days):
        cycle = start + timedelta(days=i)
        store([make_quote(carrier=carrier, cycle=cycle,
                          base=base + (i % 3) * jitter)])
        if clean:
            clean_cycle(cycle)
    return start


def test_festival_flag_does_not_consult_the_iqr():
    """
    Calendar rule and statistical rule are independent.

    The cell here has a full trailing history and real dispersion, so a fence
    exists and could fire — but the festival fare sits right on the cell's own
    mean, so the fence would never flag it. The calendar flag must fire anyway.
    That independence is the point: a Diwali surge lasts a fortnight, and by
    mid-window a trailing fence has absorbed the surge into its own quartiles
    and stopped seeing it. A festival flag that waited for the fence would let
    most of the surge leak into CORE.
    """
    cycle = _festival_cycle()
    build_history(cycle, days=14, carrier="6E", base=5000)
    store([make_quote(carrier="6E", cycle=cycle, base=5000)])
    clean_cycle(cycle)

    rows = fetch(cycle)
    assert rows, "the festival cycle should hold at least one quote"
    assert all(r.is_high_demand_outlier for r in rows), (
        "every fare travelling inside a festival window must carry the calendar flag"
    )
    assert not any(r.is_outlier for r in rows), (
        "a festival fare at the cell's own mean is not a statistical anomaly"
    )


def test_statistical_outlier_is_flagged_outside_festival_windows():
    """A cell with 14 days of history at ~₹5,000 must flag a ₹40,000 spike."""
    cycle = ANCHOR
    build_history(cycle, days=14, carrier="6E", base=5000)
    store([make_quote(carrier="6E", cycle=cycle, base=40000)])
    report = clean_cycle(cycle)

    row = fetch(cycle)[0]
    assert row.is_outlier is True
    assert not row.is_high_demand_outlier, "ANCHOR is not in a festival window"
    assert report.outliers_flagged == 1


def test_outliers_are_flagged_never_removed():
    cycle = ANCHOR
    build_history(cycle, days=14, carrier="6E", base=5000)
    store([make_quote(carrier="6E", cycle=cycle, base=40000)])
    clean_cycle(cycle)

    rows = fetch(cycle)
    assert len(rows) == 1, "the spike must still be in the table"
    assert rows[0].total_fare > 40000
    assert rows[0].status is QuoteStatus.OK, "flagged, not rejected"


def test_iqr_stands_down_on_a_small_group():
    """Below min_quotes_for_iqr a fence is noise. The stage must decline."""
    cycle = ANCHOR
    short = settings.min_quotes_for_iqr - 2
    build_history(cycle, days=short, carrier="6E", base=5000)
    store([make_quote(carrier="6E", cycle=cycle, base=40000)])
    report = clean_cycle(cycle)

    assert report.groups_skipped_small >= 1
    assert not any(r.is_outlier for r in fetch(cycle)), (
        f"an IQR over {short + 1} points is not a dispersion estimate and must not flag"
    )


def test_a_real_fare_is_never_flagged_against_a_simulated_history():
    """
    Regression guard for the worst finding of the first live Akasa run.

    The fence used to be keyed on (route, window, carrier) alone, so the first
    real observation for a cell was judged against thirty days of the project's
    OWN SIMULATED history for that cell. It flagged 10 of the first 20 genuine
    Akasa fares — not because the market moved, but because the simulator
    assumes T+1 costs ~2.85x T+30 while Akasa was selling DEL-BOM flat across
    every window. Since CORE excludes flagged rows, invented data was vetoing
    observed data out of a published series.

    A fare that was actually charged cannot be an outlier relative to a number
    this project made up. Provenance is therefore part of the grouping key, and
    with one live point the rule must stand down rather than flag.
    """
    cycle = ANCHOR
    # A long simulated history for the cell, priced high...
    build_history(cycle, days=settings.min_quotes_for_iqr + 6,
                  carrier="QP", base=12000, jitter=40)
    # ...then the first real observation, priced far below it, as Akasa's flat
    # DEL-BOM fare sat far below the simulator's steep advance-purchase curve.
    store([make_quote(carrier="QP", cycle=cycle, base=5640, taxes=0, udf=0, fee=0,
                      source_type=SourceType.LIVE, source="AkasaAir")])
    report = clean_cycle(cycle)

    live_rows = [r for r in fetch(cycle) if r.source == "AkasaAir"]
    assert live_rows, "the live observation must be stored"
    assert not any(r.is_outlier for r in live_rows), (
        "a simulated history must never flag a real observed fare as anomalous"
    )
    assert report.groups_skipped_small >= 1, (
        "with one real point the rule should abstain and report the abstention"
    )


def test_the_fence_still_fires_within_one_provenance_class():
    """
    The provenance split must not disarm the rule.

    Real fares are still judged against real fares: once a cell has enough
    genuine history, a genuine shock is still caught. Otherwise the fix above
    would have bought honesty by making the stage useless.
    """
    cycle = ANCHOR
    n = settings.min_quotes_for_iqr + 6
    start = cycle - timedelta(days=n)
    for i in range(n):
        store([make_quote(carrier="QP", cycle=start + timedelta(days=i),
                          base=5000 + (i % 3) * 25, taxes=0, udf=0, fee=0,
                          source_type=SourceType.LIVE, source="AkasaAir")])
        clean_cycle(start + timedelta(days=i))
    store([make_quote(carrier="QP", cycle=cycle, base=40000, taxes=0, udf=0, fee=0,
                      source_type=SourceType.LIVE, source="AkasaAir")])
    clean_cycle(cycle)

    flagged = [r for r in fetch(cycle) if r.source == "AkasaAir" and r.is_outlier]
    assert flagged, "a real shock against real history must still be flagged"


# ─────────────────────────────────────────────────────────────────────────────
# Imputation
# ─────────────────────────────────────────────────────────────────────────────
def test_imputation_carries_forward_and_is_labelled():
    day1, day2 = ANCHOR, ANCHOR + timedelta(days=1)
    store([make_quote(carrier="6E", cycle=day1, base=5000)])
    clean_cycle(day1)
    # Day 2: the 6E cell is missing entirely.
    store([make_quote(carrier="UK", cycle=day2, base=5200)])
    report = clean_cycle(day2)

    assert report.imputed >= 1
    imputed = [r for r in fetch(day2) if r.is_imputed]
    assert imputed, "a recent donor exists, so the gap should be filled"
    for r in imputed:
        assert r.status is QuoteStatus.IMPUTED
        assert r.imputed_from_cycle == day1, "the donor cycle must be recorded"
        assert r.source.startswith("imputed:"), (
            "an imputed row must be distinguishable by source string alone"
        )


def test_imputation_refuses_past_the_age_cap():
    """
    Carrying a stale price forward invents a measurement. Past the cap the
    pipeline must leave the hole and let coverage fall.
    """
    day1 = ANCHOR
    stale = day1 + timedelta(days=settings.impute_max_age_days + 2)
    store([make_quote(carrier="6E", cycle=day1, base=5000)])
    clean_cycle(day1)
    store([make_quote(carrier="UK", cycle=stale, base=5200)])
    report = clean_cycle(stale)

    assert not any(r.is_imputed for r in fetch(stale)), (
        f"donor is {settings.impute_max_age_days + 2} days old, past the "
        f"{settings.impute_max_age_days}-day cap"
    )
    assert report.imputation_refused >= 1


def test_imputed_rows_are_rebuilt_not_duplicated_on_rerun():
    """clean_cycle is idempotent: running twice must not double the imputations."""
    day1, day2 = ANCHOR, ANCHOR + timedelta(days=1)
    store([make_quote(carrier="6E", cycle=day1, base=5000)])
    clean_cycle(day1)
    store([make_quote(carrier="UK", cycle=day2, base=5200)])

    first = clean_cycle(day2)
    rows_after_first = len(fetch(day2))
    second = clean_cycle(day2)

    assert first.imputed == second.imputed
    assert len(fetch(day2)) == rows_after_first


def test_imputed_row_keeps_donor_source_type_but_counts_as_imputed():
    """
    An imputed row inherits its donor's source_type — but `is_imputed` is what
    the lineage counts. A live-donor fill is still a fill, not an observation.
    """
    day1, day2 = ANCHOR, ANCHOR + timedelta(days=1)
    store([make_quote(carrier="6E", cycle=day1, base=5000,
                      source_type=SourceType.LIVE, source="AkasaAir")])
    clean_cycle(day1)
    store([make_quote(carrier="UK", cycle=day2, base=5200)])
    clean_cycle(day2)

    imputed = [r for r in fetch(day2) if r.is_imputed]
    assert imputed
    assert imputed[0].source_type is SourceType.LIVE
    assert imputed[0].is_imputed is True

    from apix.scrape.runner import count_quotes

    counts = count_quotes(day2)
    assert counts["live"] == 0, "an imputed row must not be counted as a live observation"
    assert counts["imputed"] >= 1


def test_a_real_observation_landing_on_an_imputed_cell_stops_being_imputed():
    """
    Regression guard for a bug found in a live Akasa run.

    `_imputed_hash` deliberately produces the same dedupe_hash as a real quote
    for the same cell, so a later observation REPLACES the filler rather than
    duplicating it. But the upsert only updated the fare fields and left the
    flags, so a genuine ₹6,880 Akasa fare sat in a row still marked
    `is_imputed=True`. Two failures followed from the one stale flag:

      * every provenance surface counted that real fare as "carried forward",
        under-reporting live coverage — which is misreporting it;
      * `_reset_cycle` deletes rows flagged imputed, so the next `apix clean`
        would have destroyed an observation that took a 51-minute live run.
    """
    day1, day2 = ANCHOR, ANCHOR + timedelta(days=1)
    store([make_quote(carrier="6E", cycle=day1, base=5000)])
    clean_cycle(day1)
    # day2 has a different carrier, so 6E's cell is a hole that gets filled.
    store([make_quote(carrier="UK", cycle=day2, base=5200)])
    clean_cycle(day2)

    filler = [r for r in fetch(day2) if r.carrier == "6E"]
    assert filler and filler[0].is_imputed is True, "setup: the cell must be imputed"

    # Now the scraper collects that exact cell for real.
    store([make_quote(carrier="6E", cycle=day2, base=6880, taxes=0, udf=0, fee=0,
                      source_type=SourceType.LIVE, source="AkasaAir")])

    observed = [r for r in fetch(day2) if r.carrier == "6E"]
    assert len(observed) == 1, "the observation must replace the filler, not join it"
    row = observed[0]
    assert row.total_fare == 6880
    assert row.is_imputed is False, "a freshly scraped fare is not carried forward"
    assert row.imputed_from_cycle is None
    assert row.source == "AkasaAir"

    from apix.scrape.runner import count_quotes

    assert count_quotes(day2)["live"] == 1


def test_reset_never_deletes_an_observation_wearing_a_stale_imputed_flag():
    """
    Defence in depth for the same bug, from the other side.

    `_reset_cycle` deletes the cycle's fillers on the premise that `is_imputed`
    marks something this pipeline invented. If that flag is ever wrong again,
    the delete must not be the thing that discovers it — so the `source`
    prefix, written at creation and overwritten by any real observation, has to
    agree before a row is removed. Here the flag is corrupted by hand to
    simulate the failure, and the real fare must survive.
    """
    from sqlalchemy import update

    store([make_quote(carrier="6E", cycle=ANCHOR, base=6880, taxes=0, udf=0, fee=0,
                      source_type=SourceType.LIVE, source="AkasaAir")])
    with session_scope() as s:
        s.execute(
            update(FareQuote)
            .where(FareQuote.cycle_date == ANCHOR, FareQuote.carrier == "6E")
            .values(is_imputed=True, imputed_from_cycle=ANCHOR - timedelta(days=1))
        )

    clean_cycle(ANCHOR)

    survivors = [r for r in fetch(ANCHOR) if r.carrier == "6E"]
    assert survivors, "clean deleted a real observed fare because of a stale flag"
    assert survivors[0].total_fare == 6880
    assert survivors[0].is_imputed is False, "reset must also repair the stale flag"


# ─────────────────────────────────────────────────────────────────────────────
# Ordering
# ─────────────────────────────────────────────────────────────────────────────
def test_a_rejected_row_is_not_replaced_by_an_imputation():
    """
    Regression guard for a real collision.

    A validation-rejected row still occupies its cell identity. Imputing over
    it would both violate the dedupe key and, worse, replace a real (if
    unusable) observation with a fabricated one. The cell must simply stay
    uncovered.
    """
    meta = load_routes().by_route("DEL-BOM")
    day1, day2 = ANCHOR, ANCHOR + timedelta(days=1)
    store([make_quote(carrier="6E", cycle=day1, base=5000)])
    clean_cycle(day1)
    store([
        make_quote(carrier="6E", cycle=day2,
                   base=meta.sanity_max * 3, taxes=0, udf=0, fee=0),
        make_quote(carrier="UK", cycle=day2, base=5200),
    ])
    clean_cycle(day2)

    day2_6e = [r for r in fetch(day2) if r.carrier == "6E"]
    assert len(day2_6e) == 1, "the rejected observation must not be duplicated"
    assert day2_6e[0].status is QuoteStatus.VALIDATION_REJECT
    assert day2_6e[0].is_imputed is False


def test_clean_cycle_is_idempotent_over_flags():
    cycle = ANCHOR
    build_history(cycle, days=14, carrier="6E", base=5000)
    store([make_quote(carrier="6E", cycle=cycle, base=40000)])

    a = clean_cycle(cycle)
    snap_a = {(r.carrier, r.status, r.is_outlier, r.is_high_demand_outlier) for r in fetch(cycle)}
    b = clean_cycle(cycle)
    snap_b = {(r.carrier, r.status, r.is_outlier, r.is_high_demand_outlier) for r in fetch(cycle)}

    assert snap_a == snap_b
    assert (a.outliers_flagged, a.imputed) == (b.outliers_flagged, b.imputed)
