"""Deterministic scoring (HLD.md §3.6) -- pure arithmetic over understanding
records, no LLM involved. This and selection.py are the one part of the
pipeline that must be *correct*, not just *plausible* (CLAUDE.md).
"""
from __future__ import annotations

from agent.schemas import UnderstandingRecord

NOVELTY_BONUS = 0.05
KNOWN_OVERLAP_PENALTY_PER_ITEM = 0.2
CONSTRAINT_VIOLATION_PENALTY_PER_ITEM = 0.3
MAX_PENALTY = 0.9


def _topic_weights(
    records: list[UnderstandingRecord], unknown_topics: list[str]
) -> dict[tuple[str, str], float]:
    """Diminishing returns per unknown topic: the strongest candidate
    covering a topic (by confidence) claims most of its weight; a later
    candidate re-covering the same topic gets a shrinking share (harmonic
    decay: 1, 1/2, 1/3, ...), not a fresh full share."""
    weights: dict[tuple[str, str], float] = {}
    for topic in unknown_topics:
        coverers = [r for r in records if topic in r.matches_unknown]
        coverers.sort(key=lambda r: r.confidence, reverse=True)
        for rank, record in enumerate(coverers, start=1):
            weights[(record.video_id, topic)] = 1.0 / rank
    return weights


def base_relevance(
    record: UnderstandingRecord,
    unknown_topics: list[str],
    topic_weights: dict[tuple[str, str], float],
) -> float:
    if not unknown_topics:
        return 0.0
    covered = sum(topic_weights.get((record.video_id, t), 0.0) for t in unknown_topics)
    return covered / len(unknown_topics)


def known_overlap_penalty(record: UnderstandingRecord) -> float:
    return min(MAX_PENALTY, KNOWN_OVERLAP_PENALTY_PER_ITEM * len(record.overlaps_known))


def constraint_violation_penalty(record: UnderstandingRecord) -> float:
    return min(
        MAX_PENALTY, CONSTRAINT_VIOLATION_PENALTY_PER_ITEM * len(record.constraint_violations)
    )


def quality_multiplier(record: UnderstandingRecord) -> float:
    return record.quality_signal.clarity * record.quality_signal.content_density


def score_candidates(
    records: list[UnderstandingRecord],
    unknown_topics: list[str],
    cluster_ids: dict[str, int],
) -> dict[str, float]:
    """Returns video_id -> utility (§3.6's composite score). `cluster_ids`
    maps video_id -> its dedup cluster (§3.5); every video_id in `records`
    must have an entry, including singleton clusters."""
    topic_weights = _topic_weights(records, unknown_topics)

    pre_bonus: dict[str, float] = {}
    for r in records:
        rel = base_relevance(r, unknown_topics, topic_weights)
        pre_bonus[r.video_id] = (
            rel
            * (1 - known_overlap_penalty(r))
            * (1 - constraint_violation_penalty(r))
            * r.confidence
            * quality_multiplier(r)
        )

    # Novelty bonus: only the strongest pre-bonus candidate within each
    # cluster gets it -- being the best representative of a dedup group,
    # not just being in one.
    best_in_cluster: dict[int, str] = {}
    for r in records:
        cid = cluster_ids[r.video_id]
        current_best = best_in_cluster.get(cid)
        if current_best is None or pre_bonus[r.video_id] > pre_bonus[current_best]:
            best_in_cluster[cid] = r.video_id

    utility = dict(pre_bonus)
    for video_id in best_in_cluster.values():
        if pre_bonus[video_id] > 0:  # don't reward being alone in a cluster with zero relevance
            utility[video_id] += NOVELTY_BONUS

    return utility
