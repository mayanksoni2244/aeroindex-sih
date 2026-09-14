"""
Idempotency — spec §2.5.

Re-running a cycle must be a no-op, not a doubling. This is not housekeeping:
a demo gets re-run, a cron job overlaps itself, an operator presses the button
twice. If any of those inflates the panel, every index point built afterwards
is wrong, and wrong in the flattering direction — more observations than were
actually collected.

The property asserted throughout: **the database after N runs equals the
database after 1 run**, for scrape, clean and index alike.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from apix.clean.pipeline import clean_cycle
from apix.db.models import FareQuote, IndexValue, ScrapeRun, SourceType
from apix.db.session import session_scope
from apix.index.construct import build_series
from apix.scrape.runner import count_quotes, dedupe, run_cycle, upsert_quotes
from apix.scrape.simulate import simulate_cycle, simulate_quote
from tests.conftest import ANCHOR, fetch, make_quote, store

DAY0 = ANCHOR
DAY1 = ANCHOR + timedelta(days=1)


def _count(model) -> int:
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


def _index_points() -> dict:
    with session_scope() as s:
        return {
            (r.index_date, r.series, r.measure, r.frequency): r.index_value
            for r in s.scalars(select(IndexValue)).all()
        }


def _snapshot() -> dict:
    """Everything about the quotes table that a re-run must not change."""
    return {
        r.dedupe_hash: (r.total_fare, r.base_fare, r.source, r.source_type, r.status,
                r.is_outlier, r.is_high_demand_outlier, r.is_imputed)
        for r in fetch()
    }


# ─────────────────────────────────────────────────────────────────────────────
# The dedupe key
# ─────────────────────────────────────────────────────────────────────────────
def test_the_dedupe_key_identifies_an_itinerary_cell_not_a_source():
    """
    §3.1: two sources quoting the same flight on the same day are ONE
    observation. If `source` were part of the key, an OTA reselling a carrier's
    seat would count as a second data point, and the panel would inflate with
    resellers rather than with coverage.
    """
    direct = make_quote(source="Simulator:6E")
    reseller = make_quote(source="Simulator:MakeMyTrip", fee=250.0)
    assert direct.dedupe_hash() == reseller.dedupe_hash()


@pytest.mark.parametrize("field,value", [
    ("route", "DEL-BLR"),
    ("carrier", "UK"),
    ("cycle", DAY1),
])
def test_a_different_cell_gets_a_different_key(field, value):
    baseline = {"route": "DEL-BOM", "carrier": "6E", "cycle": DAY0}
    variant = dict(baseline, **{field: value})
    assert make_quote(**variant).dedupe_hash() != make_quote(**baseline).dedupe_hash()


def test_the_key_is_stable_across_processes():
    """
    sha256 of a joined string, not `hash()`.

    Python's builtin `hash` is salted per process, so a key built on it would
    silently stop matching after a restart and every re-run would duplicate.
    """
    q = make_quote()
    assert q.dedupe_hash() == make_quote().dedupe_hash()
    assert len(q.dedupe_hash()) == 64


def test_dedupe_keeps_the_most_itemised_record():
    """
    §3.1 tie-break: the record that breaks out taxes/UDF/fees wins.

    Collapsing to `total_fare` only would cost us the Core APIx, which is the
    entire RBI-facing half of the deliverable.
    """
    itemised = make_quote(base=5000, taxes=500, udf=150, fee=100, source="Simulator:6E")
    total_only = make_quote(base=5750, taxes=0, udf=0, fee=0, source="Simulator:X")
    assert itemised.dedupe_hash() == total_only.dedupe_hash()

    for order in ([itemised, total_only], [total_only, itemised]):
        kept, collapsed = dedupe(order)
        assert collapsed == 1
        assert kept[0].completeness() == 4, "the itemised record must win either way"


def test_dedupe_over_distinct_cells_collapses_nothing():
    quotes = [make_quote(route=r, carrier=c)
              for r in ("DEL-BOM", "DEL-BLR") for c in ("6E", "UK", "QP")]
    kept, collapsed = dedupe(quotes)
    assert (len(kept), collapsed) == (6, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Upsert
# ─────────────────────────────────────────────────────────────────────────────
def test_writing_the_same_quote_three_times_writes_one_row():
    q = make_quote(base=5000)
    for _ in range(3):
        store([q])
    assert _count(FareQuote) == 1


def test_a_rerun_updates_the_fare_in_place():
    """
    Same cell, new observation: the row is corrected, not appended.

    An append would let the index see two different prices for one cell in one
    cycle, and the median would land between a stale fare and a current one.
    """
    store([make_quote(base=5000)])
    store([make_quote(base=5400)])

    rows = fetch()
    assert len(rows) == 1
    assert rows[0].base_fare == pytest.approx(5400.0)


def test_an_empty_write_is_harmless():
    with session_scope() as s:
        assert upsert_quotes(s, [], {}) == 0
    assert _count(FareQuote) == 0


def test_a_large_batch_crosses_the_chunk_boundary():
    """
    The upsert chunks at 500 rows. A batch spanning chunks must still be
    exactly idempotent — an off-by-one there would duplicate a row per chunk
    and be nearly invisible in a 6,000-row panel.
    """
    quotes = [
        make_quote(route="DEL-BOM", carrier=f"C{i:03d}", cycle=DAY0, base=5000 + i)
        for i in range(1100)
    ]
    store(quotes)
    first = _count(FareQuote)
    store(quotes)

    assert first == 1100
    assert _count(FareQuote) == 1100


# ─────────────────────────────────────────────────────────────────────────────
# Whole cycles
# ─────────────────────────────────────────────────────────────────────────────
def test_running_a_cycle_twice_leaves_one_panel():
    a = run_cycle(DAY0, live=False)
    after_first = _count(FareQuote)
    b = run_cycle(DAY0, live=False)

    assert after_first > 0, "the fixture produced no quotes"
    assert _count(FareQuote) == after_first, "a second identical cycle added rows"
    assert a.quotes_total == b.quotes_total
    assert a.rows_written == b.rows_written


def test_the_simulator_is_deterministic():
    """
    Constitution §5. Two calls must produce byte-identical fares, or the
    backtest is not reproducible and neither is anything built on it.
    """
    first, tally_a = simulate_cycle(DAY0)
    second, tally_b = simulate_cycle(DAY0)

    assert tally_a == tally_b
    for a, b in zip(first, second, strict=True):
        assert (a.dedupe_hash(), a.total_fare, a.base_fare, a.taxes) == (
            b.dedupe_hash(), b.total_fare, b.base_fare, b.taxes
        )


def test_simulated_fares_do_not_depend_on_generation_order():
    """
    Each quote seeds its own RNG from its own identity, so pulling one cell out
    on its own reproduces exactly what the full cycle produced for it. A shared
    RNG stream would make every fare depend on how many were drawn before it.
    """
    from apix.config.loader import load_routes

    cfg = load_routes()
    meta = cfg.by_route("DEL-BOM")
    carrier = cfg.carriers[0]

    alone = simulate_quote(meta, carrier.code, carrier.price_multiplier, 30, DAY0,
                         f"Simulator:{carrier.code}", is_ota=False)
    in_cycle = next(
        q for q in simulate_cycle(DAY0)[0]
        if q.dedupe_hash() == alone.dedupe_hash() and q.source == alone.source
    )
    assert alone.total_fare == pytest.approx(in_cycle.total_fare)


def test_each_run_is_ledgered_even_though_rows_are_not_duplicated():
    """
    Row idempotency is not run amnesia.

    Two runs happened; `scrape_runs` records two. Collapsing them would hide
    that the cycle was re-collected, and the audit trail is the point of the
    table.
    """
    run_cycle(DAY0, live=False)
    run_cycle(DAY0, live=False)

    with session_scope() as s:
        runs = s.scalars(select(ScrapeRun)).all()
        assert len(runs) == 2
        assert all(r.source_type is SourceType.SIMULATED for r in runs)
        assert all(r.status_counts for r in runs), "a run with no histogram is silence"


def test_clean_then_scrape_again_does_not_change_the_cycle(basket):
    """
    The full realistic sequence: scrape, clean, scrape again, clean again.

    The end state must match a single scrape-then-clean. This is the path an
    operator actually takes when a cycle looked wrong and they re-ran it.
    """
    run_cycle(DAY0, live=False)
    clean_cycle(DAY0)
    once = _snapshot()

    run_cycle(DAY0, live=False)
    clean_cycle(DAY0)

    assert once == _snapshot(), "re-collecting and re-cleaning changed the cycle"


def test_rebuilding_the_index_replaces_points_rather_than_stacking_them(basket):
    """
    Four series per cycle, however many times you build.

    Stacked duplicates would not just clutter the table — /v1/index would serve
    two values for one date and the chart would draw a vertical line.
    """
    for route in ("DEL-BOM", "DEL-BLR", "BOM-BLR"):
        store([make_quote(route=route, carrier="6E", cycle=DAY0, base=5000)])
        store([make_quote(route=route, carrier="6E", cycle=DAY1, base=5300)])

    build_series()
    first, count_first = _index_points(), _count(IndexValue)

    build_series()
    build_series()

    assert count_first == _count(IndexValue) == 2 * 4, "2 cycles x 4 series"
    assert first == _index_points(), "a rebuild changed a published value"


def test_provenance_counts_survive_a_rerun(basket):
    """
    The headcount the CLI and /v1/coverage both print must not drift on re-run.

    One validation run, one number, every surface — so it has to be stable
    under exactly the operation an operator is most likely to repeat.
    """
    run_cycle(DAY0, live=False)
    clean_cycle(DAY0)
    before = count_quotes(DAY0)

    run_cycle(DAY0, live=False)
    clean_cycle(DAY0)
    after = count_quotes(DAY0)

    assert before == after
    assert sum(after.values()) == _count(FareQuote), (
        "the three provenance buckets must partition the table exactly"
    )
