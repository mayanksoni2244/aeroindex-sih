"""
Environment-driven settings (pydantic-settings).

Every tunable lives here or in the YAML config files — nothing that affects the
index is hardcoded in application code (constitution §5: reproducibility).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = CONFIG_DIR.parent
PROJECT_ROOT = PACKAGE_DIR.parent


class Settings(BaseSettings):
    """All configuration, read from environment / .env with an APIX_ prefix."""

    model_config = SettingsConfigDict(
        env_prefix="APIX_",
        env_file=os.getenv("APIX_ENV_FILE", str(PROJECT_ROOT / ".env")),
        extra="ignore",
    )

    # ── Database ─────────────────────────────────────────────────────────────
    # PostgreSQL is the target deployment (docker-compose sets this). The SQLite
    # default exists so `pytest` and the demo run on a machine without Docker;
    # the ORM layer is dialect-agnostic. Documented in README §Running.
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'apix.db'}"

    # ── API ──────────────────────────────────────────────────────────────────
    api_key: str = "dev-nso-rbi-key"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ── Scraping (Tier A) ────────────────────────────────────────────────────
    live_enabled: bool = True
    # Comma-separated adapter keys from `apix.scrape.live.REGISTRY`. There is
    # deliberately no singular `live_source` alongside this: two settings for
    # one concept meant the API could report a source that never ran, because
    # the runner read the plural and /v1/health printed the singular.
    live_sources: str = "akasa"
    ota_max_requests_per_day: int = 30
    scraper_user_agent: str = (
        "AeroIndex-Research/0.1 "
        "(SIH26056 academic prototype; contact: team-scalex@example.edu)"
    )
    scrape_rate_per_min: int = 12
    scrape_max_retries: int = 2
    scrape_timeout_ms: int = 30000
    # How many route×window cells the live runner has in flight at once.
    #
    # This does NOT raise the request rate: every search still passes the
    # shared TokenBucket at `scrape_rate_per_min`. It only lets the cells
    # overlap the long wait for the airline's fare response instead of
    # queueing it, which is where a serial cycle spent almost all of its ~51
    # minutes. Each in-flight cell is a real Chromium page, so this stays
    # small — enough to hide the waiting, not enough to look like a burst.
    live_concurrency: int = 4
    # Every basket route is attempted, including the two Akasa sells only as a
    # connection (BLR-HYD, MAA-DEL — verified 2026-09-13, see
    # recon/akasa_nonstop_check.json). Attempting them costs two cycles' worth
    # of empty cells per run and buys the honest answer: if Akasa ever adds a
    # non-stop, the coverage number rises on its own. Pruning them here would
    # make the config, not the airline, the reason a route reads as simulated.
    live_routes: str = "DEL-BOM,DEL-BLR,BOM-BLR,DEL-CCU,BLR-HYD,MAA-DEL"

    # ── Simulation (Tier B) ──────────────────────────────────────────────────
    sim_seed: int = 20260909

    # ── Cleaning ─────────────────────────────────────────────────────────────
    impute_max_age_days: int = 3
    # Tukey fence multiplier for the outlier flag. 1.5 is the textbook default;
    # exposed because the sensitivity of the headline/core gap to this choice is
    # something a statistician will want to test rather than take on trust.
    outlier_iqr_k: float = 1.5
    # Below this many quotes an IQR is not a meaningful dispersion estimate, so
    # the outlier stage stands down rather than flagging noise (edge case §11.6).
    min_quotes_for_iqr: int = 8

    # ── Index ────────────────────────────────────────────────────────────────
    elementary_formula: str = "jevons"  # "jevons" | "dutot"
    base_period: str = ""  # blank => first period with data
    # A weighted index over too few routes is not the basket it claims to be.
    # Below this, the point is still computed but flagged low-coverage in its
    # lineage so the dashboard can mark it.
    min_routes_for_index: int = 3

    # ── Derived helpers ──────────────────────────────────────────────────────
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def live_route_list(self) -> list[str]:
        return [r.strip().upper() for r in self.live_routes.split(",") if r.strip()]

    @property
    def live_sources_list(self) -> list[str]:
        return [s.strip().lower() for s in self.live_sources.split(",") if s.strip()]

    @property
    def live_sources_display(self) -> str:
        """Human-readable name of the configured Tier-A source(s), for /v1/health
        and the CLI status banner. Derived from the same list the runner
        iterates, so what is reported and what runs cannot drift apart."""
        return ", ".join(self.live_sources_list) or "(none configured)"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
