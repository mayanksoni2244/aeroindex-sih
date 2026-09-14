"""
Reconnaissance driver for Akasa Air's booking widget.

This is recon tooling, NOT pipeline code. Its only job is to drive a real
search in a real browser and record what the site actually sends, so that
RECON_RESULTS.md can state whether fare data is available as clean backend
JSON and on what endpoint. It writes nothing to the database and is not
imported by any adapter.

Compliance: Akasa's robots.txt carries no Disallow directive at all, so no
path is prohibited. The driver sends the same identifying user agent as the
other recon probes, does not rotate IPs, uses no stealth plugin, and makes
no attempt to defeat a challenge. If a challenge appeared it would log and
stop. Discovering an endpoint here is not authorisation to poll it hard; the
rate protocol in the brief still governs collection.

Usage:
    python recon/akasa_probe.py discover      # enumerate widget controls
    python recon/akasa_probe.py search        # drive a search, capture XHR
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

# Akasa's own page copy contains the rupee sign, and this prints page text on
# Windows, whose console defaults to cp1252. Without this the probe dies on a
# UnicodeEncodeError while dumping a dropdown — an artefact of the terminal,
# not of the site.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOME = "https://www.akasaair.com/"
UA = (
    "AeroIndex-Research/0.1 (SIH26056 academic prototype; "
    "contact: team-scalex@example.edu)"
)
OUT = Path(__file__).resolve().parent
HORIZON_DAYS = 30

# Every call to Akasa's backend, in order, with bodies.
CAPTURE: list[dict] = []


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------


def install_capture(ctx) -> None:
    """
    Record Akasa's backend calls, with bodies for the fare endpoints.

    Two mechanisms, deliberately:

    - `ctx.on('response')` records the URL of every call, cheaply and without
      interfering with the page. This is what proves which endpoints exist.
    - A *narrow* route interception on the availability paths fetches its own
      copy so the body survives; reading the live response gave "body
      unavailable" for exactly these calls, because the page had already
      navigated on.

    The interception is narrow on purpose. Routing `**/*` and fetching every
    request stalls the app: an earlier attempt that did so never navigated at
    all, because every script and stylesheet was being proxied.
    """
    def on_response(response) -> None:
        request = response.request
        if "qp.akasaair.com" not in request.url or "/api/" not in request.url:
            return
        CAPTURE.append(
            {
                "method": request.method,
                "url": request.url,
                "request_body": request.post_data,
                "status": response.status,
                "response_body": None,
            }
        )

    def on_route(route) -> None:
        request = route.request
        try:
            response = route.fetch()
            body = response.text()
            CAPTURE.append(
                {
                    "method": request.method,
                    "url": request.url,
                    "request_body": request.post_data,
                    "status": response.status,
                    "response_bytes": len(body),
                    "response_body": body,
                }
            )
            route.fulfill(response=response)
        except Exception as exc:
            CAPTURE.append(
                {
                    "method": request.method,
                    "url": request.url,
                    "request_body": request.post_data,
                    "status": None,
                    "response_body": f"<unavailable: {type(exc).__name__}: {exc}>",
                }
            )
            route.continue_()

    ctx.on("response", on_response)
    ctx.route("**/api/ibe/availability/**", on_route)


def find_fare_pairs(obj, out: list | None = None, depth: int = 0) -> list:
    """
    Recursively collect (date, fare) pairs from an unknown JSON shape.

    Written shape-agnostically on purpose: the lowFare response has not been
    inspected before, and guessing its structure would just add a second thing
    to be wrong about.
    """
    if out is None:
        out = []
    if depth > 8:
        return out
    if isinstance(obj, dict):
        lower = {k.lower(): k for k in obj}
        dk = next((lower[k] for k in lower if "date" in k), None)
        fk = next(
            (
                lower[k]
                for k in lower
                if any(t in k for t in ("fare", "amount", "price", "total"))
                and isinstance(obj[lower[k]], (int, float, str))
            ),
            None,
        )
        if dk and fk:
            out.append((obj[dk], obj[fk]))
        for value in obj.values():
            find_fare_pairs(value, out, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            find_fare_pairs(value, out, depth + 1)
    return out


# --------------------------------------------------------------------------
# widget interaction
# --------------------------------------------------------------------------


def discover(page) -> None:
    """Print every control in the search widget so we stop guessing at it."""
    print("== inputs ==")
    for i, el in enumerate(page.query_selector_all("input")):
        print(
            f"[{i}] name={el.get_attribute('name')!r} id={el.get_attribute('id')!r} "
            f"type={el.get_attribute('type')!r} ph={el.get_attribute('placeholder')!r} "
            f"aria={el.get_attribute('aria-label')!r} ro={el.get_attribute('readonly')!r}"
        )
    print("\n== buttons with visible text ==")
    for i, el in enumerate(page.query_selector_all("button")):
        text = (el.inner_text() or "").strip().replace("\n", " ")[:60]
        if text:
            print(f"[{i}] text={text!r} type={el.get_attribute('type')!r}")


def dismiss_cookie_banner(page) -> None:
    """
    Accept the consent banner if present.

    Discovery showed a button reading 'Accept cookies' rendering over the
    booking widget. A banner intercepting the click is a plausible reason for a
    silently dead submit, so dismiss it before touching the form.
    """
    for el in page.query_selector_all("button"):
        if "accept cookies" in (el.inner_text() or "").strip().lower():
            el.click()
            print("  dismissed cookie banner")
            time.sleep(1)
            return
    print("  no cookie banner found")


def pick_city(page, field_id: str, city: str) -> bool:
    """
    Type a city into a combobox and accept the matching suggestion.

    Discovery showed the suggestion list is a `ul.p-4` whose items read
    "DEL / Delhi / Indira Gandhi International Airport". A generic `ul li`
    selector is wrong here: the page has ~30 nav lists, so it matches "Book"
    in the site menu, clicks it, and clears the field.
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
        print(f"  !! suggestion list found but no visible item matched {city!r}")
        return False

    print(f"  {field_id}: clicking {options[0].inner_text().strip()!r}")
    options[0].click()
    time.sleep(1.5)

    got = el.input_value()
    ok = city.lower() in (got or "").lower()
    print(f"  {field_id} now reads {got!r} ({'ok' if ok else 'DID NOT TAKE'})")
    return ok


