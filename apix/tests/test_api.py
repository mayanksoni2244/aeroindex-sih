"""
API contract.

Consumers here are named institutions (NSO, RBI), and the contract they rely on
is not just field names — it is that the numbers arrive with their provenance
attached, that a caveat cannot be dropped in transit, and that an empty result
looks empty rather than looking like zero.

Four things are pinned:
  1. auth — the gate is on the data endpoints and off /v1/health;
  2. shape — every documented field is present and typed as documented;
  3. provenance — no index number is served without a lineage, at any level;
  4. honesty under emptiness — no invented 100.0, no silent defaults.
"""
from __future__ import annotations

import csv
import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from apix.api.main import API_VERSION, app
from apix.clean.pipeline import clean_cycle
from apix.config.settings import settings
from apix.db.models import DgcaMonthlyAvg, FareQuote, Measure, Series, SourceType
from apix.db.session import session_scope
from apix.index.backtest import PLACEHOLDER_MARKER
from apix.index.construct import build_series
from tests.conftest import ANCHOR, make_quote, store

DAY0 = ANCHOR
DAY1 = ANCHOR + timedelta(days=1)
AUTH = {"X-API-Key": settings.api_key}

ROUTES = ("DEL-BOM", "DEL-BLR", "BOM-BLR")

#: Every endpoint behind the API key, with a minimal valid query.
DATA_ENDPOINTS = (
    "/v1/index",
    "/v1/index.csv",
    "/v1/routes",
    "/v1/coverage",
    "/v1/backtest",
    "/v1/heatmap",
    "/v1/routes/DEL-BOM/curve",
)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def populated(basket):
    """Two cycles across three routes, cleaned and indexed — a minimal panel."""
    for route in ROUTES:
        store([make_quote(route=route, carrier="6E", cycle=DAY0, base=5000)])
        store([make_quote(route=route, carrier="UK", cycle=DAY0, base=5400)])
        store([make_quote(route=route, carrier="6E", cycle=DAY1, base=5300)])
        store([make_quote(route=route, carrier="UK", cycle=DAY1, base=5700)])
    clean_cycle(DAY0)
    clean_cycle(DAY1)
    build_series()
    return basket


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", DATA_ENDPOINTS)
def test_data_endpoints_require_a_key(client, path, populated):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get(path, headers=AUTH).status_code == 200


def test_health_is_deliberately_unauthenticated(client):
    """
    An operator has to be able to ask "what is this instance serving?" without
    a credential. The answer contains no fare data — only a self-description.
    """
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    assert resp.json()["version"] == API_VERSION


def test_health_warns_loudly_when_the_instance_holds_no_live_data(client, populated):
    """
    The fixture is 100% simulated, so the warning must fire.

    This is the guard against a demo instance being mistaken for a live one —
    the single most likely way this project could mislead someone.
    """
    body = client.get("/v1/health").json()

    assert body["live_pct_all_time"] == 0.0
    assert body["quote_count"] > 0
    assert any("NO live observations" in w for w in body["warnings"])
    assert any("not a measurement of the market" in w for w in body["warnings"])


def test_the_key_check_is_not_a_prefix_match(client, populated):
    """A constant-time full-string compare, so a truncated key is rejected."""
    truncated = settings.api_key[:-1]
    assert client.get("/v1/index", headers={"X-API-Key": truncated}).status_code == 401
    extended = settings.api_key + "x"
    assert client.get("/v1/index", headers={"X-API-Key": extended}).status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# /v1/index
# ─────────────────────────────────────────────────────────────────────────────
def test_index_response_shape(client, populated):
    body = client.get("/v1/index", headers=AUTH).json()

    assert body["series"] == "headline"
    assert body["measure"] == "total"
    assert body["frequency"] == "daily"
    assert body["count"] == len(body["points"]) == 2
    assert body["base_period"] == DAY0.isoformat()
    assert body["basket_version"]

    point = body["points"][0]
    for field in ("index_date", "frequency", "series", "measure", "index_value",
                  "base_period", "base_value", "basket_version", "lineage"):
        assert field in point, f"/v1/index dropped {field!r}"
    assert point["base_value"] == 100.0


