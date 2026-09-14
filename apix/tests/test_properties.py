'''
Property-based tests — Hypothesis.

The example-based tests check that the index behaves correctly on the cases
someone thought of. These check the axioms it must satisfy on cases nobody
thought of, which is where index-number bugs actually live: a formula that is
right on the demo data and wrong under a units change, a re-based window, or a
route that enters mid-series.

Only genuine theorems are asserted here. Jevons satisfies proportionality,
identity, time reversal, circularity and commensurability; Dutot notably does
NOT satisfy commensurability, and that difference is asserted rather than
glossed over. Nothing here asserts an ordering between Jevons and Dutot,
because none holds in general.

Tolerances are relative, not absolute: these are ratios of fares that may span
several orders of magnitude, and an `abs=1e-9` on a quantity near 10^5 is a
test that passes for the wrong reason.
'''
from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, assume, given, settings as hyp_settings
from hypothesis import strategies as st

from apix.clean.pipeline import _fence
from apix.index.construct import IndexError_, elementary_relative
from apix.scrape.base import looks_blocked, parse_inr

# Fares in a plausible-to-absurd INR range. The upper end is well beyond any
# real airfare on purpose: the maths must not depend on the values being sane.
FARES = st.floats(min_value=1.0, max_value=1e7, allow_nan=False, allow_infinity=False)
CELLS = st.text("ABCDEFGH", min_size=1, max_size=3)

#: A base and current price map over the same cells, so a relative always exists.
@st.composite
def matched_panel(draw, min_cells: int = 1, max_cells: int = 12):
    keys = draw(st.lists(CELLS, min_size=min_cells, max_size=max_cells, unique=True))
    base = {k: draw(FARES) for k in keys}
    curr = {k: draw(FARES) for k in keys}
    return base, curr


def rel_close(a: float, b: float, tol: float = 1e-9) -> bool:
    """Relative comparison — see the module docstring."""
    return math.isclose(a, b, rel_tol=tol)


# ─────────────────────────────────────────────────────────────────────────────
# The five axioms
# ─────────────────────────────────────────────────────────────────────────────
@given(matched_panel())
def test_identity(panel):
    """
    Unchanged prices, unchanged index. Exactly 1.0, not approximately.

    The single most consequential property: a flat market must not drift, or
    every published month-on-month change contains a formula artefact.
    """
    base, _ = panel
    rel, n = elementary_relative(base, dict(base))
    assert n == len(base)
    assert rel_close(rel, 1.0, tol=1e-12)


@given(matched_panel(), st.floats(min_value=0.01, max_value=100.0))
def test_proportionality(panel, k):
    """
    Scaling every current price by k scales the relative by k.

    This is what makes the number an index rather than a statistic that merely
    correlates with prices.
    """
    base, curr = panel
    plain, _ = elementary_relative(base, curr)
    scaled, _ = elementary_relative(base, {c: p * k for c, p in curr.items()})
    assert rel_close(scaled, plain * k, tol=1e-9)


@given(matched_panel())
def test_time_reversal(panel):
    """
    Running the comparison backwards inverts it: J(p0,p1) x J(p1,p0) = 1.

    Carli fails this — it is biased upward in both directions at once, which is
    why it is not used here. Asserting it pins the choice of formula rather
    than just its current output.
    """
    base, curr = panel
    forward, _ = elementary_relative(base, curr)
    backward, _ = elementary_relative(curr, base)
    assert rel_close(forward * backward, 1.0, tol=1e-9)


@given(matched_panel(), matched_panel())
def test_circularity(panel_a, panel_b):
    """
    Chaining is consistent: J(p0,p1) x J(p1,p2) = J(p0,p2).

    Without this, an index rebased at any point drifts away from the same index
    computed straight through — and the two would disagree about the same
    market with no way to say which was right.
    """
    (p0, p1), (_, p2_raw) = panel_a, panel_b
    assume(set(p1) == set(p0))
    p2 = {c: p2_raw.get(c, v * 1.1) for c, v in p1.items()}

    leg1, _ = elementary_relative(p0, p1)
    leg2, _ = elementary_relative(p1, p2)
    direct, _ = elementary_relative(p0, p2)
    assert rel_close(leg1 * leg2, direct, tol=1e-8)


