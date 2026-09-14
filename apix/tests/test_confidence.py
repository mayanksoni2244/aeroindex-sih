"""
Confidence grading — the support statement attached to every published value.

The rule these tests defend: the grade must move with the evidence, in the
right direction, and must never flatter a thin number. A badge that reads HIGH
on simulated data would be worse than no badge at all, because it would launder
the exact limitation the rest of this project works to expose.
"""
from __future__ import annotations

import pytest

from apix.index.confidence import TARGET_CELLS, grade_point


def lineage(
    live=0, simulated=0, imputed=0, cells=0, weight_pct=100.0,
    missing=None, route_cells=None, low_coverage=False,
):
    """A lineage dict shaped like the one `compute_point` stores."""
    return {
        "live_count": live,
        "simulated_count": simulated,
        "imputed_count": imputed,
        "cells_matched": cells,
        "weight_covered_pct": weight_pct,
        "routes_missing": missing or [],
        "route_cells_matched": route_cells,
        "low_coverage": low_coverage,
    }


# ─────────────────────────────────────────────────────────────────────────────
# The two ends of the scale
# ─────────────────────────────────────────────────────────────────────────────
def test_a_fully_observed_point_grades_high():
    """20 real cells over the whole basket is the best this basket can do."""
    c = grade_point(lineage(
        live=20, cells=TARGET_CELLS, weight_pct=100.0,
        route_cells={"DEL-BOM": 5, "DEL-BLR": 5, "BOM-BLR": 5, "DEL-CCU": 5},
    ))
    assert c.grade == "HIGH"
    assert c.score == 100.0
    assert c.real_cells == TARGET_CELLS


def test_a_fully_simulated_point_can_never_grade_high():
    """
    The guard that matters most.

    A sandbox point has perfect basket coverage and plenty of cells; only its
    provenance is worthless. If coverage and volume alone could carry it to
    HIGH, the badge would certify simulated data as measurement.
    """
    c = grade_point(lineage(
        simulated=143, cells=143, weight_pct=100.0,
        route_cells={"DEL-BOM": 24, "DEL-BLR": 24, "BOM-BLR": 24,
                     "DEL-CCU": 24, "BLR-HYD": 24, "MAA-DEL": 23},
    ))
    assert c.grade in {"INDICATIVE", "LOW"}
    assert c.components["provenance"] == 0.0
    assert c.components["observation"] == 0.0, (
        "simulated cells must not count as observed cells"
    )
    assert any("simulated" in r for r in c.reasons)


# ─────────────────────────────────────────────────────────────────────────────
# Monotonicity — more real evidence must never score worse
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("n", [1, 4, 8, 12, 16, 20])
def test_score_rises_with_real_cell_count(n):
    """Each extra observed cell can only help."""
    fewer = grade_point(lineage(live=max(1, n - 4), cells=max(1, n - 4)))
    more = grade_point(lineage(live=n, cells=n))
    assert more.score >= fewer.score


def test_imputed_quotes_score_below_observed_ones():
    """
    Carrying a value forward is not observing it.

    Same cell count, same coverage — the only difference is that one point's
    quotes were collected today and the other's were copied from an earlier
    cycle. The grade has to see that.
    """
    observed = grade_point(lineage(live=20, cells=20))
    carried = grade_point(lineage(live=10, imputed=10, cells=20))
    assert carried.score < observed.score
    assert any("carried forward" in r for r in carried.reasons)


def test_losing_basket_weight_lowers_the_grade():
    """A point missing half the basket is not the same measurement."""
    full = grade_point(lineage(live=20, cells=20, weight_pct=100.0))
    partial = grade_point(lineage(
        live=20, cells=20, weight_pct=48.0, missing=["DEL-BLR", "BOM-BLR"]
    ))
    assert partial.score < full.score
    assert any("basket route(s) absent" in r for r in partial.reasons)


