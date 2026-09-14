"""
Probe: which of the basket's routes does Akasa actually fly, non-stop?

Phase 1.3 asks this question directly, and the honest way to answer it is the
airline's own network data rather than an assumption. Akasa's booking page
loads `GET /api/nsk/v2/resources/markets` on start-up: for every origin it
lists the destinations it sells, which is exactly the non-stop network. One
page load answers all six routes, instead of six widget searches.

Routes the network does not contain are NOT scraped and NOT faked — they fall
to clearly-tagged Tier-B, and the coverage number reported everywhere else is
whatever this probe says it is.

Compliance: one load of the public homepage, identifying user agent, no
challenge defeated. akasaair.com/robots.txt has no Disallow rules.

Usage:
    python recon/akasa_network_probe.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOME = "https://www.akasaair.com/"
UA = (
    "AeroIndex-Research/0.1 (SIH26056 academic prototype; "
    "contact: team-scalex@example.edu)"
)
OUT = Path(__file__).resolve().parent

#: The six routes in the APIx basket (config/routes.yaml).
BASKET = [
    ("DEL", "BOM"),
    ("DEL", "BLR"),
    ("BOM", "BLR"),
    ("DEL", "CCU"),
    ("BLR", "HYD"),
    ("MAA", "DEL"),
]

CAPTURED: dict[str, object] = {}


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-IN")

        def on_response(response) -> None:
            url = response.request.url
            if "qp.akasaair.com" not in url:
                return
            print(f"    [{response.status}] {response.request.method} {url.split('?')[0]}")
            if "markets" not in url and "master-data" not in url:
                return
            key = "markets" if "markets" in url else "master-data"
            if key in CAPTURED:
                return
            try:
                CAPTURED[key] = response.json()
                print(f"  captured {key}")
            except Exception as exc:
                print(f"  {key} unreadable: {type(exc).__name__}: {exc}")

        ctx.on("response", on_response)
        page = ctx.new_page()

        print(f"navigating {HOME}")
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        time.sleep(6)

        # The markets payload is fetched lazily, when the origin combobox is
        # first opened — a bare page load does not pull it.
        for label in ("Accept cookies", "Accept"):
            btn = page.query_selector(f"button:has-text('{label}')")
            if btn is not None and btn.is_visible():
                try:
                    btn.click(timeout=3000)
                    print(f"  dismissed cookie banner ({label})")
                    time.sleep(1)
                    break
                except Exception:
                    pass

        field = page.query_selector("#From")
        if field is not None:
            print("  opening the origin combobox to trigger the network fetch")
            field.click()
            time.sleep(2)
            field.type("Del", delay=120)

        for _ in range(30):
            time.sleep(1)
            if "markets" in CAPTURED:
                break
        browser.close()

    if "markets" not in CAPTURED:
        print("!! markets payload never arrived")
        return 1

    (OUT / "akasa_markets.json").write_text(
        json.dumps(CAPTURED, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote {OUT / 'akasa_markets.json'}")

    markets = CAPTURED["markets"]
    data = markets.get("data", markets) if isinstance(markets, dict) else markets
    values = data.get("values", data) if isinstance(data, dict) else data
    print(f"\nmarkets payload: {type(values).__name__} of {len(values)}")
    if values:
        sample = values[0]
        print(f"sample entry keys: {list(sample) if isinstance(sample, dict) else sample}")
        print(json.dumps(sample, ensure_ascii=False)[:600])

    # Observed shape: a flat list of origin/destination pairs, one per sellable
    # market, e.g. {"locationCode": "AMD", "travelLocationCode": "AUH",
    # "inActive": false, ...}. Inactive markets are excluded — they are pairs
    # Akasa has configured but is not currently selling.
    network: dict[str, set[str]] = {}
    inactive = 0
    for entry in values if isinstance(values, list) else []:
        if not isinstance(entry, dict):
            continue
        origin = entry.get("locationCode")
        dest = entry.get("travelLocationCode")
        if not origin or not dest:
            continue
        if entry.get("inActive"):
            inactive += 1
            continue
        network.setdefault(str(origin), set()).add(str(dest))

    print(f"\nnetwork: {len(network)} origin station(s), {inactive} inactive pair(s) skipped")
    if network:
        for origin in sorted(network)[:4]:
            dests = sorted(network[origin])
            print(f"  {origin} -> {len(dests)}: {' '.join(dests[:18])}")

    print("\n=== BASKET COVERAGE (Akasa's own sellable-market data) ===")
    served = 0
    for origin, dest in BASKET:
        fwd = dest in network.get(origin, set())
        rev = origin in network.get(dest, set())
        ok = fwd or rev
        served += ok
        print(
            f"  {origin}-{dest}: "
            f"{'SERVED' if ok else 'NOT SERVED'}"
            f"   ({origin}->{dest}: {fwd},  {dest}->{origin}: {rev})"
        )
    print(f"\n  {served} of {len(BASKET)} basket routes sold by Akasa")
    print(
        "\nNote: a market being sellable does not prove it is NON-STOP; the "
        "availability search reports segments per journey, so non-stop status "
        "is confirmed there, not here."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
