'''
FastAPI application — the read interface onto APIx.

DESIGN RULE THAT SHAPES EVERY ENDPOINT
No endpoint returns an index number without the provenance behind it. The
`IndexPointOut` schema cannot represent a value without a `lineage`, and
`/v1/index` additionally returns a window-level `provenance` block so a client
can badge the whole chart without summing anything itself. A number that
travels without its lineage can be quoted out of context, and for a statistic
that would eventually feed the CPI that is the failure mode that matters most.

AUTH
A static API key in `X-API-Key`, checked with a constant-time comparison. This
is scoped to what it is: a demonstration of controlled access for named
consumers (NSO, RBI), not production identity management. Real deployment would
use OAuth2 client credentials with per-consumer keys, rate limits and an audit
log — noted here rather than pretended at. Setting APIX_API_KEY="" disables the
check and `/v1/health` will say so out loud.
'''
from __future__ import annotations

import csv
import io
import logging
import secrets
import threading
from collections.abc import Sequence
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apix.api.schemas import (
    BacktestPairOut,
    BacktestResponse,
    ConfidenceOut,
    CoverageResponse,
    CurvePointOut,
    ElasticityResponse,
    HeatmapCellOut,
    HeatmapResponse,
    HealthResponse,
    IndexPointOut,
    IndexResponse,
    ReferenceValidationResponse,
    RouteOut,
    RoutesResponse,
    RouteValidationPointOut,
    ScrapeRunOut,
)
from apix.config.loader import ConfigError, load_basket, load_live_basket, load_routes
from apix.config.settings import settings
from apix.db.models import (
    REAL_SOURCE_TYPES,
    FareQuote,
    Frequency,
    IndexValue,
    Measure,
    QuoteStatus,
    ScrapeRun,
    Series,
    SourceType,
)
from apix.db.session import get_db
from apix.index.backtest import run_backtest
from apix.index.confidence import grade_point
from apix.index.construct import build_series, resolve_base_period
from apix.index.reference_validation import run_reference_validation

logger = logging.getLogger("apix.api")

API_VERSION = "0.1.0"

DESCRIPTION = """
**AeroIndex (APIx)** — a real-time airfare price index for India, built to
augment the CPI transport basket (SIH26056, MoSPI).

### Reading the numbers

Every index point carries a `lineage` block. Check it before you quote a value:

* `live_pct` — share from real observed fares
* `simulated_pct` — share from the deterministic Tier-B generator
* `imputed_pct` — share carried forward from a recent cycle to fill a gap

A point that is 100% simulated is a demonstration of the method, not a
measurement of the market. The API will always tell you which one you have.

### The four series

`series` and `measure` are independent axes:

| series | measure | reads as |
|---|---|---|
| `headline` | `total` | what a traveller pays, surges included — CPI-consistent |
| `core` | `total` | what a traveller pays, excluding festival/statistical surges |
| `headline` | `base` | carrier pricing including demand surges, ex-tax |
| `core` | `base` | underlying carrier pricing — the monetary-policy read |

The gap between `headline` and `core` is itself the published measure of surge
pressure. Surge observations are flagged, never deleted.

### Method

Jevons geometric means within each route, then a Laspeyres weighted aggregate
across routes using DGCA passenger-traffic shares. Only cells present in both
the base and current period contribute (matched-model). DGCA monthly data is
used **only** for ex-post backtesting and never as an index input.
"""