def test_uneven_route_spread_is_penalised():
    """
    Twenty cells from one route is not twenty cells across four.

    Both points below have the same volume, provenance and basket weight. Only
    the distribution differs, so only breadth can separate them — which is the
    whole reason the component exists.
    """
    even = grade_point(lineage(
        live=20, cells=20,
        route_cells={"DEL-BOM": 5, "DEL-BLR": 5, "BOM-BLR": 5, "DEL-CCU": 5},
    ))
    lopsided = grade_point(lineage(
        live=20, cells=20,
        route_cells={"DEL-BOM": 17, "DEL-BLR": 1, "BOM-BLR": 1, "DEL-CCU": 1},
    ))
    assert lopsided.components["breadth"] < even.components["breadth"]
    assert lopsided.score < even.score
    assert any("unevenly distributed" in r for r in lopsided.reasons)


# ─────────────────────────────────────────────────────────────────────────────
# Degenerate inputs
# ─────────────────────────────────────────────────────────────────────────────
def test_an_empty_lineage_grades_indicative_and_says_why():
    c = grade_point({})
    assert c.grade == "INDICATIVE"
    assert c.score == 0.0
    assert any("No real observed cells" in r for r in c.reasons)


def test_a_grade_always_carries_at_least_one_reason():
    """A bare letter grade is not an explanation. Every path must justify itself."""
    for lin in (
        {},
        lineage(live=20, cells=20),
        lineage(simulated=50, cells=50),
        lineage(live=3, imputed=2, cells=5, weight_pct=40.0, missing=["DEL-CCU"]),
    ):
        assert grade_point(lin).reasons, f"no reason given for {lin}"


def test_components_stay_within_zero_and_one():
    """
    Bounds are asserted, not assumed.

    A component above 1.0 would let one dimension pay for another's failure and
    push a thin point over a band threshold.
    """
    for lin in (
        lineage(live=200, cells=500, weight_pct=250.0),  # absurd inputs
        lineage(live=0, simulated=0, cells=0, weight_pct=-10.0),
    ):
        c = grade_point(lin)
        for name, v in c.components.items():
            assert 0.0 <= v <= 1.0, f"{name} out of range: {v}"
        assert 0.0 <= c.score <= 100.0


# ─────────────────────────────────────────────────────────────────────────────
# The arithmetic ships with the result
# ─────────────────────────────────────────────────────────────────────────────
def test_published_weights_reproduce_the_published_score():
    """
    A consumer must be able to recompute the score from the payload alone.

    The dashboard prints each component as a percentage and a contribution in
    points. If the weights it multiplies by were not the weights this module
    used, the panel would show a total that does not equal the headline score —
    two documents disagreeing about one number. Publish the arithmetic and hold
    the two to each other.
    """
    c = grade_point(lineage(live=20, cells=20))
    assert c.weights, "weights must be published, not left for the client to guess"
    assert set(c.weights) == set(c.components), (
        "every component must carry a weight, and no weight may point at a "
        "component that does not exist"
    )
    recomputed = sum(c.components[k] * c.weights[k] for k in c.components) * 100.0
    assert abs(recomputed - c.score) < 1e-6, (
        f"payload weights give {recomputed}, but the engine scored {c.score}"
    )


def test_weights_sum_to_one():
    """
    Unnormalised weights would inflate or deflate every score at once.

    Weights summing above 1.0 would push a point over a band threshold it did
    not earn; summing below would strand a genuinely strong point in a lower
    band. Neither is recoverable downstream, so assert it here.
    """
    w = grade_point({}).weights
    assert abs(sum(w.values()) - 1.0) < 1e-9, f"weights sum to {sum(w.values())}"


def test_weights_survive_as_dict_serialisation():
    """The API renders `as_dict()`. A weight that is not on the wire is not shipped."""
    d = grade_point(lineage(live=12, cells=12)).as_dict()
    assert d["weights"]
    assert abs(sum(d["weights"].values()) - 1.0) < 1e-9
