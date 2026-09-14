"""
SQLAlchemy 2.x models for APIx.

Structural separation is deliberate and load-bearing:

  * `fare_quotes`      — the ONLY table the daily/weekly index reads from.
  * `dgca_monthly_avg` — backtest comparison ONLY. Never joined to, never read
                         by apix.index.construct. Enforced by
                         tests/test_dgca_separation.py.

Both PostgreSQL (deployment) and SQLite (local/test) are supported, so no
Postgres-only column types are used. Enums are emitted as VARCHAR + CHECK
(`native_enum=False`) for the same reason.
"""
from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from apix.db.time_utils import now_ist


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────
class SourceType(str, enum.Enum):
    """The provenance tag that must survive every stage of the pipeline."""

    LIVE = "live"
    LIVE_LIMITED = "live_limited"   # Tier-1.5: rate-capped OTA, weaker legal clearance
    SIMULATED = "simulated"

    @property
    def is_live(self) -> bool:
        """True for any real observed data (Tier-1 or Tier-1.5)."""
        return self in (SourceType.LIVE, SourceType.LIVE_LIMITED)


#: The same rule as `SourceType.is_live`, in a form a SQL `IN` clause can use.
#:
#: Both exist because a Python property cannot be evaluated by the database.
#: It is a named constant rather than an inline literal because the membership
#: was written out by hand in five separate queries and four of them listed only
#: LIVE — which silently under-counted real observations in `apix status`,
#: `/v1/health`, `/v1/routes` and the clean report, and let the simulator
#: overwrite Tier-1.5 rows in the upsert guard.
REAL_SOURCE_TYPES = (SourceType.LIVE, SourceType.LIVE_LIMITED)


class QuoteStatus(str, enum.Enum):
    OK = "ok"
    IMPUTED = "imputed"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    VALIDATION_REJECT = "validation_reject"


class AdvanceWindow(str, enum.Enum):
    T1 = "T+1"
    T7 = "T+7"
    T15 = "T+15"
    T30 = "T+30"
    T45 = "T+45"

    @property
    def days(self) -> int:
        return int(self.value.split("+")[1])

    @classmethod
    def from_days(cls, days: int) -> "AdvanceWindow":
        for w in cls:
            if w.days == days:
                return w
        raise ValueError(f"{days} is not a configured advance-purchase window")


class TripType(str, enum.Enum):
    ONE_WAY = "one_way"
    RETURN = "return"


class Frequency(str, enum.Enum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class Series(str, enum.Enum):
    """
    HEADLINE includes festival/high-demand outliers (what a traveller actually
    pays — CPI-consistent). CORE excludes them, isolating the underlying
    pricing trend from calendar-driven demand spikes.
    """

    HEADLINE = "headline"
    CORE = "core"


class Measure(str, enum.Enum):
    """
    Which fare aggregate the index is built on. This is a SEPARATE axis from
    `Series`, and deliberately so — collapsing the two would silently lose one
    of them.

      TOTAL — base + taxes + UDF + convenience fee. What the traveller actually
              parts with, and therefore the figure consistent with CPI
              treatment. This is the series a statistical agency (MoSPI/NSO)
              would fold into the transport basket.
      BASE  — base fare only, stripped of statutory taxes and fees. Isolates
              carrier pricing behaviour from tax policy, so a GST change does
              not read as inflation. This is the series a central bank (RBI)
              would want when reading underlying price pressure.

    Crossing the two axes gives four published series (headline/core x
    total/base), each stored as its own row with its own lineage.
    """

    TOTAL = "total"
    BASE = "base"


def _enum(e, name: str):
    """VARCHAR-backed enum so the schema is identical on SQLite and PostgreSQL."""
    return Enum(e, name=name, native_enum=False, values_callable=lambda x: [i.value for i in x])


# ─────────────────────────────────────────────────────────────────────────────
# §4.5  scrape_runs — the audit ledger
# ─────────────────────────────────────────────────────────────────────────────
class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[SourceType] = mapped_column(_enum(SourceType, "source_type"), index=True)
    cycle_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_ist, index=True
    )
    # {"OK": 12, "BLOCKED_CAPTCHA": 1, ...} — one entry per StatusCode seen.
    status_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    coverage_pct: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    quotes: Mapped[list["FareQuote"]] = relationship(back_populates="run")


