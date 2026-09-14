"""
Follow-up reconnaissance on the OTA sweep's two open questions.

1. ixigo and Kayak tripped a 'captcha' marker while also returning a real page
   title and full content. A marker match inside an inline script (a login
   widget loading reCAPTCHA, say) is not a challenge, and recording it as one
   would be a false FAIL. This distinguishes them by checking *where* the
   token appears and whether real content rendered.

2. Skyscanner exposed `POST /g/search-intent/v1/pricecalendar`, which by its
   name returns per-date fares — the exact shape the advance-purchase axis
   needs. This captures its response body.

Compliance unchanged: robots.txt for every host here was fetched and the fare
path confirmed ALLOWED in ota_probe.py before this ran. No stealth, no proxy,
no UA spoofing. Bodies are captured by narrowly routing the endpoints of
interest, which was the approach that worked for Akasa.
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

CAPTURE: list[dict] = []


def classify_challenge(page) -> dict:
    """
    Decide whether a 'captcha' token means we were actually challenged.

    A real challenge replaces the page: little content, no real title, and a
    visible message. A reference inside a script tag is just a library being
    loaded and is not a block.
    """
    html = page.content()
    low = html.lower()

    visible = page.inner_text("body")
    visible_low = visible.lower()

    hard_markers = [
        "just a moment", "checking your browser", "attention required",
        "access denied", "unusual traffic", "verify you are human",
        "enable javascript and cookies",
    ]
    return {
        "title": page.title(),
        "html_bytes": len(html),
        "visible_chars": len(visible),
        "captcha_in_html": "captcha" in low,
        "captcha_in_visible_text": "captcha" in visible_low,
        "hard_markers_in_visible": [m for m in hard_markers if m in visible_low],
        "visible_head": visible[:220].replace("\n", " | "),
    }


def probe(name: str, url: str, routes: list[str]) -> None:
    print(f"\n=== {name}: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-IN")

        def on_route(route):
            request = route.request
            try:
                response = route.fetch()
                body = response.text()
                CAPTURE.append(
                    {
                        "host": name,
                        "method": request.method,
                        "url": request.url,
                        "request_body": request.post_data,
                        "status": response.status,
                        "response_bytes": len(body),
                        "response_body": body[:20000],
                    }
                )
                route.fulfill(response=response)
            except Exception as exc:
                CAPTURE.append(
                    {
                        "host": name,
                        "method": request.method,
                        "url": request.url,
                        "status": None,
                        "response_body": f"<unavailable: {type(exc).__name__}: {exc}>",
                    }
                )
                route.continue_()

        # Narrow interception only. Routing everything stalls these apps.
        for pattern in routes:
            ctx.route(pattern, on_route)

        page = ctx.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=90000)
            print(f"  HTTP {response.status if response else '?'}")
            time.sleep(12)  # fare calls are slow; give them room

            verdict = classify_challenge(page)
            for key, value in verdict.items():
                print(f"  {key}: {value!r}")

            challenged = bool(verdict["hard_markers_in_visible"]) or (
                verdict["captcha_in_visible_text"] and verdict["visible_chars"] < 800
            )
            print(f"  >> CHALLENGED: {challenged}")
            if verdict["captcha_in_html"] and not challenged:
                print("     ('captcha' appears in markup only — a loaded script, "
                      "not a block)")

            # Any rupee-denominated fares rendered?
            body_text = page.inner_text("body")
            rupees = body_text.count("₹")
            print(f"  rupee symbols in visible text: {rupees}")
            if rupees:
                lines = [
                    line.strip()
                    for line in body_text.splitlines()
                    if "₹" in line and line.strip()
                ]
                print(f"  sample fare lines: {lines[:8]}")
        except Exception as exc:
            print(f"  NAVIGATION FAILED: {type(exc).__name__}: {exc}")
        finally:
            browser.close()


def main() -> int:
    # ixigo / Kayak: was the captcha marker real?
    probe("ixigo", "https://www.ixigo.com/search/result/flight",
          ["**/api/**", "**/search/**"])
    probe("kayak", "https://www.kayak.co.in/flights", [])

    # Skyscanner: capture the price calendar.
    probe(
        "skyscanner",
        "https://www.skyscanner.co.in/transport/flights/del/bom/",
        ["**/search-intent/**", "**/pricecalendar**"],
    )

    if CAPTURE:
        (OUT / "ota_capture.json").write_text(
            json.dumps(CAPTURE, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\n\nwrote {OUT / 'ota_capture.json'} ({len(CAPTURE)} calls)")
        for c in CAPTURE:
            print(f"\n{c['status']} {c['method']} {c['url'][:140]}")
            if c.get("request_body"):
                print(f"  REQ : {str(c['request_body'])[:300]}")
            print(f"  RESP: {str(c.get('response_body'))[:800]}")
    else:
        print("\n\nno intercepted calls captured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
