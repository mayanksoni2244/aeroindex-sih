"""
Tier-1.5 live adapter: Yatra (YT), via Playwright.

ToS Citation (https://www.yatra.com/online/terms-of-service.html):
"For the removal of doubt, it is clarified that Yatra Services, including the use of the Website, is not for commercial use but is specifically meant for personal use only."

This is a Tier-1.5 source. It is rate-capped to a maximum daily limit to respect
the commercial use clause while not explicitly violating any bot/scraping prohibitions.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date

from apix.config.settings import settings
from apix.db.models import AdvanceWindow, SourceType, TripType
from apix.db.time_utils import advance_window_days, today_ist
from apix.scrape.base import (
    ComplianceDecision,
    FetchOutcome,
    LiveSource,
    RawQuote,
    StatusCode,
    backoff_sleep,
    looks_blocked,
    looks_like_consent_wall,
    parse_inr,
)
from apix.scrape.live.akasa import harvest_fares_from_json

logger = logging.getLogger("apix.scrape.live.yatra")

class YatraSource(LiveSource):
    name = "Yatra (limited-scope)"
    base_url = "https://www.yatra.com"
    default_source_type = SourceType.LIVE_LIMITED

    URL_TEMPLATE = (
        "{base}/air-search-ui/dom2/e2e"
        "?flight_depart_date={travel_date}&arrivalDate=&class=Economy"
        "&source={origin}&destination={destination}&adults=1&children=0&infants=0"
        "&tripType=O&viewName=normal"
    )

    def __init__(self) -> None:
        super().__init__()
        # Enforce rate capping for this Tier-1.5 source.
        self.daily_cap = settings.ota_max_requests_per_day

    def search_url(self, origin: str, destination: str, travel_date: date) -> str:
        # Yatra expects dd/mm/yyyy
        formatted_date = travel_date.strftime("%d/%m/%Y")
        return self.URL_TEMPLATE.format(
            base=self.base_url,
            origin=origin,
            destination=destination,
            travel_date=formatted_date,
        )

    def check_compliance(
        self, origin: str, destination: str, travel_date: date
    ) -> ComplianceDecision:
        return self.robots.check(self.search_url(origin, destination, travel_date))

    async def search(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        cabin: str = "economy",
    ) -> FetchOutcome:
        decision = self.check_compliance(origin, destination, travel_date)
        logger.info("compliance: %s", decision.reason)
        if not decision.allowed:
            return FetchOutcome.fail(StatusCode.COMPLIANCE_REFUSED, decision.reason)

        if self.daily_cap is not None and self._daily_count >= self.daily_cap:
            return FetchOutcome.fail(
                StatusCode.RATE_LIMITED,
                f"Daily cap of {self.daily_cap} reached for {self.name}",
            )

        try:
            from playwright.async_api import async_playwright  # noqa: PLC0415
        except ImportError:
            return FetchOutcome.fail(
                StatusCode.ADAPTER_UNAVAILABLE,
                "playwright is not installed",
            )

        url = self.search_url(origin, destination, travel_date)
        cycle_date = today_ist()
        last_status = StatusCode.NETWORK_ERROR
        last_note = ""

        for attempt in range(settings.scrape_max_retries + 1):
            await self.bucket.acquire()
            harvested: list[dict] = []
            try:
                self._daily_count += 1
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=True)
                    context = await browser.new_context(
                        user_agent=settings.scraper_user_agent,
                        locale="en-IN",
                        timezone_id="Asia/Kolkata",
                    )
                    page = await context.new_page()

                    async def on_response(response):  # noqa: ANN001
                        ctype = (response.headers or {}).get("content-type", "")
                        if "json" not in ctype.lower():
                            return
                        try:
                            harvested.extend(harvest_fares_from_json(await response.json()))
                        except Exception:
                            return

                    page.on("response", on_response)

                    try:
                        await page.goto(
                            url, wait_until="domcontentloaded",
                            timeout=settings.scrape_timeout_ms,
                        )
                        await page.wait_for_timeout(6000)
                        html = await page.content()
                    except Exception as exc:
                        await browser.close()
                        name = type(exc).__name__.lower()
                        if "timeout" in name:
                            last_status, last_note = StatusCode.TIMEOUT, str(exc)[:200]
                        else:
                            last_status, last_note = StatusCode.NETWORK_ERROR, str(exc)[:200]
                        if attempt < settings.scrape_max_retries:
                            await backoff_sleep(attempt)
                            continue
                        return FetchOutcome.fail(last_status, last_note)

                    await browser.close()

                if looks_blocked(html):
                    return FetchOutcome.fail(
                        StatusCode.BLOCKED_CAPTCHA,
                        "bot challenge detected; stopping for this source",
                    )
                if looks_like_consent_wall(html) and not harvested:
                    last_status = StatusCode.MALFORMED
                    last_note = "interstitial/consent page returned instead of results"
                    if attempt < settings.scrape_max_retries:
                        await backoff_sleep(attempt)
                        continue
                    return FetchOutcome.fail(last_status, last_note)

                if not harvested:
                    return FetchOutcome.fail(
                        StatusCode.EMPTY_NO_INVENTORY,
                        "page loaded but no fare payload was observed",
                    )

                quotes = self._to_quotes(harvested, origin, destination,
                                         travel_date, cycle_date)
                if not quotes:
                    return FetchOutcome.fail(
                        StatusCode.MALFORMED,
                        f"{len(harvested)} price-like objects found but none formed a valid quote",
                    )
                return FetchOutcome.ok(quotes, f"{len(quotes)} live quote(s) from {self.name}")

            except Exception as exc:
                last_status, last_note = StatusCode.NETWORK_ERROR, str(exc)[:200]
                if attempt < settings.scrape_max_retries:
                    await backoff_sleep(attempt)
                    continue

        return FetchOutcome.fail(last_status, last_note)

    def _to_quotes(
        self,
        harvested: list[dict],
        origin: str,
        destination: str,
        travel_date: date,
        cycle_date: date,
    ) -> list[RawQuote]:
        try:
            window = AdvanceWindow.from_days(advance_window_days(travel_date, cycle_date))
        except ValueError:
            return []
        
        quotes: list[RawQuote] = []
        for item in harvested:
            total = item.get("total")
            if not total or total <= 0:
                continue
            base = item.get("base")
            tax = item.get("tax")
            if base is None and tax is not None:
                base = total - tax
            if base is None:
                base, tax = total, 0.0
            if tax is None:
                tax = max(0.0, total - base)
            
            carrier = item.get("carrier")
            if not carrier:
                carrier = "Unknown"

            quotes.append(
                RawQuote(
                    origin=origin,
                    destination=destination,
                    carrier=carrier[:8],
                    travel_date=travel_date,
                    advance_window=window,
                    base_fare=round(float(base), 2),
                    taxes=round(float(tax), 2),
                    udf=0.0,
                    convenience_fee=0.0,
                    total_fare=round(float(total), 2),
                    source=self.name,
                    source_type=self.default_source_type,
                    cycle_date=cycle_date,
                    fare_class="economy",
                    trip_type=TripType.ONE_WAY,
                    is_nonstop=True,
                    currency="INR",
                )
            )
        quotes.sort(key=lambda q: q.total_fare)
        return quotes[:10]