def parse_calendar_label(label: str | None) -> date | None:
    """
    Parse an Akasa calendar cell's aria-label into a date.

    Observed formats:
        'Choose Sunday, October 11th, 2026'
        'Not available Sunday, August 30th, 2026'

    Substring-matching the day number is not safe: "11" matches September 11th
    and October 11th alike, and the first match in DOM order is the earlier
    month — which is how an earlier attempt selected today's date instead of
    the one intended.
    """
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


def pick_date(page) -> bool:
    """
    Choose a departure date by clicking the calendar, not by typing.

    Typing into the field is what failed previously: the control accepted the
    keystrokes but kept its own value, so the form submitted with today's date.

    The cells also render the cheapest fare for each departure date as their
    text ('12\\n₹6,530'), which is itself worth recording.
    """
    target = date.today() + timedelta(days=HORIZON_DAYS)
    print(f"  target departure: {target.isoformat()}")

    for selector in ("input[name='DepartureDate']", "input[placeholder='Departure date']"):
        el = page.query_selector(selector)
        if el is not None:
            el.click()
            break
    else:
        print("  !! no departure-date trigger found")
        return False

    time.sleep(1.5)

    for attempt in range(6):
        priced = []
        for el in page.query_selector_all("td, [role='gridcell'], div[aria-label]"):
            cell_date = parse_calendar_label(el.get_attribute("aria-label"))
            if cell_date is None:
                continue
            body = (el.inner_text() or "").strip()
            if "₹" in body:
                priced.append((cell_date, body.replace("\n", " ")))
            if cell_date == target and el.get_attribute("aria-disabled") != "true":
                print(f"  clicking {cell_date.isoformat()} (cell text {body!r})")
                el.click()
                time.sleep(1.5)
                if priced:
                    print(f"  picker was showing {len(priced)} priced dates, "
                          f"e.g. {priced[:3]}")
                return True
        print(f"  pass {attempt}: {len(priced)} priced cells visible, target not reached")

        # Month panes are virtualized, so advance by scrolling if there is no
        # next-month control.
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


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def is_fare_call(url: str) -> bool:
    """
    True for a call that could carry fares.

    Deliberately excludes `_next/data/.../flight-search.json`: that is Next.js
    page data, not a fare, and matching it ended an earlier wait after one
    second — before the real availability call had a chance to fire.
    """
    low = url.lower()
    if "_next/" in low or "analytics" in low or "google.com" in low:
        return False
    return any(
        k in low
        for k in ("availability", "fare", "shopping", "itinerar", "lowest",
                  "calendarpric", "priced")
    )


