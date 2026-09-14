"""
Test fixtures.

ISOLATION IS SET UP BEFORE ANY `apix` IMPORT
`apix.config.settings` reads the environment once, at import, and
`apix.db.session` builds its engine from that. So the environment has to be
right before the first import — which is why these three lines sit at module
top level, above the imports, rather than in a fixture.

Two things are forced:

  * a throwaway SQLite file per test session, so a run can never touch a
    developer's real `apix.db`;
  * `APIX_ENV_FILE` pointed at a path that does not exist, so a local `.env`
    cannot change a test outcome. A test suite whose result depends on an
    untracked file is not a test suite.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="apix-test-"))
os.environ["APIX_DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["APIX_ENV_FILE"] = str(_TMP / "no-such.env")
os.environ.setdefault("APIX_SIM_SEED", "20260909")

from datetime import date, timedelta  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from apix.db.models import (  # noqa: E402
    AdvanceWindow,
    BasketConfigRow,
    DgcaMonthlyAvg,
    FareQuote,
    IndexValue,
    ScrapeRun,
    SourceType,
    TripType,
)
from apix.db.session import create_all, session_scope  # noqa: E402
from apix.scrape.base import RawQuote  # noqa: E402

#: A fixed anchor so no test depends on the real calendar. Chosen to sit well
#: clear of any window in festivals.yaml; the festival tests pick their own
#: dates from the config rather than hardcoding one here.
ANCHOR = date(2026, 7, 6)  # a Monday, monsoon season, no festival


@pytest.fixture(scope="session", autouse=True)
def _schema():
    create_all()


@pytest.fixture(autouse=True)
def clean_db():
    """Every test starts from an empty database."""
    with session_scope() as s:
        s.execute(delete(IndexValue))
        s.execute(delete(FareQuote))
        s.execute(delete(ScrapeRun))
        s.execute(delete(BasketConfigRow))
        s.execute(delete(DgcaMonthlyAvg))
    yield


@pytest.fixture
def basket():
    """The real basket from basket.yaml, recorded in basket_config."""
    from scripts.seed_basket import seed_basket

    seed_basket(force=True)
    from apix.config.loader import load_basket

    return load_basket()


def make_quote(
    route: str = "DEL-BOM",
    carrier: str = "6E",
    window: AdvanceWindow = AdvanceWindow.T30,
    cycle: date = ANCHOR,
    base: float = 5000.0,
    taxes: float = 500.0,
    udf: float = 150.0,
    fee: float = 100.0,
    source_type: SourceType = SourceType.SIMULATED,
    source: str = "Simulator:6E",
    total: float | None = None,
    currency: str = "INR",
) -> RawQuote:
    """A well-formed RawQuote. Components sum to total unless `total` is forced."""
    origin, destination = route.split("-")
    return RawQuote(
        origin=origin,
        destination=destination,
        carrier=carrier,
        travel_date=cycle + timedelta(days=window.days),
        advance_window=window,
        base_fare=base,
        taxes=taxes,
        udf=udf,
        convenience_fee=fee,
        total_fare=base + taxes + udf + fee if total is None else total,
        source=source,
        source_type=source_type,
        cycle_date=cycle,
        currency=currency,
        trip_type=TripType.ONE_WAY,
    )


def store(quotes: list[RawQuote], run_id: int | None = None) -> int:
    """Persist RawQuotes through the real upsert path (not a shortcut insert)."""
    from apix.scrape.runner import upsert_quotes

    with session_scope() as s:
        # upsert_quotes keys run_ids by source_type value, matching the runner.
        return upsert_quotes(s, quotes, {q.source_type.value: run_id for q in quotes})


def fetch(cycle: date | None = None) -> list[FareQuote]:
    from sqlalchemy import select

    with session_scope() as s:
        stmt = select(FareQuote)
        if cycle:
            stmt = stmt.where(FareQuote.cycle_date == cycle)
        return list(s.scalars(stmt.order_by(FareQuote.id)).all())
