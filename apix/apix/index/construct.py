"""
Index construction.

Two-level aggregation, matching how a national statistical office actually
builds a CPI component:

  LEVEL 1 — elementary aggregate, WITHIN a route
      Jevons: the geometric mean of price relatives across matched cells.

          I_r,t = exp( (1/n) * SUM_c ln( P_c,t / P_c,0 ) )

      where a *cell* c is one (carrier, advance-purchase window) and n is the
      number of cells observed in BOTH the base period and t.

      Jevons is the international standard for elementary aggregates (used by
      the ONS, BLS and Eurostat for airfares) because it is transitive, it
      handles the extreme right-skew of fare distributions without a single
      ₹40,000 T+1 seat dominating, and it implicitly allows for substitution.
      Dutot (ratio of mean prices) is available via APIX_ELEMENTARY_FORMULA for
      comparison, but is not the default: it is not invariant to the units of
      each cell and lets the most expensive cell drive the result.

  LEVEL 2 — upper aggregate, ACROSS routes
      Laspeyres with a fixed basket of DGCA passenger-traffic weights:

          APIx_t = 100 * SUM_r ( w_r * I_r,t ) / SUM_r w_r

      The denominator is the renormalisation. When a route has no matched cells
      at t, it is dropped and the remaining weights are rescaled to sum to 1 —
      rather than being treated as zero price change, which would bias the index
      toward 100 exactly when coverage is worst.

MATCHED-MODEL PRINCIPLE
Only cells present in both the base period and the current period contribute.
Without this, a carrier merely opening or closing a route would register as a
price movement. It is the single most important safeguard in the whole module.

FOUR PUBLISHED SERIES
`series` (headline / core) and `measure` (total / base) are independent axes:

    headline x total  what a traveller pays, surges included     -> MoSPI / NSO
    core     x total  what a traveller pays, surges excluded
    headline x base   carrier pricing, tax-inclusive demand
    core     x base   underlying carrier pricing                 -> RBI

Every point carries a `data_lineage` blob recording the live / simulated /
imputed split behind it. A number without its provenance is not publishable,
so the two are written in the same transaction.
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from apix.config.loader import BasketConfig, load_basket, load_live_basket
from apix.config.settings import settings
from apix.db.models import (
    REAL_SOURCE_TYPES,
    FareQuote,
    Frequency,
    IndexValue,
    Measure,
    QuoteStatus,
    Series,
    SourceType,
)
from apix.db.session import session_scope

logger = logging.getLogger("apix.index")

BASE_VALUE = 100.0

#: One elementary cell: (carrier, advance-purchase window).
Cell = tuple[str, str]


class IndexError_(ValueError):
    """Raised when an index point cannot be constructed at all."""


# ─────────────────────────────────────────────────────────────────────────────
# Results
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class RouteRelative:
    """One route's elementary price relative against the base period."""

    route: str
    relative: float
    cells_matched: int
    cells_current: int
    cells_base: int
    weight_raw: float

    @property
    def pct_change(self) -> float:
        return (self.relative - 1.0) * 100.0


@dataclass
class IndexPoint:
    index_date: date
    frequency: Frequency
    series: Series
    measure: Measure
    index_value: float
    base_period: date
    basket_version: str
    lineage: dict
    routes: list[RouteRelative] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.index_date} {self.series.value:8s} {self.measure.value:5s} "
            f"= {self.index_value:7.2f}  "
            f"(live {self.lineage['live_pct']:.0f}% / "
            f"limited {self.lineage.get('live_limited_pct', 0):.0f}% / "
            f"sim {self.lineage['simulated_pct']:.0f}% / "
            f"imp {self.lineage['imputed_pct']:.0f}%, "
            f"{len(self.lineage['routes_covered'])}/{self.lineage['routes_in_basket']} routes)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Price extraction
# ─────────────────────────────────────────────────────────────────────────────
def _price(q: FareQuote, measure: Measure) -> float:
    """
    The fare aggregate this measure is built on.

    BASE deliberately excludes taxes, UDF and the OTA convenience fee. Those are
    statutory or channel charges, not carrier pricing, so a GST revision must not
    read as airfare inflation in the series a central bank looks at.
    """
    return q.total_fare if measure is Measure.TOTAL else q.base_fare


