"""
Operational scripts.

  seed_basket.py       persist basket.yaml into the versioned basket_config table
  seed_dgca.py         load real DGCA monthly figures, or clearly-labelled placeholders
  generate_history.py  produce a simulated back-history so a fresh clone has a series

These are importable (not just runnable) because `apix.cli` calls into them —
one implementation, reachable both as `apix seed-dgca` and as
`python -m scripts.seed_dgca`. Running them directly still works and prints the
same output; the CLI is just the front door.
"""
