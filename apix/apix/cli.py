"""
`apix` — the single entry point for every operation in this project.

WHY A PYTHON CLI AND NOT JUST A MAKEFILE
A Makefile is provided too, but `make` is not present on a stock Windows box
and half of a hackathon jury's laptops are Windows. The CLI is the canonical
interface; the Makefile is a thin set of aliases over it. Anything you can do
to this system, you can do from here:

    apix init                     create tables, seed the basket
    apix compliance               check robots.txt for every Tier-A adapter
    apix scrape [--live]          run one collection cycle
    apix clean [--date D]         run the cleaning pipeline for a cycle
    apix index [--rebuild]        build the four index series
    apix backtest                 compare against the DGCA reference
    apix seed-dgca [...]          load or generate the DGCA reference table
    apix generate-history [...]   create a simulated back-history
    apix status                   what is in the database right now
    apix serve                    run the API
    apix demo                     init + history + index, end to end

Every subcommand prints what it actually did, including what it failed to do.
`status` and `demo` both end by stating the live/simulated split out loud —
there is no invocation of this tool that lets you forget which one you have.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

logger = logging.getLogger("apix.cli")

BANNER = "AeroIndex APIx — Real-time Airfare Price Index for India (SIH26056)"


def _init_console() -> str:
    """
    Make stdout safe for the characters this CLI prints, on any Windows console.

    A default Windows terminal is cp1252, which cannot encode U+2500 — the CLI
    would crash on its own section rule before printing anything useful. Try
    UTF-8 first; if the stream refuses, fall back to an ASCII rule and
    `errors="replace"`, so output degrades to readable rather than to a
    traceback. Returns the rule character to use.
    """
    global RULE
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            try:
                stream.reconfigure(errors="replace")  # type: ignore[union-attr]
            except Exception:
                pass
    try:
        "─".encode(sys.stdout.encoding or "ascii")
        RULE = "─"
    except (UnicodeEncodeError, LookupError):
        RULE = "-"
    return RULE


#: Section-rule character. Downgraded to "-" by _init_console() on a console
#: that cannot encode box-drawing.
RULE = "-"


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


def _rule(title: str = "") -> None:
    print(f"\n{RULE * 68}")
    if title:
        print(title)
        print(RULE * 68)


def _provenance_footer(
    live: int, simulated: int, imputed: int, live_limited: int = 0
) -> None:
    """
    The line every command ends on.

    Constitution §1: a number without its provenance is not a result. Printing
    this unconditionally is cheaper than remembering to print it.

    Tier-1.5 is counted into the real share and named separately, because a
    reader has to be able to tell "real, from a rate-capped source" from "real,
    airline-direct" — and because leaving it out made the printed total smaller
    than the number of rows actually collected.
    """
    real = live + live_limited
    total = real + simulated + imputed
    if total == 0:
        print("\n  (no quotes in scope)")
        return
    pct = lambda n: 100.0 * n / total  # noqa: E731
    limited_part = (
        f"{live_limited} live-limited ({pct(live_limited):.1f}%) / "
        if live_limited
        else ""
    )
    print(
        f"\n  PROVENANCE  {total} quote(s): "
        f"{live} live ({pct(live):.1f}%) / "
        f"{limited_part}"
        f"{simulated} simulated ({pct(simulated):.1f}%) / "
        f"{imputed} imputed ({pct(imputed):.1f}%)"
    )
    if real == 0:
        print("  >> NO LIVE DATA. Every index value derived from this is a")
        print("     demonstration of the method, not a measurement of the market.")


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────
def cmd_init(args: argparse.Namespace) -> int:
    from apix.config.loader import load_basket, load_festivals, load_routes
    from apix.config.settings import settings
    from apix.db.session import create_all
    from scripts.seed_basket import seed_basket

    _rule("init")
    print(f"  database    : {settings.database_url}")
    create_all()
    print("  tables      : created (or already present)")

    routes = load_routes()
    basket = load_basket()
    fests = load_festivals()
    print(f"  routes.yaml : {len(routes.routes)} routes, {len(routes.carriers)} carriers, "
          f"windows {routes.advance_windows}")
    print(f"  basket.yaml : {len(basket.routes)} weighted routes, version "
          f"{basket.basket_version!r}, sum {sum(r.weight for r in basket.routes):.6f}")
    print(f"  festivals   : {len(fests.festivals)} calendar window(s) "
          f"({', '.join(f.name for f in fests.festivals)})")

    n, version = seed_basket(force=args.force)
    if n:
        print(f"  basket_config: recorded {n} weight(s) under {version!r}")
    else:
        print(f"  basket_config: {version!r} already recorded (use --force to overwrite)")
    return 0


def cmd_compliance(args: argparse.Namespace) -> int:
    """
    Check robots.txt for every registered Tier-A adapter, live.

    This is deliberately a first-class command rather than a buried internal.
    A reviewer should be able to reproduce the compliance claim in ten seconds
    without reading any code, and see that a refusal is a real refusal.
    """
    from datetime import timedelta

    from apix.config.settings import settings
    from apix.db.time_utils import today_ist
    from apix.scrape.live import REGISTRY

    _rule("compliance — live robots.txt evaluation")
    print(f"  user-agent  : {settings.scraper_user_agent}")
    print(f"  rate limit  : {settings.scrape_rate_per_min} req/min\n")

    travel = today_ist() + timedelta(days=30)
    refusals = 0
    for key, cls in sorted(REGISTRY.items()):
        src = cls()
        try:
            decision = src.check_compliance("DEL", "BOM", travel)
        except Exception as exc:  # network failure on the robots fetch itself
            print(f"  {key:18s} ERROR   could not evaluate: {exc}")
            continue
        verdict = "ALLOWED" if decision.allowed else "REFUSED"
        if not decision.allowed:
            refusals += 1
        print(f"  {key:18s} {verdict:8s} {decision.robots_url}")
        print(f"  {'':18s}          {decision.reason}")
        print(f"  {'':18s}          would fetch: {src.search_url('DEL', 'BOM', travel)}")
        print()

    print(f"  {refusals} of {len(REGISTRY)} source(s) refuse collection on robots.txt grounds.")
    print("  A refusal is final: no retry, no proxy, no bypass. The cycle records")
    print("  COMPLIANCE_REFUSED and coverage falls honestly.")
    return 0


def cmd_scrape(args: argparse.Namespace) -> int:
    from apix.scrape.runner import count_quotes, run_cycle

    _rule("scrape")
    live = True if args.live else (False if args.no_live else None)
    report = run_cycle(cycle_date=args.date, live=live)
    print(report.summary())
    counts = count_quotes(report.cycle_date)
    _provenance_footer(
        counts["live"], counts["simulated"], counts["imputed"],
        counts.get("live_limited", 0),
    )
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from apix.clean.pipeline import clean_cycle
    from apix.db.models import FareQuote
    from apix.db.session import session_scope

    with session_scope() as s:
        target = args.date or s.scalar(select(func.max(FareQuote.cycle_date)))
    if target is None:
        print("No fare quotes in the database. Run `apix scrape` first.", file=sys.stderr)
        return 1

    _rule("clean")
    print(clean_cycle(target).summary())
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    from apix.index.construct import build_series

    _rule("index")
    points = build_series(start=args.start, end=args.end)
    if not points:
        print("  no index points produced — is there cleaned data in range?")
        return 1

    by_key: dict[tuple[str, str], list] = {}
    for p in points:
        by_key.setdefault((p.series.value, p.measure.value), []).append(p)

    print(f"  built {len(points)} point(s) across {len(by_key)} series\n")
    print(f"  {'series':9s} {'measure':8s} {'n':>4s} {'base':>8s} {'min':>8s} "
          f"{'max':>8s} {'latest':>8s}")
    for (series, measure), pts in sorted(by_key.items()):
        vals = [p.index_value for p in pts]
        print(f"  {series:9s} {measure:8s} {len(pts):4d} {vals[0]:8.2f} "
              f"{min(vals):8.2f} {max(vals):8.2f} {vals[-1]:8.2f}")

    # Provenance is counted over ONE series, not summed over all four.
    #
    # Each of the four published series (headline/core x total/base) carries its
    # own lineage over largely the same underlying quotes, so adding them up
    # counted most rows up to four times: a database holding 4,493 quotes and 20
    # live ones printed "16262 quote(s): 56 live". Both figures were inflated,
    # and the line sat directly beneath the index table where a reader takes it
    # for the size of the actual panel.
    #
    # headline/total is the reference series — it is the widest, admitting the
    # festival-surge cells that `core` excludes, so it is the closest thing to a
    # headcount of the quotes that reached the index. It is also the series the
    # dashboard and README quote, which is the point: one number, everywhere.
    counted = [
        p for p in points
        if p.series.value == "headline" and p.measure.value == "total"
    ] or points
    live = sum(p.lineage.get("live_count", 0) for p in counted)
    limited = sum(p.lineage.get("live_limited_count", 0) for p in counted)
    sim = sum(p.lineage.get("simulated_count", 0) for p in counted)
    imp = sum(p.lineage.get("imputed_count", 0) for p in counted)
    print("\n  Provenance below counts the headline/total series (one row per")
    print("  quote); the other three series re-measure the same observations.")
    _provenance_footer(live, sim, imp, limited)

    head = [p for p in points if p.series.value == "headline" and p.measure.value == "total"]
    core = [p for p in points if p.series.value == "core" and p.measure.value == "total"]
    if head and core:
        gaps = [h.index_value - c.index_value for h, c in zip(head, core)]
        print(f"\n  headline − core (total): max {max(gaps):+.2f}, "
              f"mean {sum(gaps) / len(gaps):+.2f}")
        print("  That gap IS the surge measure — festival pricing flagged, not deleted.")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from apix.db.models import Measure, Series
    from apix.index.backtest import run_backtest

    _rule("backtest")
    result = run_backtest(Series(args.series), Measure(args.measure))
    print(result.summary())
    if not result.reportable:
        print("\n  NOT REPORTABLE as validation. See the notes above.")
    return 0


def cmd_seed_dgca(args: argparse.Namespace) -> int:
    from scripts.seed_dgca import main as seed_main

    argv = []
    if args.csv:
        argv += ["--csv", str(args.csv)]
    if args.source:
        argv += ["--source", args.source]
    if args.start:
        argv += ["--start", args.start]
    if args.end:
        argv += ["--end", args.end]
    if args.replace:
        argv.append("--replace")
    _rule("seed-dgca")
    return seed_main(argv)


def cmd_generate_history(args: argparse.Namespace) -> int:
    from scripts.generate_history import main as gen_main

    argv = ["--days", str(args.days)]
    if args.end:
        argv += ["--end", args.end.isoformat()]
    if args.reset:
        argv.append("--reset")
    if args.no_index:
        argv.append("--no-index")
    _rule("generate-history")
    return gen_main(argv)


def cmd_status(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select

    from apix.config.settings import settings
    from apix.db.models import (
        BasketConfigRow,
        DgcaMonthlyAvg,
        FareQuote,
        IndexValue,
        QuoteStatus,
        ScrapeRun,
        SourceType,
    )
    from apix.db.session import session_scope
    from apix.index.backtest import PLACEHOLDER_MARKER

    _rule(BANNER)
    print(f"  database          : {settings.database_url}")
    print(f"  live scraping     : {'enabled' if settings.live_enabled else 'DISABLED'} "
          f"(source: {settings.live_sources_display})")
    print(f"  elementary formula: {settings.elementary_formula}")
    print(f"  sim seed          : {settings.sim_seed}")

    with session_scope() as s:
        quotes = s.scalar(select(func.count(FareQuote.id))) or 0
        live = s.scalar(
            select(func.count(FareQuote.id)).where(
                FareQuote.source_type == SourceType.LIVE,
                FareQuote.is_imputed.is_(False),
            )
        ) or 0
        limited = s.scalar(
            select(func.count(FareQuote.id)).where(
                FareQuote.source_type == SourceType.LIVE_LIMITED,
                FareQuote.is_imputed.is_(False),
            )
        ) or 0
        sim = s.scalar(
            select(func.count(FareQuote.id)).where(
                FareQuote.source_type == SourceType.SIMULATED,
                FareQuote.is_imputed.is_(False),
            )
        ) or 0
        imputed = s.scalar(
            select(func.count(FareQuote.id)).where(FareQuote.is_imputed.is_(True))
        ) or 0
        rejects = s.scalar(
            select(func.count(FareQuote.id)).where(
                FareQuote.status == QuoteStatus.VALIDATION_REJECT
            )
        ) or 0
        outliers = s.scalar(
            select(func.count(FareQuote.id)).where(FareQuote.is_outlier.is_(True))
        ) or 0
        festival = s.scalar(
            select(func.count(FareQuote.id)).where(
                FareQuote.is_high_demand_outlier.is_(True)
            )
        ) or 0
        first = s.scalar(select(func.min(FareQuote.cycle_date)))
        last = s.scalar(select(func.max(FareQuote.cycle_date)))
        cycles = s.scalar(select(func.count(func.distinct(FareQuote.cycle_date)))) or 0
        runs = s.scalar(select(func.count(ScrapeRun.id))) or 0
        points = s.scalar(select(func.count(IndexValue.id))) or 0
        baskets = s.execute(
            select(BasketConfigRow.basket_version, func.count(BasketConfigRow.id))
            .group_by(BasketConfigRow.basket_version)
        ).all()
        dgca = s.scalar(select(func.count(DgcaMonthlyAvg.id))) or 0
        dgca_placeholder = s.scalar(
            select(func.count(DgcaMonthlyAvg.id)).where(
                DgcaMonthlyAvg.source_note.like(f"%{PLACEHOLDER_MARKER}%")
            )
        ) or 0
        latest_points = s.execute(
            select(IndexValue.series, IndexValue.measure, IndexValue.index_value,
                   IndexValue.index_date)
            .where(IndexValue.index_date == select(func.max(IndexValue.index_date))
                   .scalar_subquery())
            .order_by(IndexValue.series, IndexValue.measure)
        ).all()

    _rule("collection")
    print(f"  cycles            : {cycles}  ({first} .. {last})" if cycles
          else "  cycles            : 0 — run `apix scrape` or `apix generate-history`")
    print(f"  scrape runs       : {runs}")
    print(f"  fare quotes       : {quotes}")
    print(f"  validation reject : {rejects}   (kept, never deleted)")
    print(f"  statistical outlr : {outliers}  (flagged, never deleted)")
    print(f"  festival/high-dmd : {festival}  (flagged, never deleted)")

    _rule("index")
    print(f"  index points      : {points}")
    for version, n in baskets:
        print(f"  basket_config     : {version} ({n} routes)")
    for series, measure, value, on in latest_points:
        sv = series.value if hasattr(series, "value") else series
        mv = measure.value if hasattr(measure, "value") else measure
        print(f"  latest {sv:8s}/{mv:5s} = {value:8.2f}   ({on})")

    _rule("dgca reference (backtest only — never an index input)")
    print(f"  rows              : {dgca}")
    if dgca_placeholder:
        print(f"  !! {dgca_placeholder} of them are {PLACEHOLDER_MARKER} — illustrative,")
        print("  !! not transcribed from any DGCA publication. Backtest results")
        print("  !! against these are not validation and are marked unreportable.")

    _provenance_footer(live, sim, imputed, limited)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from apix.config.settings import settings

    _rule("serve")
    print(f"  docs   : http://{args.host}:{args.port}/docs")
    print(f"  health : http://{args.host}:{args.port}/v1/health")
    if settings.api_key:
        print("  auth   : X-API-Key required (APIX_API_KEY is set)")
    else:
        print("  auth   : DISABLED — APIX_API_KEY is empty")
    print()
    uvicorn.run("apix.api.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """
    One command, cold clone to working dashboard data.

    Everything it produces is simulated and says so, repeatedly. `--live`
    additionally attempts one real Tier-A cycle for today, so the demo can show
    a genuine live observation (or a genuine, logged failure to obtain one)
    alongside the synthetic history.
    """
    from apix.clean.pipeline import clean_cycle
    from apix.db.time_utils import today_ist
    from apix.scrape.runner import run_cycle

    rc = cmd_init(argparse.Namespace(force=False))
    if rc:
        return rc

    rc = cmd_seed_dgca(
        argparse.Namespace(csv=None, source="", start=args.dgca_start,
                           end=args.dgca_end, replace=True)
    )
    if rc:
        return rc

    rc = cmd_generate_history(
        argparse.Namespace(days=args.days, end=None, reset=True, no_index=True)
    )
    if rc:
        return rc

    if args.live:
        _rule("live cycle (Tier-A)")
        today = today_ist()
        report = run_cycle(cycle_date=today, live=True)
        print(report.summary())
        print(clean_cycle(today).summary())

    rc = cmd_index(argparse.Namespace(start=None, end=None))
    if rc:
        return rc

    cmd_backtest(argparse.Namespace(series="headline", measure="total"))
    cmd_status(argparse.Namespace())

    _rule("next")
    print("  apix serve            # API + OpenAPI docs at /docs")
    print("  cd web && npm run dev # dashboard")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="apix", description=BANNER)
    ap.add_argument("-v", "--verbose", action="store_true", help="INFO-level logging")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create tables and record the basket")
    p.add_argument("--force", action="store_true", help="overwrite an existing basket version")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("compliance", help="check robots.txt for every Tier-A adapter")
    p.set_defaults(func=cmd_compliance)

    p = sub.add_parser("scrape", help="run one collection cycle")
    p.add_argument("--date", type=date.fromisoformat, default=None, help="cycle date")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--live", action="store_true", help="force Tier-A collection on")
    g.add_argument("--no-live", action="store_true", help="Tier-B simulation only")
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("clean", help="run the cleaning pipeline for a cycle")
    p.add_argument("--date", type=date.fromisoformat, default=None,
                   help="cycle date (default: latest)")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("index", help="build the four index series")
    p.add_argument("--start", type=date.fromisoformat, default=None)
    p.add_argument("--end", type=date.fromisoformat, default=None)
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("backtest", help="compare APIx against the DGCA reference")
    p.add_argument("--series", choices=["headline", "core"], default="headline")
    p.add_argument("--measure", choices=["total", "base"], default="total")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("seed-dgca", help="load or generate the DGCA reference table")
    p.add_argument("--csv", default=None, help="CSV of real DGCA figures")
    p.add_argument("--source", default="", help="citation, required with --csv")
    p.add_argument("--start", default=None, help="first month for placeholder mode")
    p.add_argument("--end", default=None, help="last month for placeholder mode")
    p.add_argument("--replace", action="store_true", help="clear the table first")
    p.set_defaults(func=cmd_seed_dgca)

    p = sub.add_parser("generate-history", help="create a simulated back-history")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--end", type=date.fromisoformat, default=None)
    p.add_argument("--reset", action="store_true")
    p.add_argument("--no-index", action="store_true")
    p.set_defaults(func=cmd_generate_history)

    p = sub.add_parser("status", help="what is in the database right now")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("serve", help="run the API")
    p.add_argument("--host", default="127.0.0.1", help="bind address")
    p.add_argument("--port", type=int, default=8000, help="bind port")
    p.add_argument("--reload", action="store_true", help="enable auto-reload")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("demo", help="init + reference + history + index, end to end")
    p.add_argument("--days", type=int, default=90, help="cycles of simulated history")
    p.add_argument("--live", action="store_true",
                   help="also attempt one real Tier-A cycle for today")
    p.add_argument("--dgca-start", default="2026-06")
    p.add_argument("--dgca-end", default="2026-12")
    p.set_defaults(func=cmd_demo)

    return ap


def main(argv: list[str] | None = None) -> int:
    _init_console()
    args = build_parser().parse_args(argv)
    _log(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