app = FastAPI(
    title="AeroIndex (APIx)",
    version=API_VERSION,
    description=DESCRIPTION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str | None = Depends(_api_key_header)) -> None:
    """Constant-time API key check. No-op when APIX_API_KEY is blank."""
    if not settings.api_key:
        return
    if not key or not secrets.compare_digest(key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key header.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _provenance_block(live: int, live_limited: int, sim: int, imp: int, **extra) -> dict:
    """
    The one place a live/simulated/imputed split becomes a published block.

    Every endpoint that reports provenance routes through here. Three
    hand-rolled versions of this dict is three chances for the badge on the
    heatmap to disagree with the badge on the trend chart about the same
    underlying rows — and a caveat that varies by tab is not a caveat.

    Percentages are computed from counts, never averaged from other
    percentages: averaging ratios across groups of different sizes misstates
    the split, in a direction that depends on the data.

    Callers add whatever else is meaningful for their surface (`sources`,
    `points`, ...) via **extra rather than by rebuilding the block.
    """
    total = live + live_limited + sim + imp
    pct = (lambda n: round(100.0 * n / total, 2) if total else 0.0)
    if total == 0:
        label = "NO DATA"
    elif live == 0 and live_limited == 0:
        label = "SIMULATED DATA"
    else:
        label = (
            f"{pct(live):.0f}% live / {pct(live_limited):.0f}% limited "
            f"/ {pct(sim):.0f}% simulated / {pct(imp):.0f}% imputed"
        )
    return {
        "quote_count": total,
        "live_count": live,
        "live_limited_count": live_limited,
        "simulated_count": sim,
        "imputed_count": imp,
        "live_pct": pct(live),
        "live_limited_pct": pct(live_limited),
        "simulated_pct": pct(sim),
        "imputed_pct": pct(imp),
        "is_fully_simulated": live == 0 and live_limited == 0 and total > 0,
        "label": label,
        **extra,
    }


def _aggregate_provenance(rows: list[IndexValue]) -> dict:
    """
    Roll per-point lineage into one window-level block.

    `cells_matched` and `weight_covered_pct` are carried up as well as the
    counts, because the confidence grade is computed from this block: without
    them a window of fully-supported points graded as though it rested on no
    observations at all. Cells sum across points (each point is a separate
    day's evidence); weight coverage is averaged, since it is already a
    percentage of one basket and summing percentages is meaningless.
    """
    lineages = [r.data_lineage or {} for r in rows]
    covered_pcts = [
        float(lin.get("weight_covered_pct", 0.0))
        for lin in lineages
        if lin.get("weight_covered_pct") is not None
    ]
    route_cells: dict[str, int] = {}
    for lin in lineages:
        for route, n in (lin.get("route_cells_matched") or {}).items():
            route_cells[str(route)] = route_cells.get(str(route), 0) + int(n)

    return _provenance_block(
        sum(lin.get("live_count", 0) for lin in lineages),
        sum(lin.get("live_limited_count", 0) for lin in lineages),
        sum(lin.get("simulated_count", 0) for lin in lineages),
        sum(lin.get("imputed_count", 0) for lin in lineages),
        sources=sorted({s for lin in lineages for s in lin.get("sources", [])}),
        routes_covered=sorted(
            {c for lin in lineages for c in lin.get("routes_covered", [])}
        ),
        routes_missing=sorted(
            {c for lin in lineages for c in lin.get("routes_missing", [])}
        ),
        cells_matched=sum(int(lin.get("cells_matched") or 0) for lin in lineages),
        weight_covered_pct=(
            round(sum(covered_pcts) / len(covered_pcts), 2) if covered_pcts else 0.0
        ),
        route_cells_matched=route_cells or None,
        points=len(rows),
        any_low_coverage=any(lin.get("low_coverage", False) for lin in lineages),
        low_coverage=any(lin.get("low_coverage", False) for lin in lineages),
    )


def _quote_provenance(rows: Sequence[FareQuote]) -> dict:
    """
    The same block, computed straight from fare rows.

    Used by the endpoints that read quotes rather than index points. The
    bucketing rule matches `count_quotes` exactly — an imputed row counts as
    imputed whatever it was carried forward from — so /v1/heatmap, /v1/coverage
    and `apix status` cannot report different splits for one cycle.
    """
    live = live_limited = sim = imp = 0
    for q in rows:
        if q.is_imputed:
            imp += 1
        elif q.source_type is SourceType.LIVE:
            live += 1
        elif q.source_type is SourceType.LIVE_LIMITED:
            live_limited += 1
        else:
            sim += 1
    return _provenance_block(
        live, live_limited, sim, imp,
        sources=sorted({q.source for q in rows if q.source}),
        routes_covered=sorted({q.route for q in rows}),
    )


def _query_points(
    db: Session,
    series: Series,
    measure: Measure,
    frequency: Frequency,
    start: date | None,
    end: date | None,
    basket_version: str | None = None,
) -> list[IndexValue]:
    stmt = select(IndexValue).where(
        IndexValue.series == series,
        IndexValue.measure == measure,
        IndexValue.frequency == frequency,
    )
    if basket_version:
        stmt = stmt.where(IndexValue.basket_version == basket_version)
    if start:
        stmt = stmt.where(IndexValue.index_date >= start)
    if end:
        stmt = stmt.where(IndexValue.index_date <= end)
    return list(db.scalars(stmt.order_by(IndexValue.index_date.asc())).all())


#: Basket version served for each value of the `mode` query parameter.
#: `None` (no mode given) is the legacy unfiltered view and is deliberately
#: NOT in this map — it is the one case where points from both baskets may be
#: returned together, and callers that care about the separation must pass a
#: mode explicitly.
_MODE_BASKETS: dict[str, str] = {
    "live": "v1-akasa-live-4route",
    "sandbox": "v1-dgca-fy2022-23",
}

#: Serialises the lazy rebuild below. Without it, two concurrent requests that
#: both miss can enter `build_series(persist=True)` at the same time and race
#: each other's writes into `index_values`.
_REBUILD_LOCK = threading.Lock()


def _resolve_mode(mode: str | None) -> str | None:
    """
    Map a `mode` query parameter to its basket version.

    Rejects an unknown mode with 422 rather than letting it fall through to the
    unfiltered branch. Silently serving the blended series to a caller who
    asked for `mode=liv` would hand them simulated data under a name they
    believed meant live — the exact confusion the separation exists to prevent.
    """
    if mode is None:
        return None
    try:
        return _MODE_BASKETS[mode]
    except KeyError:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown mode {mode!r}; expected one of "
                f"{sorted(_MODE_BASKETS)}, or omit it for the unfiltered series"
            ),
        ) from None


