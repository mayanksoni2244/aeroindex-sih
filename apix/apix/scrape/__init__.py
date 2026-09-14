"""
Collection layer.

  base.py      contracts: StatusCode, RawQuote, FetchOutcome, RobotsGate,
               TokenBucket, the LiveSource interface
  live/        Tier-A adapters (real sites, compliance-gated)
  simulate.py  Tier-B deterministic synthetic generator
  runner.py    orchestrates a cycle: Tier-A first, Tier-B fills the rest,
               writes the scrape_runs ledger, upserts idempotently
"""
from apix.scrape.base import (  # noqa: F401
    ComplianceDecision,
    FetchOutcome,
    LiveSource,
    RawQuote,
    RobotsGate,
    StatusCode,
    TokenBucket,
    parse_inr,
)
