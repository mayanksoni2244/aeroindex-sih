"""
Confidence grading for a published index point.

WHAT THIS IS, AND WHAT IT IS DELIBERATELY NOT
This turns the honest limitations already recorded in every point's lineage
into a single graded statement about how much evidence stands behind the
number. It is a *support* measure, not a confidence interval: it says how much
real, matched, well-spread observation the value rests on, not how likely the
value is to be within some distance of a true population mean. Calling a
weighted support score a "95% CI" would be exactly the kind of statistical
theatre this project refuses, so the vocabulary here stays plain — HIGH,
MODERATE, LOW, INDICATIVE — and every grade ships with the reasons that
produced it.

WHY IT IS WORTH HAVING
A small basket is normally something a prototype hides. Stated precisely it is
the opposite: a four-route index built on twenty observed fares, which says so
on every point, is more useful to a statistical office than a six-route index
that quietly imputes the gaps. The grade makes the difference legible at a
glance and makes it impossible to quote a thin number as though it were a
thick one.

THE FOUR COMPONENTS
Each is a 0..1 sub-score, combined with fixed weights into a 0..100 score.
They were chosen because each one can fail independently, and because every
input already exists in the lineage — nothing here needs new collection.

  * observation (0.40) — how many real matched cells back the point, against
    `target_cells`. The single biggest driver: everything else is a caveat on
    top of having actually observed something.
  * provenance  (0.30) — the share of contributing quotes that are real
    observations rather than simulated or carried forward.
  * coverage    (0.20) — the share of the basket's ORIGINAL weight represented.
    Weight, not route count: losing DEL-BOM is not the same event as losing
    BLR-HYD.
  * breadth     (0.10) — how evenly the observations are spread across the
    covered routes, so one heavily-sampled route cannot pass for a broad index.

The weights are a judgement call, declared in one place (`_WEIGHTS`) rather
than scattered through the arithmetic, so a reviewer can disagree with them
without reverse-engineering them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Cells that would make a daily point fully-supported on the live basket:
#: 4 routes x 5 advance-purchase windows. A point at or above this scores 1.0
#: on the observation component; below it, the component scales linearly.
#: This is the live grid, not an aspiration — it is what one complete cycle of
#: the current basket actually yields.
TARGET_CELLS = 20

_WEIGHTS = {
    "observation": 0.40,
    "provenance": 0.30,
    "coverage": 0.20,
    "breadth": 0.10,
}

#: Score thresholds. A point scoring below `LOW` is labelled INDICATIVE, which
#: is the grade that says "do not quote this as a measurement".
_BANDS = (
    (80.0, "HIGH"),
    (60.0, "MODERATE"),
    (35.0, "LOW"),
)


@dataclass(frozen=True)
class Confidence:
    """A graded support statement for one index point."""

    score: float
    grade: str
    components: dict[str, float]
    reasons: list[str] = field(default_factory=list)
    real_cells: int = 0
    target_cells: int = TARGET_CELLS
    #: The weights that produced `score` from `components`. Published rather
    #: than left for the client to hardcode: a dashboard that prints "40%" next
    #: to a component the engine actually weights at 0.30 would be two
    #: documents disagreeing about one number, which is the failure mode this
    #: project treats as disqualifying. Ship the arithmetic with the result.
    weights: dict[str, float] = field(default_factory=lambda: dict(_WEIGHTS))

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "grade": self.grade,
            "components": self.components,
            "reasons": self.reasons,
            "real_cells": self.real_cells,
            "target_cells": self.target_cells,
            "weights": self.weights,
        }


def _evenness(per_route: dict[str, int]) -> float:
    """
    How evenly observations are spread across the routes that reported.

    Normalised Shannon entropy over the per-route counts: 1.0 when every
    covered route contributed equally, falling toward 0 as the observations
    concentrate in one route. Entropy rather than a variance ratio because it
    is bounded and scale-free, so the number means the same thing whether the
    point rests on 8 cells or 80.

    One route reporting is defined as 1.0 rather than 0.0: with a single route
    there is no imbalance to penalise, and the coverage component is already
    the thing that punishes a narrow basket. Charging twice for one fact would
    make the score read worse than the evidence warrants.
    """
    counts = [n for n in per_route.values() if n > 0]
    if len(counts) <= 1:
        return 1.0
    total = sum(counts)
    shares = [n / total for n in counts]
    # log without importing math.log2 per call site; entropy in bits.
    from math import log

    entropy = -sum(s * log(s, 2) for s in shares)
    return max(0.0, min(1.0, entropy / log(len(counts), 2)))


def grade_point(
    lineage: dict,
    route_relatives: dict[str, int] | None = None,
    target_cells: int = TARGET_CELLS,
) -> Confidence:
    """
    Grade one index point from its lineage.

    `lineage` is the dict already stored on every `IndexValue`; nothing is
    recomputed from the database, so a grade can never disagree with the
    provenance printed beside it. `route_relatives` optionally maps route ->
    matched cell count for the breadth component; without it, breadth is
    treated as neutral (1.0) rather than guessed at, and the reason list says
    so.

    Returns a `Confidence` whose `reasons` explain every deduction in words a
    non-specialist can act on.
    """
    reasons: list[str] = []

    live = int(lineage.get("live_count", 0)) + int(lineage.get("live_limited_count", 0))
    simulated = int(lineage.get("simulated_count", 0))
    imputed = int(lineage.get("imputed_count", 0))
    total = live + simulated + imputed

    # -- observation ------------------------------------------------------
    real_cells = int(lineage.get("cells_matched", 0))
    # Matched cells counts cells backing the relative; when the point is not
    # fully live, scale it by the real share so a mostly-simulated point cannot
    # score well on volume alone.
    if total:
        real_cells = int(round(real_cells * (live / total)))
    observation = min(1.0, real_cells / target_cells) if target_cells else 0.0
    if real_cells == 0:
        reasons.append("No real observed cells back this value.")
    elif real_cells < target_cells:
        reasons.append(
            f"{real_cells} of {target_cells} target cells are backed by real "
            "observations."
        )

    # -- provenance -------------------------------------------------------
    provenance = (live / total) if total else 0.0
    if simulated:
        reasons.append(
            f"{simulated} of {total} contributing quotes are simulated."
        )
    if imputed:
        reasons.append(
            f"{imputed} of {total} contributing quotes were carried forward "
            "from an earlier cycle, not observed today."
        )

    # -- coverage ---------------------------------------------------------
    coverage = float(lineage.get("weight_covered_pct", 0.0)) / 100.0
    coverage = max(0.0, min(1.0, coverage))
    missing = lineage.get("routes_missing") or []
    if missing:
        reasons.append(
            f"{len(missing)} basket route(s) absent ({', '.join(missing)}); "
            f"{coverage * 100:.1f}% of basket weight represented."
        )

    # -- breadth ----------------------------------------------------------
    # Prefer the per-route counts the lineage already carries; the explicit
    # argument is for callers holding a better breakdown (the window-level
    # aggregate builds one by summing across points).
    spread = route_relatives or lineage.get("route_cells_matched")
    if spread and real_cells > 0:
        breadth = _evenness({str(k): int(v) for k, v in spread.items() if int(v) > 0})
        if breadth < 0.85:
            reasons.append(
                "Observations are unevenly distributed across the covered routes."
            )
    elif real_cells > 0:
        # Nothing to judge the spread by. Neutral rather than punitive: the
        # absence of a breakdown is not evidence of imbalance.
        breadth = 1.0
    else:
        # No real observation at all. Scoring breadth 1.0 here would hand a
        # point with nothing behind it a tenth of the scale for free, which is
        # how an empty lineage ended up scoring 10 instead of 0.
        breadth = 0.0

    components = {
        "observation": round(observation, 4),
        "provenance": round(provenance, 4),
        "coverage": round(coverage, 4),
        "breadth": round(breadth, 4),
    }
    score = 100.0 * sum(components[k] * w for k, w in _WEIGHTS.items())
    score = round(max(0.0, min(100.0, score)), 1)

    grade = "INDICATIVE"
    for floor, label in _BANDS:
        if score >= floor:
            grade = label
            break

    if lineage.get("low_coverage"):
        reasons.append(
            "Coverage is below the configured minimum route count for a "
            "publishable index point."
        )
    if not reasons:
        reasons.append(
            f"Fully supported: {real_cells} real matched cells across "
            f"{coverage * 100:.0f}% of basket weight."
        )

    return Confidence(
        score=score,
        grade=grade,
        components=components,
        reasons=reasons,
        real_cells=real_cells,
        target_cells=target_cells,
        weights=dict(_WEIGHTS),
    )
