"""
Persist the active basket into `basket_config`.

Why the table exists when basket.yaml already does: the YAML is the editable
source, the table is the immutable record of what was actually in force when a
given index point was computed. Every `index_values` row carries a
`basket_version`, and this table is what lets you look up the weights behind a
number published months ago.

Versions are never overwritten. Re-running with the same `basket_version` is a
no-op unless --force is passed; changing weights means minting a NEW version and
chain-linking at the overlap (see apix.index.construct.chain_link). That is how
a statistical office handles a basket revision, and it is why re-weighting can
never silently rewrite history here.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from sqlalchemy import delete, select

from apix.config.loader import load_basket
from apix.db.models import BasketConfigRow
from apix.db.session import create_all, session_scope


def seed_basket(force: bool = False) -> tuple[int, str]:
    basket = load_basket()
    with session_scope() as session:
        existing = session.scalars(
            select(BasketConfigRow).where(
                BasketConfigRow.basket_version == basket.basket_version
            )
        ).all()
        if existing and not force:
            return 0, basket.basket_version
        if existing:
            session.execute(
                delete(BasketConfigRow).where(
                    BasketConfigRow.basket_version == basket.basket_version
                )
            )
        for r in basket.routes:
            session.add(
                BasketConfigRow(
                    route=r.route,
                    weight=r.weight,
                    national_share_pct=r.national_share_pct,
                    weight_source=basket.weight_source[:500],
                    basket_version=basket.basket_version,
                    effective_from=basket.effective_from,
                    effective_to=basket.effective_to,
                )
            )
        return len(basket.routes), basket.basket_version


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Persist basket.yaml into basket_config.")
    ap.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing version (use only while iterating on the config)",
    )
    args = ap.parse_args(argv)

    create_all()
    basket = load_basket()
    n, version = seed_basket(args.force)

    if n == 0:
        print(f"Basket version {version!r} is already recorded — nothing to do.")
        print("Mint a new basket_version in basket.yaml to revise weights, or pass")
        print("--force to overwrite while iterating.")
        return 0

    print(f"Recorded {n} route weight(s) under basket version {version!r}")
    print(f"  effective from : {basket.effective_from}")
    print(f"  weight source  : {basket.weight_source[:120]}...")
    total = sum(r.weight for r in basket.routes)
    print(f"  weights sum to : {total:.6f}")
    for r in basket.routes:
        print(f"    {r.route}  share {r.national_share_pct:5.2f}%  ->  weight {r.weight:.4f}")
    if abs(total - 1.0) > 1e-6:
        print("  WARNING: weights do not sum to 1.0", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
