"""
Backtest: APIx against DGCA published monthly average fares.

THE SEPARATION RULE
DGCA monthly data is an EX-POST YARDSTICK ONLY. It is never an input to the
daily or weekly index — not as a fallback when a scrape is blocked, not as a
prior, not as a smoother. This module is the only place in the codebase that
reads `dgca_monthly_avg`, and `tests/test_dgca_separation.py` enforces that
both by static inspection and by poisoning the table and asserting the index
does not move.

Why it matters: an index validated against a source it was partly built from
validates nothing. Keeping them disjoint is what makes the correlation figure
mean something.

WHAT IS ACTUALLY BEING COMPARED
Two independently-constructed monthly series, each rebased to 100 at the first
common month:

  * APIx      — geometric mean of our daily index points within the month
  * DGCA ref  — the same basket weights applied to DGCA's published monthly
                average fares, converted to a fixed-base index

They are compared with Pearson correlation (do they move together?) and MAPE
(how far apart are the levels?).

HONESTY ABOUT SAMPLE SIZE
Pearson r on two points is always exactly ±1 and means nothing. On three it is
barely better. `BacktestResult.sufficient` is False below
`MIN_MONTHS_FOR_CORRELATION`, and every caller — API, dashboard, README — is
required to surface `months_compared` next to any correlation it prints. A
prototype that has collected six weeks of data must say so rather than quoting
an r-value as if it had years.
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select

from apix.config.loader import load_basket
from apix.db.models import DgcaMonthlyAvg, Frequency, IndexValue, Measure, Series
from apix.db.session import session_scope

logger = logging.getLogger("apix.index.backtest")

#: Below this many overlapping months, a correlation coefficient is not
#: reportable as evidence of anything.
MIN_MONTHS_FOR_CORRELATION = 6

#: Marker written into `dgca_monthly_avg.source_note` by the seed script when
#: the row is illustrative rather than transcribed from a DGCA publication.
#: Detected here so a backtest run against placeholder data can never be quoted
#: as validation against official statistics.
PLACEHOLDER_MARKER = "SYNTHETIC-PLACEHOLDER"


@dataclass
class MonthPair:
    month: str  # "YYYY-MM"
    apix: float
    dgca: float
    routes_used: list[str] = field(default_factory=list)

    @property
    def abs_pct_error(self) -> float:
        return abs(self.apix - self.dgca) / self.dgca * 100.0 if self.dgca else 0.0


@dataclass
class BacktestResult:
    pairs: list[MonthPair]
    pearson_r: float | None
    mape: float | None
    months_compared: int
    sufficient: bool
    base_month: str | None
    series: Series
    measure: Measure
    #: True when any reference row is illustrative rather than official DGCA
    #: data. The API and dashboard must badge the result when this is set.
    uses_placeholder_reference: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def reportable(self) -> bool:
        """Whether this result may be quoted as validation without caveat."""
        return self.sufficient and not self.uses_placeholder_reference

    def summary(self) -> str:
        lines = [
            f"backtest {self.series.value}/{self.measure.value} vs DGCA monthly",
            f"  months compared : {self.months_compared}",
            f"  base month      : {self.base_month}",
        ]
        if self.pearson_r is None:
            lines.append("  pearson r       : n/a (need at least 2 months)")
        else:
            lines.append(f"  pearson r       : {self.pearson_r:+.4f}")
        lines.append(
            f"  MAPE            : {self.mape:.2f}%" if self.mape is not None
            else "  MAPE            : n/a"
        )
        if self.uses_placeholder_reference:
            lines.append(
                "  !! REFERENCE DATA IS ILLUSTRATIVE, NOT OFFICIAL DGCA FIGURES."
            )
        if not self.sufficient:
            lines.append(
                f"  !! NOT STATISTICALLY MEANINGFUL: {self.months_compared} month(s) of "
                f"overlap, below the {MIN_MONTHS_FOR_CORRELATION}-month threshold. "
                "Report this alongside any figure quoted from this run."
            )
        lines += [f"  note            : {n}" for n in self.notes]
        for p in self.pairs:
            lines.append(
                f"    {p.month}  APIx {p.apix:7.2f}   DGCA-ref {p.dgca:7.2f}   "
                f"|err| {p.abs_pct_error:5.2f}%"
            )
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────────────
def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation. None when undefined (n < 2 or a series is constant)."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None  # a flat series has no correlation, not a perfect one
    return num / (dx * dy)


def mape(actual: list[float], predicted: list[float]) -> float | None:
    """Mean absolute percentage error. Skips zero denominators rather than dividing."""
    pairs = [(a, p) for a, p in zip(actual, predicted) if a != 0]
    if not pairs:
        return None
    return statistics.fmean(abs(a - p) / abs(a) * 100.0 for a, p in pairs)


# ─────────────────────────────────────────────────────────────────────────────
# Series assembly
# ─────────────────────────────────────────────────────────────────────────────
def _apix_monthly(
    series: Series, measure: Measure
) -> tuple[dict[str, float], list[str]]:
    """Monthly geometric mean of the stored daily index points."""
    notes: list[str] = []
    with session_scope() as session:
        rows = session.scalars(
            select(IndexValue).where(
                IndexValue.frequency == Frequency.DAILY,
                IndexValue.series == series,
                IndexValue.measure == measure,
            )
        ).all()
        buckets: dict[str, list[float]] = {}
        for r in rows:
            if r.index_value > 0:
                buckets.setdefault(r.index_date.strftime("%Y-%m"), []).append(
                    r.index_value
                )

    if not buckets:
        notes.append("no stored daily index points — run `apix index` first")
    partial = [m for m, v in buckets.items() if len(v) < 15]
    if partial:
        notes.append(
            f"month(s) {sorted(partial)} have fewer than 15 daily points and are "
            "monthly averages of a partial month"
        )
    monthly = {
        m: math.exp(statistics.fmean(math.log(v) for v in vals))
        for m, vals in buckets.items()
    }
    return monthly, notes


def _dgca_monthly() -> tuple[dict[str, float], dict[str, list[str]], list[str], bool]:
    """
    Basket-weighted average DGCA fare per month.

    Uses the SAME basket weights as APIx so the two series differ only in their
    underlying price data, not in how routes are combined. Weights are
    renormalised over the routes DGCA actually reports in that month, exactly as
    the index does when coverage is incomplete.
    """
    notes: list[str] = []
    basket = load_basket()
    weights = {r.route: r.weight for r in basket.routes}

    with session_scope() as session:
        rows = session.scalars(select(DgcaMonthlyAvg)).all()

    by_month: dict[str, dict[str, float]] = {}
    for r in rows:
        if r.route in weights:
            by_month.setdefault(r.month, {})[r.route] = r.avg_fare

    ignored = sorted({r.route for r in rows if r.route not in weights})
    if ignored:
        notes.append(f"DGCA rows for non-basket route(s) {ignored} ignored")

    placeholders = sum(1 for r in rows if PLACEHOLDER_MARKER in (r.source_note or ""))
    if placeholders:
        notes.append(
            f"!! {placeholders} of {len(rows)} reference row(s) are "
            f"{PLACEHOLDER_MARKER} — illustrative values, NOT transcribed from a "
            "DGCA publication. This run exercises the backtest mechanism; it is "
            "not validation against official statistics and must not be reported "
            "as such. Load real figures with `apix seed-dgca --csv <file>`."
        )

    monthly: dict[str, float] = {}
    routes_used: dict[str, list[str]] = {}
    for month, fares in by_month.items():
        wsum = sum(weights[r] for r in fares)
        if wsum <= 0:
            continue
        monthly[month] = sum(weights[r] / wsum * f for r, f in fares.items())
        routes_used[month] = sorted(fares)
    return monthly, routes_used, notes, bool(placeholders)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def run_backtest(
    series: Series = Series.HEADLINE,
    measure: Measure = Measure.TOTAL,
) -> BacktestResult:
    """
    Compare APIx to the DGCA reference over their overlapping months.

    Both series are rebased to 100 at the first common month so levels are
    comparable — APIx is an index and DGCA is a rupee average, and comparing
    them raw would be a category error.
    """
    apix, notes_a = _apix_monthly(series, measure)
    dgca, routes_used, notes_d, placeholder = _dgca_monthly()
    notes = notes_a + notes_d

    common = sorted(set(apix) & set(dgca))
    if not common:
        notes.append(
            "no overlapping months between APIx and DGCA — "
            "seed DGCA data with `apix seed-dgca` and build the index first"
        )
        return BacktestResult(
            [], None, None, 0, False, None, series, measure, placeholder, notes
        )

    base_month = common[0]
    a0, d0 = apix[base_month], dgca[base_month]
    pairs = [
        MonthPair(
            month=m,
            apix=round(100.0 * apix[m] / a0, 4),
            dgca=round(100.0 * dgca[m] / d0, 4),
            routes_used=routes_used.get(m, []),
        )
        for m in common
    ]

    xs = [p.apix for p in pairs]
    ys = [p.dgca for p in pairs]
    r = pearson(xs, ys)
    err = mape(ys, xs)

    if len(pairs) < MIN_MONTHS_FOR_CORRELATION:
        notes.append(
            f"Only {len(pairs)} overlapping month(s). Correlation over so few "
            "points is not evidence; quote it with the month count attached or "
            "not at all."
        )
    if len(pairs) >= 2 and r is None:
        notes.append("one of the series is constant over the overlap — r undefined")

    result = BacktestResult(
        pairs=pairs,
        pearson_r=round(r, 6) if r is not None else None,
        mape=round(err, 4) if err is not None else None,
        months_compared=len(pairs),
        sufficient=len(pairs) >= MIN_MONTHS_FOR_CORRELATION and r is not None,
        base_month=base_month,
        series=series,
        measure=measure,
        uses_placeholder_reference=placeholder,
        notes=notes,
    )
    logger.info("backtest complete: %s", result.summary())
    return result
