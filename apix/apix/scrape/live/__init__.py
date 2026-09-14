"""
Registry of Tier-A (live) adapters.

Exactly one source is registered, and that is deliberate. Reconnaissance
against every carrier and OTA named in the problem statement found that Akasa
Air is the only one whose published crawl policy leaves the booking path
unrestricted:

    Akasa Air          `User-Agent: *`, no Disallow rules at all  -> USED
    SpiceJet           disallows /api/v1, /public/, /externalBooking
    Air India Express  disallows /flight-availability — the fare path itself
    IndiGo, Air India  robots.txt did not respond
    MakeMyTrip, Yatra, Skyscanner, Kayak, Ixigo
                       robots-blocked, ToS-prohibited, or too ambiguous to
                       rely on for a government-facing prototype

The OTA adapters written against the excluded sources have been moved to
`archive/excluded-sources/adapters/` rather than deleted, so the reconnaissance
work remains auditable — but nothing imports them and nothing can select them.
Re-run `apix compliance` to re-check the live policies; these files change.

`AirIndiaExpressSource` is importable from this package but is deliberately
absent from `REGISTRY`. It is evidence, not capability: a real carrier with a
real published `Disallow` on the fare path, used by the compliance tests to
prove the gate refuses *before* a request is sent. Being unregistered is the
part that matters, and `test_compliance.py` asserts it.

`APIX_LIVE_SOURCES` picks the adapter(s), and the interface is unchanged, so a
source cleared by a future data-sharing agreement is added by writing one
`LiveSource` subclass and one line in REGISTRY — no change to cleaning, the
index or the API (spec §2.2, "pluggable").
"""
from __future__ import annotations

from apix.scrape.base import LiveSource
from apix.scrape.live.airindiaexpress import AirIndiaExpressSource
from apix.scrape.live.akasa import AkasaAirSource

#: key -> adapter class. Keys are what you put in APIX_LIVE_SOURCES.
#:
#: One entry, on purpose. Adding a key here is the single act that lets a
#: source contribute data to the index, which is why the list of what is *not*
#: here is documented above rather than left implicit.
REGISTRY: dict[str, type[LiveSource]] = {
    "akasa": AkasaAirSource,
}


def get_live_source(name: str) -> LiveSource:
    """Instantiate the adapter named by `name`. Raises KeyError with a useful list."""
    key = (name or "").strip().lower()
    if key not in REGISTRY:
        raise KeyError(
            f"unknown live source {name!r}; available: {', '.join(sorted(REGISTRY))}"
        )
    return REGISTRY[key]()


def available_sources() -> list[str]:
    return sorted(REGISTRY)


__all__ = [
    "REGISTRY",
    "AirIndiaExpressSource",
    "AkasaAirSource",
    "get_live_source",
    "available_sources",
]
