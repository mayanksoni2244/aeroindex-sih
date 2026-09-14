"""
Tests for Live Index isolation and Reference Point Validation.

Enforces:
  1. Live basket contains exactly the 4 non-stop routes operated by Akasa Air.
  2. Renormalised DGCA weights sum to exactly 1.0.
  3. Live Index mode in API and CSV contains 0 simulated or imputed quotes (100% real).
  4. Reference Point Validation validates against Ixigo benchmarks with no circular synthetic data.
"""
from __future__ import annotations

from datetime import date
import pytest
from fastapi.testclient import TestClient

from apix.api.main import app
from apix.clean.pipeline import clean_cycle
from apix.config.loader import load_live_basket
from apix.db.models import SourceType
from apix.index.construct import build_series
from apix.index.reference_validation import run_reference_validation
from scripts.seed_external_benchmark import seed_external_benchmarks
from tests.conftest import ANCHOR, make_quote, store


def test_live_basket_definition_and_weights():
    """Live basket must have exactly 4 routes and weights summing to 1.0."""
    basket = load_live_basket()
    assert basket.basket_version == "v1-akasa-live-4route"
    assert {r.route for r in basket.routes} == {"DEL-BOM", "DEL-BLR", "BOM-BLR", "DEL-CCU"}
    total_w = sum(r.weight for r in basket.routes)
    assert abs(total_w - 1.0) < 1e-4
    for route in ["DEL-BOM", "DEL-BLR", "BOM-BLR", "DEL-CCU"]:
        assert basket.weight_of(route) > 0


def test_live_index_zero_simulated_isolation(basket):
    """Live index must contain 100% real data and 0% simulated data."""
    cycle = ANCHOR
    # Insert live quotes for the 4 Akasa routes
    quotes = [
        make_quote(
            route=route,
            carrier="QP",
            source_type=SourceType.LIVE,
            source="Akasa:Test",
            cycle=cycle,
        )
        for route in ["DEL-BOM", "DEL-BLR", "BOM-BLR", "DEL-CCU"]
    ]
    # Insert a simulated quote for a route that should NOT leak into live index
    quotes.append(
        make_quote(
            route="MAA-DEL",
            carrier="6E",
            source_type=SourceType.SIMULATED,
            source="Simulator:6E",
            cycle=cycle,
        )
    )

    store(quotes)
    clean_cycle(cycle)

    points = build_series(
        start=cycle,
        end=cycle,
        only_live=True,
    )
    assert len(points) == 4  # 2 series (headline/core) * 2 measures (total/base)

    client = TestClient(app)
    headers = {"X-API-Key": "dev-nso-rbi-key"}

    # Test /v1/index with mode=live
    resp = client.get("/v1/index?mode=live&series=headline&measure=total", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["provenance"]["live_pct"] == 100.0
    assert data["provenance"]["simulated_pct"] == 0.0
    assert data["provenance"]["imputed_pct"] == 0.0
    assert data["provenance"]["is_fully_simulated"] is False
    assert data["provenance"]["live_count"] == 4
    assert data["provenance"]["simulated_count"] == 0


def test_live_csv_zero_simulated(basket):
    """CSV export in live mode must return 200 and declare 100% live provenance."""
    cycle = ANCHOR
    quotes = [
        make_quote(
            route=route,
            carrier="QP",
            source_type=SourceType.LIVE,
            source="Akasa:Test",
            cycle=cycle,
        )
        for route in ["DEL-BOM", "DEL-BLR", "BOM-BLR", "DEL-CCU"]
    ]
    store(quotes)
    clean_cycle(cycle)

    build_series(
        start=cycle,
        end=cycle,
        only_live=True,
    )

    client = TestClient(app)
    headers = {"X-API-Key": "dev-nso-rbi-key"}
    resp = client.get("/v1/index.csv?mode=live&series=headline&measure=total", headers=headers)
    assert resp.status_code == 200
    lines = resp.text.strip().split("\n")
    assert len(lines) >= 2
    header = lines[0].split(",")
    assert "live_pct" in header
    assert "simulated_pct" in header
    assert "basket_version" in header
    row = lines[1].split(",")
    assert row[header.index("basket_version")] == "v1-akasa-live-4route"
    assert float(row[header.index("live_pct")]) == 100.0
    assert float(row[header.index("simulated_pct")]) == 0.0


def test_reference_validation_endpoint(basket):
    """Reference validation must return point deviations against external benchmark."""
    cycle = ANCHOR
    quotes = [
        make_quote(route="DEL-BOM", carrier="QP", base=4500.0, source_type=SourceType.LIVE, cycle=cycle),
        make_quote(route="DEL-BLR", carrier="QP", base=5000.0, source_type=SourceType.LIVE, cycle=cycle),
        make_quote(route="BOM-BLR", carrier="QP", base=3500.0, source_type=SourceType.LIVE, cycle=cycle),
        make_quote(route="DEL-CCU", carrier="QP", base=4800.0, source_type=SourceType.LIVE, cycle=cycle),
    ]
    store(quotes)
    clean_cycle(cycle)

    # Seed external benchmarks
    seed_external_benchmarks()

    res = run_reference_validation(measure="total")
    assert res.routes_compared == 4
    assert res.caption != ""
    assert res.points[0].benchmark_fare > 0
    assert res.points[0].pct_deviation is not None

    client = TestClient(app)
    headers = {"X-API-Key": "dev-nso-rbi-key"}
    resp = client.get("/v1/reference-validation?measure=total", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["routes_compared"] == 4
    assert "Ixigo" in data["caption"]
    assert data["points"][0]["pct_deviation"] is not None