@given(matched_panel(), st.floats(min_value=0.01, max_value=1000.0))
def test_jevons_is_commensurable(panel, k):
    """
    Rescaling ONE cell in both periods changes nothing.

    This is the property that makes Jevons the right elementary formula here.
    Cells are (carrier, advance-window) pairs whose fare levels differ by large
    factors — a full-service carrier at T+1 against a low-cost carrier at T+45.
    A formula sensitive to those levels would silently weight expensive cells
    more heavily, and the weighting would come from nowhere.
    """
    base, curr = panel
    victim = sorted(base)[0]

    plain, _ = elementary_relative(base, curr)
    rescaled, _ = elementary_relative(
        {c: (p * k if c == victim else p) for c, p in base.items()},
        {c: (p * k if c == victim else p) for c, p in curr.items()},
    )
    assert rel_close(rescaled, plain, tol=1e-9)


@given(matched_panel(min_cells=2))
def test_dutot_follows_the_rescaled_cell_while_jevons_ignores_it(panel):
    """
    Dutot is not commensurable, stated as the limit it actually is.

    An earlier version of this test asserted that rescaling one cell moves the
    Dutot answer by more than 0.1%. Hypothesis found the hole: on
    base={'A': 31, 'AA': 1}, curr={'A': 32, 'AA': 1} the shift is 0.098% and the
    assertion failed. The magnitude of the distortion depends on the panel and
    can be made arbitrarily small, so no threshold on it is a theorem.

    What *is* a theorem: as one cell's units are scaled without bound, that cell
    dominates both sums and Dutot converges to that cell's own price relative,
    however the rest of the panel moved. Jevons is untouched, because a unit
    change adds log(k) to numerator and denominator alike and cancels.

    The scale factor is derived from the panel rather than fixed, so it dominates
    whatever magnitudes were drawn — a hardcoded 1e6 is not large enough next to
    a cell already priced at 1e7.
    """
    base, curr = panel
    victim = sorted(base)[0]
    scale = 1e9 * (sum(base.values()) + sum(curr.values())) / min(base[victim],
                                                                 curr[victim])

    def rescale(mapping):
        return {c: (p * scale if c == victim else p) for c, p in mapping.items()}

    jevons_before, _ = elementary_relative(base, curr, "jevons")
    jevons_after, _ = elementary_relative(rescale(base), rescale(curr), "jevons")
    dutot_after, _ = elementary_relative(rescale(base), rescale(curr), "dutot")

    assert rel_close(jevons_after, jevons_before, tol=1e-9)
    assert rel_close(dutot_after, curr[victim] / base[victim], tol=1e-6)


def test_dutot_and_jevons_disagree_on_a_witness_that_cannot_be_filtered_away():
    """
    The counter-example as a fixed panel, not a generated one.

    The property test above can in principle draw a panel where the victim's own
    relative already equals the aggregate, and then converging to it proves
    nothing about the two formulas differing. This witness is hand-written so it
    can never be filtered, shrunk or assumed away: two cells, one flat at 1000
    and one that doubles, with the flat cell denominated in paise instead of
    rupees.

    Jevons reads the same 1.414 either way. Dutot reads 1.500 in rupees and
    1.010 in paise — a 50% rise or a 1% rise, from one choice of unit.
    """
    rupees = ({"flat": 1000.0, "doubles": 1000.0},
              {"flat": 1000.0, "doubles": 2000.0})
    paise = ({"flat": 100000.0, "doubles": 1000.0},
             {"flat": 100000.0, "doubles": 2000.0})

    jevons_r, _ = elementary_relative(*rupees, "jevons")
    jevons_p, _ = elementary_relative(*paise, "jevons")
    dutot_r, _ = elementary_relative(*rupees, "dutot")
    dutot_p, _ = elementary_relative(*paise, "dutot")

    assert rel_close(jevons_r, math.sqrt(2.0), tol=1e-12)
    assert rel_close(jevons_p, math.sqrt(2.0), tol=1e-12)
    assert rel_close(dutot_r, 3000.0 / 2000.0, tol=1e-12)
    assert rel_close(dutot_p, 102000.0 / 101000.0, tol=1e-12)
    assert abs(dutot_r - dutot_p) > 0.48