@pytest.mark.parametrize("series", [s.value for s in Series])
@pytest.mark.parametrize("measure", [m.value for m in Measure])
def test_all_four_series_are_published(client, populated, series, measure):
    """
    The dual-audience claim is two axes, not one: headline/core for outlier
    treatment, total/base for tax inclusion. All four combinations must be
    independently retrievable or the RBI-facing half does not exist.
    """
    resp = client.get("/v1/index", params={"series": series, "measure": measure},
                      headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert (body["series"], body["measure"]) == (series, measure)
    assert body["count"] > 0
    assert all(p["series"] == series and p["measure"] == measure
               for p in body["points"])


def test_the_base_period_point_is_exactly_one_hundred(client, populated):
    body = client.get("/v1/index", headers=AUTH).json()
    first = body["points"][0]
    assert first["index_date"] == first["base_period"]
    assert first["index_value"] == pytest.approx(100.0)


def test_a_date_filter_narrows_without_rebasing(client, populated):
    """
    Filtering is a view, not a re-computation.

    If asking for a sub-window silently re-based the series, two consumers
    querying different windows would get different numbers for the same day —
    and both would think they had the index.
    """
    full = client.get("/v1/index", headers=AUTH).json()
    narrowed = client.get("/v1/index", params={"start": DAY1.isoformat()},
                          headers=AUTH).json()

    assert narrowed["count"] == 1
    assert narrowed["base_period"] == full["base_period"]
    assert narrowed["points"][0]["index_value"] == pytest.approx(
        full["points"][-1]["index_value"]
    )


def test_an_empty_window_returns_an_empty_series_not_a_zero(client, populated):
    """
    No data is not a value. A 0.0 or a 100.0 here would plot as a real point.
    """
    far = (DAY1 + timedelta(days=400)).isoformat()
    body = client.get("/v1/index", params={"start": far}, headers=AUTH).json()

    assert body["count"] == 0
    assert body["points"] == []
    assert body["base_period"] is None
    assert body["provenance"]["quote_count"] == 0
    assert body["provenance"]["label"] == "NO DATA", (
        "'0% live / 0% simulated' reads as a measured split of nothing"
    )


def test_an_unknown_series_is_rejected_not_defaulted(client, populated):
    """
    422, not a silent fall back to headline. Serving core numbers labelled
    headline — or vice versa — is the one confusion this project cannot afford.
    """
    assert client.get("/v1/index", params={"series": "hedonic"},
                      headers=AUTH).status_code == 422
    assert client.get("/v1/index", params={"measure": "net"},
                      headers=AUTH).status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# Provenance travels with every number
# ─────────────────────────────────────────────────────────────────────────────
def test_window_provenance_is_recomputed_from_counts_not_averaged(client, populated):
    """
    The window block must equal the sum of its points, exactly.

    Averaging the per-point percentages would be subtly wrong whenever points
    differ in size — the direction of the error being unpredictable, which is
    worse than a bias.
    """
    body = client.get("/v1/index", headers=AUTH).json()
    prov = body["provenance"]

    assert prov["live_count"] == sum(p["lineage"]["live_count"] for p in body["points"])
    assert prov["simulated_count"] == sum(
        p["lineage"]["simulated_count"] for p in body["points"]
    )
    assert prov["imputed_count"] == sum(
        p["lineage"]["imputed_count"] for p in body["points"]
    )
    assert prov["quote_count"] == (
        prov["live_count"] + prov["simulated_count"] + prov["imputed_count"]
    )
    assert prov["live_pct"] + prov["simulated_pct"] + prov["imputed_pct"] == (
        pytest.approx(100.0, abs=0.05)
    )


def test_the_window_badge_is_never_blank(client, populated):
    """The dashboard renders this string verbatim in a persistent badge."""
    prov = client.get("/v1/index", headers=AUTH).json()["provenance"]
    assert prov["label"] == "SIMULATED DATA"
    assert prov["is_fully_simulated"] is True


@pytest.mark.parametrize("path", ["/v1/heatmap", "/v1/routes/DEL-BOM/curve"])
def test_derived_endpoints_carry_provenance_too(client, populated, path):
    """
    A heatmap cell and an elasticity curve are fare numbers as much as the
    index is. Shipping either without its split would let a screenshot of the
    dashboard escape with no label on it.
    """
    prov = client.get(path, headers=AUTH).json()["provenance"]
    assert prov["quote_count"] > 0
    assert prov["label"]


#: The keys every provenance block must carry, whatever computed it.
PROVENANCE_CORE = {
    "quote_count", "live_count", "simulated_count", "imputed_count",
    "live_pct", "simulated_pct", "imputed_pct", "is_fully_simulated", "label",
}


@pytest.mark.parametrize("path", ["/v1/index", "/v1/heatmap",
                                  "/v1/routes/DEL-BOM/curve"])
def test_every_provenance_block_has_the_same_shape(client, populated, path):
    """
    One block, one shape, wherever it is computed from.

    These three endpoints derive provenance from different tables — index
    lineage in one case, raw fare rows in the other two. A consumer badging a
    chart should not have to know which, and a block missing `label` on one tab
    is how an unlabelled screenshot gets out.
    """
    prov = client.get(path, headers=AUTH).json()["provenance"]

    assert PROVENANCE_CORE <= set(prov), (
        f"{path} provenance is missing {PROVENANCE_CORE - set(prov)}"
    )
    assert prov["quote_count"] == (
        prov["live_count"] + prov["simulated_count"] + prov["imputed_count"]
    ), "the three buckets must partition the rows exactly"
    assert prov["live_pct"] + prov["simulated_pct"] + prov["imputed_pct"] == (
        pytest.approx(100.0, abs=0.05)
    )


def test_the_badge_string_has_exactly_one_definition():
    """
    Static: the label is built in one place.

    The four endpoints that publish provenance each used to build this dict by
    hand, and three of the four had quietly dropped `label`. Counting the
    literal keeps that from happening again — a second occurrence means someone
    has started a fifth copy.
    """
    import ast
    from pathlib import Path

    source = Path(__import__("apix.api.main", fromlist=["__file__"]).__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    hits = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value == "SIMULATED DATA"
    ]
    assert len(hits) == 1, (
        f"'SIMULATED DATA' is built in {len(hits)} places (lines {hits}); "
        "route every provenance block through _provenance_block instead"
    )


# ─────────────────────────────────────────────────────────────────────────────
# CSV export
# ─────────────────────────────────────────────────────────────────────────────
def test_csv_export_is_parseable_and_self_describing(client, populated):
    resp = client.get("/v1/index.csv", headers=AUTH)
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]

    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 2
    for row in rows:
        assert row["series"] == "headline"
        assert float(row["index_value"]) > 0
        # A spreadsheet detached from this API must still answer "how real?"
        assert float(row["live_pct"]) + float(row["simulated_pct"]) + float(
            row["imputed_pct"]
        ) == pytest.approx(100.0, abs=0.05)
        assert row["sources"]