def _eligible(q: FareQuote, series: Series) -> bool:
    """
    Whether a quote may enter this series.

    Validation-rejected rows never enter either series: they are suspected
    parsing errors, not prices.

    CORE additionally drops both outlier flags — the statistical fence
    (`is_outlier`) and the calendar festival rule (`is_high_demand_outlier`).
    Note what this does NOT do: it does not delete them. They remain in the
    table, they remain in HEADLINE, and the gap between the two series is
    itself the published measure of surge pressure.
    """
    if q.status == QuoteStatus.VALIDATION_REJECT:
        return False
    if series is Series.CORE and (q.is_outlier or q.is_high_demand_outlier):
        return False
    return True


def _cells(
    quotes: list[FareQuote], series: Series, measure: Measure
) -> dict[Cell, float]:
    """
    Collapse a period's quotes for one route into {cell: price}.

    After dedupe there is at most one quote per cell per cycle, so the median is
    a formality — but it is the right formality: if a future source ever yields
    several quotes per cell, the median resists a single bad row without any
    other code changing.
    """
    buckets: dict[Cell, list[float]] = {}
    for q in quotes:
        if not _eligible(q, series):
            continue
        price = _price(q, measure)
        if price is None or price <= 0:
            continue
        buckets.setdefault(
            (q.carrier, q.advance_purchase_window.value), []
        ).append(price)
    return {cell: statistics.median(v) for cell, v in buckets.items() if v}


# ─────────────────────────────────────────────────────────────────────────────
# Level 1 — elementary aggregate
# ─────────────────────────────────────────────────────────────────────────────
def elementary_relative(
    base_cells: dict[Cell, float],
    current_cells: dict[Cell, float],
    formula: str | None = None,
) -> tuple[float | None, int]:
    """
    Price relative for one route: Jevons (default) or Dutot.

    Returns (relative, n_used). `None` when no cell is usable in both periods —
    the caller must then drop the route, never assume no change.

    `n_used` counts the cells that actually backed the number, which is not
    always the number of matched cells: a cell with a non-positive price in
    either period is dropped from both the maths and the count. See below.
    """
    formula = (formula or settings.elementary_formula).lower()
    if formula not in ("jevons", "dutot"):
        # Checked before the empty-input return so a typo in
        # APIX_ELEMENTARY_FORMULA surfaces as an error rather than as a route
        # that quietly produces nothing.
        raise IndexError_(
            f"unknown elementary formula {formula!r}; expected 'jevons' or 'dutot'"
        )

    matched = sorted(set(base_cells) & set(current_cells))
    if not matched:
        return None, 0

    # A non-positive fare has no logarithm and no meaning as a price. Validation
    # should have rejected it upstream; if one reaches here anyway it is dropped
    # from the count as well as from the sum. Leaving it in the denominator
    # would drag the relative toward 1.0 — a fabricated "no change" — and would
    # report the cell as backing a number it took no part in, which is the same
    # overstatement of support that the lineage block exists to prevent.
    usable = [c for c in matched if base_cells[c] > 0 and current_cells[c] > 0]
    if len(usable) != len(matched):
        logger.warning(
            "%d of %d matched cells had a non-positive price and were excluded",
            len(matched) - len(usable), len(matched),
        )
    if not usable:
        return None, 0
    n = len(usable)

    if formula == "dutot":
        # Ratio of arithmetic means. Offered for comparison only.
        num = sum(current_cells[c] for c in usable)
        den = sum(base_cells[c] for c in usable)
        return (num / den if den > 0 else None), n

    # Jevons, computed in log space. Summing logs then exponentiating avoids the
    # floating-point overflow a raw product of many ratios would risk, and keeps
    # the result numerically stable for large n.
    total = sum(math.log(current_cells[c] / base_cells[c]) for c in usable)
    return math.exp(total / n), n


