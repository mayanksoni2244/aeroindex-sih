"""
Cleaning pipeline.

Order matters and is fixed (spec §3):

    1. dedupe      — collapse identical itinerary cells, keep the most complete
                     record. Done at collection time (scrape/runner.dedupe) and
                     guaranteed by the unique index on `dedupe_hash`.
    2. validate    — reject fares outside per-route sanity bounds; flag records
                     whose components do not add up to their total.
    3. outliers    — Tukey IQR fence per (route, advance window), computed over a
                     trailing history so a spike is judged against that cell's
                     own recent behaviour rather than against other routes.
    4. impute      — carry the last known good value forward into empty cells,
                     capped at APIX_IMPUTE_MAX_AGE_DAYS. Past the cap we leave
                     the hole and let coverage fall.
    5. normalize   — currency and rounding discipline.

Two rules run through all of it:

  * NOTHING IS DELETED. Every judgement is a flag on the row. A statistician
    who disagrees with our fence can recompute from the raw table.
  * FESTIVAL SPIKES ARE NOT ERRORS. A ₹22,000 Diwali fare is the true price
    someone paid; deleting it would understate the CPI. It is flagged
    `is_high_demand_outlier`, kept in the HEADLINE series and excluded from
    CORE. This is a deliberate, documented deviation from the pitch deck, which
    described dropping such points — see README §Deviations.

Re-running the pipeline for a cycle is idempotent: flags are reset for that
cycle before being recomputed, so a second pass cannot compound.
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from apix.config.loader import ConfigError, load_basket, load_festivals, load_routes
from apix.config.settings import settings
from apix.db.models import REAL_SOURCE_TYPES, FareQuote, QuoteStatus
from apix.db.session import session_scope
from apix.db.time_utils import now_ist

logger = logging.getLogger("apix.clean")

#: Components must reconcile to the total within this many rupees. Larger than
#: zero because real fare pages round each line item independently.
COMPONENT_TOLERANCE_INR = 1.5

#: How much history the IQR fence is fitted over.
OUTLIER_LOOKBACK_DAYS = 30


@dataclass
class CleanReport:
    cycle_date: date
    examined: int = 0
    validation_rejects: int = 0
    component_mismatches: int = 0
    currency_rejects: int = 0
    outliers_flagged: int = 0
    high_demand_outliers: int = 0
    groups_skipped_small: int = 0
    imputed: int = 0
    imputation_refused: int = 0
    cells_missing: int = 0
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"clean {self.cycle_date}  ({self.duration_ms} ms)",
            f"  examined            : {self.examined}",
            f"  validation rejects  : {self.validation_rejects}",
            f"  component mismatch  : {self.component_mismatches}",
            f"  currency rejects    : {self.currency_rejects}",
            f"  outliers flagged    : {self.outliers_flagged} "
            f"(of which festival/high-demand: {self.high_demand_outliers})",
            f"  groups too small    : {self.groups_skipped_small} (IQR stood down)",
            f"  imputed             : {self.imputed}",
            f"  imputation refused  : {self.imputation_refused} (older than "
            f"{settings.impute_max_age_days}d cap)",
            f"  cells left empty    : {self.cells_missing}",
        ]
        lines += [f"  note                : {n}" for n in self.notes]
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 0. Reset (idempotency)
# ─────────────────────────────────────────────────────────────────────────────
def _reset_cycle(session: Session, cycle_date: date) -> None:
    """
    Clear derived state for the cycle so a re-run recomputes rather than
    compounds. Imputed rows are *deleted* rather than reset: they are synthetic
    fillers this pipeline created, not observations, so regenerating them from
    scratch is correct. Observed rows are only ever un-flagged, never removed.

    The delete is narrowed by `source LIKE 'imputed:%'` and not by `is_imputed`
    alone, because that flag has been wrong before. `_imputed_hash` makes a
    filler share its cell's dedupe_hash so a later real observation replaces it;
    when the upsert failed to clear the flag, a genuine live Akasa fare sat in a
    row still marked imputed and this delete would have destroyed it. The
    `source` prefix is written once at creation and overwritten by any real
    observation, so it identifies a pipeline-created filler even if a flag lies.
    Both conditions must hold, so the two records of the same fact have to agree
    before anything is removed.
    """
    session.execute(
        delete(FareQuote).where(
            FareQuote.cycle_date == cycle_date,
            FareQuote.is_imputed.is_(True),
            FareQuote.source.startswith("imputed:"),
        )
    )
    session.execute(
        update(FareQuote)
        .where(FareQuote.cycle_date == cycle_date)
        .values(
            status=QuoteStatus.OK,
            is_outlier=False,
            is_high_demand_outlier=False,
            is_component_mismatch=False,
            # Every filler was just deleted, so whatever remains in this cycle
            # is an observation. Saying so here repairs a stale flag rather than
            # carrying it forward: a real fare that inherited `is_imputed` from
            # the cell it replaced is restored to what it is, and `impute` will
            # re-derive genuine fillers a few steps later.
            is_imputed=False,
            imputed_from_cycle=None,
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. Validation
# ─────────────────────────────────────────────────────────────────────────────
def validate(session: Session, cycle_date: date, report: CleanReport) -> None:
    """
    Sanity-bound check and component reconciliation.

    The bounds in routes.yaml are deliberately wide. They are a *parsing-error*
    guard — they catch "₹45" (a fee mis-read as a fare) and "₹9,00,000" (a
    currency or decimal blunder). They are NOT a plausibility filter on real
    market prices: narrowing them until surge fares fall outside would be
    exactly the silent censoring this project exists to avoid. Genuine surges
    are handled downstream by the outlier *flag*, which keeps the data.
    """
    routes_cfg = load_routes()
    quotes = session.scalars(
        select(FareQuote).where(FareQuote.cycle_date == cycle_date)
    ).all()
    report.examined = len(quotes)

    for q in quotes:
        # Currency: this index is denominated in INR. A non-INR row is not
        # convertible here without a rate we do not have and would not want to
        # guess, so it is rejected rather than silently converted.
        if (q.currency or "INR").upper() != "INR":
            q.status = QuoteStatus.VALIDATION_REJECT
            report.currency_rejects += 1
            report.validation_rejects += 1
            continue

        try:
            meta = routes_cfg.by_route(q.route)
        except ConfigError:
            q.status = QuoteStatus.VALIDATION_REJECT
            report.validation_rejects += 1
            report.notes.append(f"{q.route} has no sanity bounds in routes.yaml")
            continue

        if not (meta.sanity_min <= q.total_fare <= meta.sanity_max):
            q.status = QuoteStatus.VALIDATION_REJECT
            report.validation_rejects += 1
            continue

        # Components should reconcile. When they do not we keep the row and
        # flag it — the total is what the traveller pays and is still a valid
        # observation; it is the decomposition we cannot vouch for.
        parts = (q.base_fare or 0) + (q.taxes or 0) + (q.udf or 0) + (q.convenience_fee or 0)
        if abs(parts - q.total_fare) > COMPONENT_TOLERANCE_INR:
            q.is_component_mismatch = True
            report.component_mismatches += 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. Outlier flagging
# ─────────────────────────────────────────────────────────────────────────────
def _fence(values: list[float], k: float) -> tuple[float, float]:
    """Tukey fence (Q1 - k·IQR, Q3 + k·IQR) using inclusive quartiles."""
    q1, _median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    iqr = q3 - q1
    return q1 - k * iqr, q3 + k * iqr


def flag_outliers(session: Session, cycle_date: date, report: CleanReport) -> None:
    """
    Two independent flags, deliberately kept apart.

    `is_high_demand_outlier` — CALENDAR rule. The travel date falls inside a
        declared festival/holiday window. This is asserted from the calendar
        alone and does not consult the IQR. It has to work that way: a Diwali
        surge lasts a fortnight, so within a few days the trailing fence has
        absorbed the surge into its own quartiles and mid-festival fares stop
        looking extreme. Conditioning the festival flag on the fence would
        therefore quietly let most of the surge leak into CORE — the exact
        opposite of what CORE is for.

    `is_outlier` — STATISTICAL rule. A Tukey fence per (route, window, carrier),
        fitted on that cell's own trailing history. This is what catches the
        *unexplained* move: a one-day capacity shock, a fare war, a mis-scrape
        that survived validation.

    The carve-out runs both ways. Festival observations are excluded from the
    fit as well as being flagged, so a fortnight of surge cannot inflate the
    fence and mask a genuine non-calendar anomaly sitting next to it.

    Grouping key matters. Fitting the fence across routes would compare DEL-BOM
    to BLR-HYD; across windows, the T+1:T+30 ratio is ~2.9x by construction so
    the advance-purchase curve itself would look anomalous; across carriers, a
    full-service carrier sits ~25% above a low-cost one structurally and that
    spread would swamp the signal. Per cell, what remains is day-of-week
    variation and noise — against which a real shock stands out.

    PROVENANCE IS PART OF THE GROUPING KEY, and that is not a refinement.
    Without it, the first live cycle was judged against thirty days of
    simulated history for the same cell, so the generator's assumptions decided
    whether a real observed fare was anomalous. It flagged 10 of the first 20
    genuine Akasa fares — not because the market did anything unusual, but
    because the simulator's advance-purchase curve assumes T+1 costs ~2.85x
    T+30 while Akasa was in fact selling DEL-BOM flat at ₹6,880 across all five
    windows. The real observation was the accurate one and the fence called it
    an outlier. Since CORE excludes flagged rows, simulated data would have
    been vetoing real data out of a published series.

    A fare that was actually charged and observed cannot be "wrong" relative to
    a number this project invented. So each provenance class is fitted on its
    own kind: real against real, simulated against simulated. Early on that
    means real cells have too few points and the rule stands down and says so —
    which is the honest answer. With one day of live history there is no basis
    for calling any live fare anomalous, and `groups_skipped_small` reports the
    abstention rather than hiding it.

    Imputed rows are excluded from the fit entirely. They are copies of an
    earlier observation, so they add no information about dispersion while
    narrowing the quartiles, which biases the fence toward flagging — in the
    one direction that costs the index real data.

    Neither flag deletes anything. HEADLINE keeps every row; CORE excludes both
    flags; the gap between the two series is the published surge measure.
    """
    k = settings.outlier_iqr_k
    festivals = load_festivals()
    since = cycle_date - timedelta(days=OUTLIER_LOOKBACK_DAYS)

    rows = session.scalars(
        select(FareQuote).where(
            FareQuote.cycle_date <= cycle_date,
            FareQuote.cycle_date >= since,
            FareQuote.status != QuoteStatus.VALIDATION_REJECT,
        )
    ).all()

    # ── Calendar rule ────────────────────────────────────────────────────────
    for r in rows:
        if r.cycle_date != cycle_date:
            continue
        fest = festivals.surge_for(r.travel_date)
        if fest is not None:
            r.is_high_demand_outlier = True
            report.high_demand_outliers += 1

    # ── Statistical rule, fitted and applied on non-festival data only ───────
    #
    # The fourth key element is the provenance class — `is_live`, not the raw
    # source_type, so a Tier-1 and a Tier-1.5 observation are fitted together
    # (both are real fares) while neither is ever fitted against the simulator.
    groups: dict[tuple[str, str, str, bool], list[FareQuote]] = {}
    for r in rows:
        if festivals.is_surge(r.travel_date):
            continue
        if r.is_imputed:
            # A copy of an older value: no new information about dispersion,
            # and it narrows the quartiles in the direction that over-flags.
            continue
        groups.setdefault(
            (r.route, r.advance_purchase_window.value, r.carrier,
             r.source_type.is_live), []
        ).append(r)

    for (route, window, carrier, is_real), members in groups.items():
        today = [m for m in members if m.cycle_date == cycle_date]
        if not today:
            continue
        if len(members) < settings.min_quotes_for_iqr:
            # Too little data for a dispersion estimate. Flagging here would be
            # noise dressed as a finding, so we stand down and say so. On the
            # first live cycles this is the branch real observations take, and
            # that abstention is the correct answer rather than a gap: one day
            # of real history cannot establish what is anomalous for a cell.
            report.groups_skipped_small += 1
            continue

        lo, hi = _fence([m.total_fare for m in members], k)
        for m in today:
            if m.total_fare < lo or m.total_fare > hi:
                m.is_outlier = True
                report.outliers_flagged += 1
                logger.debug(
                    "statistical outlier %s %s %s %s ₹%.0f (fence %.0f–%.0f, %s)",
                    route, window, carrier, m.travel_date, m.total_fare, lo, hi,
                    "observed" if is_real else "simulated",
                )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Imputation
# ─────────────────────────────────────────────────────────────────────────────
def impute_missing(session: Session, cycle_date: date, report: CleanReport) -> None:
    """
    Fill empty cells by carrying the last good observation forward, capped.

    The donor must be the SAME (route, carrier, advance window) — comparing
    like with like — and no older than `APIX_IMPUTE_MAX_AGE_DAYS`. Past that
    cap we refuse: a week-old fare is not evidence about today, and an index
    propped up by stale carry-forward is worse than one that admits a gap. The
    hole then shows up honestly as reduced coverage and the basket weights
    renormalise over the routes that do have data.

    Every filled row carries `is_imputed=True` and `imputed_from_cycle`, is
    counted separately in the index lineage, and is never reported as live.
    """
    routes_cfg = load_routes()
    basket = load_basket()
    max_age = settings.impute_max_age_days

    # `present` counts EVERY row already filed under this cycle, including
    # validation-rejected ones. A rejected row still occupies its itinerary
    # cell's identity (`dedupe_hash`), so imputing over it would collide — and
    # the right answer is not to overwrite it anyway. We keep the raw rejected
    # observation for audit and leave the cell empty, letting coverage fall
    # honestly rather than fabricating a replacement under the same identity.
    rejected_cells = 0
    present: set[tuple[str, str, str]] = set()
    for r in session.scalars(
        select(FareQuote).where(FareQuote.cycle_date == cycle_date)
    ).all():
        present.add((r.route, r.carrier, r.advance_purchase_window.value))
        if r.status == QuoteStatus.VALIDATION_REJECT:
            rejected_cells += 1
    if rejected_cells:
        report.notes.append(
            f"{rejected_cells} cell(s) hold only a validation-rejected quote and "
            "were left empty rather than imputed over."
        )

    expected = {
        (route, carrier.code, f"T+{w}")
        for route in basket.route_codes
        for w in routes_cfg.advance_windows
        for carrier in routes_cfg.carriers
    }

    for route, carrier, window in sorted(expected - present):
        donor = session.scalars(
            select(FareQuote)
            .where(
                FareQuote.route == route,
                FareQuote.carrier == carrier,
                FareQuote.advance_purchase_window == window,
                FareQuote.cycle_date < cycle_date,
                FareQuote.cycle_date >= cycle_date - timedelta(days=max_age),
                FareQuote.status != QuoteStatus.VALIDATION_REJECT,
                FareQuote.is_imputed.is_(False),  # never impute from an imputation
            )
            .order_by(FareQuote.cycle_date.desc())
            .limit(1)
        ).first()

        if donor is None:
            report.imputation_refused += 1
            report.cells_missing += 1
            continue

        window_days = int(window.split("+")[1])
        travel_date = cycle_date + timedelta(days=window_days)
        filled = FareQuote(
            run_id=donor.run_id,
            origin=donor.origin,
            destination=donor.destination,
            route=donor.route,
            carrier=donor.carrier,
            advance_purchase_window=donor.advance_purchase_window,
            fare_class=donor.fare_class,
            trip_type=donor.trip_type,
            is_nonstop=donor.is_nonstop,
            currency=donor.currency,
            travel_date=travel_date,
            base_fare=donor.base_fare,
            taxes=donor.taxes,
            udf=donor.udf,
            convenience_fee=donor.convenience_fee,
            total_fare=donor.total_fare,
            source=f"imputed:{donor.source}",
            source_type=donor.source_type,
            scrape_timestamp=now_ist(),
            cycle_date=cycle_date,
            status=QuoteStatus.IMPUTED,
            is_imputed=True,
            imputed_from_cycle=donor.cycle_date,
            dedupe_hash=_imputed_hash(route, carrier, window, travel_date, cycle_date),
        )
        session.add(filled)
        report.imputed += 1


def _imputed_hash(
    route: str, carrier: str, window: str, travel_date: date, cycle_date: date
) -> str:
    import hashlib

    origin, destination = route.split("-")
    parts = [
        origin, destination, carrier, travel_date.isoformat(), window,
        "economy", "one_way", cycle_date.isoformat(),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# 5. Normalize
# ─────────────────────────────────────────────────────────────────────────────
def normalize(session: Session, cycle_date: date, report: CleanReport) -> None:
    """Rounding discipline so downstream comparisons are exact."""
    for q in session.scalars(
        select(FareQuote).where(FareQuote.cycle_date == cycle_date)
    ).all():
        q.base_fare = round(q.base_fare, 2)
        q.taxes = round(q.taxes or 0.0, 2)
        q.udf = round(q.udf or 0.0, 2)
        q.convenience_fee = round(q.convenience_fee or 0.0, 2)
        q.total_fare = round(q.total_fare, 2)
        q.currency = (q.currency or "INR").upper()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def clean_cycle(cycle_date: date) -> CleanReport:
    """Run the full pipeline for one cycle. Idempotent."""
    started = time.monotonic()
    report = CleanReport(cycle_date=cycle_date)

    with session_scope() as session:
        _reset_cycle(session, cycle_date)
        session.flush()
        validate(session, cycle_date, report)
        session.flush()
        flag_outliers(session, cycle_date, report)
        session.flush()
        impute_missing(session, cycle_date, report)
        session.flush()
        normalize(session, cycle_date, report)

        live = session.scalars(
            select(FareQuote).where(
                FareQuote.cycle_date == cycle_date,
                # Both real tiers. Naming LIVE alone under-reported the cycle:
                # a Tier-1.5 row is an observation of a real published fare and
                # counting it as absent here made the clean report disagree with
                # `count_quotes` and with the index lineage about the same cycle.
                FareQuote.source_type.in_(REAL_SOURCE_TYPES),
                FareQuote.is_imputed.is_(False),
            )
        ).all()
        if live:
            report.notes.append(f"{len(live)} live observation(s) in this cycle.")

    report.duration_ms = int((time.monotonic() - started) * 1000)
    logger.info("clean %s: %s", cycle_date, report.summary())
    return report