def test_csv_has_a_filename_so_a_download_is_identifiable(client, populated):
    resp = client.get("/v1/index.csv", params={"series": "core", "measure": "base"},
                      headers=AUTH)
    disposition = resp.headers.get("content-disposition", "")
    assert "attachment" in disposition
    assert "core" in disposition and "base" in disposition, (
        "an unlabelled apix.csv on someone's desktop is a series nobody can identify"
    )


# ─────────────────────────────────────────────────────────────────────────────
# /v1/routes and /v1/coverage
# ─────────────────────────────────────────────────────────────────────────────
def test_routes_expose_the_weight_arithmetic(client, populated):
    """
    The weights are the DGCA shares renormalised. Both numbers are published so
    a reviewer can redo the division rather than trust it.
    """
    body = client.get("/v1/routes", headers=AUTH).json()

    assert body["weights_sum"] == pytest.approx(1.0, abs=1e-4)
    assert body["weight_source"], "the provenance of the weights must be named"
    assert body["basket_version"]

    in_basket = [r for r in body["routes"] if r["in_basket"]]
    assert in_basket
    for r in in_basket:
        assert r["national_share_pct"] is not None
        assert r["weight"] is not None


def test_coverage_reports_what_was_filled_in(client, populated):
    body = client.get("/v1/coverage", headers=AUTH).json()

    assert body["cycle_date"] == DAY1.isoformat(), "defaults to the latest cycle"
    assert body["quotes_total"] > 0
    assert body["live_count"] + body["simulated_count"] + body["imputed_count"] == (
        body["quotes_total"]
    )
    assert body["live_pct"] + body["simulated_pct"] + body["imputed_pct"] == (
        pytest.approx(100.0, abs=0.05)
    )
    for field in ("validation_rejects", "outliers", "high_demand_outliers",
                  "routes_covered", "routes_missing", "status_counts", "recent_runs"):
        assert field in body, f"/v1/coverage dropped {field!r}"


def test_coverage_agrees_with_the_cli_headcount(client, populated):
    """
    Constitution: one number, used everywhere.

    `apix status` prints its footer from `count_quotes`; /v1/coverage computes
    its own. These two disagreeing about the same cycle is a bug class this
    project has already hit once, so it has a test.
    """
    from apix.scrape.runner import count_quotes

    body = client.get("/v1/coverage", params={"cycle_date": DAY1.isoformat()},
                      headers=AUTH).json()
    cli = count_quotes(DAY1)

    assert (body["live_count"], body["simulated_count"], body["imputed_count"]) == (
        cli["live"], cli["simulated"], cli["imputed"]
    )