# ─────────────────────────────────────────────────────────────────────────────
# Lineage
# ─────────────────────────────────────────────────────────────────────────────
def _lineage(
    contributing: list[FareQuote],
    routes_covered: list[str],
    basket: BasketConfig,
    weight_covered: float,
) -> dict:
    """
    Provenance of one index point.

    An imputed row is counted as IMPUTED regardless of whether its donor was
    live or simulated. Carrying a live label onto a value that was not observed
    today would overstate how much of the index is real — which is precisely
    the failure this project is built to avoid. The three percentages therefore
    partition the contributing quotes and sum to 100.
    """
    total = len(contributing)
    live = sum(
        1 for q in contributing
        if q.source_type == SourceType.LIVE and not q.is_imputed
    )
    live_limited = sum(
        1 for q in contributing
        if q.source_type == SourceType.LIVE_LIMITED and not q.is_imputed
    )
    simulated = sum(
        1 for q in contributing
        if q.source_type == SourceType.SIMULATED and not q.is_imputed
    )
    imputed = sum(1 for q in contributing if q.is_imputed)
    pct = (lambda n: round(100.0 * n / total, 2) if total else 0.0)

    covered = sorted(routes_covered)
    missing = sorted(set(basket.route_codes) - set(covered))
    return {
        "quote_count": total,
        "live_count": live,
        "live_limited_count": live_limited,
        "simulated_count": simulated,
        "imputed_count": imputed,
        "live_pct": pct(live),
        "live_limited_pct": pct(live_limited),
        "simulated_pct": pct(simulated),
        "imputed_pct": pct(imputed),
        "sources": sorted({q.source for q in contributing}),
        "routes_covered": covered,
        "routes_missing": missing,
        "routes_in_basket": len(basket.route_codes),
        # Share of the ORIGINAL basket weight actually represented. More
        # informative than a route count: losing DEL-BOM (27.7%) is not the
        # same event as losing BLR-HYD (8.9%).
        "weight_covered_pct": round(100.0 * weight_covered, 2),
        "low_coverage": len(covered) < settings.min_routes_for_index,
        "outliers_included": any(q.is_outlier for q in contributing),
        "high_demand_points": sum(1 for q in contributing if q.is_high_demand_outlier),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Level 2 — upper aggregate
# ─────────────────────────────────────────────────────────────────────────────
def _quotes_for(
    session: Session, cycle_date: date, only_live: bool = False
) -> dict[str, list[FareQuote]]:
    stmt = select(FareQuote).where(FareQuote.cycle_date == cycle_date)
    if only_live:
        stmt = stmt.where(
            FareQuote.source_type.in_(REAL_SOURCE_TYPES),
            FareQuote.is_imputed.is_(False),
        )
    rows = session.scalars(stmt).all()
    out: dict[str, list[FareQuote]] = {}
    for r in rows:
        out.setdefault(r.route, []).append(r)
    return out


def compute_point(
    session: Session,
    index_date: date,
    base_period: date,
    series: Series,
    measure: Measure,
    frequency: Frequency = Frequency.DAILY,
    basket: BasketConfig | None = None,
    only_live: bool = False,
) -> IndexPoint:
    """
    Build one index point: Jevons within routes, weighted Laspeyres across them.

    Raises IndexError_ if no route has a matched cell — an index over nothing is
    not a small number, it is an absent one, and returning 100.0 there would be
    a fabrication.
    """
    if basket is None:
        basket = load_live_basket() if only_live else load_basket()
    base_by_route = _quotes_for(session, base_period, only_live=only_live)
    curr_by_route = _quotes_for(session, index_date, only_live=only_live)

    relatives: list[RouteRelative] = []
    contributing: list[FareQuote] = []

    for br in basket.routes:
        base_cells = _cells(base_by_route.get(br.route, []), series, measure)
        curr_cells = _cells(curr_by_route.get(br.route, []), series, measure)
        rel, matched = elementary_relative(base_cells, curr_cells)
        if rel is None:
            logger.debug(
                "%s dropped from %s/%s on %s: no cell matched the base period",
                br.route, series.value, measure.value, index_date,
            )
            continue
        relatives.append(
            RouteRelative(
                route=br.route,
                relative=rel,
                cells_matched=matched,
                cells_current=len(curr_cells),
                cells_base=len(base_cells),
                weight_raw=br.weight,
            )
        )
        matched_cells = set(base_cells) & set(curr_cells)
        contributing.extend(
            q for q in curr_by_route.get(br.route, [])
            if _eligible(q, series)
            and (q.carrier, q.advance_purchase_window.value) in matched_cells
        )

    if not relatives:
        raise IndexError_(
            f"no route had a cell matched to the base period {base_period} on "
            f"{index_date} — cannot compute {series.value}/{measure.value}"
        )

    # Renormalise over covered routes. See module docstring: dropping a route
    # must not be silently equivalent to "that route didn't move".
    weight_covered = sum(r.weight_raw for r in relatives)
    value = BASE_VALUE * sum(
        (r.weight_raw / weight_covered) * r.relative for r in relatives
    )

    lineage = _lineage(
        contributing, [r.route for r in relatives], basket, weight_covered
    )
    lineage["elementary_formula"] = settings.elementary_formula
    lineage["cells_matched"] = sum(r.cells_matched for r in relatives)
    lineage["route_relatives"] = {
        r.route: round(r.relative, 6) for r in relatives
    }
    # Cells backing each route, not just the total. Without this the confidence
    # grade cannot tell a point spread evenly over four routes from one where
    # a single route supplied almost everything — and a breadth component that
    # has to assume an even spread is not measuring anything.
    lineage["route_cells_matched"] = {
        r.route: r.cells_matched for r in relatives
    }

    return IndexPoint(
        index_date=index_date,
        frequency=frequency,
        series=series,
        measure=measure,
        index_value=round(value, 4),
        base_period=base_period,
        basket_version=basket.basket_version,
        lineage=lineage,
        routes=relatives,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Base period
# ─────────────────────────────────────────────────────────────────────────────
def resolve_base_period(session: Session, only_live: bool = False) -> date:
    """
    The base period is the earliest cycle holding cleaned data, unless pinned by
    APIX_BASE_PERIOD. It is resolved once and reused: silently re-basing would
    make every historical value change underneath a consumer of the API.
    """
    if settings.base_period:
        return date.fromisoformat(settings.base_period)
    stmt = select(FareQuote.cycle_date).order_by(FareQuote.cycle_date.asc())
    if only_live:
        stmt = stmt.where(
            FareQuote.source_type.in_(REAL_SOURCE_TYPES),
            FareQuote.is_imputed.is_(False),
        )
    first = session.scalars(stmt.limit(1)).first()
    if first is None:
        raise IndexError_("no fare quotes in the database — nothing to base on")
    return first


# ─────────────────────────────────────────────────────────────────────────────
# Persistence / batch build
# ─────────────────────────────────────────────────────────────────────────────
def _persist(session: Session, point: IndexPoint) -> None:
    session.execute(
        delete(IndexValue).where(
            IndexValue.index_date == point.index_date,
            IndexValue.frequency == point.frequency,
            IndexValue.series == point.series,
            IndexValue.measure == point.measure,
            IndexValue.basket_version == point.basket_version,
        )
    )
    session.add(
        IndexValue(
            index_date=point.index_date,
            frequency=point.frequency,
            series=point.series,
            measure=point.measure,
            index_value=point.index_value,
            base_period=point.base_period,
            base_value=BASE_VALUE,
            basket_version=point.basket_version,
            data_lineage=point.lineage,
        )
    )


def build_series(
    start: date | None = None,
    end: date | None = None,
    frequency: Frequency = Frequency.DAILY,
    persist: bool = True,
    only_live: bool = False,
) -> list[IndexPoint]:
    """
    Compute all four series over a date range and (by default) store them.

    A cycle that cannot produce a point is skipped with a logged reason rather
    than filled with a placeholder — a gap in the chart is honest, an invented
    100.0 is not.
    """
    points: list[IndexPoint] = []
    with session_scope() as session:
        base_period = resolve_base_period(session, only_live=only_live)
        basket = load_live_basket() if only_live else load_basket()

        stmt = (
            select(FareQuote.cycle_date)
            .distinct()
            .order_by(FareQuote.cycle_date.asc())
        )
        if only_live:
            stmt = stmt.where(
                FareQuote.source_type.in_(REAL_SOURCE_TYPES),
                FareQuote.is_imputed.is_(False),
            )
        cycles = list(session.scalars(stmt).all())
        if start:
            cycles = [c for c in cycles if c >= start]
        if end:
            cycles = [c for c in cycles if c <= end]

        for cycle in cycles:
            for series in Series:
                for measure in Measure:
                    try:
                        point = compute_point(
                            session, cycle, base_period, series, measure,
                            frequency=frequency, basket=basket, only_live=only_live,
                        )
                    except IndexError_ as exc:
                        logger.info("skipping %s %s/%s: %s",
                                    cycle, series.value, measure.value, exc)
                        continue
                    points.append(point)
                    if persist:
                        _persist(session, point)

    logger.info("built %d index point(s) across %d cycle(s)",
                len(points), len({p.index_date for p in points}))
    return points


def weekly_from_daily(points: list[IndexPoint]) -> list[IndexPoint]:
    """
    Collapse daily points to ISO weeks by geometric mean.

    Geometric, not arithmetic: these are index numbers built multiplicatively,
    so averaging them multiplicatively is what keeps a week of +10% then -10%
    landing back near where it started. Lineage counts are summed across the
    week and the percentages recomputed from those sums.
    """
    buckets: dict[tuple, list[IndexPoint]] = {}
    for p in points:
        iso = p.index_date.isocalendar()
        buckets.setdefault(
            (iso.year, iso.week, p.series, p.measure, p.basket_version), []
        ).append(p)

    weekly: list[IndexPoint] = []
    for (_y, _w, series, measure, basket_version), members in sorted(
        buckets.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2].value, kv[0][3].value)
    ):
        members.sort(key=lambda p: p.index_date)
        # Week is labelled by its Monday so consecutive weeks are evenly spaced.
        week_start = members[0].index_date - timedelta(
            days=members[0].index_date.weekday()
        )
        value = math.exp(
            sum(math.log(m.index_value) for m in members) / len(members)
        )

        live = sum(m.lineage["live_count"] for m in members)
        # Summed with the same four buckets the daily lineage uses. Omitting
        # live_limited here would make the weekly percentages fail to sum to
        # 100 and would quietly discard a Tier-1.5 source's contribution.
        limited = sum(m.lineage.get("live_limited_count", 0) for m in members)
        sim = sum(m.lineage["simulated_count"] for m in members)
        imp = sum(m.lineage["imputed_count"] for m in members)
        total = live + limited + sim + imp
        pct = (lambda n: round(100.0 * n / total, 2) if total else 0.0)
        covered = sorted({r for m in members for r in m.lineage["routes_covered"]})

        weekly.append(
            IndexPoint(
                index_date=week_start,
                frequency=Frequency.WEEKLY,
                series=series,
                measure=measure,
                index_value=round(value, 4),
                base_period=members[0].base_period,
                basket_version=basket_version,
                lineage={
                    "quote_count": total,
                    "live_count": live,
                    "live_limited_count": limited,
                    "simulated_count": sim,
                    "imputed_count": imp,
                    "live_pct": pct(live),
                    "live_limited_pct": pct(limited),
                    "simulated_pct": pct(sim),
                    "imputed_pct": pct(imp),
                    "sources": sorted({s for m in members for s in m.lineage["sources"]}),
                    "routes_covered": covered,
                    "routes_missing": sorted(
                        {r for m in members for r in m.lineage["routes_missing"]}
                        - set(covered)
                    ),
                    "routes_in_basket": members[0].lineage["routes_in_basket"],
                    "weight_covered_pct": round(
                        sum(m.lineage["weight_covered_pct"] for m in members)
                        / len(members), 2,
                    ),
                    "low_coverage": any(m.lineage["low_coverage"] for m in members),
                    "days_in_week": len(members),
                    "aggregation": "geometric mean of daily points",
                },
            )
        )
    return weekly


