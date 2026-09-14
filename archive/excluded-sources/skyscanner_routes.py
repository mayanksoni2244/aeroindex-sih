"""
Extract (date, fare) pairs from Skyscanner's month view.

The price-calendar figures render into the DOM on first load, so they can be
read without waiting for an XHR. This pairs each fare with its date by walking
the calendar cells, which is what makes the numbers usable as evidence for
routes.yaml bounds rather than a bare list of amounts.

Compliance: as skyscanner_probe.py — robots-permitted path, identifying UA,
one page load per route, polite spacing between routes.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = (
    "AeroIndex-Research/0.1 (SIH26056 academic prototype; "
    "contact: team-scalex@example.edu)"
)
OUT = Path(__file__).resolve().parent

# The six basket routes, as Skyscanner path segments.
ROUTES = [
    ("DEL-BOM", "del", "bom"),
    ("DEL-BLR", "del", "blr"),
    ("BOM-BLR", "bom", "blr"),
    ("DEL-CCU", "del", "ccu"),
    ("MAA-DEL", "maa", "del"),
    ("BLR-HYD", "blr", "hyd"),
]

# Read the calendar cells: each carries an aria-label or title with the date,
# and the fare as text. Selector list is broad because the exact structure is
# not known in advance and guessing it is how the Akasa probe wasted attempts.
CELL_JS = """
() => {
  const out = [];
  const seen = new Set();
  const nodes = document.querySelectorAll(
    '[class*="Calendar"] *, [class*="calendar"] *, [data-testid*="calendar"] *, ' +
    '[class*="BpkCalendar"] *, td, [role="gridcell"], button'
  );
  for (const el of nodes) {
    const text = (el.innerText || '').trim();
    if (!text.includes('\\u20b9')) continue;          // must show a fare
    if (el.querySelector('*') && el.children.length > 3) continue;  // want leaves
    const label =
      el.getAttribute('aria-label') ||
      el.getAttribute('title') ||
      (el.closest('[aria-label]') && el.closest('[aria-label]').getAttribute('aria-label')) ||
      (el.parentElement && (el.parentElement.getAttribute('aria-label') || '')) ||
      '';
    const key = label + '||' + text;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ label: label.slice(0, 120), text: text.replace(/\\n/g, ' ').slice(0, 80) });
  }
  return out;
}
"""


def scrape_route(page, code: str, origin: str, destination: str) -> dict:
    url = f"https://www.skyscanner.co.in/transport/flights/{origin}/{destination}/"
    print(f"\n=== {code}: {url}")
    result = {"route": code, "url": url, "cells": [], "status": None, "note": ""}
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=90000)
        result["status"] = response.status if response else None
        print(f"  HTTP {result['status']}")

        # Wait for a fare to appear rather than sleeping a fixed amount. The
        # first sweep gave DEL-BOM 63 cells and the other five zero, all on
        # HTTP 200 with no challenge — the signature of reading the DOM before
        # the calendar rendered, not of being blocked.
        rendered = False
        for waited in range(30):
            time.sleep(1)
            try:
                if "₹" in page.inner_text("body"):
                    rendered = True
                    print(f"  fare text present after {waited + 1}s")
                    break
            except Exception:
                continue
        if not rendered:
            print("  no fare text after 30s")
        time.sleep(3)

        body = page.inner_text("body").lower()
        for marker in ("just a moment", "checking your browser", "access denied",
                       "unusual traffic", "verify you are human"):
            if marker in body:
                result["note"] = f"CHALLENGED: {marker!r}"
                print(f"  >> {result['note']} — stopping this route, not retrying")
                return result

        cells = page.evaluate(CELL_JS)
        result["cells"] = cells
        print(f"  {len(cells)} priced cell(s)")

        # The usable form is the aria-label, e.g.
        # 'Saturday, 12 September 2026, ₹5,524'. Pull those into dated pairs.
        # Matching the label as a whole is brittle — the wording around the
        # date and fare varies between cells — so find each part on its own.
        date_re = re.compile(r"(\d{1,2}\s+[A-Z][a-z]+\s+20\d\d)")
        fare_re = re.compile(r"[₹]\s*([\d,]+)")
        pairs = []
        for cell in cells:
            label = cell["label"]
            date_hit = date_re.search(label)
            fare_hit = fare_re.search(label) or fare_re.search(cell["text"])
            if not (date_hit and fare_hit):
                continue
            pairs.append(
                {
                    "date": date_hit.group(1),
                    "fare_inr": int(fare_hit.group(1).replace(",", "")),
                }
            )
        # De-duplicate: the same cell matches at several nesting levels.
        unique = {(p["date"], p["fare_inr"]): p for p in pairs}
        result["dated_fares"] = sorted(unique.values(), key=lambda p: p["fare_inr"])
        print(f"  {len(result['dated_fares'])} dated fare(s)")
        for pair in result["dated_fares"][:8]:
            print(f"     {pair['date']}: ₹{pair['fare_inr']:,}")
        if result["dated_fares"]:
            lo = result["dated_fares"][0]["fare_inr"]
            hi = result["dated_fares"][-1]["fare_inr"]
            print(f"  range: ₹{lo:,} – ₹{hi:,}")
        elif cells:
            # Fares present but no date attached — worth seeing the raw shape
            # rather than silently reporting zero.
            print("  fares found but no date in the label; raw sample:")
            for cell in cells[:6]:
                print(f"     {cell['label'][:80]!r} -> {cell['text']!r}")
    except Exception as exc:
        result["note"] = f"{type(exc).__name__}: {exc}"
        print(f"  FAILED: {result['note']}")
    return result


def main() -> int:
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-IN")
        page = ctx.new_page()

        for code, origin, destination in ROUTES:
            results.append(scrape_route(page, code, origin, destination))
            time.sleep(8)  # polite spacing between routes

        browser.close()

    (OUT / "skyscanner_routes.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n\nwrote {OUT / 'skyscanner_routes.json'}")

    print("\n================ SUMMARY ================")
    for r in results:
        state = r["note"] or f"{len(r['cells'])} priced cells"
        print(f"{r['route']:<9} HTTP {str(r['status']):<5} {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
