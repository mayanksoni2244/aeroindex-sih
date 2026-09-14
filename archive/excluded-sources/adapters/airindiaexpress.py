'''
Tier-A adapter: Air India Express (IX) — a real, working compliance refusal.

This adapter exists to prove the compliance gate does something, using an actual
site rather than a mock. Air India Express publishes:

    User-agent: *
    Disallow: /flight-availability
    ...

The fare-search path is therefore off limits to us. `check_compliance()` returns
allowed=False and `search()` returns COMPLIANCE_REFUSED **without sending a
single request to their servers**. That is the whole point: the refusal happens
before the network call, not after.

Run `apix compliance --source airindiaexpress` to see it against the live
robots.txt. If Air India Express ever removes that rule, this adapter starts
working on its own — the decision is theirs to make, read at runtime, and not
hardcoded here. (`_ROBOTS_SNAPSHOT` below is documentation of what we observed,
not the value the gate uses.)
'''
from __future__ import annotations

import asyncio
import logging
from datetime import date

from apix.scrape.base import (
    ComplianceDecision,
    FetchOutcome,
    LiveSource,
    StatusCode,
)

logger = logging.getLogger("apix.scrape.live.airindiaexpress")

#: What https://www.airindiaexpress.com/robots.txt returned on 2026-09-09.
#: Kept for the record only — never consulted at runtime.
_ROBOTS_SNAPSHOT = """User-agent: *
Disallow: /flight-availability
"""


class AirIndiaExpressSource(LiveSource):
    name = "AirIndiaExpress"
    base_url = "https://www.airindiaexpress.com"
    carrier_code = "IX"

    URL_TEMPLATE = (
        "{base}/flight-availability"
        "?origin={origin}&destination={destination}&departureDate={travel_date}"
        "&adults=1&cabin=Economy"
    )

    def search_url(self, origin: str, destination: str, travel_date: date) -> str:
        return self.URL_TEMPLATE.format(
            base=self.base_url,
            origin=origin,
            destination=destination,
            travel_date=travel_date.isoformat(),
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
        if not decision.allowed:
            logger.info(
                "COMPLIANCE_REFUSED for %s %s-%s: %s",
                self.name, origin, destination, decision.reason,
            )
            return FetchOutcome.fail(
                StatusCode.COMPLIANCE_REFUSED,
                f"{decision.reason} (no request was sent)",
            )

        # Only reachable if the operator later permits this path. We do not
        # speculatively implement a scraper for a source we are not allowed to
        # read; that work happens if and when permission exists.
        return FetchOutcome.fail(
            StatusCode.ADAPTER_UNAVAILABLE,
            "robots.txt now permits this path, but no fetch logic is implemented "
            "for this source yet",
        )

    async def close(self) -> None:
        await asyncio.sleep(0)