# ─────────────────────────────────────────────────────────────────────────────
# Chain-linking (spec §5.5)
# ─────────────────────────────────────────────────────────────────────────────
def chain_link(
    old_series: list[tuple[date, float]],
    new_series: list[tuple[date, float]],
    overlap: date,
) -> list[tuple[date, float]]:
    """
    Splice a re-based series onto its predecessor at an overlap period.

    When the basket is revised (new DGCA weights, a route added or dropped) the
    new series starts from its own base and is NOT comparable to the old one in
    levels. Chain-linking rescales it by the ratio of the two values at a period
    both cover:

        link = old(overlap) / new(overlap)
        chained(t) = new(t) * link           for t >= overlap

    The level is then continuous across the revision while every growth rate
    within each segment is preserved — which is exactly what a statistical
    office does at a basket update, and why `basket_config` is versioned rather
    than edited in place.
    """
    old_map = dict(old_series)
    new_map = dict(new_series)
    if overlap not in old_map or overlap not in new_map:
        raise IndexError_(
            f"overlap period {overlap} is not present in both series — "
            "cannot chain-link without a common period"
        )
    if new_map[overlap] == 0:
        raise IndexError_(f"new series is zero at the overlap {overlap}")

    link = old_map[overlap] / new_map[overlap]
    chained = [(d, v) for d, v in sorted(old_series) if d < overlap]
    chained += [(d, round(v * link, 4)) for d, v in sorted(new_series) if d >= overlap]
    return chained