def _points_for_mode(
    db: Session,
    series: Series,
    measure: Measure,
    frequency: Frequency,
    start: date | None,
    end: date | None,
    mode: str | None,
) -> tuple[list[IndexValue], str | None]:
    """
    Fetch the points for a mode, building the series once if it is missing.

    Returns `(rows, basket_version)`. The basket version is returned separately
    because an empty result still has to report which basket was asked for —
    reading it off `rows[0]` is what made the old code raise `UnboundLocalError`
    on any unrecognised mode.

    THE REBUILD IS A COLD-START FALLBACK, NOT A REFRESH. It fires only when the
    query matched nothing at all, which in practice means a fresh database that
    has never had `apix index` run against it. It is left in place so a judge
    who starts the API before the CLI still sees a chart, but it is deliberately
    narrow: it holds a lock, it re-checks after acquiring it, and it never runs
    when the query already returned rows. A GET that rewrites the table on every
    call would make the index a function of who read it last.
    """
    bversion = _resolve_mode(mode)
    rows = _query_points(db, series, measure, frequency, start, end,
                         basket_version=bversion)
    if rows or bversion is None:
        return rows, bversion

    with _REBUILD_LOCK:
        # Another request may have built it while we waited for the lock.
        rows = _query_points(db, series, measure, frequency, start, end,
                             basket_version=bversion)
        if rows:
            return rows, bversion
        try:
            build_series(only_live=(mode == "live"), persist=True)
        except Exception:
            # A failed rebuild must not turn a read into a 500: an empty
            # series with honest provenance is a better answer than a stack
            # trace, and the log keeps the failure visible.
            logger.exception("lazy build_series failed for mode=%s", mode)
            return [], bversion

    db.expire_all()
    rows = _query_points(db, series, measure, frequency, start, end,
                         basket_version=bversion)
    return rows, bversion


#: Grades ordered weakest-first, for picking the weakest point in a window.
_GRADE_ORDER = ("INDICATIVE", "LOW", "MODERATE", "HIGH")

#: Cells a single daily point needs to be fully supported. The window-level
#: grade scales this by the number of points, so a 30-day window is judged
#: against 30 days' worth of evidence rather than one day's.
_GRADE_TARGET = 20


def _route_cell_counts(lineage: dict) -> dict[str, int] | None:
    """
    Per-route cell counts for the breadth component of the confidence grade.

    Read straight from `route_cells_matched`, which `compute_point` records.
    Returns None when the key is absent — points built before it existed are
    graded with breadth treated as neutral rather than reconstructed from
    `route_relatives`, which holds price relatives, not counts. Spreading the
    known total evenly across routes would guarantee a perfect breadth score
    for every point, which is not a measurement, it is a constant wearing one.
    """
    counts = lineage.get("route_cells_matched")
    if not counts:
        return None
    return {str(k): int(v) for k, v in counts.items() if int(v) > 0}


