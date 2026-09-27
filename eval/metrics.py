"""Automated, reference-free metrics (HLD.md §6.1) -- pure functions, no
LLM calls, no network. Runs against a CurriculumOutput plus the
scored-candidate pool that produced it. eval/run_eval.py aggregates these
per test-set scenario.
"""
from __future__ import annotations

import re
from statistics import mean

from agent.schemas import CurriculumOutput, ScoredCandidate

BUDGET_UNDER_TOLERANCE = 0.25  # §6.1: ~25% under is fine; the spec's own sketch is ~17% under
PHASE_ORDER = {"setup": 0, "concept": 1, "hands-on-project": 2, "advanced-followup": 3}


def budget_compliance(output: CurriculumOutput) -> dict:
    over = output.total_minutes > output.budget_minutes
    under_fraction = (
        (output.budget_minutes - output.total_minutes) / output.budget_minutes
        if output.budget_minutes
        else 0.0
    )
    return {
        "over_budget": over,
        "under_fraction": round(under_fraction, 4),
        "hard_fail": over,
        "soft_flag": (not over) and under_fraction > BUDGET_UNDER_TOLERANCE,
    }


def curriculum_shape_sanity(output: CurriculumOutput, candidate_pool_size: int) -> dict:
    """Heuristic per §6.1 -- there's no fixed count target (SKILLS #10), so
    this checks the *shape* of the outcome, not a specific number."""
    n = len(output.curriculum)
    has_infeasibility_warning = any("budget too small" in w for w in output.warnings)
    zero_unjustified = n == 0 and candidate_pool_size > 0 and not has_infeasibility_warning
    one_dominates = (
        n == 1
        and output.budget_minutes > 0
        and output.curriculum[0].duration_minutes / output.budget_minutes > 0.9
        and not has_infeasibility_warning
    )
    fragmented = n >= 5 and mean(i.duration_minutes for i in output.curriculum) < 0.05 * output.budget_minutes
    return {
        "pick_count": n,
        "zero_unjustified": zero_unjustified,
        "one_dominates_without_warning": one_dominates,
        "fragmented": fragmented,
        "passes": not (zero_unjustified or one_dominates or fragmented),
    }


def unknown_topic_coverage(output: CurriculumOutput) -> dict:
    """(a) the LLM's own claim, via `goal_coverage`. The independent
    embedding cross-check (§6.1's method (b)) runs in run_eval.py, where
    real transcripts are available -- this function only covers (a)."""
    if not output.goal_coverage:
        return {"fraction_covered": None, "total_topics": 0}
    covered = sum(1 for v in output.goal_coverage.values() if v != "uncovered")
    total = len(output.goal_coverage)
    return {"fraction_covered": covered / total if total else None, "total_topics": total}


def known_topic_leakage(picks: list[ScoredCandidate]) -> dict:
    total_overlaps = sum(len(p.understanding.overlaps_known) for p in picks)
    picks_with_overlap = sum(1 for p in picks if p.understanding.overlaps_known)
    return {"total_overlaps_known": total_overlaps, "picks_with_overlap": picks_with_overlap}


def constraint_compliance(picks: list[ScoredCandidate]) -> dict:
    violations = [p for p in picks if p.understanding.constraint_violations]
    return {"violation_count": len(violations), "passes": len(violations) == 0}


def redundancy(picks: list[ScoredCandidate]) -> dict:
    """No two selected videos should share a dedup cluster (§3.5) -- the
    clustering already did the embedding-similarity work upstream."""
    cluster_ids = [p.cluster_id for p in picks]
    duplicate_count = len(cluster_ids) - len(set(cluster_ids))
    return {"duplicate_cluster_count": duplicate_count, "passes": duplicate_count == 0}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def grounding_rate(output: CurriculumOutput, transcripts_by_id: dict[str, str]) -> dict:
    """Fraction of picks whose `evidence_snippet` is a verifiable substring
    of the actual stored transcript -- the most direct check that picks are
    grounded in content, not metadata."""
    checked = 0
    hits = 0
    for item in output.curriculum:
        transcript = transcripts_by_id.get(item.video_id)
        if not transcript:
            continue
        checked += 1
        snippet = _normalize(item.evidence_snippet)
        if snippet and snippet in _normalize(transcript):
            hits += 1
    return {"checked": checked, "grounded_hits": hits, "rate": hits / checked if checked else None}


def sequencing_sanity(picks_in_order: list[ScoredCandidate]) -> dict:
    phases = [PHASE_ORDER[p.understanding.phase] for p in picks_in_order]
    inversions = sum(1 for a, b in zip(phases, phases[1:]) if b < a)
    return {"inversions": inversions, "passes": inversions == 0}


def content_quality(picks: list[ScoredCandidate], candidate_pool: list[ScoredCandidate]) -> dict:
    def avg_quality(items: list[ScoredCandidate]) -> float | None:
        if not items:
            return None
        return mean(
            i.understanding.quality_signal.clarity * i.understanding.quality_signal.content_density
            for i in items
        )

    picked_avg = avg_quality(picks)
    pool_avg = avg_quality(candidate_pool)
    return {
        "picked_avg_quality": picked_avg,
        "pool_avg_quality": pool_avg,
        "below_pool_average": picked_avg is not None and pool_avg is not None and picked_avg < pool_avg,
    }
