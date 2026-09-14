"""
Index construction — the golden dataset.

Every number asserted here is hand-computable from the fixture above it. That
is the point of a golden test: if the arithmetic ever changes, the failure
message tells you the old value, the new one, and the formula that should have
produced it, without anyone having to re-derive the method from the code.

The four properties this file defends:
  1. The published formula is the formula implemented (Jevons then Laspeyres).
  2. Matched-model — a route that appears or disappears is not a price change.
  3. HEADLINE and CORE differ by exactly the flagged observations, and only by
     those.
  4. TOTAL and BASE are independent of the series axis.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from apix.config.loader import load_basket
from apix.db.models import AdvanceWindow, Frequency, Measure, Series
from apix.db.session import session_scope
from apix.index.construct import (
    IndexError_,
    compute_point,
    elementary_relative,
    resolve_base_period,
)
from tests.conftest import ANCHOR, make_quote, store

DAY0 = ANCHOR
DAY1 = ANCHOR + timedelta(days=1)

#: make_quote defaults: base 5000 + taxes 500 + udf 150 + fee 100.
TOTAL0 = 5750.0
BASE0 = 5000.0


# ─────────────────────────────────────────────────────────────────────────────
# Elementary formula
# ─────────────────────────────────────────────────────────────────────────────
def test_jevons_is_geometric_not_arithmetic():
    """
    Two cells, one doubling and one halving, is a 1.0 relative under Jevons.

    Under an arithmetic mean of relatives it would be 1.25 — a 25% "increase"
    invented purely by the choice of formula. This is the single clearest
    reason a price index uses geometric means at the elementary level, so it
    gets its own test.
    """
    base = {("6E", "T+30"): 100.0, ("UK", "T+30"): 100.0}
    curr = {("6E", "T+30"): 200.0, ("UK", "T+30"): 50.0}
    rel, n = elementary_relative(base, curr, formula="jevons")
    assert n == 2
    assert rel == pytest.approx(1.0)
    assert rel != pytest.approx(1.25)


def test_dutot_is_the_ratio_of_means():
    base = {("6E", "T+30"): 100.0, ("UK", "T+30"): 300.0}
    curr = {("6E", "T+30"): 200.0, ("UK", "T+30"): 200.0}
    rel, n = elementary_relative(base, curr, formula="dutot")
    assert (rel, n) == (pytest.approx(400.0 / 400.0), 2)


def test_unmatched_cells_are_dropped_not_assumed_unchanged():
    """Matched-model: a cell present in only one period contributes nothing."""
    base = {("6E", "T+30"): 100.0, ("UK", "T+30"): 100.0}
    curr = {("6E", "T+30"): 200.0, ("QP", "T+30"): 99999.0}
    rel, n = elementary_relative(base, curr)
    assert n == 1, "only the 6E cell exists in both periods"
    assert rel == pytest.approx(2.0), "the QP entrant must not move the index"


def test_no_overlap_returns_none_not_one():
    """
    An empty intersection is missing information, not "no change".

    Returning 1.0 here would publish a flat index for a route we observed
    nothing comparable about.
    """
    rel, n = elementary_relative({("6E", "T+30"): 100.0}, {("UK", "T+30"): 100.0})
    assert (rel, n) == (None, 0)


def test_unknown_formula_raises():
    with pytest.raises(IndexError_):
        elementary_relative({("a", "b"): 1.0}, {("a", "b"): 1.0}, formula="fisher")


# ─────────────────────────────────────────────────────────────────────────────
# Golden dataset — full Laspeyres aggregation
# ─────────────────────────────────────────────────────────────────────────────
def _golden_fixture():
    """
    DEL-BOM doubles, DEL-BLR is flat. No other route is observed.

        DEL-BOM  ₹5,750 -> ₹11,500   relative 2.0   raw weight 0.2770
        DEL-BLR  ₹5,750 -> ₹ 5,750   relative 1.0   raw weight 0.2200

    Weights renormalise over covered routes only (Σw = 0.4970), so:

        APIx = 100 × (0.2770×2.0 + 0.2200×1.0) / 0.4970
             = 100 × 0.7740 / 0.4970
             = 155.7344...
    """
    for route in ("DEL-BOM", "DEL-BLR"):
        store([make_quote(route=route, carrier="6E", cycle=DAY0, base=BASE0)])
    store([make_quote(route="DEL-BOM", carrier="6E", cycle=DAY1,
                      base=BASE0 * 2, taxes=1000, udf=300, fee=200)])
    store([make_quote(route="DEL-BLR", carrier="6E", cycle=DAY1, base=BASE0)])


GOLDEN_EXPECTED = 100.0 * (0.2770 * 2.0 + 0.2200 * 1.0) / (0.2770 + 0.2200)


def test_golden_index_value(basket):
    _golden_fixture()
    with session_scope() as s:
        assert resolve_base_period(s) == DAY0
        point = compute_point(s, DAY1, DAY0, Series.HEADLINE, Measure.TOTAL,
                              Frequency.DAILY, basket)

    assert point.index_value == pytest.approx(GOLDEN_EXPECTED, abs=1e-4)
    assert point.index_value == pytest.approx(155.7344, abs=1e-3)
    assert point.base_period == DAY0
    assert {r.route for r in point.routes} == {"DEL-BOM", "DEL-BLR"}
    rel = {r.route: r.relative for r in point.routes}
    assert rel["DEL-BOM"] == pytest.approx(2.0)
    assert rel["DEL-BLR"] == pytest.approx(1.0)


def test_base_period_is_exactly_one_hundred(basket):
    _golden_fixture()
    with session_scope() as s:
        for series in Series:
            for measure in Measure:
                p = compute_point(s, DAY0, DAY0, series, measure, Frequency.DAILY, basket)
                assert p.index_value == pytest.approx(100.0), (
                    f"{series.value}/{measure.value} must be 100.00 at the base period"
                )


def test_weights_match_the_published_dgca_shares(basket):
    """
    The weights in basket.yaml must be the renormalised DGCA shares, not
    round numbers chosen to look tidy.

        DEL-BOM 4.13 / 14.91 = 0.2770
        DEL-BLR 3.28 / 14.91 = 0.2200
    """
    shares = {r.route: r.national_share_pct for r in basket.routes}
    total_share = sum(shares.values())
    for r in basket.routes:
        assert r.weight == pytest.approx(r.national_share_pct / total_share, abs=5e-5), (
            f"{r.route}: weight {r.weight} is not {r.national_share_pct}/{total_share:.2f}"
        )
    assert sum(r.weight for r in basket.routes) == pytest.approx(1.0, abs=1e-4)


def test_route_entry_does_not_move_the_index(basket):
    """
    A route observed for the first time in the current period must not
    contribute — its "price change" is undefined, not zero.
    """
    _golden_fixture()
    with session_scope() as s:
        before = compute_point(s, DAY1, DAY0, Series.HEADLINE, Measure.TOTAL,
                               Frequency.DAILY, basket).index_value

    store([make_quote(route="BLR-HYD", carrier="6E", cycle=DAY1, base=99000)])
    with session_scope() as s:
        after = compute_point(s, DAY1, DAY0, Series.HEADLINE, Measure.TOTAL,
                              Frequency.DAILY, basket).index_value

    assert after == pytest.approx(before), (
        "an entrant route with no base-period observation changed the index"
    )


def test_index_over_nothing_raises_rather_than_returning_100(basket):
    """No matched route is an absent number, not a neutral one."""
    store([make_quote(route="DEL-BOM", carrier="6E", cycle=DAY0)])
    store([make_quote(route="DEL-BLR", carrier="UK", cycle=DAY1)])
    with session_scope() as s, pytest.raises(IndexError_):
        compute_point(s, DAY1, DAY0, Series.HEADLINE, Measure.TOTAL,
                      Frequency.DAILY, basket)


# ─────────────────────────────────────────────────────────────────────────────
# The two axes
# ─────────────────────────────────────────────────────────────────────────────
def test_core_excludes_flagged_rows_and_headline_keeps_them(basket):
    """
    CORE and HEADLINE differ by exactly the flagged observations.

    Both routes have two carriers in both periods. On DAY1 the DEL-BOM 6E cell
    triples and is flagged as a high-demand outlier; UK is flat. HEADLINE must
    see the surge, CORE must not, and the flagged row must still be in the
    table afterwards.
    """
    from sqlalchemy import select

    from apix.db.models import FareQuote

    for route in ("DEL-BOM", "DEL-BLR"):
        for carrier in ("6E", "UK"):
            store([make_quote(route=route, carrier=carrier, cycle=DAY0, base=BASE0)])
            store([make_quote(route=route, carrier=carrier, cycle=DAY1, base=BASE0)])

    with session_scope() as s:
        surge = s.scalars(
            select(FareQuote).where(
                FareQuote.route == "DEL-BOM",
                FareQuote.carrier == "6E",
                FareQuote.cycle_date == DAY1,
            )
        ).one()
        surge.base_fare = BASE0 * 3
        surge.total_fare = TOTAL0 * 3
        surge.is_high_demand_outlier = True

    with session_scope() as s:
        head = compute_point(s, DAY1, DAY0, Series.HEADLINE, Measure.TOTAL,
                             Frequency.DAILY, basket)
        core = compute_point(s, DAY1, DAY0, Series.CORE, Measure.TOTAL,
                             Frequency.DAILY, basket)

    assert head.index_value > core.index_value, "the surge must show up in HEADLINE"
    assert core.index_value == pytest.approx(100.0), (
        "with the surge excluded, every remaining cell is flat"
    )
    # Jevons over {3.0, 1.0} on DEL-BOM, 1.0 on DEL-BLR.
    del_bom = math.sqrt(3.0 * 1.0)
    expected = 100.0 * (0.2770 * del_bom + 0.2200 * 1.0) / (0.2770 + 0.2200)
    assert head.index_value == pytest.approx(expected, abs=1e-3)

    with session_scope() as s:
        still_there = s.scalars(
            select(FareQuote).where(
                FareQuote.cycle_date == DAY1, FareQuote.is_high_demand_outlier.is_(True)
            )
        ).all()
    assert len(still_there) == 1, "CORE excludes the row; it does not delete it"


def test_measure_axis_is_independent_of_series_axis(basket):
    """
    A pure tax increase moves TOTAL and leaves BASE alone.

    This is the RBI-vs-NSO split working: a GST revision is inflation a
    statistical office must capture and a central bank must not mistake for
    carrier pricing.
    """
    for route in ("DEL-BOM", "DEL-BLR"):
        store([make_quote(route=route, carrier="6E", cycle=DAY0, base=BASE0)])
        # Same base fare, taxes doubled.
        store([make_quote(route=route, carrier="6E", cycle=DAY1,
                          base=BASE0, taxes=1000, udf=300, fee=200)])

    with session_scope() as s:
        total = compute_point(s, DAY1, DAY0, Series.HEADLINE, Measure.TOTAL,
                              Frequency.DAILY, basket)
        base_m = compute_point(s, DAY1, DAY0, Series.HEADLINE, Measure.BASE,
                               Frequency.DAILY, basket)

    assert base_m.index_value == pytest.approx(100.0), (
        "base fares did not move, so the BASE measure must not move"
    )
    assert total.index_value > 100.0, "the tax rise must show in the TOTAL measure"
    assert total.index_value == pytest.approx(100.0 * 6500.0 / 5750.0, abs=1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# Lineage
# ─────────────────────────────────────────────────────────────────────────────
def test_lineage_percentages_partition_to_one_hundred(basket):
    _golden_fixture()
    with session_scope() as s:
        p = compute_point(s, DAY1, DAY0, Series.HEADLINE, Measure.TOTAL,
                          Frequency.DAILY, basket)
    lin = p.lineage
    assert lin["live_pct"] + lin["simulated_pct"] + lin["imputed_pct"] == pytest.approx(100.0)
    assert lin["live_count"] + lin["simulated_count"] + lin["imputed_count"] == lin["quote_count"]


def test_lineage_records_uncovered_basket_routes(basket):
    """Coverage must be legible: four of six routes were not observed."""
    _golden_fixture()
    with session_scope() as s:
        p = compute_point(s, DAY1, DAY0, Series.HEADLINE, Measure.TOTAL,
                          Frequency.DAILY, basket)
    lin = p.lineage
    assert set(lin["routes_covered"]) == {"DEL-BOM", "DEL-BLR"}
    assert len(lin["routes_missing"]) == 4
    assert lin["routes_in_basket"] == 6
    assert lin["weight_covered_pct"] == pytest.approx(49.7, abs=0.2)
    assert lin["low_coverage"] is True, "2 of 6 routes is below min_routes_for_index"