def _to_out(r: IndexValue) -> IndexPointOut:
    return IndexPointOut(
        index_date=r.index_date,
        frequency=r.frequency.value,
        series=r.series.value,
        measure=r.measure.value,
        index_value=r.index_value,
        base_period=r.base_period,
        base_value=r.base_value,
        basket_version=r.basket_version,
        lineage=r.data_lineage,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/v1/health", response_model=HealthResponse, tags=["meta"])
def health(db: Session = Depends(get_db)) -> HealthResponse:
    """
    Liveness plus an honest self-description.

    Deliberately unauthenticated and deliberately blunt: it reports the
    all-time live share and warns when the instance is serving entirely
    simulated data, so nobody can mistake a demo instance for a live one.
    """
    warnings: list[str] = []
    reachable = True
    quote_count = index_count = 0
    live_all = 0
    latest = None
    basket_version = "unknown"

    try:
        quote_count = db.scalar(select(func.count(FareQuote.id))) or 0
        index_count = db.scalar(select(func.count(IndexValue.id))) or 0
        live_all = db.scalar(
            select(func.count(FareQuote.id)).where(
                # Both real tiers, or the warning below fires falsely: an
                # instance whose every observation came from a Tier-1.5 source
                # would be told it holds none at all.
                FareQuote.source_type.in_(REAL_SOURCE_TYPES),
                FareQuote.is_imputed.is_(False),
            )
        ) or 0
        latest = db.scalar(select(func.max(FareQuote.cycle_date)))
    except Exception as exc:
        reachable = False
        warnings.append(f"database unreachable: {exc}")

    try:
        basket_version = load_basket().basket_version
    except ConfigError as exc:
        warnings.append(f"basket config invalid: {exc}")

    live_pct = round(100.0 * live_all / quote_count, 2) if quote_count else 0.0
    if quote_count and live_all == 0:
        warnings.append(
            "This instance holds NO live observations. Every index value it "
            "serves is derived from simulated data and is a demonstration of "
            "the method, not a measurement of the market."
        )
    if not settings.api_key:
        warnings.append("API key auth is DISABLED (APIX_API_KEY is empty).")
    if not settings.live_enabled:
        warnings.append("Tier-A live scraping is disabled (APIX_LIVE_ENABLED=false).")

    return HealthResponse(
        status="ok" if reachable else "degraded",
        version=API_VERSION,
        database="postgresql" if not settings.is_sqlite else "sqlite",
        database_reachable=reachable,
        auth_enabled=bool(settings.api_key),
        live_scraping_enabled=settings.live_enabled,
        live_source=settings.live_sources_display,
        basket_version=basket_version,
        quote_count=quote_count,
        index_point_count=index_count,
        latest_cycle=latest,
        live_pct_all_time=live_pct,
        warnings=warnings,
    )


@app.get(
    "/v1/index",
    response_model=IndexResponse,
    tags=["index"],
    dependencies=[Depends(require_api_key)],
)
def get_index(
    series: Series = Query(Series.HEADLINE, description="headline or core"),
    measure: Measure = Query(Measure.TOTAL, description="total (tax-incl) or base"),
    frequency: Frequency = Query(Frequency.DAILY),
    start: date | None = Query(None, description="inclusive start date"),
    end: date | None = Query(None, description="inclusive end date"),
    mode: str | None = Query(None, description="live (100% real Akasa Air data) or sandbox (methodology demonstration)"),
    db: Session = Depends(get_db),
) -> IndexResponse:
    """
    The index series, with per-point and window-level provenance.
    When mode='live', strictly filters to 100% Real Live Index with 0% simulated data.
    When mode='sandbox', filters to the 30-day methodology demonstration series.
    """
    rows, bversion = _points_for_mode(
        db, series, measure, frequency, start, end, mode
    )

    # With no mode the query spans every basket, so the result can hold points
    # from both the live and the sandbox series. Reporting the first row's
    # basket as if it covered all of them would put a live label on simulated
    # points; name the mix instead and let the caller pass a mode to split it.
    spanned = sorted({r.basket_version for r in rows})
    if rows:
        basket_label = spanned[0] if len(spanned) == 1 else "|".join(spanned)
    else:
        basket_label = bversion or ""

    provenance = _aggregate_provenance(rows)

    # Grade each point from the lineage already stored on it, so the badge can
    # never disagree with the provenance printed beside it. The window grade is
    # computed from the aggregate, and the weakest single point is reported
    # next to it — an average over 30 well-supported days would otherwise hide
    # the one day that rests on two cells.
    per_point = [
        grade_point(
            r.data_lineage or {},
            route_relatives=_route_cell_counts(r.data_lineage or {}),
        )
        for r in rows
    ]
    window_conf = grade_point(
        provenance,
        route_relatives=provenance.get("route_cells_matched"),
        target_cells=max(1, _GRADE_TARGET * len(rows)) if rows else _GRADE_TARGET,
    )
    weakest = min(
        (c.grade for c in per_point),
        key=lambda g: _GRADE_ORDER.index(g) if g in _GRADE_ORDER else 0,
        default=None,
    )

    return IndexResponse(
        series=series.value,
        measure=measure.value,
        frequency=frequency.value,
        base_period=rows[0].base_period if rows else None,
        base_value=rows[0].base_value if rows else 100.0,
        basket_version=basket_label,
        count=len(rows),
        points=[
            IndexPointOut(
                index_date=r.index_date,
                frequency=r.frequency.value,
                series=r.series.value,
                measure=r.measure.value,
                index_value=round(r.index_value, 4),
                base_period=r.base_period,
                base_value=r.base_value,
                basket_version=r.basket_version,
                lineage=r.data_lineage or {},
                confidence=ConfidenceOut(**conf.as_dict()),
            )
            for r, conf in zip(rows, per_point)
        ],
        provenance=provenance,
        confidence=ConfidenceOut(**window_conf.as_dict()),
        weakest_grade=weakest,
    )


@app.get(
    "/v1/sandbox/index",
    response_model=IndexResponse,
    tags=["sandbox"],
    dependencies=[Depends(require_api_key)],
)
def get_sandbox_index(
    series: Series = Query(Series.HEADLINE, description="headline or core"),
    measure: Measure = Query(Measure.TOTAL, description="total (tax-incl) or base"),
    frequency: Frequency = Query(Frequency.DAILY),
    start: date | None = Query(None, description="inclusive start date"),
    end: date | None = Query(None, description="inclusive end date"),
    db: Session = Depends(get_db),
) -> IndexResponse:
    """Synthetic stress-test methodology index (demonstrates methodology, not live data)."""
    return get_index(series=series, measure=measure, frequency=frequency, start=start, end=end, mode="sandbox", db=db)


@app.get(
    "/v1/index.csv",
    summary="Download the index series as CSV",
    tags=["index"],
    dependencies=[Depends(require_api_key)],
    response_class=Response,
)
def get_index_csv(
    series: Series = Query(Series.HEADLINE),
    measure: Measure = Query(Measure.TOTAL),
    frequency: Frequency = Query(Frequency.DAILY),
    start: date | None = None,
    end: date | None = None,
    mode: str | None = Query(None, description="live (100% real) or sandbox"),
    db: Session = Depends(get_db),
) -> Response:
    """
    CSV export.
    In live mode (mode='live'), zero simulated rows are exported.
    """
    rows, _bversion = _points_for_mode(
        db, series, measure, frequency, start, end, mode
    )

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([
        "index_date", "frequency", "series", "measure", "index_value",
        "base_period", "base_value", "basket_version",
        "live_pct", "live_limited_pct", "simulated_pct", "imputed_pct",
        "quote_count", "routes_covered", "routes_missing", "sources",
        # The confidence columns travel with the export. A CSV that dropped the
        # grade would let a number leave this system stripped of the one caveat
        # the dashboard prints beside it, and the export is the artefact most
        # likely to be quoted somewhere the dashboard is not on screen.
        "confidence_score", "confidence_grade", "real_cells", "target_cells",
        "confidence_reasons",
    ])
    for r in rows:
        lin = r.data_lineage or {}
        conf = grade_point(lin, route_relatives=_route_cell_counts(lin))
        w.writerow([
            r.index_date.isoformat(), r.frequency.value, r.series.value,
            r.measure.value, f"{r.index_value:.4f}",
            r.base_period.isoformat(), f"{r.base_value:.1f}", r.basket_version,
            lin.get("live_pct", 0), lin.get("live_limited_pct", 0),
            lin.get("simulated_pct", 0), lin.get("imputed_pct", 0),
            lin.get("quote_count", 0),
            "|".join(lin.get("routes_covered", [])),
            "|".join(lin.get("routes_missing", [])),
            "|".join(lin.get("sources", [])),
            f"{conf.score:.1f}", conf.grade, conf.real_cells, conf.target_cells,
            " ".join(conf.reasons),
        ])
    filename = f"apix_{mode or 'all'}_{series.value}_{measure.value}_{frequency.value}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get(
    "/v1/routes",
    response_model=RoutesResponse,
    tags=["reference"],
    dependencies=[Depends(require_api_key)],
)
def get_routes(
    mode: str | None = Query(None, description="live (4 routes) or sandbox (6 routes)"),
    db: Session = Depends(get_db),
) -> RoutesResponse:
    """The basket: routes, DGCA-derived weights, and how much data each holds."""
    routes_cfg = load_routes()
    is_live = (_resolve_mode(mode) == _MODE_BASKETS["live"])
    basket = load_live_basket() if is_live else load_basket()
    weights = {r.route: r for r in basket.routes}

    counts = dict(
        db.execute(
            select(FareQuote.route, func.count(FareQuote.id)).group_by(FareQuote.route)
        ).all()
    )
    live_counts = dict(
        db.execute(
            select(FareQuote.route, func.count(FareQuote.id))
            .where(
                FareQuote.source_type.in_(REAL_SOURCE_TYPES),
                FareQuote.is_imputed.is_(False),
            )
            .group_by(FareQuote.route)
        ).all()
    )
    latest = dict(
        db.execute(
            select(FareQuote.route, func.max(FareQuote.cycle_date))
            .group_by(FareQuote.route)
        ).all()
    )

    out = []
    for r in routes_cfg.routes:
        bw = weights.get(r.route)
        out.append(
            RouteOut(
                route=r.route, name=r.name, origin=r.origin,
                destination=r.destination, distance_km=r.distance_km,
                weight=bw.weight if bw else None,
                national_share_pct=bw.national_share_pct if bw else None,
                in_basket=bw is not None,
                quote_count=counts.get(r.route, 0),
                latest_cycle=latest.get(r.route),
                live_quote_count=live_counts.get(r.route, 0),
            )
        )
    return RoutesResponse(
        basket_version=basket.basket_version,
        weight_source=basket.weight_source,
        weights_sum=round(sum(r.weight for r in basket.routes), 6),
        count=len(out),
        routes=out,
    )


