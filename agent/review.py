"""Bounded review loop (HLD.md §3.9) -- `agent/graph.py` wires this as a
conditional edge with an explicit `review_iterations` counter carried in
graph state, never relying on LangGraph's generic `recursion_limit` to
enforce the cap (CLAUDE.md principle #5). The actual "is this good"
judgment lives in `agent/critique.py` (shared with `eval/judge.py`); this
module only owns the loop's routing/bookkeeping logic -- pure functions,
unit-testable without any network call.
"""
from __future__ import annotations

from typing import Literal

from agent import critique
from agent.schemas import (
    CurriculumOutput,
    PersonaInput,
    ReviewIssue,
    ReviewVerdict,
    ScoredCandidate,
)

MAX_REVIEW_ITERATIONS = 3


def run_review(
    input_payload: PersonaInput,
    draft_output: CurriculumOutput,
    picks_in_order: list[ScoredCandidate],
    candidate_pool: list[ScoredCandidate],
    *,
    trace=None,
) -> ReviewVerdict:
    # tier="cheap" here (not critique.evaluate's own "strong" default): this
    # is the path that runs on every real user request, so it gets the
    # lightweight model. eval/judge.py leaves the default alone -- offline
    # grading fidelity is untouched (CLAUDE.md: the eval matters most). The deterministic anchoring in critique.deterministic_issues
    # is unaffected either way -- it's not an LLM call.
    return critique.evaluate(
        input_payload, draft_output, picks_in_order, candidate_pool, tier="cheap", trace=trace
    )


def route_after_review(verdict: ReviewVerdict, iterations: int) -> Literal["sel", "narr", "out"]:
    """Pure routing decision. The cap itself (§3.9): only loop back while
    not-yet-approved AND `iterations` is still below MAX_REVIEW_ITERATIONS;
    otherwise ship as-is -- §3.9's "run ships anyway" rule, with any still-
    open blocking issue surfaced into the output's warnings by render.py
    instead of blocking forever."""
    if verdict.approved or iterations >= MAX_REVIEW_ITERATIONS:
        return "out"
    blocking = [i for i in verdict.issues if i.severity == "blocking"]
    if not blocking:
        return "out"
    stages = {i.stage_to_fix for i in blocking}
    if "selection" in stages:
        return "sel"
    return "narr"


def excluded_ids_after(verdict: ReviewVerdict | None, previously_excluded: set[str]) -> set[str]:
    """Accumulates video_ids flagged blocking/selection across iterations --
    a candidate excluded on iteration 1 must stay excluded on iteration 2,
    not silently become eligible again just because the latest verdict
    didn't happen to re-flag it (§3.9: reselect from the already-
    discovered, already-understood pool, with flagged candidates
    excluded)."""
    if verdict is None:
        return set(previously_excluded)
    newly_flagged = {
        issue.target
        for issue in verdict.issues
        if issue.severity == "blocking"
        and issue.stage_to_fix == "selection"
        and issue.target != "overall"
    }
    return set(previously_excluded) | newly_flagged


def narrative_feedback(verdict: ReviewVerdict | None) -> list[ReviewIssue]:
    """Blocking issues narrative.py should address on a targeted rewrite --
    a fresh read of only the latest verdict (unlike selection's exclusions,
    stale feedback about a pick that's no longer even selected would be
    meaningless to carry forward)."""
    if verdict is None:
        return []
    return [
        issue
        for issue in verdict.issues
        if issue.severity == "blocking" and issue.stage_to_fix == "narrative"
    ]
