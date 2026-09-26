"""Synthetic fixture builders shared by the deterministic-core tests
(scoring, selection, metrics) -- no network, no LLM."""
from __future__ import annotations

from agent.schemas import Candidate, QualitySignal, ScoredCandidate, UnderstandingRecord


def make_candidate(video_id: str, duration_minutes: float, title: str | None = None) -> Candidate:
    return Candidate(
        video_id=video_id,
        title=title or f"Video {video_id}",
        channel="Some Channel",
        url=f"https://youtube.com/watch?v={video_id}",
        duration_minutes=duration_minutes,
        transcript_available=True,
        transcript=f"transcript for {video_id}",
    )


def make_understanding(
    video_id: str,
    *,
    matches_unknown: list[str] | None = None,
    overlaps_known: list[str] | None = None,
    constraint_violations: list[str] | None = None,
    phase: str = "concept",
    confidence: float = 0.8,
    clarity: float = 0.8,
    content_density: float = 0.8,
) -> UnderstandingRecord:
    return UnderstandingRecord(
        video_id=video_id,
        covers_topics=matches_unknown or [],
        primary_style="project-based",
        depth="intro",
        phase=phase,
        matches_unknown=matches_unknown or [],
        overlaps_known=overlaps_known or [],
        constraint_violations=constraint_violations or [],
        evidence_snippet=f"evidence for {video_id}",
        grounded=True,
        confidence=confidence,
        quality_signal=QualitySignal(clarity=clarity, content_density=content_density, accuracy_flags=[]),
    )


def make_scored(
    video_id: str,
    duration_minutes: float,
    utility: float,
    *,
    cluster_id: int,
    matches_unknown: list[str] | None = None,
    phase: str = "concept",
) -> ScoredCandidate:
    return ScoredCandidate(
        candidate=make_candidate(video_id, duration_minutes),
        understanding=make_understanding(video_id, matches_unknown=matches_unknown, phase=phase),
        cluster_id=cluster_id,
        utility=utility,
    )
