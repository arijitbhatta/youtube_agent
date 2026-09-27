"""Output rendering (HLD.md §3.10) -- one source of truth (CurriculumOutput)
renders to two views, JSON and Markdown. Reasons for selected picks and
notable dropped runner-ups come from narrative.py's LLM phrasing; every
other dropped candidate gets a cheap deterministic reason computed here --
not every drop is worth an LLM call (CLAUDE.md's usage-cap discipline).
"""
from __future__ import annotations

from agent.schemas import (
    CurriculumItem,
    CurriculumOutput,
    DroppedItem,
    NarrativeDraft,
    PersonaInput,
    ReviewSummary,
    ScoredCandidate,
)

_DEFAULT_DROPPED_REASON = (
    "Did not fit the remaining time budget after higher-utility picks were selected."
)


def _rank_by_utility(all_scored: list[ScoredCandidate]) -> dict[str, int]:
    """1-indexed rank by utility across every scored candidate (selected and
    dropped) -- this is `would_have_ranked` in the output schema (§2.5)."""
    ordered = sorted(all_scored, key=lambda sc: sc.utility, reverse=True)
    return {sc.candidate.video_id: rank for rank, sc in enumerate(ordered, start=1)}


def _goal_coverage(selected: list[ScoredCandidate], unknown_topics: list[str]) -> dict[str, str]:
    coverage: dict[str, str] = {}
    for topic in unknown_topics:
        coverers = [sc for sc in selected if topic in sc.understanding.matches_unknown]
        if not coverers:
            coverage[topic] = "uncovered"
        elif any(sc.understanding.confidence >= 0.6 for sc in coverers):
            coverage[topic] = "covered"
        else:
            coverage[topic] = "partially covered"
    return coverage


def render_output(
    input_payload: PersonaInput,
    selected: list[ScoredCandidate],
    dropped: list[ScoredCandidate],
    selection_warnings: list[str],
    narrative: NarrativeDraft,
    review_summary: ReviewSummary,
) -> CurriculumOutput:
    reason_by_id = {item.video_id: item.reason for item in narrative.items}
    dropped_reason_by_id = {item.video_id: item.reason_dropped for item in narrative.dropped}
    rank_by_id = _rank_by_utility(selected + dropped)

    curriculum = []
    for order, sc in enumerate(selected, start=1):
        # Every selected item is asked about in narrative.py, so a missing
        # reason means the model dropped/mismatched an id at the boundary
        # filter -- fall back to the grounded topics rather than nothing.
        reason = reason_by_id.get(sc.candidate.video_id) or (
            f"Covers {', '.join(sc.understanding.matches_unknown) or 'the learning goal'} "
            f"(utility {sc.utility:.2f})."
        )
        curriculum.append(
            CurriculumItem(
                order=order,
                video_id=sc.candidate.video_id,
                title=sc.candidate.title,
                url=sc.candidate.url,
                duration_minutes=sc.candidate.duration_minutes,
                reason=reason,
                evidence_snippet=sc.understanding.evidence_snippet,
                confidence=sc.understanding.confidence,
                grounded=sc.understanding.grounded,
            )
        )

    considered_and_dropped = []
    for sc in dropped:
        reason_dropped = dropped_reason_by_id.get(sc.candidate.video_id, _DEFAULT_DROPPED_REASON)
        considered_and_dropped.append(
            DroppedItem(
                video_id=sc.candidate.video_id,
                title=sc.candidate.title,
                reason_dropped=reason_dropped,
                would_have_ranked=rank_by_id.get(sc.candidate.video_id),
            )
        )

    warnings = list(selection_warnings)
    for sc in selected:
        if sc.understanding.quality_signal.accuracy_flags:
            warnings.append(
                f"{sc.candidate.video_id}: unverified accuracy concern(s) -- "
                f"{', '.join(sc.understanding.quality_signal.accuracy_flags)}"
            )
        if not sc.understanding.grounded:
            warnings.append(f"{sc.candidate.video_id}: selected without a grounded transcript")
    for issue in review_summary.unresolved_blocking_issues:
        warnings.append(f"unresolved review issue ({issue.target}): {issue.issue}")

    total_minutes = sum(sc.candidate.duration_minutes for sc in selected)

    return CurriculumOutput(
        persona_id=input_payload.persona_id,
        budget_minutes=input_payload.time_budget_minutes,
        total_minutes=total_minutes,
        curriculum=curriculum,
        review=review_summary,
        considered_and_dropped=considered_and_dropped,
        warnings=warnings,
        goal_coverage=_goal_coverage(selected, input_payload.user_context.unknown),
    )


def render_markdown(output: CurriculumOutput) -> str:
    lines = [f"# Curriculum for `{output.persona_id}`", ""]
    lines.append(f"**Budget:** {output.budget_minutes} min &nbsp; **Used:** {output.total_minutes:.0f} min")
    lines.append("")

    if output.warnings:
        lines.append("## Warnings")
        for w in output.warnings:
            lines.append(f"- {w}")
        lines.append("")

    lines.append("## Curriculum")
    for item in output.curriculum:
        lines.append(f"### {item.order}. {item.title}")
        lines.append(f"- **Video:** {item.url}")
        lines.append(f"- **Duration:** {item.duration_minutes:.0f} min")
        lines.append(f"- **Confidence:** {item.confidence:.2f} &nbsp; **Grounded:** {item.grounded}")
        lines.append(f"- **Why:** {item.reason}")
        lines.append(f'- **Evidence:** "{item.evidence_snippet}"')
        lines.append("")

    if output.considered_and_dropped:
        lines.append("## Considered and dropped")
        for d in output.considered_and_dropped:
            rank_str = f" (would have ranked #{d.would_have_ranked})" if d.would_have_ranked else ""
            lines.append(f"- **{d.title}**{rank_str}: {d.reason_dropped}")
        lines.append("")

    lines.append("## Goal coverage")
    for topic, status in output.goal_coverage.items():
        lines.append(f"- {topic}: {status}")
    lines.append("")

    lines.append("## Review")
    lines.append(f"- Iterations: {output.review.iterations} &nbsp; Approved: {output.review.approved}")
    if output.review.unresolved_blocking_issues:
        lines.append("- Unresolved blocking issues:")
        for issue in output.review.unresolved_blocking_issues:
            lines.append(f"  - [{issue.target}] {issue.issue}")

    return "\n".join(lines)


def render_markdown_lean(output: CurriculumOutput) -> str:
    """Learner-facing Markdown (HLD.md §5.2) -- what ui/app.py shows, not the
    full `render_markdown` above (which the CLI and eval keep). Omits the
    internal/eval surfaces -- warnings, confidence, grounded, evidence,
    goal coverage, the reviewer, and the ranked considered-and-dropped list
    (CLAUDE.md principle #4: transparency lives in the trace/JSON/CLI, not in
    what a learner is shown). Title is rendered as plain text rather than
    inside a markdown link so an upstream title can't break the link syntax.
    """
    n = len(output.curriculum)
    noun = "video" if n == 1 else "videos"
    lines = [
        "# Your learning plan",
        "",
        f"**{n} {noun}**",
        "",
    ]
    for item in output.curriculum:
        lines.append(f"**{item.order}. {item.title}** · {item.duration_minutes:.0f} min")
        lines.append(f"[Watch on YouTube]({item.url})")
        lines.append(f"{item.reason}")
        lines.append("")
    if output.total_minutes > output.budget_minutes:
        lines.append(
            "Note: this plan runs a little over your budget — the last item or two "
            "may be skippable if you're short on time."
        )
        lines.append("")
    return "\n".join(lines)