# ─────────────────────────────────────────────────────────────────────────────
# Bounds and matching
# ─────────────────────────────────────────────────────────────────────────────
@given(matched_panel())
def test_the_relative_lies_between_the_smallest_and_largest_price_change(panel):
    """
    Mean-value property: an aggregate outside the range of its own inputs is a
    bug that no amount of eyeballing a chart would catch.
    """
    base, curr = panel
    rel, _ = elementary_relative(base, curr)
    ratios = [curr[c] / base[c] for c in base]

    assert min(ratios) <= rel * (1 + 1e-9)
    assert rel <= max(ratios) * (1 + 1e-9)


@given(matched_panel(), FARES, CELLS)
def test_a_route_entering_the_panel_is_not_a_price_change(panel, price, newcomer):
    """
    Matched-model, the property version.

    A cell that exists only in the current period contributes nothing. If it
    did, the index would move whenever a carrier added a flight — reporting a
    coverage change as inflation, which is the classic way a real index gets
    this wrong.
    """
    base, curr = panel
    assume(newcomer not in base)

    before, n_before = elementary_relative(base, curr)
    after, n_after = elementary_relative(base, {**curr, newcomer: price})

    assert n_after == n_before
    assert rel_close(after, before, tol=1e-12)


@given(matched_panel(), matched_panel())
def test_only_the_intersection_ever_counts(panel_a, panel_b):
    base, _ = panel_a
    _, curr = panel_b
    rel, n = elementary_relative(base, curr)

    overlap = set(base) & set(curr)
    assert n == len(overlap)
    if not overlap:
        assert rel is None, "no overlap must yield None, never 1.0"


