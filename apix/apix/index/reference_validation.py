"""
Reference Point Validation: Live Index route averages vs external independently-published benchmarks.

Replaces the synthetic-against-synthetic backtest.
Compares calculated route averages from genuine live data against published
Ixigo / The Indian Express December 2024 one-way fare figures.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select

from apix.db.models import ExternalBenchmarkAvgFare, FareQuote, Measure, QuoteStatus, REAL_SOURCE_TYPES
from apix.db.session import session_scope

logger = logging.getLogger("apix.index.reference_validation")

VALIDATION_CAPTION = (
    "Compares calculated averages against the nearest available independently-published "
    "fare benchmark (Ixigo/Indian Express, Dec 2024), since DGCA does not publish "
    "route-level fare data. This is a point check, not a 30-day trend backtest — "
    "full trend validation requires 30 days of continuous live operation."
)


@dataclass
class RouteValidationPoint:
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


@dataclass
class ReferenceValidationResult:
    points: list[RouteValidationPoint]
    routes_compared: int
    overall_mape: float
    caption: str
    reportable: bool = True
    notes: list[str] = field(default_factory=list)


def run_reference_validation(
    cycle_date: date | None = None,
    measure: Measure = Measure.TOTAL,
) -> ReferenceValidationResult:
    """
    Perform point deviation check: compare live quote averages against external benchmarks.
    Zero simulated data is included in this validation.
    """
    with session_scope() as session:
        # Load external benchmarks
        benchmarks = session.scalars(
            select(ExternalBenchmarkAvgFare).order_by(ExternalBenchmarkAvgFare.route)
        ).all()
        bench_map = {b.route: b for b in benchmarks}

        # Query live quotes (strictly real observations, not imputed, not synthetic)
        stmt = (
            select(FareQuote)
            .where(
                FareQuote.source_type.in_(REAL_SOURCE_TYPES),
                FareQuote.is_imputed.is_(False),
                FareQuote.status != QuoteStatus.VALIDATION_REJECT,
            )
        )
        if cycle_date:
            stmt = stmt.where(FareQuote.cycle_date == cycle_date)

        quotes = session.scalars(stmt).all()

        route_prices: dict[str, list[float]] = {}
        for q in quotes:
            price = q.total_fare if measure is Measure.TOTAL else q.base_fare
            if price and price > 0:
                route_prices.setdefault(q.route, []).append(price)

        points: list[RouteValidationPoint] = []
        mapes: list[float] = []

        for route, bench in bench_map.items():
            prices = route_prices.get(route, [])
            if not prices:
                continue
            live_avg = round(float(statistics.mean(prices)), 2)
            diff = round(live_avg - bench.benchmark_fare, 2)
            pct_dev = round((diff / bench.benchmark_fare) * 100.0, 2)
            mapes.append(abs(pct_dev))

            points.append(
                RouteValidationPoint(
                    route=route,
                    direction=bench.direction,
                    live_avg_fare=live_avg,
                    benchmark_fare=bench.benchmark_fare,
                    diff_inr=diff,
                    pct_deviation=pct_dev,
                    quotes_count=len(prices),
                    source_citation=bench.source_citation,
                    article_url=bench.article_url,
                    benchmark_month=bench.benchmark_month,
                )
            )

        overall_mape = round(statistics.mean(mapes), 2) if mapes else 0.0

        notes = []
        if not points:
            notes.append("No live observations found for covered benchmark routes.")
        else:
            notes.append(
                f"Validated {len(points)} route(s) using genuine live data against published December 2024 benchmarks."
            )

        return ReferenceValidationResult(
            points=points,
            routes_compared=len(points),
            overall_mape=overall_mape,
            caption=VALIDATION_CAPTION,
            reportable=len(points) > 0,
            notes=notes,
        )