def run_search() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-IN")
        install_capture(ctx)
        page = ctx.new_page()

        print(f"navigating {HOME}")
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)
        dismiss_cookie_banner(page)

        print("filling origin/destination")
        pick_city(page, "From", "Delhi")
        pick_city(page, "To", "Mumbai")
        pick_date(page)

        readback = {}
        for name in ("From", "To", "DepartureDate"):
            el = page.query_selector(f"input[name='{name}']")
            readback[name] = el.input_value() if el is not None else "<missing>"
        print(f"widget readback: {readback}")

        # Clear the buffer so what follows is the search, not page-load chatter.
        CAPTURE.clear()
        print("submitting search")

        # Discovery showed the control is <button> with text 'Search Flights'
        # and no type='submit' — which is what the first attempt targeted.
        submit = None
        for btn in page.query_selector_all("button"):
            if "search flight" in (btn.inner_text() or "").strip().lower():
                submit = btn
                break
        if submit is None:
            print("!! no 'Search Flights' button found")
        else:
            submit.click()

        for elapsed in range(90):
            time.sleep(1)
            hits = [c for c in CAPTURE if is_fare_call(c["url"])]
            if hits:
                print(f"  fare call seen after {elapsed + 1}s: {hits[-1]['url']}")
                time.sleep(6)  # let sibling calls (calendar, upsell) land
                break
            if elapsed and elapsed % 15 == 0:
                print(f"  …{elapsed}s, {len(CAPTURE)} calls so far, url={page.url}")

        print(f"\nURL after submit: {page.url}")
        print(f"page title: {page.title()!r}")

        # What the results page actually rendered. If fares are visible here,
        # they are real listings and can be cited as such.
        try:
            text = page.inner_text("body")
            print(f"body text ({len(text)} chars), first 1500:\n{text[:1500]}")
        except Exception as exc:
            print(f"  body read failed: {type(exc).__name__}")

        print("\n-- all qp.akasaair.com calls this run --")
        for c in CAPTURE:
            print(f"   {c['status']} {c['method']} {c['url'][:150]}")

        save(CAPTURE)
        browser.close()


def save(calls: list[dict]) -> None:
    """Persist the capture, plus the two fare responses verbatim."""
    # De-duplicate: the widget retries some calls and both copies are identical.
    seen, unique = set(), []
    for c in calls:
        key = (c["method"], c["url"], c.get("request_body"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    (OUT / "akasa_capture.json").write_text(
        json.dumps(unique, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nwrote {OUT / 'akasa_capture.json'} ({len(unique)} distinct calls)")

    for c in unique:
        body = c.get("response_body") or ""
        if not body.startswith("{"):
            continue
        name = None
        if "lowFare" in c["url"]:
            name = "akasa_lowfare.json"
        elif "availability/search" in c["url"]:
            name = "akasa_availability.json"
        elif "availability/v2/search" in c["url"]:
            name = "akasa_availability_get.json"
        if name is None:
            continue
        (OUT / name).write_text(body, encoding="utf-8")
        print(f"wrote {OUT / name} ({len(body)} bytes)")

        pairs = find_fare_pairs(json.loads(body))
        if pairs:
            print(f"  {len(pairs)} date/fare pair(s) in {name}, first 10:")
            for d, f in pairs[:10]:
                print(f"     {d} -> {f}")
        else:
            print(f"  no date/fare pairs recognised in {name}; "
                  f"top-level keys: {list(json.loads(body).keys())[:10]}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "search"
    if mode == "discover":
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_context(user_agent=UA, locale="en-IN").new_page()
            page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
            time.sleep(4)
            discover(page)
            browser.close()
    elif mode == "search":
        run_search()
    else:
        print(f"unknown mode {mode!r}; use 'discover' or 'search'")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
