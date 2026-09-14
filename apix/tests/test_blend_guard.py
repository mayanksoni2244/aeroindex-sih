"""
The blend guard.

Constitution §1: live and simulated data may coexist, but the split must always
be visible and simulated data must never be able to overwrite an observation.
These are the tests that would fail if someone made the demo look better by
quietly presenting synthetic numbers as real ones.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from apix.db.models import Frequency, IndexValue, Measure, Series, SourceType
from apix.db.session import session_scope
from apix.index.construct import build_series, compute_point
from tests.conftest import ANCHOR, fetch, make_quote, store

DAY0 = ANCHOR
DAY1 = ANCHOR + timedelta(days=1)


def test_simulated_cannot_overwrite_a_live_row():
    """
    The database itself refuses the substitution.

    Tier-B already skips cells that live data covers, so this is the second
    line of defence: even if that skip logic were broken, the ON CONFLICT
    guard means a synthetic row cannot bury an observation.
    """
    live = make_quote(carrier="6E", cycle=DAY0, base=5000,
                      source_type=SourceType.LIVE, source="AkasaAir")
    store([live])

    sim = make_quote(carrier="6E", cycle=DAY0, base=9999,
                     source_type=SourceType.SIMULATED, source="Simulator:6E")
    assert sim.dedupe_hash() == live.dedupe_hash(), "fixture must target the same cell"
    store([sim])

    rows = fetch(DAY0)
    assert len(rows) == 1
    assert rows[0].source_type is SourceType.LIVE
    assert rows[0].source == "AkasaAir"
    assert rows[0].base_fare == pytest.approx(5000.0), (
        "a simulated fare overwrote a live observation"
    )


def test_live_may_overwrite_a_simulated_row():
    """The guard is directional: a real observation always wins."""
    store([make_quote(carrier="6E", cycle=DAY0, base=9999,
                      source_type=SourceType.SIMULATED, source="Simulator:6E")])
    store([make_quote(carrier="6E", cycle=DAY0, base=5000,
                      source_type=SourceType.LIVE, source="AkasaAir")])

    rows = fetch(DAY0)
    assert len(rows) == 1
    assert rows[0].source_type is SourceType.LIVE
    assert rows[0].base_fare == pytest.approx(5000.0)


def test_dedupe_prefers_live_over_simulated_in_memory():
    """The same preference applies before the write, during collapse."""
    from apix.scrape.runner import dedupe

    sim = make_quote(carrier="6E", cycle=DAY0, base=9999,
                     source_type=SourceType.SIMULATED, source="Simulator:6E")
    live = make_quote(carrier="6E", cycle=DAY0, base=5000,
                      source_type=SourceType.LIVE, source="AkasaAir")

    for order in ([sim, live], [live, sim]):
        kept, collapsed = dedupe(order)
        assert collapsed == 1
        assert len(kept) == 1
        assert kept[0].source_type is SourceType.LIVE, (
            "dedupe must prefer the observation regardless of input order"
        )


def test_lineage_exposes_the_split_on_a_blended_cycle(basket):
    """
    A mixed cycle must report both halves, not round to the flattering one.

    Six matched cells on DAY1: two live, four simulated -> 33.3 / 66.7.
    """
    for route in ("DEL-BOM", "DEL-BLR"):
        for carrier in ("6E", "UK", "QP"):
            store([make_quote(route=route, carrier=carrier, cycle=DAY0, base=5000)])

    for route in ("DEL-BOM", "DEL-BLR"):
        store([make_quote(route=route, carrier="6E", cycle=DAY1, base=5200,
                          source_type=SourceType.LIVE, source="AkasaAir")])
        for carrier in ("UK", "QP"):
            store([make_quote(route=route, carrier=carrier, cycle=DAY1, base=5100)])

    with session_scope() as s:
        point = compute_point(s, DAY1, DAY0, Series.HEADLINE, Measure.TOTAL,
                              Frequency.DAILY, basket)

    lin = point.lineage
    assert lin["quote_count"] == 6
    assert lin["live_count"] == 2
    assert lin["simulated_count"] == 4
    assert lin["live_pct"] == pytest.approx(33.33, abs=0.01)
    assert lin["simulated_pct"] == pytest.approx(66.67, abs=0.01)
    assert lin["live_pct"] + lin["simulated_pct"] + lin["imputed_pct"] == pytest.approx(100.0)
    assert "AkasaAir" in lin["sources"]
    assert any(src.startswith("Simulator") for src in lin["sources"])


def test_a_fully_simulated_point_says_so(basket):
    for route in ("DEL-BOM", "DEL-BLR"):
        store([make_quote(route=route, carrier="6E", cycle=DAY0)])
        store([make_quote(route=route, carrier="6E", cycle=DAY1)])

    with session_scope() as s:
        point = compute_point(s, DAY1, DAY0, Series.HEADLINE, Measure.TOTAL,
                              Frequency.DAILY, basket)

    assert point.lineage["live_pct"] == 0.0
    assert point.lineage["simulated_pct"] == pytest.approx(100.0)


def _three_route_history():
    for route in ("DEL-BOM", "DEL-BLR", "BOM-BLR"):
        store([make_quote(route=route, carrier="6E", cycle=DAY0)])
        store([make_quote(route=route, carrier="6E", cycle=DAY1, base=5300)])


def test_every_index_point_carries_a_lineage(basket):
    """
    Structural, not conventional.

    A persisted index row with an empty lineage would be a number whose
    provenance had been lost. No code path should be able to produce one.
    """
    _three_route_history()
    assert build_series()

    with session_scope() as s:
        rows = s.scalars(select(IndexValue)).all()

    assert rows
    for r in rows:
        assert r.data_lineage, f"{r.index_date} {r.series} has no lineage"
        for key in ("live_pct", "simulated_pct", "imputed_pct", "quote_count",
                    "routes_covered", "sources"):
            assert key in r.data_lineage, f"lineage missing {key!r}"
        pcts = (r.data_lineage["live_pct"] + r.data_lineage["simulated_pct"]
                + r.data_lineage["imputed_pct"])
        assert pcts == pytest.approx(100.0, abs=0.05)


def test_api_never_serves_a_value_without_a_lineage(basket):
    """The schema makes the omission unrepresentable — assert it end to end."""
    from fastapi.testclient import TestClient

    from apix.api.main import app
    from apix.config.settings import settings

    _three_route_history()
    build_series()

    client = TestClient(app)
    body = client.get("/v1/index", headers={"X-API-Key": settings.api_key}).json()

    assert body["points"]
    for p in body["points"]:
        assert p["lineage"]["quote_count"] > 0
        total = (p["lineage"]["live_pct"] + p["lineage"]["simulated_pct"]
                 + p["lineage"]["imputed_pct"])
        assert total == pytest.approx(100.0, abs=0.05)
    assert body["provenance"]["label"], "the window badge must never be blank"
    assert body["provenance"]["is_fully_simulated"] is True


def test_csv_export_carries_provenance_columns(basket):
    """
    A spreadsheet detached from this API must still answer "how much was real?"

    So the provenance travels in the file, not in a separate download nobody
    keeps.
    """
    from fastapi.testclient import TestClient

    from apix.api.main import app
    from apix.config.settings import settings

    _three_route_history()
    build_series()

    client = TestClient(app)
    text = client.get("/v1/index.csv", headers={"X-API-Key": settings.api_key}).text
    header = text.splitlines()[0].split(",")
    for col in ("live_pct", "simulated_pct", "imputed_pct", "quote_count", "sources"):
        assert col in header, f"CSV export dropped the {col!r} column"
