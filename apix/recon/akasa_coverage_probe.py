"""
Phase 1.3: which basket routes does Akasa actually return fares for, today?

The network probe (`akasa_network_probe.py`) shows all six basket routes are
sellable markets. Sellable is not the same as "returns a non-stop fare for the
dates the index asks about" — a market can be sold as a connection, or have no
inventory in a given window. This probe settles it by running the production
adapter itself against every route and every configured advance window, and
reporting the grid.

Whatever this prints is the coverage number the README, the dashboard and the
API report. It is not rounded up and no cell is assumed.

Usage:
    python recon/akasa_coverage_probe.py            # all routes, all windows
    python recon/akasa_coverage_probe.py 7 15       # only those windows
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

from apix.config.loader import load_routes
from apix.db.time_utils import today_ist
from apix.scrape.live import get_live_source

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = Path(__file__).resolve().parent / "akasa_coverage.json"


async def main() -> int:
    routes_cfg = load_routes()
    windows = [int(a) for a in sys.argv[1:]] or list(routes_cfg.advance_windows)
    cycle = today_ist()
    source = get_live_source("akasa")

    grid: dict[str, dict[str, dict]] = {}
    try:
        for meta in routes_cfg.routes:
            route = meta.route
            grid[route] = {}
            for days in windows:
                travel = cycle + timedelta(days=days)
                started = time.monotonic()
                outcome = await source.search(meta.origin, meta.destination, travel)
                elapsed = time.monotonic() - started
                cheapest = min(
                    (q.total_fare for q in outcome.quotes), default=None
                )
                grid[route][f"T+{days}"] = {
                    "status": outcome.status.value,
                    "quotes": len(outcome.quotes),
                    "cheapest_total": cheapest,
                    "travel_date": travel.isoformat(),
                    "seconds": round(elapsed, 1),
                    "note": (outcome.note or "")[:160],
                }
                flag = "OK " if outcome.status.value == "OK" else "-- "
                fare = f"{cheapest:>8.0f}" if cheapest else "       -"
                print(
                    f"{flag}{route:<8} T+{days:<3} {travel}  "
                    f"{outcome.status.value:<20} n={len(outcome.quotes):<3} "
                    f"{fare}  {elapsed:>4.0f}s  {(outcome.note or '')[:60]}"
                )
    finally:
        await source.close()

    OUT.write_text(json.dumps(grid, indent=2), encoding="utf-8")

    cells = [c for r in grid.values() for c in r.values()]
    ok = [c for c in cells if c["status"] == "OK"]
    routes_live = [r for r, cols in grid.items()
                   if any(c["status"] == "OK" for c in cols.values())]

    print(f"\nwrote {OUT}")
    print("=" * 62)
    print(f"  cells live : {len(ok)} of {len(cells)} "
          f"({100.0 * len(ok) / len(cells):.0f}%)")
    print(f"  routes live: {len(routes_live)} of {len(grid)} "
          f"-> {', '.join(sorted(routes_live)) or 'none'}")
    dead = sorted(set(grid) - set(routes_live))
    if dead:
        print(f"  routes with no live fare: {', '.join(dead)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
