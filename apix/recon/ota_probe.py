"""
Reconnaissance: OTAs / aggregators as fare sources.

Rationale: the five PS-named carriers yielded one viable source (Akasa). An
aggregator that permits fare-path access would cover every carrier on every
basket route from one place, so the source class is worth testing on the same
terms as the airlines — and on the same compliance-first basis.

Compliance: this script asks permission before it asks anything else. For each
host it fetches robots.txt verbatim, saves it as evidence, and evaluates the
real fare-search paths against it. A host whose fare path is disallowed is
recorded as FAIL (compliance) and NOT loaded in a browser. Unretrievable
robots.txt fails closed. No stealth, no proxy, no UA spoofing, no retry
escalation.

Usage:
    python recon/ota_probe.py robots      # permission only (safe, no browsing)
    python recon/ota_probe.py browse      # load only the permitted ones
"""

from __future__ import annotations

import sys
import time
import urllib.robotparser as robotparser
from pathlib import Path

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = (
    "AeroIndex-Research/0.1 (SIH26056 academic prototype; "
    "contact: team-scalex@example.edu)"
)
OUT = Path(__file__).resolve().parent

# Real fare-search paths, not homepages. The homepage being crawlable says
# nothing about whether the search results are, and the search path is the one
# we would actually need.
TARGETS: list[tuple[str, str, list[str]]] = [
    ("makemytrip", "https://www.makemytrip.com", [
        "/flight/search",
        "/flights/",
        "/api/flights/search",
    ]),
    ("goibibo", "https://www.goibibo.com", [
        "/flights/",
        "/flights/air-DEL-BOM-20261011--1-0-0-E-D/",
        "/api/flights/search",
    ]),
    ("cleartrip", "https://www.cleartrip.com", [
        "/flights/results",
        "/flights/international/results",
        "/flight/search",
    ]),
    ("easemytrip", "https://www.easemytrip.com", [
        "/flightlist/",
        "/FlightList/",
        "/flight/search",
    ]),
    ("yatra", "https://www.yatra.com", [
        "/flights/search",
        "/air-search-ui/dom2/e2e",
        "/flights",
    ]),
    ("ixigo", "https://www.ixigo.com", [
        "/search/result/flight",
        "/flights",
        "/api/v2/search",
    ]),
    ("skyscanner", "https://www.skyscanner.co.in", [
        "/transport/flights/del/bom/",
        "/transport/flights",
        "/g/conductor/v1/fps3/search",
    ]),
    ("kayak", "https://www.kayak.co.in", [
        "/flights/DEL-BOM/2026-10-11",
        "/flights",
        "/s/horizon/exploreapi",
    ]),
    ("googleflights", "https://www.google.com", [
        "/travel/flights",
        "/travel/flights/search",
    ]),
]

CHALLENGE_MARKERS = (
    "just a moment", "cf-chl", "captcha", "checking your browser",
    "attention required", "access denied", "unusual traffic",
    "enable javascript and cookies",
)


def fetch_robots(name: str, origin: str) -> tuple[str | None, int | None, str]:
    """Fetch robots.txt verbatim. Returns (text, status, note)."""
    url = f"{origin}/robots.txt"
    try:
        with httpx.Client(
            headers={"User-Agent": UA}, timeout=30.0, follow_redirects=True
        ) as client:
            response = client.get(url)
        text = response.text
        (OUT / f"robots_{name}.txt").write_text(
            f"# Fetched {url}\n"
            f"# HTTP {response.status_code}, {len(text)} bytes\n"
            f"# UA sent: {UA}\n"
            f"# ---- verbatim below ----\n{text}",
            encoding="utf-8",
        )
        return text, response.status_code, "ok"
    except Exception as exc:
        note = f"{type(exc).__name__}: {exc}"
        (OUT / f"robots_{name}.txt").write_text(
            f"# Fetched {url}\n"
            f"# FAILED: {note}\n"
            f"# UA sent: {UA}\n"
            f"# Under the fail-closed rule an unretrievable robots.txt is\n"
            f"# treated as NOT PERMITTED. No fare path on this host was\n"
            f"# requested.\n",
            encoding="utf-8",
        )
        return None, None, note


def check(name: str, origin: str, paths: list[str]) -> dict:
    text, status, note = fetch_robots(name, origin)
    result = {
        "name": name,
        "origin": origin,
        "robots_status": status,
        "robots_note": note,
        "paths": {},
        "verdict": None,
    }

    if text is None:
        result["verdict"] = "FAIL (compliance: robots.txt unretrievable, fail-closed)"
        return result

    parser = robotparser.RobotFileParser()
    parser.parse(text.splitlines())

    allowed_any = False
    for path in paths:
        try:
            ok = parser.can_fetch(UA, origin + path)
        except Exception as exc:
            ok = False
            result["paths"][path] = f"parse-error: {type(exc).__name__}"
            continue
        result["paths"][path] = "ALLOWED" if ok else "DISALLOWED"
        allowed_any = allowed_any or ok

    result["verdict"] = (
        "PERMITTED (at least one fare path)" if allowed_any
        else "FAIL (compliance: every fare path disallowed)"
    )
    return result


def run_robots() -> list[dict]:
    results = []
    for name, origin, paths in TARGETS:
        print(f"\n=== {name} ({origin}) ===")
        result = check(name, origin, paths)
        print(f"  robots.txt: HTTP {result['robots_status']} ({result['robots_note']})")
        for path, state in result["paths"].items():
            print(f"    {state:<10} {path}")
        print(f"  VERDICT: {result['verdict']}")
        results.append(result)
        time.sleep(2)  # polite spacing between hosts

    print("\n\n================ SUMMARY ================")
    for r in results:
        print(f"{r['name']:<14} {r['verdict']}")
    return results


def run_browse(results: list[dict]) -> None:
    """Load only hosts whose fare path robots.txt permits."""
    from playwright.sync_api import sync_playwright

    permitted = [r for r in results if r["verdict"].startswith("PERMITTED")]
    if not permitted:
        print("\nNo host permits a fare path. Nothing to browse. Stopping.")
        return

    print(f"\n\n=== browsing {len(permitted)} permitted host(s) ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-IN")

        for r in permitted:
            allowed_path = next(
                (path for path, state in r["paths"].items() if state == "ALLOWED"), ""
            )
            url = r["origin"] + allowed_path
            print(f"\n--- {r['name']}: {url}")
            page = ctx.new_page()
            calls: list[str] = []
            page.on(
                "response",
                lambda resp: calls.append(
                    f"{resp.status} {resp.request.method} {resp.url[:130]}"
                )
                if resp.request.resource_type in ("xhr", "fetch")
                else None,
            )
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(6)
                body = page.content()
                low = body.lower()
                hits = [m for m in CHALLENGE_MARKERS if m in low]
                print(f"    HTTP {response.status if response else '?'}, "
                      f"{len(body)} bytes, title={page.title()!r}")
                print(f"    challenge markers: {hits or 'none'}")
                print(f"    fare-ish XHR: {sum('₹' in c for c in calls)} / {len(calls)} XHR")
                for c in calls[:12]:
                    print(f"      {c}")
            except Exception as exc:
                print(f"    NAVIGATION FAILED: {type(exc).__name__}: {exc}")
            finally:
                page.close()
            time.sleep(3)

        browser.close()


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "robots"
    results = run_robots()
    if mode == "browse":
        run_browse(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
