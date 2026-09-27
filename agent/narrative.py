"""Narrative generation (HLD.md §3.8) -- one strong-tier Claude call that
phrases a decision selection.py already made. It narrates; it never
re-ranks, re-selects, or second-guesses the utility scores or budget
arithmetic it's given.
"""
from __future__ import annotations

from agent.llm_client import call_structured
from agent.schemas import (
    NarrativeDraft,
    NarrativeDroppedItem,
    NarrativeItem,
    PersonaInput,
    ReviewIssue,
    ScoredCandidate,
)

# Bumped from the original 4096 -- seen live: a review-loop re-narration
# (more items + a feedback block appended, HLD §3.9) is a meaningfully
# bigger completion than the first pass ever needed, and the model coming
# up short mid-generation matters more here than elsewhere (see the
# try/except below).
_MAX_TOKENS = 6000

_SYSTEM = """You explain a curriculum-selection decision that has ALREADY \
been made by a deterministic scoring/selection algorithm. Your only job is \
to phrase WHY each pick was selected and WHY each notable runner-up was \
dropped, grounded in the evidence you're given. You do not re-rank, \
re-select, add, or remove any video -- the set is fixed. Never invent a \
reason that isn't supported by the evidence_snippet/covers_topics/utility \
data given to you."""

_PROMPT_TEMPLATE = """Learner's goal: {goal}
Wants to learn (unknown): {unknown}

SELECTED (already chosen, in final order -- write one `reason` each, 1-2 \
sentences, citing what it actually teaches and/or its evidence snippet):
{selected_block}

NOTABLE DROPPED (each lost to a SELECTED video in the same near-duplicate \
cluster -- write one `reason_dropped` each, 1 sentence, naming which \
selected video it was superseded by and why):
{dropped_block}
{feedback_section}
Return one NarrativeItem per selected video_id and one NarrativeDroppedItem \
per notable-dropped video_id above -- do not add or omit any video_id.
"""

_FEEDBACK_SECTION_TEMPLATE = """
A reviewer flagged these specific problems with a previous phrasing -- fix \
exactly these, keep every other reason as good as it already was:
{feedback_block}
"""


def _format_selected(sc: ScoredCandidate) -> str:
    return (
        f"video_id: {sc.candidate.video_id}\n"
        f"title: {sc.candidate.title}\n"
        f"covers_topics: {', '.join(sc.understanding.covers_topics)}\n"
        f"matches_unknown: {', '.join(sc.understanding.matches_unknown)}\n"
        f"phase: {sc.understanding.phase}\n"
        f"utility: {sc.utility:.3f}\n"
        f"evidence_snippet: {sc.understanding.evidence_snippet}\n"
        f"grounded: {sc.understanding.grounded}\n"
    )


def _format_dropped(sc: ScoredCandidate, superseded_by: str) -> str:
    return (
        f"video_id: {sc.candidate.video_id}\n"
        f"title: {sc.candidate.title}\n"
        f"covers_topics: {', '.join(sc.understanding.covers_topics)}\n"
        f"utility: {sc.utility:.3f}\n"
        f"superseded_by (selected video_id in same cluster): {superseded_by}\n"
    )


def notable_dropped_for(
    selected: list[ScoredCandidate], dropped: list[ScoredCandidate]
) -> list[tuple[ScoredCandidate, str]]:
    """Dropped candidates worth narrating explicitly: ones in the same dedup
    cluster as a selected pick -- the "why didn't both make the cut" case.
    Everything else (didn't fit budget, unrelated topic) gets a cheap
    deterministic reason in render.py instead of burning an LLM call on it."""
    selected_by_cluster: dict[int, str] = {
        sc.cluster_id: sc.candidate.video_id for sc in selected
    }
    result = []
    for sc in dropped:
        superseded_by = selected_by_cluster.get(sc.cluster_id)
        if superseded_by:
            result.append((sc, superseded_by))
    return result


