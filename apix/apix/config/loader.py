"""
YAML configuration loader with fail-fast validation.

Edge case §11.26: a basket file naming a route that does not exist in
routes.yaml (or a malformed IATA code) must raise a clear error at load time
rather than silently producing a wrong index.
"""
from __future__ import annotations

import re
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from apix.config.settings import CONFIG_DIR

IATA_RE = re.compile(r"^[A-Z]{3}$")
ROUTE_RE = re.compile(r"^[A-Z]{3}-[A-Z]{3}$")


class ConfigError(ValueError):
    """Raised when a YAML config file is structurally or semantically invalid."""


# ─────────────────────────────────────────────────────────────────────────────
# routes.yaml
# ─────────────────────────────────────────────────────────────────────────────
class RouteMeta(BaseModel):
    route: str
    origin: str
    destination: str
    name: str
    distance_km: int = Field(gt=0)
    sim_base_fare: float = Field(gt=0)
    sanity_min: float = Field(gt=0)
    sanity_max: float = Field(gt=0)

    @field_validator("route")
    @classmethod
    def _route_code(cls, v: str) -> str:
        if not ROUTE_RE.match(v):
            raise ValueError(f"route {v!r} must look like 'DEL-BOM' (two IATA codes)")
        return v

    @field_validator("origin", "destination")
    @classmethod
    def _iata(cls, v: str) -> str:
        if not IATA_RE.match(v):
            raise ValueError(f"{v!r} is not a 3-letter uppercase IATA code")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "RouteMeta":
        if self.route != f"{self.origin}-{self.destination}":
            raise ValueError(
                f"route {self.route!r} does not match origin/destination "
                f"{self.origin}-{self.destination}"
            )
        if self.sanity_min >= self.sanity_max:
            raise ValueError(f"{self.route}: sanity_min must be < sanity_max")
        return self


class CarrierMeta(BaseModel):
    code: str
    name: str
    price_multiplier: float = Field(gt=0)


class FareComponents(BaseModel):
    taxes_pct_of_base: float = Field(ge=0)
    udf_flat_inr: float = Field(ge=0)
    convenience_fee_inr: float = Field(ge=0)


class RoutesConfig(BaseModel):
    routes: list[RouteMeta]
    carriers: list[CarrierMeta]
    advance_windows: list[int]
    fare_components: FareComponents

    @field_validator("advance_windows")
    @classmethod
    def _windows(cls, v: list[int]) -> list[int]:
        if not v or any(w <= 0 for w in v):
            raise ValueError("advance_windows must be non-empty positive integers")
        return v

    def by_route(self, route: str) -> RouteMeta:
        for r in self.routes:
            if r.route == route:
                return r
        raise ConfigError(f"route {route!r} is not defined in routes.yaml")

    @property
    def route_codes(self) -> list[str]:
        return [r.route for r in self.routes]


# ─────────────────────────────────────────────────────────────────────────────
# basket.yaml
# ─────────────────────────────────────────────────────────────────────────────
class BasketRoute(BaseModel):
    route: str
    national_share_pct: float = Field(gt=0)
    weight: float = Field(gt=0, le=1)


class BasketConfig(BaseModel):
    basket_version: str
    weight_source: str
    effective_from: date
    effective_to: date | None = None
    routes: list[BasketRoute]

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "BasketConfig":
        total = sum(r.weight for r in self.routes)
        if abs(total - 1.0) > 1e-4:
            raise ValueError(
                f"basket weights must sum to 1.0 (got {total:.6f}). "
                "Renormalise national_share_pct across the basket."
            )
        dupes = {r.route for r in self.routes if
                 [x.route for x in self.routes].count(r.route) > 1}
        if dupes:
            raise ValueError(f"duplicate routes in basket: {sorted(dupes)}")
        return self

    @property
    def route_codes(self) -> list[str]:
        return [r.route for r in self.routes]

    def weight_of(self, route: str) -> float:
        for r in self.routes:
            if r.route == route:
                return r.weight
        raise ConfigError(f"route {route!r} is not in basket {self.basket_version!r}")


