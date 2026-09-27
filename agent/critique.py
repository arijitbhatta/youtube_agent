"""Shared quality-judgment rubric (HLD.md §3.9 / §6.2) -- the single source
of truth for "is this curriculum good." `agent/review.py` (online, gates
one real run) and `eval/judge.py` (offline, grades the test set, Phase 8)
both import `evaluate()` below; CLAUDE.md: do not write a second, separate
quality-judgment prompt.

Anchored on deterministic checks, not vibes (§3.9): any failure of §6.1's
automated metrics (eval/metrics.py) on this specific draft becomes a
`blocking` ReviewIssue no matter what the LLM's own judgment says -- the
LLM can only ADD issues on top of that set, never dismiss one. This is
enforced here in code (`approved` is forced False whenever a deterministic
check fails, regardless of the LLM's own `approved` field), not by asking
the model nicely not to override it (CLAUDE.md principle #2).
"""
from __future__ import annotations

from eval import metrics
from agent.llm_client import Tier, call_structured
from agent.schemas import (
    CurriculumOutput,
    PersonaInput,
    ReviewIssue,
    ReviewVerdict,
    ScoredCandidate,
)

_SYSTEM = """You are reviewing a learning curriculum a separate pipeline \
already built: it already selected which videos to include, in what \
order, and phrased why. Judge one question: would following these picks, \
in this order, within the stated budget, plausibly get THIS learner to \
their goal? Flag any pick whose stated `reason` doesn't actually cite its \
`evidence_snippet`/`covers_topics`, any ordering that wouldn't make sense \
to a learner working through it top to bottom, or any pick with a clearly \
weaker signal than an available alternative. You are told which \
deterministic checks already failed on this draft, if any -- you cannot \
dismiss or override those; you may only add further issues on top of \
them."""

_PROMPT_TEMPLATE = """Learner's goal: {goal}
Learner's background: {background}
Wants to learn (unknown): {unknown}
Constraints: {constraints}
Budget: {budget} minutes (used: {used:.0f} minutes)

Curriculum, in final order:
{items_block}

Deterministic checks already run on this draft (you cannot override these):
{deterministic_block}

Return a verdict: `approved` (true only if you found no blocking issue of \
your own -- note a failed deterministic check above is already forced \
blocking regardless of what you return here), and `issues` (each with \
severity blocking/minor, stage_to_fix selection/narrative, target: a \
video_id or "overall", issue, suggested_fix).
"""


def _format_item(item) -> str:
    return (
        f"video_id: {item.video_id}\n"
        f"title: {item.title}\n"
        f"order: {item.order}\n"
        f"reason: {item.reason}\n"
        f"evidence_snippet: {item.evidence_snippet}\n"
        f"confidence: {item.confidence:.2f}\n"
        f"grounded: {item.grounded}\n"
    )


def _format_deterministic(issues: list[ReviewIssue]) -> str:
    if not issues:
        return "(none failed)"
    return "\n".join(f"- [{i.target}] {i.issue}" for i in issues)


