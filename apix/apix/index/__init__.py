"""
Index layer.

  construct.py — Jevons elementary aggregates, Laspeyres upper aggregate with
                 DGCA passenger-traffic weights, four published series, full
                 lineage on every point, chain-linking across basket revisions.
  backtest.py  — ex-post comparison against DGCA monthly averages. The ONLY
                 module permitted to read `dgca_monthly_avg`.
"""
from apix.index.backtest import (  # noqa: F401
    MIN_MONTHS_FOR_CORRELATION,
    BacktestResult,
    mape,
    pearson,
    run_backtest,
)
from apix.index.construct import (  # noqa: F401
    BASE_VALUE,
    IndexPoint,
    RouteRelative,
    build_series,
    chain_link,
    compute_point,
    elementary_relative,
    resolve_base_period,
    weekly_from_daily,
)
