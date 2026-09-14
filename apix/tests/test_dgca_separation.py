"""
DGCA separation — constitution §4.

DGCA monthly averages are an ex-post yardstick. They are never an input to the
daily or weekly index: not as a fallback when a scrape is blocked, not as a
prior, not as a smoother. An index validated against a source it was partly
built from validates nothing.

This is enforced two ways, because either alone is weak:

  1. STATIC — no module in the index path may even name the DGCA table. Catches
     the mistake at the moment someone writes it.
  2. DYNAMIC — poison `dgca_monthly_avg` with absurd values and assert every
     index point is bit-identical. Catches any path the static check misses,
     including one reached through a string-built query.
"""
from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from apix.db.models import DgcaMonthlyAvg, IndexValue, Measure, Series
from apix.db.session import session_scope
from apix.index.backtest import MIN_MONTHS_FOR_CORRELATION, PLACEHOLDER_MARKER, run_backtest
from apix.index.construct import build_series
from tests.conftest import ANCHOR, make_quote, store

PACKAGE = Path(__file__).resolve().parent.parent / "apix"

FORBIDDEN_NAMES = {"DgcaMonthlyAvg", "dgca_monthly_avg"}

#: Modules that may legitimately touch the DGCA table, each for a stated reason.
ALLOWED = {
    PACKAGE / "index" / "backtest.py",   # the only consumer — that is its job
    PACKAGE / "db" / "models.py",        # defines the table
    PACKAGE / "db" / "__init__.py",      # re-exports the model
    PACKAGE / "api" / "main.py",         # /v1/backtest delegates to backtest.py
    PACKAGE / "cli.py",                  # `apix status`, `apix seed-dgca`
}


