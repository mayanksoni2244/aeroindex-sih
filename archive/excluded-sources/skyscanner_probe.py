"""
Capture Skyscanner's price-calendar response.

The OTA sweep showed `POST /g/search-intent/v1/pricecalendar` firing on the
DEL-BOM month view — by name, per-date fares, which is exactly the shape the
advance-purchase axis needs. An earlier attempt to grab the body via route
interception failed with "Request context disposed" because the browser was
torn down while the fetch was still in flight.

This version listens instead of intercepting, and reads bodies through
`response.finished()` before closing anything, which avoids that race.

Compliance: skyscanner.co.in robots.txt was fetched in ota_probe.py and
`/transport/flights/...` and `/g/conductor/...` were both ALLOWED; evidence is
at recon/robots_skyscanner.txt. Identifying UA, no stealth, no proxy, one
page load.
"""

from __future__ import annotations

import json
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
URL = "https://www.skyscanner.co.in/transport/flights/del/bom/"

WANTED = ("pricecalendar", "search-intent", "conductor", "flights-quotes")


def main() -> int:
    captured: list[dict] = []
    pending: list = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-IN")
        page = ctx.new_page()

        def on_response(response) -> None:
            if not any(k in response.url.lower() for k in WANTED):
                return
            pending.append(response)

        page.on("response", on_response)

        print(f"navigating {URL}")
        response = page.goto(URL, wait_until="domcontentloaded", timeout=90000)
        print(f"HTTP {response.status if response else '?'}")

        # Let the calendar load, then read bodies while the page is still open.
        time.sleep(15)

        print(f"{len(pending)} matching response(s)")
        for resp in pending:
            entry = {
                "method": resp.request.method,
                "url": resp.url,
                "status": resp.status,
                "request_body": resp.request.post_data,
            }
            try:
                resp.finished()
                body = resp.text()
                entry["response_bytes"] = len(body)
                entry["response_body"] = body[:60000]
            except Exception as exc:
                entry["response_body"] = f"<unavailable: {type(exc).__name__}: {exc}>"
            captured.append(entry)

        # Also record what the page rendered, since the month view shows a
        # per-date fare grid directly.
        text = page.inner_text("body")
        fare_lines = [
            line.strip()
            for line in text.splitlines()
            if "₹" in line and line.strip() and line.strip() != "₹ INR"
        ]
        print(f"\nvisible fare lines: {len(fare_lines)}")
        for line in fare_lines[:25]:
            print(f"   {line}")

        (OUT / "skyscanner_capture.json").write_text(
            json.dumps(
                {"calls": captured, "visible_fare_lines": fare_lines},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        browser.close()

    print(f"\nwrote {OUT / 'skyscanner_capture.json'}")
    for c in captured:
        print(f"\n{c['status']} {c['method']} {c['url'][:130]}")
        if c.get("request_body"):
            print(f"  REQ ({len(str(c['request_body']))}B): "
                  f"{str(c['request_body'])[:400]}")
        print(f"  RESP ({c.get('response_bytes')}B): "
              f"{str(c.get('response_body'))[:1200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
