"""Constrained selection + sequencing (HLD.md §4) -- pure code, no LLM, no
count target. Feed it synthetic scored candidates and assert on the exact
subset/order that comes out (tests/test_selection.py); that's the point.
"""
from __future__ import annotations

from agent.schemas import ScoredCandidate

PHASE_ORDER = {"setup": 0, "concept": 1, "hands-on-project": 2, "advanced-followup": 3}
INFEASIBILITY_COVERAGE_THRESHOLD = 0.34  # below this, most of `unknown` is left uncovered


def select_and_sequence(
    scored: list[ScoredCandidate],
    budget_minutes: float,
    unknown_topics: list[str],
) -> tuple[list[ScoredCandidate], list[ScoredCandidate], list[str]]:
    """Returns (selected, in final phase-sorted order; dropped; warnings)."""
    by_id = {sc.candidate.video_id: sc for sc in scored}

    def duration(video_id: str) -> float:
        return by_id[video_id].candidate.duration_minutes

    # 1. Greedy pass by bang-per-minute (utility / duration).
    ranked = sorted(
        scored,
        key=lambda sc: sc.utility / max(sc.candidate.duration_minutes, 0.01),
        reverse=True,
    )

    picked_ids: list[str] = []
    cluster_rep: dict[int, str] = {}
    remaining_budget = float(budget_minutes)

    for sc in ranked:
        vid = sc.candidate.video_id
        rep_id = cluster_rep.get(sc.cluster_id)
        if rep_id is not None:
            rep = by_id[rep_id]
            if rep.utility >= sc.utility:
                continue  # cluster already represented by an equal-or-stronger pick
            freed = remaining_budget + duration(rep_id)
            if duration(vid) > freed:
                continue
            remaining_budget = freed - duration(vid)
            picked_ids.remove(rep_id)
            picked_ids.append(vid)
            cluster_rep[sc.cluster_id] = vid
        else:
            if duration(vid) > remaining_budget:
                continue
            remaining_budget -= duration(vid)
            picked_ids.append(vid)
            cluster_rep[sc.cluster_id] = vid

    def covered_topics(ids: list[str]) -> set[str]:
        covered: set[str] = set()
        for vid in ids:
            covered.update(by_id[vid].understanding.matches_unknown)
        return covered

    # 2. Coverage check: bounded swaps, one attempt per still-uncovered topic.
    uncovered = [t for t in unknown_topics if t not in covered_topics(picked_ids)]
    for topic in uncovered:
        candidates_for_topic = [
            sc
            for sc in scored
            if topic in sc.understanding.matches_unknown and sc.candidate.video_id not in picked_ids
        ]
        if not candidates_for_topic or not picked_ids:
            continue
        best = max(candidates_for_topic, key=lambda sc: sc.utility)
        lowest_id = min(picked_ids, key=lambda vid: by_id[vid].utility)
        lowest = by_id[lowest_id]
        freed = remaining_budget + duration(lowest_id)
        best_vid = best.candidate.video_id
        # Coverage matters more than the lowest pick's utility here -- this
        # step exists specifically to force coverage of an otherwise-missed
        # topic, at some utility cost, bounded to one swap per topic.
        if duration(best_vid) <= freed:
            remaining_budget = freed - duration(best_vid)
            picked_ids.remove(lowest_id)
            del cluster_rep[lowest.cluster_id]
            picked_ids.append(best_vid)
            cluster_rep[best.cluster_id] = best_vid

    # 3. Infeasibility check -- no minimum pick count, just an honest warning.
    warnings: list[str] = []
    if not picked_ids:
        warnings.append(
            "budget too small to responsibly cover this goal; no candidate fit within the budget"
        )
    elif unknown_topics:
        final_covered = covered_topics(picked_ids) & set(unknown_topics)
        coverage_fraction = len(final_covered) / len(unknown_topics)
        if coverage_fraction < INFEASIBILITY_COVERAGE_THRESHOLD:
            warnings.append(
                "budget too small to responsibly cover this goal; curriculum addresses a "
                f"scoped-down subset ({len(final_covered)}/{len(unknown_topics)} unknown topics "
                "covered) -- see notes"
            )

    # 4. Sequencing: by phase, tie-broken by utility descending.
    picked = [by_id[vid] for vid in picked_ids]
    picked.sort(key=lambda sc: (PHASE_ORDER[sc.understanding.phase], -sc.utility))

    dropped = [sc for vid, sc in by_id.items() if vid not in picked_ids]

    return picked, dropped, warnings