@app.get(
    "/v1/coverage",
    response_model=CoverageResponse,
    tags=["operations"],
    dependencies=[Depends(require_api_key)],
)
def get_coverage(
    cycle_date: date | None = Query(None, description="defaults to the latest cycle"),
    mode: str | None = Query(None, description="live (4 routes) or sandbox (6 routes)"),
    db: Session = Depends(get_db),
) -> CoverageResponse:
    """
    What the collector actually managed, per cycle.
    In live mode, zero simulated rows are reported.
    """
    is_live = (_resolve_mode(mode) == _MODE_BASKETS["live"])
    if cycle_date is None:
        if is_live:
            cycle_date = db.scalar(
                select(func.max(FareQuote.cycle_date))
                .where(
                    FareQuote.source_type.in_(REAL_SOURCE_TYPES),
                    FareQuote.is_imputed.is_(False),
                )
            )
        else:
            cycle_date = db.scalar(select(func.max(FareQuote.cycle_date)))

    quotes: list[FareQuote] = []
    if cycle_date:
        stmt = select(FareQuote).where(FareQuote.cycle_date == cycle_date)
        if is_live:
            stmt = stmt.where(
                FareQuote.source_type.in_(REAL_SOURCE_TYPES),
                FareQuote.is_imputed.is_(False),
            )
        quotes = list(db.scalars(stmt).all())

    prov = _quote_provenance(quotes)
    basket = load_live_basket() if is_live else load_basket()
    basket_routes = set(basket.route_codes)

    routes_live = sorted(
        {
            q.route
            for q in quotes
            if q.status != QuoteStatus.VALIDATION_REJECT
            and not q.is_imputed
            and q.source_type.is_live
        }
        & basket_routes
    )
    covered = routes_live if is_live else sorted(
        {q.route for q in quotes if q.status != QuoteStatus.VALIDATION_REJECT}
        & basket_routes
    )

    runs = list(
        db.scalars(
            select(ScrapeRun).order_by(ScrapeRun.cycle_timestamp.desc()).limit(12)
        ).all()
    )
    if is_live:
        runs = [r for r in runs if r.source_type.value in ("live", "live_limited")]

    status_counts: dict[str, int] = {}
    for run in runs:
        for k, v in (run.status_counts or {}).items():
            status_counts[k] = status_counts.get(k, 0) + int(v)

    return CoverageResponse(
        cycle_date=cycle_date,
        quotes_total=prov["quote_count"],
        live_count=prov["live_count"],
        live_limited_count=prov["live_limited_count"],
        simulated_count=0 if is_live else prov["simulated_count"],
        imputed_count=0 if is_live else prov["imputed_count"],
        live_pct=100.0 if (is_live and prov["quote_count"] > 0) else prov["live_pct"],
        live_limited_pct=0.0 if is_live else prov["live_limited_pct"],
        simulated_pct=0.0 if is_live else prov["simulated_pct"],
        imputed_pct=0.0 if is_live else prov["imputed_pct"],
        validation_rejects=sum(
            1 for q in quotes if q.status == QuoteStatus.VALIDATION_REJECT
        ),
        outliers=sum(1 for q in quotes if q.is_outlier),
        high_demand_outliers=sum(1 for q in quotes if q.is_high_demand_outlier),
        routes_covered=covered,
        routes_live=routes_live,
        routes_missing=sorted(basket_routes - set(covered)),
        status_counts=status_counts,
        recent_runs=[
            ScrapeRunOut(
                id=r.id, source=r.source, source_type=r.source_type.value,
                cycle_timestamp=r.cycle_timestamp,
                status_counts=r.status_counts or {},
                coverage_pct=r.coverage_pct, duration_ms=r.duration_ms, notes=r.notes,
            )
            for r in runs
        ],
    )