def deterministic_issues(
    output: CurriculumOutput,
    picks_in_order: list[ScoredCandidate],
    candidate_pool: list[ScoredCandidate],
) -> list[ReviewIssue]:
    """Wraps eval/metrics.py's pure §6.1 checks into ReviewIssues. Only the
    checks with a clear pass/fail (not the descriptive-only ones like
    `content_quality`/`grounding_rate`) become blocking issues here --
    those two are reported in eval/run_eval.py's aggregate report instead,
    since "the picked average is below the pool average" isn't on its own
    evidence of a wrong pick (a tighter-budget pool can force that)."""
    issues: list[ReviewIssue] = []

    budget = metrics.budget_compliance(output)
    if budget["hard_fail"]:
        issues.append(
            ReviewIssue(
                severity="blocking",
                stage_to_fix="selection",
                target="overall",
                issue=(
                    f"Curriculum exceeds the time budget "
                    f"({output.total_minutes:.0f} > {output.budget_minutes} minutes)."
                ),
                suggested_fix="Re-run selection; the selected set must fit the stated budget.",
            )
        )

    shape = metrics.curriculum_shape_sanity(output, len(candidate_pool))
    if not shape["passes"]:
        if shape["zero_unjustified"]:
            reason = "no picks were selected despite feasible candidates existing"
        elif shape["one_dominates_without_warning"]:
            reason = "a single pick consumes almost the entire budget without an infeasibility warning"
        else:
            reason = "the curriculum is fragmented into many low-utility short clips"
        issues.append(
            ReviewIssue(
                severity="blocking",
                stage_to_fix="selection",
                target="overall",
                issue=f"Curriculum shape looks wrong: {reason}.",
                suggested_fix="Re-run selection over the existing candidate pool.",
            )
        )

    redundancy = metrics.redundancy(picks_in_order)
    if not redundancy["passes"]:
        issues.append(
            ReviewIssue(
                severity="blocking",
                stage_to_fix="selection",
                target="overall",
                issue=(
                    f"{redundancy['duplicate_cluster_count']} selected pick(s) share a "
                    "dedup cluster with another selected pick."
                ),
                suggested_fix="Re-run selection; two picks from the same near-duplicate cluster must never both be kept.",
            )
        )

    sequencing = metrics.sequencing_sanity(picks_in_order)
    if not sequencing["passes"]:
        issues.append(
            ReviewIssue(
                severity="blocking",
                stage_to_fix="selection",
                target="overall",
                issue=(
                    f"{sequencing['inversions']} sequencing inversion(s): a more-advanced-phase "
                    "pick appears before a more-foundational one."
                ),
                suggested_fix="Re-run selection/sequencing; picks must follow setup -> concept -> hands-on-project -> advanced-followup.",
            )
        )

    constraints = metrics.constraint_compliance(picks_in_order)
    if not constraints["passes"]:
        violating_ids = [
            p.candidate.video_id for p in picks_in_order if p.understanding.constraint_violations
        ]
        for video_id in violating_ids:
            issues.append(
                ReviewIssue(
                    severity="blocking",
                    stage_to_fix="selection",
                    target=video_id,
                    issue="This pick violates a constraint the learner explicitly stated.",
                    suggested_fix="Exclude this candidate and reselect from the remaining pool.",
                )
            )

    return issues


def llm_judgment(
    input_payload: PersonaInput,
    output: CurriculumOutput,
    deterministic: list[ReviewIssue],
    *,
    tier: Tier = "strong",
    trace=None,
) -> ReviewVerdict:
    items_block = "\n---\n".join(_format_item(item) for item in output.curriculum)
    prompt = _PROMPT_TEMPLATE.format(
        goal=input_payload.goal,
        background=input_payload.user_context.background,
        unknown=", ".join(input_payload.user_context.unknown) or "(none stated)",
        constraints=input_payload.user_context.constraints or "(none stated)",
        budget=output.budget_minutes,
        used=output.total_minutes,
        items_block=items_block or "(empty curriculum)",
        deterministic_block=_format_deterministic(deterministic),
    )
    return call_structured(
        prompt,
        ReviewVerdict,
        tier=tier,
        system=_SYSTEM,
        max_tokens=4096,
        trace=trace,
        trace_label="critique",
    )


def evaluate(
    input_payload: PersonaInput,
    output: CurriculumOutput,
    picks_in_order: list[ScoredCandidate],
    candidate_pool: list[ScoredCandidate],
    *,
    tier: Tier = "strong",
    trace=None,
) -> ReviewVerdict:
    """The one entrypoint both agent/review.py and eval/judge.py call.
    Deterministic failures are computed first and can never be dismissed:
    `approved` is forced False whenever any exist, or whenever the LLM's
    own verdict raised a blocking issue, regardless of the LLM's own
    top-level `approved` field. `tier` defaults to "strong" (eval/judge.py's
    offline grading stays untouched, per CLAUDE.md's "the eval matters most"); agent/review.py's online path passes tier="cheap"
    explicitly."""
    det_issues = deterministic_issues(output, picks_in_order, candidate_pool)
    try:
        llm_verdict = llm_judgment(input_payload, output, det_issues, tier=tier, trace=trace)
    except RuntimeError:
        # Same graceful-degradation shape as agent/narrative.py's fallback:
        # a flaky judgment call must not crash an otherwise-complete run.
        # Falling back to an empty, "approved" LLM-side verdict is safe
        # because it can only ADD issues on top of det_issues, never
        # remove one -- the line below still forces approved=False
        # whenever det_issues is non-empty, so a real deterministic
        # failure is never silently waved through by this fallback.
        llm_verdict = ReviewVerdict(approved=True, issues=[])

    seen = {(i.target, i.stage_to_fix, i.issue) for i in llm_verdict.issues}
    merged_issues = list(llm_verdict.issues) + [
        i for i in det_issues if (i.target, i.stage_to_fix, i.issue) not in seen
    ]
    llm_has_blocking = any(i.severity == "blocking" for i in llm_verdict.issues)
    approved = llm_verdict.approved and not det_issues and not llm_has_blocking
    return ReviewVerdict(approved=approved, issues=merged_issues)
