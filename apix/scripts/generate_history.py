"""
Generate a simulated back-history so the dashboard and backtest have a series
to show on a fresh clone.

EVERY ROW THIS PRODUCES IS TAGGED `simulated`. That tag travels through
cleaning, into the index lineage, out through the API and onto the dashboard
badge. Nothing generated here is ever presented as observed market data.

Why simulate history at all: fares can only be observed forward in time. A
prototype started today has no past. Rather than leave the charts empty (or,
far worse, quietly backfill "live" rows for dates nobody scraped), we generate
a deterministic synthetic history, label it, and show the label everywhere.

Determinism: driven by `APIX_SIM_SEED`, so the same seed reproduces the same
history byte for byte, on any machine. That is what makes the demo repeatable
and the backtest reproducible.
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from sqlalchemy import delete

from apix.clean.pipeline import clean_cycle
from apix.config.settings import settings
from apix.db.models import FareQuote, IndexValue, ScrapeRun
from apix.db.session import create_all, session_scope
from apix.db.time_utils import today_ist
from apix.index.construct import build_series
from apix.scrape.runner import run_cycle

logger = logging.getLogger("apix.scripts.generate_history")


def generate(
    days: int,
    end: date | None = None,
    reset: bool = False,
    build_index: bool = True,
) -> dict:
    end = end or today_ist()
    start = end - timedelta(days=days - 1)

    if reset:
        with session_scope() as session:
            session.execute(delete(IndexValue))
            session.execute(delete(FareQuote))
            session.execute(delete(ScrapeRun))

    collected = cleaned = 0
    outliers = high_demand = imputed = refused = 0
    for i in range(days):
        cycle = start + timedelta(days=i)
        # live=False: Tier-A cannot observe a past date, and fabricating "live"
        # rows for one would be the single worst thing this codebase could do.
        report = run_cycle(cycle_date=cycle, live=False)
        collected += report.rows_written
        clean = clean_cycle(cycle)
        cleaned += clean.examined
        outliers += clean.outliers_flagged
        high_demand += clean.high_demand_outliers
        imputed += clean.imputed
        refused += clean.imputation_refused
        if (i + 1) % 10 == 0 or i == days - 1:
            print(f"  ... {i + 1}/{days} cycles ({cycle})")

    points = build_series() if build_index else []
    return {
        "start": start,
        "end": end,
        "cycles": days,
        "quotes": collected,
        "examined": cleaned,
        "outliers": outliers,
        "high_demand": high_demand,
        "imputed": imputed,
        "imputation_refused": refused,
        "index_points": len(points),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate simulated APIx back-history.")
    ap.add_argument("--days", type=int, default=90, help="number of cycles (default 90)")
    ap.add_argument("--end", type=date.fromisoformat, default=None,
                    help="last cycle date, YYYY-MM-DD (default: today IST)")
    ap.add_argument("--reset", action="store_true",
                    help="delete existing quotes, runs and index points first")
    ap.add_argument("--no-index", action="store_true", help="skip index construction")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    create_all()

    print(f"Generating {args.days} simulated cycle(s), seed={settings.sim_seed}")
    stats = generate(args.days, args.end, args.reset, not args.no_index)

    print()
    print(f"Done: {stats['start']} .. {stats['end']}")
    print(f"  fare quotes written    : {stats['quotes']}")
    print(f"  quotes examined        : {stats['examined']}")
    print(f"  statistical outliers   : {stats['outliers']}")
    print(f"  festival/high-demand   : {stats['high_demand']}")
    print(f"  imputed cells          : {stats['imputed']}")
    print(f"  imputation refused     : {stats['imputation_refused']} (past the "
          f"{settings.impute_max_age_days}-day cap)")
    print(f"  index points built     : {stats['index_points']}")
    print()
    print("  All of the above is SIMULATED data, tagged source_type='simulated'.")
    print("  Collect real observations with:  apix scrape --live")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