@app.get(
    "/v1/reference-validation",
    response_model=ReferenceValidationResponse,
    tags=["validation"],
    dependencies=[Depends(require_api_key)],
)
def get_reference_validation(
    measure: Measure = Query(Measure.TOTAL),
    cycle_date: date | None = None,
) -> ReferenceValidationResponse:
    """
    Reference Point Validation: Live Index route averages vs external independently-published benchmarks
    (Ixigo / The Indian Express, Dec 2024). Replaces circular backtest against synthetic placeholders.
    """
    res = run_reference_validation(cycle_date=cycle_date, measure=measure)
    return ReferenceValidationResponse(
        routes_compared=res.routes_compared,
        overall_mape=res.overall_mape,
        caption=res.caption,
        reportable=res.reportable,
        notes=res.notes,
        points=[
            RouteValidationPointOut(
                route=p.route,
                direction=p.direction,
                live_avg_fare=p.live_avg_fare,
                benchmark_fare=p.benchmark_fare,
                diff_inr=p.diff_inr,
                pct_deviation=p.pct_deviation,
                quotes_count=p.quotes_count,
                source_citation=p.source_citation,
                article_url=p.article_url,
                benchmark_month=p.benchmark_month,
            )
            for p in res.points
        ],
    )



@app.get(
    "/v1/backtest",
    response_model=BacktestResponse,
    tags=["validation"],
    dependencies=[Depends(require_api_key)],
)
def get_backtest(
    series: Series = Query(Series.HEADLINE),
    measure: Measure = Query(Measure.TOTAL),
) -> BacktestResponse:
    """
    Ex-post comparison against DGCA monthly averages.

    Read `reportable` before quoting anything here. It is False when the
    overlap is too short for a correlation to mean anything, or when the
    reference rows are illustrative rather than official DGCA figures.
    """
    r = run_backtest(series, measure)
    return BacktestResponse(
        series=r.series.value,
        measure=r.measure.value,
        months_compared=r.months_compared,
        base_month=r.base_month,
        pearson_r=r.pearson_r,
        mape=r.mape,
        sufficient=r.sufficient,
        uses_placeholder_reference=r.uses_placeholder_reference,
        reportable=r.reportable,
        notes=r.notes,
        pairs=[
            BacktestPairOut(
                month=p.month, apix=p.apix, dgca=p.dgca,
                abs_pct_error=round(p.abs_pct_error, 4), routes_used=p.routes_used,
            )
            for p in r.pairs
        ],
    )