def _index_path_modules() -> list[Path]:
    """Every module the daily/weekly index actually runs through."""
    return [
        PACKAGE / "index" / "construct.py",
        PACKAGE / "clean" / "pipeline.py",
        PACKAGE / "scrape" / "runner.py",
        PACKAGE / "scrape" / "simulate.py",
        PACKAGE / "scrape" / "base.py",
        PACKAGE / "config" / "loader.py",
    ]


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of the `ast.Constant` nodes that are docstrings rather than code."""
    out: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            out.add(id(body[0].value))
    return out


def _code_references(module: Path) -> list[str]:
    """
    The forbidden names a module actually *uses*, as opposed to mentions.

    Parsed, not grepped, and docstrings are excluded deliberately. Several
    modules explain the separation rule in prose, and flagging that prose would
    train people to delete the explanation to get the suite green — the exact
    opposite of what this file exists for. Comments drop out for the same
    reason: the parser discards them.

    Everything else still counts — identifiers, attributes, imports, and plain
    string literals, which is how a reference smuggled through raw SQL or a
    `getattr` would look.

    Returns one description per reference, in source order.
    """
    source = module.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(module))
    except SyntaxError as exc:  # a module that will not parse cannot be cleared
        pytest.fail(f"{module} does not parse, so this test cannot clear it: {exc}")

    docstrings = _docstring_nodes(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            found.append((node.lineno, f".{node.attr}"))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found += [(node.lineno, f"import {a.name}")
                      for a in node.names if a.name in FORBIDDEN_NAMES]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            found += [(node.lineno, f"string literal {n!r}")
                      for n in sorted(FORBIDDEN_NAMES) if n in node.value]
    return [f"{what} (line {line})" for line, what in sorted(found)]


@pytest.mark.parametrize("module", _index_path_modules(), ids=lambda p: p.name)
def test_index_path_module_never_names_the_dgca_table(module):
    """Static: the identifier must not be used anywhere in the index path."""
    assert module.exists(), f"{module} is missing — update this test's module list"
    found = _code_references(module)

    assert not found, (
        f"{module.name} references the DGCA table: {found}. "
        "DGCA monthly data is a backtest yardstick only (constitution §4)."
    )


def test_only_expected_modules_mention_the_dgca_table():
    """
    Whole-package sweep, so a new module cannot quietly add a reference.

    The targeted test above names the modules the index is known to run
    through; this one catches the module nobody thought to add to that list.

    If you are adding a legitimate consumer, add it to ALLOWED with a comment
    saying why — a deliberate decision, not an accident.
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if path in ALLOWED:
            continue
        hits = _code_references(path)
        if hits:
            offenders[str(path.relative_to(PACKAGE.parent))] = hits

    assert not offenders, (
        f"unexpected DGCA references: {offenders}. Either remove them or add "
        "the module to ALLOWED in this test with a justification."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic enforcement
# ─────────────────────────────────────────────────────────────────────────────
def _seed_index() -> dict:
    day0, day1 = ANCHOR, ANCHOR + timedelta(days=1)
    for route in ("DEL-BOM", "DEL-BLR", "BOM-BLR"):
        store([make_quote(route=route, carrier="6E", cycle=day0, base=5000)])
        store([make_quote(route=route, carrier="6E", cycle=day1, base=5500)])
    build_series()
    return _snapshot()


def _snapshot() -> dict:
    with session_scope() as s:
        return {
            (r.index_date, r.series.value, r.measure.value, r.frequency.value): r.index_value
            for r in s.scalars(select(IndexValue)).all()
        }


def test_poisoning_the_dgca_table_does_not_move_the_index(basket):
    """
    Dynamic: absurd reference values must change nothing.

    This is the check that survives refactors. If any code path ever starts
    consulting DGCA data to build an index point, these numbers move.
    """
    before = _seed_index()
    assert before, "fixture produced no index points"

    with session_scope() as s:
        for route in ("DEL-BOM", "DEL-BLR", "BOM-BLR"):
            for month in ("2026-06", "2026-07", "2026-08"):
                s.add(DgcaMonthlyAvg(
                    route=route, month=month, avg_fare=999_999.0,
                    passenger_count=1, source_note="POISON - test fixture",
                ))

    build_series()
    assert _snapshot() == before, (
        "index values changed after poisoning dgca_monthly_avg — something in "
        "the index path is reading the backtest reference table"
    )


def test_the_index_does_not_need_the_dgca_table_at_all(basket):
    """Not merely ignoring the reference data — not needing it."""
    with session_scope() as s:
        s.add(DgcaMonthlyAvg(route="DEL-BOM", month="2026-07", avg_fare=6000.0))

    before = _seed_index()

    with session_scope() as s:
        s.execute(delete(DgcaMonthlyAvg))

    build_series()
    assert _snapshot() == before


# ─────────────────────────────────────────────────────────────────────────────
# Backtest honesty
# ─────────────────────────────────────────────────────────────────────────────
def _seed_reference(note: str) -> None:
    month = f"{ANCHOR.year}-{ANCHOR.month:02d}"
    with session_scope() as s:
        for route in ("DEL-BOM", "DEL-BLR", "BOM-BLR"):
            s.add(DgcaMonthlyAvg(
                route=route, month=month, avg_fare=6000.0, source_note=note,
            ))


def test_backtest_marks_placeholder_reference_as_unreportable(basket):
    """
    A backtest against illustrative rows may run, but may not be quoted.

    `reportable` is the single flag the CLI, the API and the dashboard all
    read, so there is exactly one definition of "may this be presented as
    validation" and it cannot drift between surfaces.
    """
    _seed_index()
    _seed_reference(f"{PLACEHOLDER_MARKER}: illustrative")

    result = run_backtest(Series.HEADLINE, Measure.TOTAL)
    assert result.uses_placeholder_reference is True
    assert result.reportable is False
    assert any(PLACEHOLDER_MARKER in n for n in result.notes)


def test_backtest_is_not_reportable_below_the_month_threshold(basket):
    """Pearson r on two points is always ±1. It must not read as evidence."""
    _seed_index()
    _seed_reference("DGCA Monthly Traffic Report (test citation)")

    result = run_backtest()
    assert result.uses_placeholder_reference is False, "this fixture cites a source"
    assert result.months_compared < MIN_MONTHS_FOR_CORRELATION
    assert result.sufficient is False
    assert result.reportable is False
    assert any(str(result.months_compared) in n for n in result.notes), (
        "the month count must be stated alongside the caveat"
    )


def test_pearson_on_a_flat_series_is_none_not_one():
    """
    A constant series has zero variance, so correlation is undefined.

    Returning 1.0 here would manufacture a perfect fit out of nothing — the
    most flattering possible bug.
    """
    from apix.index.backtest import pearson

    assert pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert pearson([1.0, 2.0, 3.0], [5.0, 5.0, 5.0]) is None
    assert pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
