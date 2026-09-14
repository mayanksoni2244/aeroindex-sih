"""
Capture the REAL Akasa fare payloads, with bodies, from the working widget flow.

Why this exists
---------------
The live adapter builds a deep-link URL of the form
`/booking/select-flight?origin=DEL&destination=BOM&departureDate=...` and then
harvests *any* price-shaped object out of *any* JSON response. Run against the
real site that URL renders no fares at all, so the harvester fell through to
Akasa's Storyblok CMS payload and filed marketing copy as live airline fares.
That is the single worst class of bug this project can have, and it is
invisible from the outside: status OK, ten quotes, plausible-looking numbers.

The flow that genuinely produces fares is the one `akasa_probe.py` already
proved: navigate the homepage, fill the From/To comboboxes, pick a date from
the calendar, and press "Search Flights". This script runs exactly that flow
and writes down the response bodies, so the adapter can be rewritten against
observed reality rather than a guessed contract.

It is recon tooling: it writes nothing to the database and no adapter imports
it.

Compliance: only the public www.akasaair.com booking page is navigated, by the
site's own JavaScript, under an identifying user agent. Akasa's robots.txt for
that host carries no Disallow directive at all. Nothing here defeats a
challenge, rotates an identity, or polls a backend host directly.

Usage:
    python recon/akasa_capture_real.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime, timedelta
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
HORIZON_DAYS = 30

#: url -> {"method", "status", "request_body", "body"}
BODIES: dict[str, dict] = {}


def install_capture(ctx) -> None:
    """
    Record every call to Akasa's own backend, with its response body.

    The body is read from the `response` event rather than by intercepting the
    request. Intercepting with `route.fetch()` also works, but routing a path
    the SPA is actively using risks stalling it, and the response event is
    enough here — the earlier failure mode was reading a *finished* response
    after navigation, not reading one as it arrives.

    `response.finished()` is awaited first because the body is not guaranteed
    complete at event time.
    """

    async def on_response(response):  # noqa: ANN001
        request = response.request
        if "akasaair.com" not in request.url or "/api/" not in request.url:
            return
        key = f"{request.method} {request.url.split('?')[0]}"
        if key in BODIES:
            return  # first capture wins; the widget repeats identical calls
        try:
            await response.finished()
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype.lower():
                BODIES[key] = {
                    "method": request.method,
                    "url": request.url,
                    "status": response.status,
                    "request_body": request.post_data,
                    "content_type": ctype,
                    "body": None,
                }
                return
            body = await response.text()
            BODIES[key] = {
                "method": request.method,
                "url": request.url,
                "status": response.status,
                "request_body": request.post_data,
                "content_type": ctype,
                "body": json.loads(body) if body.strip().startswith(("{", "[")) else body,
            }
            print(f"  captured {key} ({len(body)} bytes)")
        except Exception as exc:
            BODIES[key] = {
                "method": request.method,
                "url": request.url,
                "status": None,
                "request_body": request.post_data,
                "error": f"{type(exc).__name__}: {exc}",
                "body": None,
            }

    ctx.on("response", on_response)


def dismiss_cookie_banner(page) -> None:
    for el in page.query_selector_all("button"):
        if "accept cookies" in (el.inner_text() or "").strip().lower():
            el.click()
            print("  dismissed cookie banner")
            time.sleep(1)
            return


def pick_city(page, field_id: str, city: str) -> bool:
    """
    Type a city into a combobox and accept the matching suggestion.

    The suggestion list is `ul.p-4`; a generic `ul li` matches the site nav
    instead (see akasa_probe.py, where that cost several attempts).
    """
    el = page.query_selector(f"#{field_id}")
    if el is None:
        print(f"  !! #{field_id} not found")
        return False
    el.click()
    el.fill("")
    el.type(city, delay=120)

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
        print(f"  !! suggestion list found but nothing matched {city!r}")
        return False

    print(f"  {field_id} <- {options[0].inner_text().strip()!r}")
    options[0].click()
    time.sleep(1.5)
    got = el.input_value()
    ok = city.lower() in (got or "").lower()
    print(f"  {field_id} now reads {got!r} ({'ok' if ok else 'DID NOT TAKE'})")
    return ok


def parse_calendar_label(label: str | None) -> date | None:
    """'Choose Sunday, October 11th, 2026' -> date(2026, 10, 11)."""
    if not label:
        return None
    text = re.sub(r"^\s*(Choose|Not available)\s+", "", label).strip()
    text = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", text)
    for fmt in ("%A, %B %d, %Y", "%a, %b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def pick_date(page, target: date) -> bool:
    """Choose a departure date by clicking the calendar, never by typing."""
    print(f"  target departure: {target.isoformat()}")
    for selector in (
        "input[name='DepartureDate']",
        "input[placeholder='Departure date']",
    ):
        el = page.query_selector(selector)
        if el is not None:
            el.click()
            break
    else:
        print("  !! no departure-date trigger found")
        return False

    time.sleep(1.5)

    for attempt in range(8):
        for el in page.query_selector_all("td, [role='gridcell'], div[aria-label]"):
            cell_date = parse_calendar_label(el.get_attribute("aria-label"))
            if cell_date is None or cell_date != target:
                continue
            if el.get_attribute("aria-disabled") == "true":
                print(f"  !! {target} is disabled (no inventory that day)")
                return False
            body = (el.inner_text() or "").strip().replace("\n", " ")
            print(f"  clicking {cell_date.isoformat()} (cell text {body!r})")
            el.click()
            time.sleep(1.5)
            got = page.query_selector("input[name='DepartureDate']")
            print(f"  field now reads {got.input_value()!r}" if got else "")
            return True

        nxt = page.query_selector(
            "button[aria-label*='Next'], .next-month, [aria-label='Next Month']"
        )
        if nxt is not None:
            nxt.click()
        else:
            page.mouse.wheel(0, 400)
        time.sleep(1)

    print("  !! could not locate the target day cell")
    return False


def main() -> int:
    target = date.today() + timedelta(days=HORIZON_DAYS)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-IN")
        install_capture(ctx)
        page = ctx.new_page()

        print(f"navigating {HOME}")
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)
        dismiss_cookie_banner(page)

        print("filling the search widget")
        pick_city(page, "From", "Delhi")
        pick_city(page, "To", "Mumbai")
        pick_date(page, target)

        submit = None
        for btn in page.query_selector_all("button"):
            if "search flight" in (btn.inner_text() or "").strip().lower():
                submit = btn
                break
        if submit is None:
            print("!! no 'Search Flights' button found")
        else:
            print("submitting search")
            # The widget fires its availability calls here; wait for fare text.
            submit.click()

        rendered = False
        for waited in range(45):
            time.sleep(1)
            try:
                if "₹" in page.inner_text("body"):
                    rendered = True
                    print(f"  fare text present after {waited + 1}s")
                    break
            except Exception:
                continue
        if not rendered:
            print("  no fare text after 45s")
        time.sleep(6)  # let sibling calls (calendar, upsell) land

        print(f"url after submit: {page.url}")
        print(f"title: {page.title()!r}")

        # What the results page shows. If fares are visible, they are listings.
        try:
            text = page.inner_text("body")
            fares = re.findall(r"₹\s*[\d,]+", text)
            print(f"rupee strings on page ({len(fares)}): {fares[:20]}")
            (OUT / "akasa_results_text.txt").write_text(text, encoding="utf-8")
            print(f"wrote {OUT / 'akasa_results_text.txt'}")
        except Exception as exc:
            print(f"  body read failed: {type(exc).__name__}: {exc}")

        browser.close()

    (OUT / "akasa_real_bodies.json").write_text(
        json.dumps(BODIES, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nwrote {OUT / 'akasa_real_bodies.json'} ({len(BODIES)} distinct call(s))")
    for key in BODIES:
        print(f"   {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