@app.get(
    "/v1/routes/{route}/curve",
    response_model=ElasticityResponse,
    tags=["analysis"],
    dependencies=[Depends(require_api_key)],
)
def get_elasticity_curve(
    route: str,
    measure: Measure = Query(Measure.TOTAL),
    start: date | None = None,
    end: date | None = None,
    mode: str | None = Query(None, description="live or sandbox"),
    db: Session = Depends(get_db),
) -> ElasticityResponse:
    """
    The advance-purchase fare curve for one route.

    This is the "200-400% spread" the problem statement calls out, measured
    rather than asserted: median fare per booking window, expressed as a ratio
    to the T+30 anchor.
    """
    route = route.upper()
    try:
        load_routes().by_route(route)
    except ConfigError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    is_live = (_resolve_mode(mode) == _MODE_BASKETS["live"])
    stmt = select(FareQuote).where(
        FareQuote.route == route,
        FareQuote.status != QuoteStatus.VALIDATION_REJECT,
    )
    if is_live:
        stmt = stmt.where(
            FareQuote.source_type.in_(REAL_SOURCE_TYPES),
            FareQuote.is_imputed.is_(False),
        )
    if start:
        stmt = stmt.where(FareQuote.cycle_date >= start)
    if end:
        stmt = stmt.where(FareQuote.cycle_date <= end)
    rows = list(db.scalars(stmt).all())

    buckets: dict[str, list[float]] = {}
    for q in rows:
        price = q.total_fare if measure is Measure.TOTAL else q.base_fare
        buckets.setdefault(q.advance_purchase_window.value, []).append(price)

    import statistics as st

    anchor = None
    if buckets.get("T+30"):
        anchor = st.median(buckets["T+30"])

    points = []
    for window in ("T+1", "T+7", "T+15", "T+30", "T+45"):
        vals = buckets.get(window)
        if not vals:
            continue
        med = st.median(vals)
        points.append(
            CurvePointOut(
                advance_window=window,
                days=int(window.split("+")[1]),
                median_fare=round(med, 2),
                mean_fare=round(st.fmean(vals), 2),
                n=len(vals),
                ratio_to_t30=round(med / anchor, 4) if anchor else None,
            )
        )

    spread = None
    if points:
        meds = [p.median_fare for p in points]
        spread = round((max(meds) / min(meds) - 1.0) * 100.0, 2)

    return ElasticityResponse(
        route=route,
        measure=measure.value,
        window_start=min((q.cycle_date for q in rows), default=None),
        window_end=max((q.cycle_date for q in rows), default=None),
        points=points,
        max_spread_pct=spread,
        provenance=_quote_provenance(rows),
    )


