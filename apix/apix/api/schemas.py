"""
Pydantic response models for the APIx API.

Every response that carries an index number also carries its provenance. That
is a deliberate schema-level constraint, not a convention: `IndexPointOut` has
no way to express a value without a `lineage`, so no endpoint can accidentally
serve a bare number. A consumer at NSO or RBI can always answer "how much of
this is real?" from the payload alone.
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class LineageOut(BaseModel):
    """Where an index point's data came from. Percentages sum to 100."""

    quote_count: int
    live_count: int = 0
    simulated_count: int = 0
    imputed_count: int = 0
    live_pct: float
    simulated_pct: float
    imputed_pct: float
    sources: list[str] = Field(default_factory=list)
    routes_covered: list[str] = Field(default_factory=list)
    routes_missing: list[str] = Field(default_factory=list)
    routes_in_basket: int = 0
    weight_covered_pct: float = 0.0
    low_coverage: bool = False
    high_demand_points: int = 0
    elementary_formula: str | None = None
    cells_matched: int | None = None
    route_relatives: dict[str, float] | None = None
    route_cells_matched: dict[str, int] | None = None


class ConfidenceOut(BaseModel):
    """
    How much real evidence stands behind a published value.

    A support grade, deliberately not a confidence interval: it reports how
    much observed, matched, well-spread data the number rests on, not a
    probability that it is near some true value. `reasons` carries the plain
    explanation for every deduction, so the grade never travels as a bare
    letter a reader has to take on trust.
    """

    score: float
    grade: str
    components: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    real_cells: int = 0
    target_cells: int = 0
    #: The weights the engine applied to `components` to reach `score`. Carried
    #: on the wire so the dashboard renders the same arithmetic rather than its
    #: own recollection of it.
    weights: dict[str, float] = Field(default_factory=dict)


class IndexPointOut(BaseModel):
    index_date: date
    frequency: str
    series: str
    measure: str
    index_value: float
    base_period: date
    base_value: float
    basket_version: str
    lineage: LineageOut
    #: Support grade for this point. Optional so a cached or legacy payload
    #: remains valid, but every live response sets it.
    confidence: ConfidenceOut | None = None


class IndexResponse(BaseModel):
    series: str
    measure: str
    frequency: str
    base_period: date | None
    basket_version: str | None
    count: int
    #: Aggregate provenance across the whole returned window. This is what the
    #: dashboard's persistent global badge renders — a consumer should never
    #: have to sum it themselves to find out what they are looking at.
    provenance: dict
    #: Support grade for the window as a whole, computed from the aggregate
    #: provenance. The weakest point's grade is reported alongside it so a
    #: strong average cannot hide a thin day.
    confidence: ConfidenceOut | None = None
    weakest_grade: str | None = None
    points: list[IndexPointOut]


class RouteOut(BaseModel):
    route: str
    name: str
    origin: str
    destination: str
    distance_km: int
    weight: float | None = None
    national_share_pct: float | None = None
    in_basket: bool
    quote_count: int = 0
    latest_cycle: date | None = None
    live_quote_count: int = 0


class RoutesResponse(BaseModel):
    basket_version: str
    weight_source: str
    weights_sum: float
    count: int
    routes: list[RouteOut]


class ScrapeRunOut(BaseModel):
    id: int
    source: str
    source_type: str
    cycle_timestamp: datetime
    status_counts: dict[str, int]
    coverage_pct: float
    duration_ms: int
    notes: str | None = None


class CoverageResponse(BaseModel):
    """
    Operational truth: what was attempted, what came back, what was filled in.

    This endpoint is the reason a blocked scrape is not a silent failure. Every
    status code the collector can emit shows up here with a count.
    """

    cycle_date: date | None
    quotes_total: int
    live_count: int
    #: Tier-1.5. Carried separately rather than folded into `live_count` so a
    #: consumer can weigh a rate-capped source differently — but it must be
    #: present, or the four counts stop summing to `quotes_total` and the panel
    #: silently loses a bucket.
    live_limited_count: int = 0
    simulated_count: int
    imputed_count: int
    live_pct: float
    live_limited_pct: float = 0.0
    simulated_pct: float
    imputed_pct: float
    validation_rejects: int
    outliers: int
    high_demand_outliers: int
    routes_covered: list[str]
    #: The subset of `routes_covered` that got a REAL observed fare this cycle
    #: (Tier-1 or Tier-1.5, not imputed). Carried separately because
    #: `routes_covered` counts a simulated cell as covered — correct for "does
    #: the index have a cell here?", wrong for "how much of this is real?", and
    #: the dashboard needs to answer the second without inferring it.
    routes_live: list[str] = Field(default_factory=list)
    routes_missing: list[str]
    status_counts: dict[str, int]
    recent_runs: list[ScrapeRunOut]


class BacktestPairOut(BaseModel):
    month: str
    apix: float
    dgca: float
    abs_pct_error: float
    routes_used: list[str] = Field(default_factory=list)


class BacktestResponse(BaseModel):
    series: str
    measure: str
    months_compared: int
    base_month: str | None
    pearson_r: float | None
    mape: float | None
    #: False when the overlap is too short for a correlation to mean anything.
    sufficient: bool
    #: True when the reference rows are illustrative rather than official DGCA
    #: figures. Clients MUST badge the result when this is set.
    uses_placeholder_reference: bool
    #: sufficient AND not placeholder. The only condition under which this
    #: result may be quoted as validation without a caveat attached.
    reportable: bool
    notes: list[str]
    pairs: list[BacktestPairOut]


class RouteValidationPointOut(BaseModel):
    route: str
    direction: str
    live_avg_fare: float
    benchmark_fare: float
    diff_inr: float
    pct_deviation: float
    quotes_count: int
    source_citation: str
    article_url: str
    benchmark_month: str


class ReferenceValidationResponse(BaseModel):
    routes_compared: int
    overall_mape: float
    caption: str
    reportable: bool
    notes: list[str]
    points: list[RouteValidationPointOut]


class CurvePointOut(BaseModel):
    advance_window: str
    days: int
    median_fare: float
    mean_fare: float
    n: int
    ratio_to_t30: float | None = None


class ElasticityResponse(BaseModel):
    """Advance-purchase fare curve — the T+1 vs T+30 spread the PS asks about."""

    route: str
    measure: str
    window_start: date | None
    window_end: date | None
    points: list[CurvePointOut]
    max_spread_pct: float | None
    provenance: dict


class HeatmapCellOut(BaseModel):
    route: str
    advance_window: str
    median_fare: float
    n: int
    #: Provenance of THIS cell: live | live_limited | imputed | simulated. A
    #: cell backed by more than one tier reports the weakest one present, so
    #: the grid never labels a partly-filled cell as fully observed. This is
    #: what lets the dashboard badge each square instead of only the panel.
    source_type: str = "simulated"
    #: Convenience for the client: real observation (Tier-1 or Tier-1.5).
    is_live: bool = False
    #: Movement of this cell since the base period. Null when the base period
    #: has no matching cell — unmatched means unknown, not unchanged.
    pct_change_vs_base: float | None = None


class HeatmapResponse(BaseModel):
    cycle_date: date | None
    base_period: date | None = None
    measure: str
    cells: list[HeatmapCellOut]
    provenance: dict


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    database_reachable: bool
    auth_enabled: bool
    live_scraping_enabled: bool
    live_source: str
    basket_version: str
    quote_count: int
    index_point_count: int
    latest_cycle: date | None
    #: Honest headline: the share of stored quotes that are real observations.
    live_pct_all_time: float
    warnings: list[str] = Field(default_factory=list)