@given(matched_panel())
def test_cell_ordering_does_not_affect_the_result(panel):
    """
    Commodity reversal. Dict iteration order tracks insertion, so a formula
    that accumulated in a float-order-sensitive way would give different
    answers for the same data depending on the order rows came back from the
    database.
    """
    base, curr = panel
    forward, _ = elementary_relative(base, curr)
    reverse, _ = elementary_relative(
        dict(reversed(list(base.items()))), dict(reversed(list(curr.items())))
    )
    assert rel_close(forward, reverse, tol=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# Non-positive prices: dropped from the maths AND from the count
# ─────────────────────────────────────────────────────────────────────────────
@given(matched_panel(min_cells=2), st.floats(min_value=-1e5, max_value=0.0))
def test_a_non_positive_price_is_excluded_from_the_count_too(panel, bad):
    """
    Regression, found by writing this file.

    The cell was dropped from the sum but left in the denominator, which pulled
    the relative toward 1.0 and reported the cell as backing a number it took
    no part in. Both halves are asserted: the value must equal the index over
    the surviving cells exactly, and the count must equal how many survived.
    """
    base, curr = panel
    victim = sorted(base)[0]
    poisoned = {**base, victim: bad}

    rel, n = elementary_relative(poisoned, curr)
    survivors = {c: v for c, v in base.items() if c != victim}
    expected, expected_n = elementary_relative(
        survivors, {c: curr[c] for c in survivors}
    )

    assert n == expected_n == len(base) - 1
    assert rel_close(rel, expected, tol=1e-12)


@given(st.sampled_from(["jevons", "dutot"]), matched_panel())
def test_both_formulas_agree_when_every_cell_moves_by_the_same_factor(formula, panel):
    """
    Where the formulas must agree, they do.

    Uniform inflation is the one case with an unambiguous right answer, so a
    disagreement here would mean one of them is simply wrong rather than
    differently weighted.
    """
    base, _ = panel
    k = 1.37
    rel, _ = elementary_relative(base, {c: p * k for c, p in base.items()}, formula)
    assert rel_close(rel, k, tol=1e-9)


@given(st.text(min_size=1).filter(lambda s: s.lower() not in ("jevons", "dutot")))
def test_an_unknown_formula_always_raises(name):
    """Never a silent fallback: a mistyped setting must not pick a formula."""
    with pytest.raises(IndexError_):
        elementary_relative({"a": 1.0}, {"a": 2.0}, name)


# ─────────────────────────────────────────────────────────────────────────────
# The Tukey fence
# ─────────────────────────────────────────────────────────────────────────────
@given(st.lists(FARES, min_size=4, max_size=200),
       st.floats(min_value=0.5, max_value=5.0))
def test_the_fence_contains_the_median(values, k):
    """A fence that excludes its own centre would flag most of the panel."""
    lo, hi = _fence(values, k)
    import statistics as stats

    assert lo <= stats.median(values) <= hi


@given(st.lists(FARES, min_size=4, max_size=100),
       st.floats(min_value=0.5, max_value=5.0),
       st.floats(min_value=0.01, max_value=100.0))
def test_the_fence_is_scale_equivariant(values, k, scale):
    """
    Flagging must not depend on the unit.

    The same fares in paise rather than rupees must flag the same rows — if the
    fence were not equivariant, the outlier count would change with a currency
    or component redefinition and nobody would connect the two.
    """
    lo, hi = _fence(values, k)
    lo_s, hi_s = _fence([v * scale for v in values], k)

    assert rel_close(lo_s, lo * scale, tol=1e-6)
    assert rel_close(hi_s, hi * scale, tol=1e-6)


@given(st.lists(FARES, min_size=4, max_size=100), st.floats(min_value=0.5, max_value=3.0))
def test_a_wider_fence_never_flags_more(values, k):
    """Monotone in k, so the tuning knob turns the way its name implies."""
    lo_tight, hi_tight = _fence(values, k)
    lo_wide, hi_wide = _fence(values, k + 1.0)

    assert lo_wide <= lo_tight + 1e-9
    assert hi_wide >= hi_tight - 1e-9


@given(st.floats(min_value=1.0, max_value=1e6), st.integers(min_value=4, max_value=50))
def test_a_constant_series_has_a_degenerate_fence(value, n):
    """
    Zero IQR: every point sits on both edges.

    The fence must not then flag everything (or nothing-but-crash). A carrier
    with one fixed fare on a route is not 100% anomalous.
    """
    lo, hi = _fence([value] * n, 1.5)
    assert lo <= value <= hi


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────
@given(st.integers(min_value=1, max_value=10_000_000))
def test_a_rendered_price_parses_back_to_itself(rupees):
    """
    Round trip through the formats a site actually renders, including the
    Indian digit grouping (1,23,456) that a naive thousands-separator parser
    gets wrong.
    """
    plain = f"{rupees}"
    western = f"{rupees:,}"
    indian = _indian_grouping(rupees)

    for rendered in (plain, western, indian, f"₹ {indian}", f"INR {western}",
                     f"Rs. {plain}.00"):
        assert parse_inr(rendered) == pytest.approx(float(rupees)), rendered


def _indian_grouping(n: int) -> str:
    """1234567 -> '12,34,567'. Last three digits, then pairs."""
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


@given(st.text(max_size=40).filter(lambda s: not any(c.isdigit() for c in s)))
def test_text_without_digits_is_never_a_fare(junk):
    """
    No digits, no number — never a 0.0.

    A parser that returned 0.0 for "Sold out" would inject a free flight into
    the index, and a geometric mean containing a zero is not merely wrong, it
    is undefined.
    """
    assert parse_inr(junk) is None


@hyp_settings(suppress_health_check=[HealthCheck.filter_too_much])
@given(st.text(max_size=200))
def test_block_detection_never_raises_on_arbitrary_input(page):
    """
    It is fed whatever a site returns, including truncated HTML and binary
    fragments. A crash here would turn a block — the moment we most need to
    record honestly — into a stack trace.
    """
    assert looks_blocked(page) in (True, False)
