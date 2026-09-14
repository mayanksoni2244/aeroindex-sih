"""
Cycle orchestration.

One *cycle* = one collection pass over the basket for one IST calendar date.

Order of operations, and why:

  1. Tier-A (live) runs FIRST, for the routes in `APIX_LIVE_ROUTES`. Real
     observations get first claim on every itinerary cell they cover.
  2. The cells Tier-A actually filled become `skip_keys`. Tier-B is told to
     leave them alone, so a genuine observation can never be displaced by, or
     double-counted against, a synthetic one.
  3. Tier-B (simulated) fills the remainder of the basket so the index has a
     complete panel — every one of those rows tagged `simulated`.
  4. Everything is de-duplicated in Python (§3.1: collapse to the most complete
     record), then upserted on `dedupe_hash`. Re-running a cycle is a no-op
     rather than a doubling (§2.5).
  5. A `scrape_runs` row is written per source with its full status histogram,
     whether or not anything was collected. A blocked run leaves a record; it
     does not leave silence.

Tier-A is attempted only when the cycle date is today (IST). You cannot scrape
a past date, and pretending otherwise would put fabricated "live" rows in the
history. Historical cycles are therefore 100% simulated and say so.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from apix.config.loader import ConfigError, RouteMeta, load_routes
from apix.config.settings import settings
from apix.db.models import REAL_SOURCE_TYPES, FareQuote, QuoteStatus, ScrapeRun, SourceType
from apix.db.session import session_scope
from apix.db.time_utils import now_ist, today_ist
from apix.scrape.base import FetchOutcome, RawQuote, StatusCode
from apix.scrape.live import get_live_source
from apix.scrape.simulate import simulate_cycle

logger = logging.getLogger("apix.scrape.runner")


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class CycleReport:
    """What one cycle actually did. Printed by the CLI, surfaced by /v1/coverage."""

    cycle_date: date
    live_source: str
    live_status_counts: dict[str, int] = field(default_factory=dict)
    sim_status_counts: dict[str, int] = field(default_factory=dict)
    quotes_live: int = 0
    quotes_simulated: int = 0
    rows_written: int = 0
    duplicates_collapsed: int = 0
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def quotes_total(self) -> int:
        return self.quotes_live + self.quotes_simulated

    @property
    def live_pct(self) -> float:
        return 100.0 * self.quotes_live / self.quotes_total if self.quotes_total else 0.0

    def summary(self) -> str:
        lines = [
            f"cycle {self.cycle_date}  ({self.duration_ms} ms)",
            f"  live source     : {self.live_source}",
            f"  live statuses   : {self.live_status_counts or '{}'}",
            f"  quotes          : {self.quotes_total} "
            f"({self.quotes_live} live = {self.live_pct:.1f}% live)",
            f"  rows written    : {self.rows_written} "
            f"({self.duplicates_collapsed} duplicate(s) collapsed)",
        ]
        # Printed, not just stored. A cycle that collected nothing live has a
        # reason, and the operator should not have to query the database to
        # find out what it was.
        for note in self.notes[:12]:
            lines.append(f"  - {note}")
        if len(self.notes) > 12:
            lines.append(f"  - ... and {len(self.notes) - 12} more (see scrape_runs.notes)")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication (spec §3.1)
# ─────────────────────────────────────────────────────────────────────────────
def _preference(q: RawQuote) -> tuple:
    """
    Sort key for choosing a winner among quotes sharing a dedupe_hash.
    Higher is better. Live always beats simulated; then the more itemised
    record; then the cheaper one, purely so the choice is deterministic.
    """
    return (
        # `.is_live`, not `is SourceType.LIVE`: a Tier-1.5 (LIVE_LIMITED)
        # observation is still a real observation and must outrank a synthetic
        # one. Comparing against LIVE alone scored live_limited and simulated
        # identically at 0, so a simulated row with more populated components
        # could win the cell and bury a real fare.
        1 if q.source_type.is_live else 0,
        q.completeness(),
        -q.total_fare,
    )


def dedupe(quotes: list[RawQuote]) -> tuple[list[RawQuote], int]:
    """Collapse quotes sharing a dedupe_hash. Returns (kept, n_collapsed)."""
    best: dict[str, RawQuote] = {}
    collapsed = 0
    for q in quotes:
        key = q.dedupe_hash()
        incumbent = best.get(key)
        if incumbent is None:
            best[key] = q
            continue
        collapsed += 1
        if _preference(q) > _preference(incumbent):
            best[key] = q
    return list(best.values()), collapsed


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────
def _row_from_quote(q: RawQuote, run_id: int | None) -> dict:
    return {
        "run_id": run_id,
        "origin": q.origin,
        "destination": q.destination,
        "route": q.route,
        "carrier": q.carrier,
        "advance_purchase_window": q.advance_window,
        "fare_class": q.fare_class,
        "trip_type": q.trip_type,
        "is_nonstop": q.is_nonstop,
        "currency": q.currency,
        "travel_date": q.travel_date,
        "base_fare": q.base_fare,
        "taxes": q.taxes,
        "udf": q.udf,
        "convenience_fee": q.convenience_fee,
        "total_fare": q.total_fare,
        "source": q.source,
        "source_type": q.source_type,
        "scrape_timestamp": q.scrape_timestamp,
        "cycle_date": q.cycle_date,
        "status": QuoteStatus.OK,
        "dedupe_hash": q.dedupe_hash(),
    }


def upsert_quotes(session: Session, quotes: list[RawQuote], run_ids_by_source: dict[str, int]) -> int:
    """
    Idempotent write keyed on `dedupe_hash` (§2.5).

    The ON CONFLICT clause carries a guard: an existing **live** row is never
    overwritten by a **simulated** one. Tier-B already skips live-covered cells,
    so this is a second line of defence — if the skip logic were ever broken,
    the database would still refuse to let synthetic data bury an observation.

    THE UPDATE ALSO CLEARS THE CLEANING FLAGS, and that is not cosmetic.
    `clean._imputed_hash` deliberately produces the same hash as
    `RawQuote.dedupe_hash` so that a carried-forward filler occupies its
    itinerary cell and a later real observation *replaces* it instead of
    duplicating it. But the flags describe the value, and the value has just
    changed. Leaving them behind left a genuine Akasa fare sitting in a row
    still marked `is_imputed=True`, which had two consequences, both bad:

      1. Every provenance count — `apix status`, /v1/coverage, /v1/heatmap, the
         index lineage — filed that real observation under "carried forward".
         The live share was reported lower than it actually was, and
         under-reporting real coverage is still misreporting it.
      2. `clean._reset_cycle` DELETES rows flagged `is_imputed` for the cycle,
         on the correct assumption that they are pipeline-created fillers. With
         a stale flag that assumption is false, so the next `apix clean` would
         have destroyed a real observed fare that took a 51-minute live run to
         collect.

    `is_outlier` / `is_high_demand_outlier` / `is_component_mismatch` are reset
    for the same reason: they are verdicts on the previous value. `clean`
    re-derives them, but between a scrape and a clean the API is readable, and
    it must not report a verdict about a fare that is no longer there.
    """
    if not quotes:
        return 0

    rows = [_row_from_quote(q, run_ids_by_source.get(q.source)) for q in quotes]
    table = FareQuote.__table__
    dialect = session.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert

    written = 0
    # Chunked so a large history backfill does not build one enormous statement.
    for i in range(0, len(rows), 500):
        chunk = rows[i : i + 500]
        stmt = insert(table).values(chunk)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.dedupe_hash],
            set_={
                "base_fare": excluded.base_fare,
                "taxes": excluded.taxes,
                "udf": excluded.udf,
                "convenience_fee": excluded.convenience_fee,
                "total_fare": excluded.total_fare,
                "source": excluded.source,
                "source_type": excluded.source_type,
                "scrape_timestamp": excluded.scrape_timestamp,
                "run_id": excluded.run_id,
                # The row now holds a freshly collected observation, so it is
                # by definition not carried forward from an earlier cycle.
                "status": excluded.status,
                "is_imputed": False,
                "imputed_from_cycle": None,
                "is_outlier": False,
                "is_high_demand_outlier": False,
                "is_component_mismatch": False,
            },
            where=~(
                # Any real observation, Tier-1 or Tier-1.5. Naming only LIVE
                # here left live_limited rows overwritable by the simulator.
                table.c.source_type.in_([t.value for t in REAL_SOURCE_TYPES])
                & (excluded.source_type == SourceType.SIMULATED.value)
            ),
        )
        session.execute(stmt)
        written += len(chunk)
    return written


def _record_run(
    session: Session,
    source: str,
    source_type: SourceType,
    status_counts: dict[str, int],
    duration_ms: int,
    note: str,
) -> int:
    attempted = sum(status_counts.values())
    ok = status_counts.get(StatusCode.OK.value, 0)
    run = ScrapeRun(
        source=source,
        source_type=source_type,
        cycle_timestamp=now_ist(),
        status_counts=status_counts,
        coverage_pct=round(100.0 * ok / attempted, 2) if attempted else 0.0,
        duration_ms=duration_ms,
        notes=note[:1000] or None,
    )
    session.add(run)
    session.flush()
    return run.id


# ─────────────────────────────────────────────────────────────────────────────
# Tier-A
# ─────────────────────────────────────────────────────────────────────────────
async def collect_live_source(
    cycle_date: date,
    source_key: str,
) -> tuple[list[RawQuote], dict[str, int], str, SourceType, list[str]]:
    """
    Run one Tier-A/1.5 adapter over `APIX_LIVE_ROUTES` × advance windows.

    THE GRID IS WALKED CONCURRENTLY, BUT THE REQUEST RATE IS UNCHANGED.
    Every search still passes through the adapter's shared `TokenBucket`, which
    is a single asyncio-locked gate at `scrape_rate_per_min`. Concurrency
    therefore buys exactly one thing: the 30 cells overlap their waiting
    instead of queueing it. A cell costs a few seconds of work and up to 75
    seconds of waiting for Akasa's fare POST, so serially the cycle was ~51
    minutes of mostly-idle time. What it does NOT do is send more traffic per
    minute — that ceiling belongs to the bucket, not to the loop, and raising
    politeness limits to go faster is not on the table.

    `APIX_LIVE_CONCURRENCY` caps how many pages are open at once. It is small
    on purpose: each page is a real Chromium tab, and a host being polite to
    should not see a burst of tabs either.

    FAILURE IS PER-CELL. `source.search` is contracted never to raise, but this
    wraps each cell anyway — if one ever does, that cell alone becomes a
    NETWORK_ERROR note and the other 29 still land. A cycle that loses one cell
    is a coverage number; a cycle that raises loses every cell collected so far,
    because nothing is written until the run completes.
    """
    notes: list[str] = []
    counts: dict[str, int] = {}

    def bump(status: StatusCode) -> None:
        counts[status.value] = counts.get(status.value, 0) + 1

    try:
        source = get_live_source(source_key)
    except KeyError as exc:
        notes.append(str(exc))
        bump(StatusCode.ADAPTER_UNAVAILABLE)
        return [], counts, source_key, SourceType.LIVE, notes

    routes_cfg = load_routes()

    # Build the full grid first so the fan-out is over a flat list of cells
    # rather than nested loops — a route missing from routes.yaml is reported
    # once here instead of being rediscovered inside every worker.
    cells: list[tuple[str, RouteMeta, int]] = []
    for route_code in settings.live_route_list:
        try:
            meta = routes_cfg.by_route(route_code)
        except ConfigError:
            notes.append(f"{route_code} is not in routes.yaml — skipped")
            continue
        for window_days in routes_cfg.advance_windows:
            cells.append((route_code, meta, window_days))

    quotes: list[RawQuote] = []
    # Set as soon as any cell is blocked. Every worker checks it before
    # starting, so a block stops the source across all of them rather than
    # only on the branch that happened to see it. The constitution's rule is
    # "on a block, log it and stop for that source" — concurrency must not
    # turn that into "stop one of five workers".
    blocked = asyncio.Event()
    sem = asyncio.Semaphore(max(1, settings.live_concurrency))

    async def collect_cell(
        route_code: str, meta: RouteMeta, window_days: int
    ) -> tuple[str, int, FetchOutcome | None]:
        if blocked.is_set():
            return route_code, window_days, None
        travel_date = cycle_date + timedelta(days=window_days)
        async with sem:
            # Re-check after queueing: a block may have landed while waiting.
            if blocked.is_set():
                return route_code, window_days, None
            try:
                outcome = await source.search(
                    meta.origin, meta.destination, travel_date
                )
            except Exception as exc:
                # The adapter promises not to raise. If that promise is ever
                # broken, one cell must not take the cycle down with it.
                outcome = FetchOutcome.fail(
                    StatusCode.NETWORK_ERROR,
                    f"unhandled {type(exc).__name__}: {exc}"[:200],
                )
        if outcome.status is StatusCode.BLOCKED_CAPTCHA:
            blocked.set()
        return route_code, window_days, outcome

    try:
        results = await asyncio.gather(
            *(collect_cell(rc, meta, wd) for rc, meta, wd in cells)
        )
    finally:
        await source.close()

    # Tally in grid order, not completion order, so two runs over the same
    # inventory produce the same notes in the same sequence.
    skipped = 0
    for route_code, window_days, outcome in results:
        if outcome is None:
            skipped += 1
            continue
        bump(outcome.status)
        if outcome.status is StatusCode.OK:
            quotes.extend(outcome.quotes)
        else:
            notes.append(
                f"{route_code} T+{window_days}: {outcome.status.value}"
                + (f" — {outcome.note}" if outcome.note else "")
            )

    if blocked.is_set():
        notes.append(
            f"Stopping Tier-A for {source.name} after a block "
            f"(no bypass attempted); {skipped} cell(s) not attempted."
        )

    return quotes, counts, source.name, source.default_source_type, notes


# ─────────────────────────────────────────────────────────────────────────────
# Full cycle
# ─────────────────────────────────────────────────────────────────────────────
def tier_a_plan(cycle_date: date, live: bool | None = None) -> tuple[bool, str]:
    """
    Decide whether Tier-A is attempted for this cycle; explain it when it is not.

    Split out of `run_cycle` because it is the one decision that determines
    whether a cycle can contain real data at all, and both reasons for skipping
    must reach the run notes — a 100%-simulated cycle has to say why, or a
    reader cannot tell a deliberate historical backfill from a scraper that
    quietly stopped working.

    An explicit `live=` wins over `APIX_LIVE_ENABLED`: `apix scrape --live` is a
    direct instruction, and the env flag is only the default. `live=None`
    follows the flag.
    """
    if live is False or (live is None and not settings.live_enabled):
        return False, (
            "Tier-A explicitly disabled for this cycle, so it is 100% simulated."
        )
    if cycle_date != today_ist():
        return False, (
            f"Tier-A skipped: cycle date {cycle_date} is not today ({today_ist()}). "
            "Live fares cannot be observed retroactively, so this cycle is 100% simulated."
        )
    return True, ""


def run_cycle(cycle_date: date | None = None, live: bool | None = None) -> CycleReport:
    """
    Execute one full collection cycle and persist it.

    `live=False` forces a simulated-only cycle (used by history generation and
    by tests); `live=True` forces an attempt; `live=None` follows
    `APIX_LIVE_ENABLED`. See `tier_a_plan`.
    """
    started = time.monotonic()
    cycle_date = cycle_date or today_ist()

    attempt_live, skip_reason = tier_a_plan(cycle_date, live)
    if not attempt_live:
        live_quotes: list[RawQuote] = []
        notes = [skip_reason]
        live_runs = []
        live_source_name = (
            "(disabled)" if (live is False or not settings.live_enabled)
            else "(not attempted)"
        )
    else:
        live_quotes = []
        notes = []
        live_runs = []
        names = []
        for source_key in settings.live_sources_list:
            q, c, name, stype, n = asyncio.run(collect_live_source(cycle_date, source_key))
            live_quotes.extend(q)
            notes.extend(n)
            live_runs.append((name, stype, c))
            names.append(name)
        live_source_name = ", ".join(names) or "(none configured)"

    # Cells a live observation already covers — Tier-B must not touch them.
    skip_keys = {
        (q.route, q.carrier, q.advance_window.days, q.travel_date) for q in live_quotes
    }
    if skip_keys:
        notes.append(
            f"{len(skip_keys)} itinerary cell(s) covered by live data; "
            "Tier-B yielded those cells."
        )

    sim_quotes, sim_counts = simulate_cycle(cycle_date, skip_keys=skip_keys)

    all_quotes = live_quotes + sim_quotes
    kept, collapsed = dedupe(all_quotes)

    # Merge the per-source histograms rather than reporting only the first.
    # The old form dropped every status count whenever more than one Tier-A
    # source ran, so a cycle's failures became invisible in the report.
    merged_live_counts: dict[str, int] = {}
    for _name, _stype, counts in live_runs:
        for status, n in counts.items():
            merged_live_counts[status] = merged_live_counts.get(status, 0) + n

    report = CycleReport(
        cycle_date=cycle_date,
        live_source=live_source_name,
        live_status_counts=merged_live_counts,
        sim_status_counts=sim_counts,
        quotes_live=sum(1 for q in kept if q.source_type.is_live),
        quotes_simulated=sum(1 for q in kept if q.source_type is SourceType.SIMULATED),
        duplicates_collapsed=collapsed,
        # The notes ARE the honesty of the report: which routes failed, which
        # were blocked, why a cycle is 100% simulated. This said `notes=[]`,
        # so all of it reached the scrape_runs row and none of it reached the
        # operator running `apix scrape`.
        notes=notes,
    )

    with session_scope() as session:
        run_ids_by_source: dict[str, int] = {}
        for name, stype, counts in live_runs:
            if counts or live_quotes:
                rid = _record_run(
                    session,
                    source=name,
                    source_type=stype,
                    status_counts=counts,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    note="; ".join(notes),
                )
                run_ids_by_source[name] = rid
                
        rid = _record_run(
            session,
            source="Simulator",
            source_type=SourceType.SIMULATED,
            status_counts=sim_counts,
            duration_ms=int((time.monotonic() - started) * 1000),
            note=f"Tier-B fill for cycle {cycle_date}",
        )
        run_ids_by_source["Simulator"] = rid
        report.rows_written = upsert_quotes(session, kept, run_ids_by_source)

    report.duration_ms = int((time.monotonic() - started) * 1000)
    logger.info("cycle %s complete: %s", cycle_date, report.summary())
    return report


def count_quotes(cycle_date: date | None = None) -> dict[str, int]:
    """
    Provenance headcount, one key per SourceType plus "imputed".

    An imputed row is counted as imputed regardless of which source_type it was
    carried forward from — the same rule the index lineage uses. Counting it
    under its donor's tag would let a carried-forward value be presented as a
    fresh observation, and would make this function disagree with /v1/coverage
    about the same cycle. The buckets partition the rows exactly.

    Every SourceType is pre-seeded to zero rather than created on first sight.
    The dict used to start as three literal keys, so a `live_limited` row landed
    in a fourth key the only caller never read: `apix scrape` printed a total
    that was short by however many Tier-1.5 quotes the cycle collected, which is
    the one direction a provenance line must never be wrong in.
    """
    with session_scope() as session:
        stmt = select(FareQuote.source_type, FareQuote.is_imputed)
        if cycle_date:
            stmt = stmt.where(FareQuote.cycle_date == cycle_date)
        rows = session.execute(stmt).all()
    out: dict[str, int] = {t.value: 0 for t in SourceType}
    out["imputed"] = 0
    for source_type, is_imputed in rows:
        if is_imputed:
            key = "imputed"
        else:
            key = source_type.value if hasattr(source_type, "value") else str(source_type)
        out[key] = out.get(key, 0) + 1
    return out
