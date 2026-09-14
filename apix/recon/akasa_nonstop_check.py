"""
Phase 1.3 follow-up: are MAA-DEL and BLR-HYD unserved, or connection-only?

The coverage grid reports EMPTY_NO_INVENTORY for both. That status covers two
different facts, and the README must state the right one:

  * Akasa returns journeys but every one has 2+ segments -> it sells the
    city-pair only as a CONNECTION. The index excludes it because a connecting
    fare is not price-comparable to a non-stop one (matched-model, spec §3.6).
  * Akasa returns no journeys at all -> it does not operate the city-pair,
    and the market row in its own reference data is a bookable-network entry
    rather than a flown route.

This drives the production adapter and reads the raw bodies it captured, so the
answer is evidence rather than inference. Nothing here is a data source: it
writes to recon/, never to the database.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from apix.db.time_utils import today_ist
from apix.scrape.live.akasa import FARE_ENDPOINT, AkasaAirSource
from apix.config.settings import settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT = Path(__file__).resolve().parent / "akasa_nonstop_check.json"
PAIRS = [("MAA", "DEL"), ("BLR", "HYD"), ("DEL", "BOM")]


def journeys_for(bodies: list[Any], origin: str, dest: str) -> list[dict]:
    """Every journey the response offered for this market, stops included."""
    wanted = f"{origin.upper()}|{dest.upper()}"
    found: list[dict] = []
    for body in bodies:
        data = (body or {}).get("data") or {}
        for result in data.get("results") or []:
            for trip in (result or {}).get("trips") or []:
                for market in (trip or {}).get("journeysAvailableByMarket") or []:
                    if str((market or {}).get("key") or "").upper() != wanted:
                        continue
                    for j in market.get("value") or []:
                        if isinstance(j, dict):
                            found.append(j)
    return found


async def probe(src: AkasaAirSource, origin: str, dest: str, travel) -> dict:
    context = await src._ensure_browser()
    page = await context.new_page()
    bodies: list[Any] = []

    async def on_response(response):  # noqa: ANN001
        if FARE_ENDPOINT not in response.request.url:
            return
        if response.request.method != "POST":
            return
        try:
            bodies.append(await response.json())
        except Exception:
            return

    page.on("response", on_response)
    try:
        await page.goto(src.HOME, wait_until="domcontentloaded",
                        timeout=settings.scrape_timeout_ms)
        await page.wait_for_timeout(5000)
        await src._dismiss_cookie_banner(page)
        if not await src._pick_station(page, "From", origin):
            return {"error": f"origin {origin} not selectable"}
        if not await src._pick_station(page, "To", dest):
            return {"error": f"destination {dest} not selectable"}
        if not await src._pick_date(page, travel):
            return {"error": f"{travel} not offered in the calendar"}
        submit = await src._find_submit(page)
        if submit is None or await submit.is_disabled():
            return {"error": "search control unavailable"}
        await submit.click()
        for _ in range(src.FARE_WAIT_S):
            await page.wait_for_timeout(1000)
            if journeys_for(bodies, origin, dest):
                break
        js = journeys_for(bodies, origin, dest)
        segs = [len((j.get("segments") or [])) for j in js]
        return {
            "travel_date": travel.isoformat(),
            "fare_responses": len(bodies),
            "journeys": len(js),
            "segment_counts": segs,
            "nonstop": sum(1 for s in segs if s == 1),
            "connecting": sum(1 for s in segs if s > 1),
            "flight_numbers": [
                (j.get("designator") or {}).get("flightNumber") for j in js[:8]
            ],
        }
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def main() -> int:
    travel = today_ist() + timedelta(days=15)
    src = AkasaAirSource()
    report: dict[str, dict] = {}
    try:
        for origin, dest in PAIRS:
            r = await probe(src, origin, dest, travel)
            report[f"{origin}-{dest}"] = r
            if "error" in r:
                verdict = f"probe failed: {r['error']}"
            elif r["journeys"] == 0:
                verdict = (
                    f"NO journeys in {r['fare_responses']} fare response(s) "
                    "-> city-pair not operated"
                )
            else:
                verdict = (
                    f"{r['nonstop']} non-stop / {r['connecting']} connecting "
                    f"(segments {r['segment_counts']})"
                )
            print(f"{origin}-{dest} on {travel}: {verdict}")
    finally:
        await src.close()

    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