# ─────────────────────────────────────────────────────────────────────────────
# §4.1  fare_quotes — the only input to the index
# ─────────────────────────────────────────────────────────────────────────────
class FareQuote(Base):
    __tablename__ = "fare_quotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scrape_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # ── Route / itinerary identity ───────────────────────────────────────────
    origin: Mapped[str] = mapped_column(String(3))
    destination: Mapped[str] = mapped_column(String(3))
    route: Mapped[str] = mapped_column(String(7), index=True)  # "DEL-BOM"
    carrier: Mapped[str] = mapped_column(String(8), index=True)
    advance_purchase_window: Mapped[AdvanceWindow] = mapped_column(
        _enum(AdvanceWindow, "advance_window"), index=True
    )
    fare_class: Mapped[str] = mapped_column(String(16), default="economy")
    trip_type: Mapped[TripType] = mapped_column(_enum(TripType, "trip_type"),
                                                default=TripType.ONE_WAY)
    is_nonstop: Mapped[bool] = mapped_column(Boolean, default=True)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    travel_date: Mapped[date] = mapped_column(Date, index=True)

    # ── Fare decomposition (§3.4 — never collapsed to total alone) ────────────
    base_fare: Mapped[float] = mapped_column(Float)
    taxes: Mapped[float] = mapped_column(Float, default=0.0)
    udf: Mapped[float] = mapped_column(Float, default=0.0)
    convenience_fee: Mapped[float] = mapped_column(Float, default=0.0)
    total_fare: Mapped[float] = mapped_column(Float)

    # ── Provenance (constitution §1) ─────────────────────────────────────────
    source: Mapped[str] = mapped_column(String(64), index=True)  # "AkasaAir" | "Simulator:6E"
    source_type: Mapped[SourceType] = mapped_column(_enum(SourceType, "source_type"), index=True)
    scrape_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_ist, index=True
    )
    cycle_date: Mapped[date] = mapped_column(Date, index=True)

    # ── Cleaning flags (nothing is ever silently deleted) ────────────────────
    status: Mapped[QuoteStatus] = mapped_column(_enum(QuoteStatus, "quote_status"),
                                                default=QuoteStatus.OK, index=True)
    is_outlier: Mapped[bool] = mapped_column(Boolean, default=False)
    is_high_demand_outlier: Mapped[bool] = mapped_column(Boolean, default=False)
    is_imputed: Mapped[bool] = mapped_column(Boolean, default=False)
    imputed_from_cycle: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_component_mismatch: Mapped[bool] = mapped_column(Boolean, default=False)

    # ── Idempotency (§2.5) ───────────────────────────────────────────────────
    dedupe_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    run: Mapped[ScrapeRun | None] = relationship(back_populates="quotes")

    __table_args__ = (
        # (origin, destination, scrape_timestamp) per spec §4.1. The separate
        # source_type index is created by `index=True` on that column above.
        Index("ix_fare_quotes_route_ts", "origin", "destination", "scrape_timestamp"),
        CheckConstraint("base_fare >= 0", name="ck_fare_quotes_base_nonneg"),
        CheckConstraint("total_fare >= 0", name="ck_fare_quotes_total_nonneg"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# §4.2  dgca_monthly_avg — BACKTEST ONLY
# ─────────────────────────────────────────────────────────────────────────────
class DgcaMonthlyAvg(Base):
    """
    Historical DGCA monthly average fares (legacy backtest reference).
    """

    __tablename__ = "dgca_monthly_avg"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route: Mapped[str] = mapped_column(String(7), index=True)
    month: Mapped[str] = mapped_column(String(7), index=True)  # "YYYY-MM"
    avg_fare: Mapped[float] = mapped_column(Float)
    passenger_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (UniqueConstraint("route", "month", name="uq_dgca_route_month"),)


# ─────────────────────────────────────────────────────────────────────────────
# §4.2b  external_benchmark_avg_fare — REFERENCE POINT VALIDATION ONLY
# ─────────────────────────────────────────────────────────────────────────────
class ExternalBenchmarkAvgFare(Base):
    """
    Independently-published external fare benchmark.
    Used exclusively for reference point validation against real market averages
    (e.g., Ixigo average one-way fare data, Dec 2024, as reported by Indian Express).
    """

    __tablename__ = "external_benchmark_avg_fare"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route: Mapped[str] = mapped_column(String(7), index=True)
    direction: Mapped[str] = mapped_column(String(64), default="both")
    benchmark_fare: Mapped[float] = mapped_column(Float)
    benchmark_month: Mapped[str] = mapped_column(String(7), index=True)  # "2024-12"
    source_citation: Mapped[str] = mapped_column(String(500))
    article_url: Mapped[str] = mapped_column(String(500))
    retrieval_date: Mapped[date] = mapped_column(Date)

    __table_args__ = (
        UniqueConstraint("route", "direction", "benchmark_month", name="uq_benchmark_route_dir_month"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# §4.3  basket_config — versioned, never overwritten
# ─────────────────────────────────────────────────────────────────────────────
class BasketConfigRow(Base):
    __tablename__ = "basket_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route: Mapped[str] = mapped_column(String(7), index=True)
    weight: Mapped[float] = mapped_column(Float)
    national_share_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_source: Mapped[str] = mapped_column(String(500))
    basket_version: Mapped[str] = mapped_column(String(64), index=True)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (
        UniqueConstraint("basket_version", "route", name="uq_basket_version_route"),
        CheckConstraint("weight > 0", name="ck_basket_weight_positive"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# §4.4  index_values — persisted with full lineage
# ─────────────────────────────────────────────────────────────────────────────
class IndexValue(Base):
    __tablename__ = "index_values"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_date: Mapped[date] = mapped_column(Date, index=True)
    frequency: Mapped[Frequency] = mapped_column(_enum(Frequency, "frequency"), index=True)
    series: Mapped[Series] = mapped_column(_enum(Series, "series"), index=True)
    measure: Mapped[Measure] = mapped_column(
        _enum(Measure, "measure"), default=Measure.TOTAL, index=True
    )
    index_value: Mapped[float] = mapped_column(Float)
    base_period: Mapped[date] = mapped_column(Date)
    base_value: Mapped[float] = mapped_column(Float, default=100.0)
    basket_version: Mapped[str] = mapped_column(String(64), index=True)

    # {"live_pct":.., "simulated_pct":.., "imputed_pct":.., "sources":[..],
    #  "routes_covered":[..], "routes_missing":[..], "quote_count":..}
    data_lineage: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "index_date", "frequency", "series", "measure", "basket_version",
            name="uq_index_point",
        ),
        CheckConstraint("index_value >= 0", name="ck_index_nonneg"),
    )
