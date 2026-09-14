"""
HTTP layer.

  main.py     — the FastAPI app and its endpoints
  schemas.py  — Pydantic response models

Kept read-only on purpose. Collection, cleaning and index construction happen
in the pipeline (`apix scrape` / `apix clean` / `apix index`), never inside a
request handler. An index number must be reproducible from a recorded run, and
a value that could be recomputed differently by an incidental HTTP call would
not be.
"""
from __future__ import annotations

__all__ = ["app"]


def __getattr__(name: str):
    # Lazy so that importing `apix.api` (e.g. in a test collecting modules)
    # does not force FastAPI and the DB engine to load.
    if name == "app":
        from apix.api.main import app

        return app
    raise AttributeError(name)