# ─────────────────────────────────────────────────────────────────────────────
# /v1/backtest — the caveat must survive the wire
# ─────────────────────────────────────────────────────────────────────────────
def test_backtest_reports_unreportable_rather_than_hiding_the_result(client, populated):
    """
    A backtest on two months of illustrative reference data still returns —
    but `reportable` is false and the notes say why.

    Suppressing it would invite someone to compute it by hand and quote it
    without the caveat; returning it bare would be worse.
    """
    month = f"{ANCHOR.year}-{ANCHOR.month:02d}"
    with session_scope() as s:
        for route in ROUTES:
            s.add(DgcaMonthlyAvg(
                route=route, month=month, avg_fare=6000.0,
                source_note=f"{PLACEHOLDER_MARKER}: illustrative",
            ))

    body = client.get("/v1/backtest", headers=AUTH).json()

    assert body["uses_placeholder_reference"] is True
    assert body["reportable"] is False
    assert body["notes"], "an unreportable result must explain itself"
    assert any(PLACEHOLDER_MARKER in n for n in body["notes"])


def test_backtest_without_reference_data_is_honest_about_it(client, populated):
    body = client.get("/v1/backtest", headers=AUTH).json()

    assert body["months_compared"] == 0
    assert body["pearson_r"] is None, "no overlap must not become a correlation"
    assert body["sufficient"] is False
    assert body["reportable"] is False


# ─────────────────────────────────────────────────────────────────────────────
# /v1/heatmap and the elasticity curve
# ─────────────────────────────────────────────────────────────────────────────
def test_heatmap_marks_an_unmatched_cell_null_not_zero(client, populated):
    """
    `pct_change_vs_base` is null when the base period has no matching cell.

    0.0 would render as "no change" in the same colour as a genuinely flat
    cell — an unknown painted as a measurement.
    """
    body = client.get("/v1/heatmap", headers=AUTH).json()
    assert body["base_period"] == DAY0.isoformat()
    assert body["cells"]

    store([make_quote(route="BLR-HYD", carrier="6E", cycle=DAY1, base=4000)])
    fresh = client.get("/v1/heatmap", headers=AUTH).json()
    entrant = [c for c in fresh["cells"] if c["route"] == "BLR-HYD"]
    assert entrant, "the new cell should appear in the current period"
    assert entrant[0]["pct_change_vs_base"] is None


def test_every_heatmap_cell_carries_its_own_provenance(client, populated):
    """
    The grid is the fastest way to read coverage, so each square must say what
    it is rather than borrowing the panel's average.

    A dashboard that badges only the panel shows "20% live" over thirty
    identical-looking squares, and a reader cannot tell which six are real.
    """
    body = client.get("/v1/heatmap", headers=AUTH).json()
    cells = body["cells"]
    assert cells

    for c in cells:
        assert c["source_type"] in ("live", "live_limited", "imputed", "simulated")
        assert c["is_live"] is (c["source_type"] in ("live", "live_limited"))

    # `populated` is entirely simulated, and the cells must say so rather than
    # defaulting to something friendlier.
    assert {c["source_type"] for c in cells} == {"simulated"}


def test_a_live_cell_and_a_simulated_cell_are_distinguishable(client, populated):
    """One real observation must be visibly real, cell by cell."""
    store([make_quote(
        route="DEL-CCU", carrier="QP", cycle=DAY1, base=4200,
        source_type=SourceType.LIVE, source="Akasa Air",
    )])
    cells = client.get("/v1/heatmap", headers=AUTH).json()["cells"]

    live = [c for c in cells if c["route"] == "DEL-CCU"]
    assert live and live[0]["source_type"] == "live" and live[0]["is_live"] is True
    assert all(
        c["is_live"] is False for c in cells if c["route"] in ROUTES
    ), "a simulated cell must not inherit the live badge from its neighbour"


def test_a_mixed_cell_reports_the_weakest_tier_present(client, populated):
    """
    Rounding a half-filled cell up to "live" is the exact overstatement this
    project exists to avoid, so a mixed cell reads as simulated.
    """
    store([
        make_quote(route="DEL-CCU", carrier="QP", cycle=DAY1, base=4200,
                   source_type=SourceType.LIVE, source="Akasa Air"),
        make_quote(route="DEL-CCU", carrier="6E", cycle=DAY1, base=4600),
    ])
    cells = client.get("/v1/heatmap", headers=AUTH).json()["cells"]
    cell = [c for c in cells if c["route"] == "DEL-CCU"][0]

    assert cell["n"] == 2
    assert cell["source_type"] == "simulated"
    assert cell["is_live"] is False