# ─────────────────────────────────────────────────────────────────────────────
# festivals.yaml
# ─────────────────────────────────────────────────────────────────────────────
class Festival(BaseModel):
    name: str
    start: date
    end: date
    multiplier: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "Festival":
        if self.end < self.start:
            raise ValueError(f"festival {self.name!r}: end is before start")
        return self

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end


class FestivalConfig(BaseModel):
    festivals: list[Festival]

    def surge_for(self, travel_date: date) -> Festival | None:
        """Return the festival window covering `travel_date`, if any."""
        for f in self.festivals:
            if f.covers(travel_date):
                return f
        return None

    def is_surge(self, travel_date: date) -> bool:
        return self.surge_for(travel_date) is not None


# ─────────────────────────────────────────────────────────────────────────────
# Loading
# ─────────────────────────────────────────────────────────────────────────────
def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"config file missing: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a YAML mapping at the top level")
    return data


@lru_cache(maxsize=1)
def load_routes(config_dir: Path | None = None) -> RoutesConfig:
    path = (config_dir or CONFIG_DIR) / "routes.yaml"
    try:
        return RoutesConfig.model_validate(_read_yaml(path))
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"invalid routes.yaml: {exc}") from exc


@lru_cache(maxsize=1)
def load_basket(config_dir: Path | None = None) -> BasketConfig:
    path = (config_dir or CONFIG_DIR) / "basket.yaml"
    try:
        basket = BasketConfig.model_validate(_read_yaml(path))
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"invalid basket.yaml: {exc}") from exc

    # Cross-file referential integrity — the fail-fast guard for edge case 26.
    known = set(load_routes(config_dir).route_codes)
    unknown = [r for r in basket.route_codes if r not in known]
    if unknown:
        raise ConfigError(
            f"basket.yaml references route(s) {unknown} that are not defined in "
            f"routes.yaml (known routes: {sorted(known)})"
        )
    return basket


@lru_cache(maxsize=1)
def load_festivals(config_dir: Path | None = None) -> FestivalConfig:
    path = (config_dir or CONFIG_DIR) / "festivals.yaml"
    try:
        return FestivalConfig.model_validate(_read_yaml(path))
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"invalid festivals.yaml: {exc}") from exc


def load_live_basket(config_dir: Path | None = None) -> BasketConfig:
    """
    Renormalised 4-route basket for the Live Index (Akasa Air non-stop network).
    Covers DEL-BOM, DEL-BLR, BOM-BLR, and DEL-CCU (12.09% of national domestic traffic).
    """
    full = load_basket(config_dir)
    live_routes = {"DEL-BOM", "DEL-BLR", "BOM-BLR", "DEL-CCU"}
    routes_subset = [r for r in full.routes if r.route in live_routes]
    total_share = sum(r.national_share_pct for r in routes_subset)
    renorm = [
        BasketRoute(
            route=r.route,
            national_share_pct=r.national_share_pct,
            weight=round(r.national_share_pct / total_share, 4),
        )
        for r in routes_subset
    ]
    diff = round(1.0 - sum(r.weight for r in renorm), 4)
    if diff != 0:
        renorm[0] = BasketRoute(
            route=renorm[0].route,
            national_share_pct=renorm[0].national_share_pct,
            weight=round(renorm[0].weight + diff, 4),
        )
    return BasketConfig(
        basket_version="v1-akasa-live-4route",
        weight_source=f"DGCA FY2022-23 national shares renormalised across 4 Akasa non-stop routes ({total_share:.2f}% national coverage)",
        effective_from=full.effective_from,
        effective_to=None,
        routes=renorm,
    )


def clear_config_cache() -> None:
    """Drop memoised configs (used by tests that write temporary YAML)."""
    load_routes.cache_clear()
    load_basket.cache_clear()
    load_festivals.cache_clear()
