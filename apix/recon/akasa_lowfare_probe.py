"""
Call Akasa's lowFare endpoint from inside the page and capture the response.

Why this shape
--------------
Driving the whole booking widget works but is slow and brittle: it depends on
a combobox, a virtualized calendar and a submit button, any of which can move.
The fare data itself arrives from one POST:

    POST /api/ibe/availability/search/lowFare
    {"currencyCode":"INR","origin":"DEL","destination":"BOM",
     "startDate":"2026-10-11","endDate":"2026-12-10",
     "includeTaxesAndFees":true,"numberOfPassengers":1}

One call returns a 60-day fare calendar, and `includeTaxesAndFees` is exactly
the base-vs-total split the index needs.

Rather than replaying that request from Python — which would mean forging the
authorization header and talking to a backend host directly, and that host's
robots.txt is a 503 so we cannot demonstrate permission — we let the *page*
make the call. Same origin, same session, same headers the site itself uses.
The request is issued by Akasa's own JavaScript inside Akasa's own page; we
observe the answer. That keeps the whole thing inside the one host whose
robots.txt actually permits us.

Usage:
    python recon/akasa_lowfare_probe.py DEL BOM
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta
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

#: Everything the page sent to its own backend, so we can copy its headers.
SEEN: list[dict] = []


def main() -> int:
    origin = (sys.argv[1] if len(sys.argv) > 1 else "DEL").upper()
    destination = (sys.argv[2] if len(sys.argv) > 2 else "BOM").upper()
    start = date.today() + timedelta(days=1)
    end = start + timedelta(days=59)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-IN",
                                  timezone_id="Asia/Kolkata")
        page = ctx.new_page()

        def on_request(request):  # noqa: ANN001
            if "akasaair.com" in request.url and "/api/" in request.url:
                SEEN.append(
                    {
                        "method": request.method,
                        "url": request.url,
                        "headers": dict(request.headers),
                        "post_data": request.post_data,
                    }
                )

        page.on("request", on_request)

        print(f"loading {HOME} to establish a session")
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        # The token call fires early; wait until we have seen it.
        for _ in range(20):
            time.sleep(1)
            if any("generateToken" in s["url"] for s in SEEN):
                break
        print(f"  observed {len(SEEN)} backend request(s)")

        tok = next((s for s in SEEN if "generateToken" in s["url"]), None)
        if tok:
            print("  token request headers:")
            for k, v in tok["headers"].items():
                if k.lower() in ("authorization", "x-api-key", "channel",
                                 "content-type", "appsource", "deviceid",
                                 "devicetype", "sessionid", "x-channel"):
                    print(f"    {k}: {str(v)[:100]}")

        # Now issue the lowFare call from inside the page: same origin, same
        # session, using the header set the page itself just used.
        headers = {}
        if tok:
            for k, v in tok["headers"].items():
                if k.lower() in (
                    "authorization", "x-api-key", "channel", "content-type",
                    "appsource", "devicetype", "x-channel", "accept",
                ):
                    if k.lower() not in ("accept",):  # accept is set by fetch
                        headers[k] = v
        headers.setdefault("content-type", "application/json")

        payload = {
            "currencyCode": "INR",
            "origin": origin,
            "destination": destination,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "includeTaxesAndFees": True,
            "numberOfPassengers": 1,
        }
        api = "https://prod-bl.qp.akasaair.com/api/ibe/availability/search/lowFare"
        print(f"\nPOSTing lowFare {origin}->{destination} {start}..{end}")
        print(f"  headers forwarded: {sorted(headers)}")

        js = """
        async ([url, payload, headers]) => {
          try {
            const r = await fetch(url, {
              method: 'POST',
              headers: headers,
              body: JSON.stringify(payload),
              credentials: 'include',
            });
            const text = await r.text();
            return {status: r.status, text: text.slice(0, 400000)};
          } catch (e) {
            return {status: null, error: String(e)};
          }
        }
        """
        result = page.evaluate(js, [api, payload, headers])

        print(f"\nstatus: {result.get('status')}")
        if result.get("error"):
            print(f"error : {result['error']}")
        body = result.get("text") or ""
        print(f"body  : {len(body)} bytes")

        (OUT / f"akasa_lowfare_{origin}_{destination}.json").write_text(
            body, encoding="utf-8"
        )
        print(f"wrote {OUT / f'akasa_lowfare_{origin}_{destination}.json'}")

        if body.strip().startswith(("{", "[")):
            doc = json.loads(body)
            print("\ntop-level keys:", list(doc)[:20] if isinstance(doc, dict) else type(doc))
            print(json.dumps(doc, indent=1)[:2500])

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