def test_coverage_reports_all_four_provenance_buckets(client, populated):
    """
    The four counts partition the cycle. If one is missing from the response,
    the panel's own numbers stop adding up to its own total.
    """
    body = client.get("/v1/coverage", headers=AUTH).json()
    counts = ("live_count", "live_limited_count", "simulated_count", "imputed_count")
    for key in counts:
        assert key in body, f"{key} missing from /v1/coverage"
    assert sum(body[k] for k in counts) == body["quotes_total"]
    assert (
        body["live_pct"] + body["live_limited_pct"]
        + body["simulated_pct"] + body["imputed_pct"]
    ) == pytest.approx(100.0, abs=0.05)


def test_routes_live_counts_only_real_observations_not_simulated_cover(
    client, populated
):
    """
    `routes_covered` and `routes_live` answer two different questions, and the
    dashboard headline uses the second.

    `routes_covered` means "the index has a cell here", which a simulated quote
    satisfies. Using it to say "N of 6 routes live" would report full live
    coverage on a wholly simulated instance — the single most damaging
    overstatement this project could make, because it is the one number a judge
    will read off the page and repeat.
    """
    body = client.get("/v1/coverage", headers=AUTH).json()
    assert body["routes_covered"], "fixture should cover routes"
    assert body["routes_live"] == [], (
        "an all-simulated instance has zero live routes, however many routes "
        "it has quotes for"
    )

    store([make_quote(
        route="DEL-CCU", carrier="QP", cycle=DAY1, base=4200,
        source_type=SourceType.LIVE, source="Akasa Air",
    )])
    after = client.get("/v1/coverage", headers=AUTH).json()
    assert after["routes_live"] == ["DEL-CCU"]
    # And it stays a subset of what is covered, or the panel could claim a
    # route is live that the index has no cell for.
    assert set(after["routes_live"]) <= set(after["routes_covered"])


def test_an_imputed_row_does_not_make_its_route_read_as_live(client, populated):
    """
    A carried-forward value is a copy of an older observation, not a new one.
    Counting it as live would let coverage drift upward on a day the collector
    returned nothing at all.
    """
    from sqlalchemy import update

    store([make_quote(
        route="DEL-CCU", carrier="QP", cycle=DAY1, base=4200,
        source_type=SourceType.LIVE, source="Akasa Air",
    )])
    # Flag it the way the clean stage would: the provenance tag of an imputed
    # cell keeps the tier it was copied from, which is exactly why `is_imputed`
    # has to be consulted separately.
    with session_scope() as s:
        s.execute(
            update(FareQuote)
            .where(FareQuote.route == "DEL-CCU", FareQuote.cycle_date == DAY1)
            .values(is_imputed=True)
        )

    body = client.get("/v1/coverage", headers=AUTH).json()
    assert "DEL-CCU" not in body["routes_live"]


def test_the_base_period_heatmap_is_all_zeroes(client, populated):
    """Sanity: the base period compared against itself has not moved."""
    body = client.get("/v1/heatmap", params={"cycle_date": DAY0.isoformat()},
                      headers=AUTH).json()
    changes = [c["pct_change_vs_base"] for c in body["cells"]]
    assert changes
    assert all(c == pytest.approx(0.0) for c in changes)


def test_the_elasticity_curve_is_anchored_at_t30(client, populated):
    body = client.get("/v1/routes/DEL-BOM/curve", headers=AUTH).json()

    assert body["route"] == "DEL-BOM"
    assert body["points"]
    anchor = [p for p in body["points"] if p["days"] == 30]
    if anchor:
        assert anchor[0]["ratio_to_t30"] == pytest.approx(1.0)


def test_an_unknown_route_is_a_404_not_an_empty_curve(client, populated):
    resp = client.get("/v1/routes/XXX-YYY/curve", headers=AUTH)
    assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Documentation
# ─────────────────────────────────────────────────────────────────────────────
def test_the_openapi_schema_is_served_and_documents_every_endpoint(client):
    """
    An institutional consumer integrates against the schema, not against a
    README. If an endpoint is not in here, it does not exist for them.
    """
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    for path in ("/v1/health", "/v1/index", "/v1/index.csv", "/v1/routes",
                 "/v1/coverage", "/v1/backtest", "/v1/heatmap"):
        assert path in paths, f"{path} is undocumented"
    assert schema["info"]["version"] == API_VERSION


def test_the_api_is_read_only(client, populated):
    """
    Nothing here mutates. An index a consumer can POST to is not a published
    statistic, and the collection path deliberately lives in the CLI.
    """
    schema = client.get("/openapi.json").json()
    for path, ops in schema["paths"].items():
        assert set(ops) <= {"get"}, f"{path} exposes {set(ops) - {'get'}}"
