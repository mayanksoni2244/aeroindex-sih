"""
Probe: drive Akasa's booking widget end to end and capture the fare JSON.

Why this exists
---------------
The production adapter builds a deep link
(`/booking/select-flight?origin=DEL&...`) and hopes the page fetches fares.
It does not: that URL renders no fares at all, and the adapter's generic
"harvest any price-shaped number from any JSON" walker then picks numbers out
of Akasa's Storyblok CMS payload. That is the bug. The real fare surface is
the widget's own search, which posts to
`prod-bl.qp.akasaair.com/api/ibe/availability/search/lowFare` and returns a
60-day fare calendar in one response.

This probe drives that widget exactly as a browser does — type, pick the
suggestion, pick the date, click Search — and dumps every `qp.akasaair.com`
response body to `akasa_widget_bodies.json` so the adapter can be rewritten
against observed structure rather than guessed structure.

Compliance: akasaair.com/robots.txt carries no Disallow directive (User-Agent:
*, zero rules). One search per route, no IP rotation, no stealth, identifying
user agent, and any challenge aborts the run rather than being defeated.

Usage:
    python recon/akasa_widget_probe.py DEL BOM
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
OUT = Path(__file__).resolve().parent / "akasa_widget_bodies.json"

BODIES: dict[str, dict] = {}


def install_capture(ctx) -> None:
    """
    Record every backend call and its body.

    `await response.finished()` before touching the body matters: without it
    the read races the navigation and returns "body unavailable". The probe
    that skipped this step captured URLs but no fare payloads.
    """
    def on_response(response) -> None:
        request = response.request
        if "akasaair.com" not in request.url or "/api/" not in request.url:
            return
        key = f"{request.method} {request.url.split('?')[0]}"
        if key in BODIES:
            return  # the widget repeats identical calls; first body wins
        try:
            body = response.json()
        except Exception:
            return
        BODIES[key] = body
        size = len(json.dumps(body))
        print(f"  [captured] {key}  ({size:,} bytes)")

    ctx.on("response", on_response)


def dismiss_cookie(page) -> None:
    for label in ("Accept cookies", "Accept", "I Agree"):
        btn = page.query_selector(f"button:has-text('{label}')")
        if btn is not None and btn.is_visible():
            try:
                btn.click(timeout=3000)
                print(f"  dismissed cookie banner via {label!r}")
                time.sleep(1)
                return
            except Exception:
                pass


def pick_city(page, field_id: str, city: str) -> bool:
    """
    Type a city and accept the matching suggestion.

    The selector is `ul.p-4 li` on purpose. A generic `ul li` matches the site
    nav menu (~30 lists) and clicks "Book" instead, clearing the field. The
    same reason applies to the suggestion text: it must name the city.
    """
    el = page.query_selector(f"#{field_id}")
    if el is None:
        print(f"  !! #{field_id} not found")
        return False
    el.click()
    el.fill("")
    el.type(city, delay=110)
    try:
        page.wait_for_selector(
            f"ul.p-4 li:has-text('{city}')", timeout=10000, state="visible"
        )
    except Exception:
        print(f"  !! no suggestion naming {city!r} appeared")
        return False
    options = [
        o
        for o in page.query_selector_all("ul.p-4 li")
        if o.is_visible() and city.lower() in (o.inner_text() or "").lower()
    ]
    if not options:
        print(f"  !! suggestions visible but none named {city!r}")
        return False
    print(f"  {field_id}: choosing {options[0].inner_text().strip()[:45]!r}")
    options[0].click()
    time.sleep(1.5)
    return True


def pick_date(page) -> bool:
    """
    Open the date picker and let the low-fare calendar populate.

    Clicking rather than typing: the field does not accept a typed date into
    the widget's internal state, which is what gates the submit button. Merely
    *opening* the calendar is useful in itself — it is what makes the page
    fetch per-day fares for the chosen pair, so this doubles as a fare source
    even when the search itself does not go through.
    """
    el = page.query_selector("input[name='DepartureDate']")
    if el is None:
        print("  !! DepartureDate not found")
        return False
    el.click()
    time.sleep(6)

    # Read whatever the calendar rendered, as a cross-check on the JSON.
    priced = []
    for cell in page.query_selector_all("td, div[role='button'], button"):
        try:
            if not cell.is_visible():
                continue
            text = (cell.inner_text() or "").strip()
        except Exception:
            continue
        if "₹" in text and len(text) < 40:
            priced.append(text.replace("\n", " "))
    print(f"  date: {len(priced)} priced cells rendered")
    for row in priced[:6]:
        print(f"        {row!r}")
    return True


def find_submit(page):
    for btn in page.query_selector_all("button"):
        if "search flight" in (btn.inner_text() or "").strip().lower():
            return btn
    return None


def main() -> int:
    origin = sys.argv[1] if len(sys.argv) > 1 else "DEL"
    dest = sys.argv[2] if len(sys.argv) > 2 else "BOM"
    city = {"DEL": "Delhi", "BOM": "Mumbai", "BLR": "Bengaluru",
            "CCU": "Kolkata", "HYD": "Hyderabad", "MAA": "Chennai"}
    if origin not in city or dest not in city:
        print(f"unknown airport code in {origin}/{dest}")
        return 2

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-IN")
        install_capture(ctx)
        page = ctx.new_page()

        print(f"navigating {HOME}")
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        dismiss_cookie(page)

        print(f"filling {origin} -> {dest}")
        if not (pick_city(page, "From", city[origin])
                and pick_city(page, "To", city[dest])):
            print("!! city selection failed")
            browser.close()
            return 1
        pick_date(page)

        submit = find_submit(page)
        if submit is None:
            print("!! no Search Flights button")
            browser.close()
            return 1
        print(f"submit disabled? {submit.is_disabled()}")
        BODIES.clear()
        print("submitting…")
        submit.click()

        for elapsed in range(60):
            time.sleep(1)
            if any("lowFare" in k or "availability" in k.lower() for k in BODIES):
                print(f"  fare call landed after {elapsed + 1}s")
                time.sleep(7)
                break
            if elapsed and elapsed % 15 == 0:
                print(f"  …{elapsed}s  url={page.url}  keys={list(BODIES)}")

        print(f"\nURL after submit: {page.url}")
        body_text = page.inner_text("body")
        rupees = [w for w in body_text.split() if "₹" in w]
        print(f"rupee tokens on rendered page: {len(rupees)}  e.g. {rupees[:8]}")

        browser.close()

    OUT.write_text(json.dumps(BODIES, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\nwrote {OUT.name}: {len(BODIES)} endpoints")
    for k, v in BODIES.items():
        print(f"  {k}  ({len(json.dumps(v)):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
