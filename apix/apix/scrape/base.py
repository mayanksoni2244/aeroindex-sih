"""
Scraping contracts shared by every source adapter.

Design rules that follow directly from the constitution:

* A failure is never generic. Each distinct failure mode gets its own
  `StatusCode` so the coverage panel can tell "the airline blocked us" apart
  from "there were genuinely no seats".
* Compliance is checked at *runtime*, per request path, against the live
  robots.txt — not asserted once in a comment.
* The only anti-bot resilience permitted is politeness: a token bucket, jitter,
  and capped exponential backoff. There is no evasion of any kind here, and
  adding some would violate the project's own stated ethics (see README
  §Compliance and §13 Anti-Requirements).
"""
from __future__ import annotations

import asyncio
import enum
import hashlib
import logging
import random
import re
import time
import urllib.robotparser
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urlparse

import httpx

from apix.config.settings import settings
from apix.db.models import AdvanceWindow, SourceType, TripType
from apix.db.time_utils import now_ist

logger = logging.getLogger("apix.scrape")


# ─────────────────────────────────────────────────────────────────────────────
# Status taxonomy (spec §2.3)
# ─────────────────────────────────────────────────────────────────────────────
class StatusCode(str, enum.Enum):
    """
    Outcome of a single (source, route, window) fetch attempt.

    The first eight are mandated by the spec. Two are added because folding
    them into an existing code would lose information a reviewer needs:

      COMPLIANCE_REFUSED  — robots.txt disallowed the path, so we never sent the
                            request. Materially different from BLOCKED_CAPTCHA,
                            where the server refused *us*: one is our own ethics
                            gate working, the other is their defence working.
      ADAPTER_UNAVAILABLE — the adapter could not run at all (e.g. the Playwright
                            browser binary is not installed). Not a network
                            fault and not the site's doing; calling it
                            NETWORK_ERROR would misattribute blame.
    """

    OK = "OK"
    TIMEOUT = "TIMEOUT"
    EMPTY_NO_INVENTORY = "EMPTY_NO_INVENTORY"
    BLOCKED_CAPTCHA = "BLOCKED_CAPTCHA"
    MALFORMED = "MALFORMED"
    RATE_LIMITED = "RATE_LIMITED"
    NETWORK_ERROR = "NETWORK_ERROR"
    VALIDATION_REJECT = "VALIDATION_REJECT"
    COMPLIANCE_REFUSED = "COMPLIANCE_REFUSED"
    ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"

    @property
    def is_success(self) -> bool:
        return self is StatusCode.OK