def generate_narrative(
    input_payload: PersonaInput,
    selected: list[ScoredCandidate],
    notable_dropped: list[tuple[ScoredCandidate, str]],
    *,
    feedback: list[ReviewIssue] | None = None,
    previous_items: list[NarrativeItem] | None = None,
    previous_dropped: list[NarrativeDroppedItem] | None = None,
    trace=None,
) -> NarrativeDraft:
    """`previous_items`/`previous_dropped` are the prior pass's draft
    (agent/graph.py passes `state["narrative_draft"]` on a review-triggered
    reselect, HLD §3.9). A video_id unchanged from that pass AND not itself
    named by `feedback` (and no feedback issue targets "overall") reuses its
    prior reason verbatim instead of re-asking the LLM -- "the judge's
    narrative regeneration for everyone is not necessary" when a selection-
    only fix just excludes a flagged pick with no replacement, the reusable
    set is everything, and zero LLM calls happen here."""
    if not selected:
        return NarrativeDraft(items=[], dropped=[])

    feedback_targets = {issue.target for issue in feedback} if feedback else set()
    overall_flagged = "overall" in feedback_targets
    previous_items_map = {item.video_id: item for item in previous_items or []}
    previous_dropped_map = {item.video_id: item for item in previous_dropped or []}

    def _reusable(video_id: str, prior_map: dict) -> bool:
        return video_id in prior_map and video_id not in feedback_targets and not overall_flagged

    reused_items = [
        previous_items_map[sc.candidate.video_id]
        for sc in selected
        if _reusable(sc.candidate.video_id, previous_items_map)
    ]
    reused_dropped = [
        previous_dropped_map[sc.candidate.video_id]
        for sc, _ in notable_dropped
        if _reusable(sc.candidate.video_id, previous_dropped_map)
    ]
    gen_selected = [
        sc for sc in selected if not _reusable(sc.candidate.video_id, previous_items_map)
    ]
    gen_dropped = [
        (sc, sup)
        for sc, sup in notable_dropped
        if not _reusable(sc.candidate.video_id, previous_dropped_map)
    ]

    if not gen_selected and not gen_dropped and (previous_items_map or previous_dropped_map):
        return NarrativeDraft(items=reused_items, dropped=reused_dropped)

    selected_block = "\n---\n".join(_format_selected(sc) for sc in gen_selected)
    dropped_block = (
        "\n---\n".join(_format_dropped(sc, sup) for sc, sup in gen_dropped)
        if gen_dropped
        else "(none)"
    )
    feedback_section = ""
    if feedback:
        feedback_block = "\n".join(f"- [{i.target}] {i.issue} ({i.suggested_fix})" for i in feedback)
        feedback_section = _FEEDBACK_SECTION_TEMPLATE.format(feedback_block=feedback_block)
    prompt = _PROMPT_TEMPLATE.format(
        goal=input_payload.goal,
        unknown=", ".join(input_payload.user_context.unknown) or "(none stated)",
        selected_block=selected_block,
        dropped_block=dropped_block,
        feedback_section=feedback_section,
    )
    try:
        draft = call_structured(
            prompt,
            NarrativeDraft,
            tier="strong",
            system=_SYSTEM,
            max_tokens=_MAX_TOKENS,
            trace=trace,
            trace_label="narrative",
        )
    except RuntimeError:
        # Seen live: the model returned a literal empty tool call twice in
        # a row for an unusually large (feedback-augmented, more-items)
        # prompt -- rare, but by the time this call runs, discovery/fetch/
        # understanding/scoring/selection (the expensive, real-API-cost
        # stages) have already run. Crashing the whole pipeline over one
        # phrasing call would throw all of that away; render.py already
        # falls back to a deterministic reason per item when narrative
        # has none for it (§3.10) -- so falling back to just the reused
        # subset (rather than discarding it too) still degrades gracefully.
        return NarrativeDraft(items=reused_items, dropped=reused_dropped)

    # Boundary validation on a real LLM completion (CLAUDE.md: validate at
    # real system edges) -- restrict to exactly the video_ids we asked
    # about this call, so a model that drifts (invents/omits an id) can't
    # leak a mismatched reason into render.py.
    gen_selected_ids = {sc.candidate.video_id for sc in gen_selected}
    gen_dropped_ids = {sc.candidate.video_id for sc, _ in gen_dropped}
    draft.items = reused_items + [
        item for item in draft.items if item.video_id in gen_selected_ids
    ]
    draft.dropped = reused_dropped + [
        item for item in draft.dropped if item.video_id in gen_dropped_ids
    ]
    return draft