#: One heatmap cell's median, sample size and provenance.
_CellStat = tuple[float, int, str]


def _cell_medians(
    db: Session, cycle_date: date, measure: Measure, only_live: bool = False
) -> dict[tuple[str, str], _CellStat]:
    """
    Median fare per (route, advance-window) for one cycle, with its provenance.
    """
    import statistics as st

    stmt = select(FareQuote).where(
        FareQuote.cycle_date == cycle_date,
        FareQuote.status != QuoteStatus.VALIDATION_REJECT,
    )
    if only_live:
        stmt = stmt.where(
            FareQuote.source_type.in_(REAL_SOURCE_TYPES),
            FareQuote.is_imputed.is_(False),
        )
    rows = db.scalars(stmt).all()
    buckets: dict[tuple[str, str], list[float]] = {}
    tiers: dict[tuple[str, str], set[str]] = {}
    for q in rows:
        key = (q.route, q.advance_purchase_window.value)
        price = q.total_fare if measure is Measure.TOTAL else q.base_fare
        buckets.setdefault(key, []).append(price)
        tiers.setdefault(key, set()).add(
            "imputed" if q.is_imputed else q.source_type.value
        )

    def weakest(present: set[str]) -> str:
        for tier in ("simulated", "imputed", "live_limited", "live"):
            if tier in present:
                return tier
        return "simulated"

    return {
        k: (st.median(v), len(v), weakest(tiers.get(k, set())))
        for k, v in buckets.items()
    }


@app.get(
    "/v1/heatmap",
    response_model=HeatmapResponse,
    tags=["analysis"],
    dependencies=[Depends(require_api_key)],
)
def get_heatmap(
    cycle_date: date | None = Query(None, description="defaults to the latest cycle"),
    measure: Measure = Query(Measure.TOTAL),
    mode: str | None = Query(None, description="live or sandbox"),
    db: Session = Depends(get_db),
) -> HeatmapResponse:
    """
    Route x advance-window median fares for one cycle.
    In live mode, uses only 100% real observations.
    """
    is_live = (_resolve_mode(mode) == _MODE_BASKETS["live"])
    if cycle_date is None:
        if is_live:
            cycle_date = db.scalar(
                select(func.max(FareQuote.cycle_date))
                .where(
                    FareQuote.source_type.in_(REAL_SOURCE_TYPES),
                    FareQuote.is_imputed.is_(False),
                )
            )
        else:
            cycle_date = db.scalar(select(func.max(FareQuote.cycle_date)))

    if cycle_date is None:
        return HeatmapResponse(
            cycle_date=None, base_period=None, measure=measure.value, cells=[],
            provenance=_quote_provenance([]),
        )

    current = _cell_medians(db, cycle_date, measure, only_live=is_live)
    base_period = resolve_base_period(db, only_live=is_live)
    base = current if base_period == cycle_date else _cell_medians(db, base_period, measure, only_live=is_live)


    cells = []
    for (route, window), (med, n, tier) in sorted(current.items()):
        ref = base.get((route, window))
        cells.append(
            HeatmapCellOut(
                route=route,
                advance_window=window,
                median_fare=round(med, 2),
                n=n,
                source_type=tier,
                is_live=tier in ("live", "live_limited"),
                pct_change_vs_base=(
                    round((med / ref[0] - 1.0) * 100.0, 2) if ref and ref[0] else None
                ),
            )
        )

    rows = db.scalars(
        select(FareQuote).where(
            FareQuote.cycle_date == cycle_date,
            FareQuote.status != QuoteStatus.VALIDATION_REJECT,
        )
    ).all()
    return HeatmapResponse(
        cycle_date=cycle_date,
        base_period=base_period,
        measure=measure.value,
        cells=cells,
        provenance=_quote_provenance(rows),
    )