# ─────────────────────────────────────────────────────────────────────────────
# Value objects
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(slots=True)
class RawQuote:
    """One observed fare, before cleaning. Always carries its provenance."""

    origin: str
    destination: str
    carrier: str
    travel_date: date
    advance_window: AdvanceWindow
    base_fare: float
    taxes: float
    udf: float
    convenience_fee: float
    total_fare: float
    source: str
    source_type: SourceType
    cycle_date: date
    fare_class: str = "economy"
    trip_type: TripType = TripType.ONE_WAY
    is_nonstop: bool = True
    currency: str = "INR"
    scrape_timestamp: datetime = field(default_factory=now_ist)

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"

    def dedupe_hash(self) -> str:
        """
        Stable identity for one itinerary cell in one cycle (spec §3.1).

        `source` is deliberately NOT part of the key: two sources quoting the
        same flight on the same day are the same observation, and §3.1 says to
        collapse them keeping the most complete record. Tier-B yields to Tier-A
        for any cell live data already covers, so a live row is never silently
        overwritten by a simulated one.
        """
        parts = [
            self.origin,
            self.destination,
            self.carrier,
            self.travel_date.isoformat(),
            self.advance_window.value,
            self.fare_class,
            self.trip_type.value,
            self.cycle_date.isoformat(),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def completeness(self) -> int:
        """
        How itemised this record is. Used to pick a winner on dedupe collision
        — a quote that breaks out taxes/UDF/convenience beats one that only
        reports a total.
        """
        return sum(1 for v in (self.base_fare, self.taxes, self.udf,
                               self.convenience_fee) if v and v > 0)


@dataclass(slots=True)
class FetchOutcome:
    """Result of one fetch attempt: exactly one status, plus any quotes."""

    status: StatusCode
    quotes: list[RawQuote] = field(default_factory=list)
    note: str = ""

    @classmethod
    def ok(cls, quotes: list[RawQuote], note: str = "") -> "FetchOutcome":
        # An empty OK is a contradiction: no seats is EMPTY_NO_INVENTORY.
        if not quotes:
            return cls(StatusCode.EMPTY_NO_INVENTORY, [], note or "no offers returned")
        return cls(StatusCode.OK, quotes, note)

    @classmethod
    def fail(cls, status: StatusCode, note: str = "") -> "FetchOutcome":
        return cls(status, [], note)


@dataclass(slots=True)
class ComplianceDecision:
    allowed: bool
    reason: str
    robots_url: str


# ─────────────────────────────────────────────────────────────────────────────
# Politeness primitives (the ONLY anti-bot handling we implement)
# ─────────────────────────────────────────────────────────────────────────────
class TokenBucket:
    """Simple async token bucket: at most `rate_per_min` requests per minute."""

    def __init__(self, rate_per_min: int):
        self.rate_per_min = max(1, rate_per_min)
        self._interval = 60.0 / self.rate_per_min
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._interval - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            # Human-like jitter so we never emit a perfectly periodic signal.
            await asyncio.sleep(random.uniform(0.2, 0.9))
            self._last = time.monotonic()


async def backoff_sleep(attempt: int, base: float = 1.5, cap: float = 20.0) -> None:
    """Capped exponential backoff with jitter. Never hammers a struggling host."""
    delay = min(cap, base * (2**attempt)) * random.uniform(0.7, 1.0)
    await asyncio.sleep(delay)


# ─────────────────────────────────────────────────────────────────────────────
# robots.txt gate
# ─────────────────────────────────────────────────────────────────────────────
class RobotsGate:
    """
    Fetches and evaluates robots.txt for a target URL.

    Fails CLOSED: if robots.txt cannot be read we refuse to scrape, because we
    cannot demonstrate permission. That is the conservative reading and the one
    a reviewer should expect.
    """

    def __init__(self, user_agent: str | None = None, timeout: float = 10.0):
        self.user_agent = user_agent or settings.scraper_user_agent
        self.timeout = timeout
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _parser_for(self, url: str) -> tuple[urllib.robotparser.RobotFileParser | None, str]:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        robots_url = f"{origin}/robots.txt"
        if origin in self._cache:
            return self._cache[origin], robots_url
        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            resp = httpx.get(
                robots_url,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
                follow_redirects=True,
            )
            if resp.status_code == 404:
                # No robots.txt at all == no restrictions expressed (RFC 9309).
                parser = urllib.robotparser.RobotFileParser()
                parser.parse([])
            elif resp.status_code >= 400:
                parser = None
            else:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(resp.text.splitlines())
        except Exception as exc:  # network failure -> unknown -> refuse
            logger.warning("robots.txt fetch failed for %s: %s", robots_url, exc)
            parser = None
        self._cache[origin] = parser
        return parser, robots_url

    def check(self, url: str) -> ComplianceDecision:
        parser, robots_url = self._parser_for(url)
        if parser is None:
            return ComplianceDecision(
                False, "robots.txt unreachable — refusing (fail closed)", robots_url
            )
        allowed = parser.can_fetch(self.user_agent, url)
        reason = (
            f"robots.txt allows {url} for {self.user_agent!r}"
            if allowed
            else f"robots.txt DISALLOWS {url} for {self.user_agent!r}"
        )
        return ComplianceDecision(allowed, reason, robots_url)


# ─────────────────────────────────────────────────────────────────────────────
# Fare parsing (edge cases §11.9 / §11.10)
# ─────────────────────────────────────────────────────────────────────────────
_DEVANAGARI = str.maketrans("०१२३४५६७८९", "0123456789")
_NUM_RE = re.compile(r"\d[\d, \s]*(?:\.\d+)?")


def parse_inr(raw: str | float | int | None) -> float | None:
    """
    Turn a displayed fare into a clean numeric INR value.

    Handles ``"₹ 4,599"``, ``"Rs. 4 599.00"``, ``"INR 4,599"`` and
    Devanagari digits. Returns None when nothing numeric is present — callers
    map that to MALFORMED rather than guessing a value.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).translate(_DEVANAGARI)
    match = _NUM_RE.search(text)
    if not match:
        return None
    cleaned = re.sub(r"[, \s]", "", match.group(0))
    try:
        return float(cleaned)
    except ValueError:
        return None


# Markers that mean "the site is challenging us". We detect these in order to
# STOP — never to work around them.
_BLOCK_MARKERS = (
    "captcha",
    "are you a robot",
    "unusual traffic",
    "cf-challenge",
    "cf_chl",
    "attention required! | cloudflare",
    "access denied",
    "request blocked",
    "px-captcha",
    "please verify you are a human",
)


def looks_blocked(html: str) -> bool:
    """True if the response is a bot challenge / block page."""
    lowered = html.lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def looks_like_consent_wall(html: str) -> bool:
    """
    Interstitial / cookie-consent page returned with HTTP 200 (edge case §11.1).
    Treated as MALFORMED and retried once, not counted as a real empty result.
    """
    lowered = html.lower()
    hints = ("accept all cookies", "we use cookies", "consent", "continue to site")
    return any(h in lowered for h in hints) and "flight" not in lowered


# ─────────────────────────────────────────────────────────────────────────────
# Adapter interface
# ─────────────────────────────────────────────────────────────────────────────
class LiveSource(ABC):
    """
    Interface every Tier-A (real, live) adapter implements.

    Pluggable by design: `APIX_LIVE_SOURCE` selects the concrete class, so a
    different ToS-cleared source can be swapped in without the cleaning, index
    or API layers changing at all.
    """

    #: Human-readable source name, stored on every quote (e.g. "AkasaAir").
    name: str = "unnamed"
    #: Origin used for the robots.txt check.
    base_url: str = ""
    #: SourceType tag for quotes from this adapter.
    default_source_type: SourceType = SourceType.LIVE
    #: Hard daily request cap. None = unlimited (Tier-1). Set by Tier-1.5.
    daily_cap: int | None = None

    def __init__(self) -> None:
        self.robots = RobotsGate()
        self.bucket = TokenBucket(settings.scrape_rate_per_min)
        self._daily_count: int = 0

    @abstractmethod
    def search_url(self, origin: str, destination: str, travel_date: date) -> str:
        """The exact URL that would be fetched — also what compliance is checked against."""

    @abstractmethod
    def check_compliance(
        self, origin: str, destination: str, travel_date: date
    ) -> ComplianceDecision:
        """Evaluate robots.txt for the search path. Must be called before search()."""

    @abstractmethod
    async def search(
        self,
        origin: str,
        destination: str,
        travel_date: date,
        cabin: str = "economy",
    ) -> FetchOutcome:
        """Fetch live fares. Must never raise: map every failure to a StatusCode."""

    async def close(self) -> None:
        """Release browser/session resources."""
        return None
