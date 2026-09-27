"""Follow-up Q&A over a finished run's trace (HLD.md §5.1). Not a second
retrieval or judgment pass -- a read path over data the pipeline already
computed. The whole answer is grounded in one assembled "run summary": a
learner-facing digest of the goal, the ordered plan, the selection logic,
and every video the run looked at (with topics and outcome). The summary is
assembled deterministically from the trace and *cleansed* of internal
details (transcripts, accuracy flags, numeric scores) so the phrasing model
is never handed — and therefore never parrots — anything the learner
shouldn't see.

`ui/app.py` (HLD §5.2) wraps `answer_question_stream` directly as its chat
layer -- no new pipeline logic there either.
"""
from __future__ import annotations

from typing import Iterator

from agent.llm_client import call_structured, stream_text
from agent.schemas import FollowupAnswer
from agent.trace import Trace

# Plain-language digest of the deterministic selection logic (agent/scoring.py
# + agent/selection.py), so "how was my plan chosen?" is answerable without
# the model inventing a rule. This is static system knowledge about the
# pipeline's own fixed algorithm, not per-run data.
_HOW_CHOSEN = (
    "Videos matching the goal and the topics the learner wants to learn are "
    "searched for, and what each video actually covers is read. Each video is "
    "scored for how well it matches the topics the learner wants to learn, "
    "with novelty counted so it isn't just re-covering what the learner "
    "already knows; a video is penalized for going over things the learner "
    "already knows or for violating a stated constraint, and adjusted for "
    "clarity and how much it actually teaches. Near-duplicate videos are "
    "grouped, and only the strongest of each group is kept. Videos are then "
    "picked greedily in order of value-per-minute of the learner's time until "
    "the time budget is spent; if one of the learner's target topics is still "
    "uncovered after that, the weakest pick is swapped for the strongest video "
    "covering the missed topic. Finally the chosen videos are ordered so the "
    "learning flows from setup through core concepts to hands-on practice and "
    "advanced follow-ups."
)

_SYSTEM = """You are a friendly, warm assistant explaining a learner's \
finished video-learning plan. Answer ONLY from the "RUN SUMMARY" you are \
given -- never invent a video, topic, reason, score, or outcome that isn't \
there. Never mention transcripts, metadata, accuracy flags, or numeric \
scores; speak in plain, learner-friendly terms and name the actual video \
title when one is relevant. If the summary doesn't contain what the learner \
asked about, say so in one brief, polite sentence and offer the closest \
thing you *can* answer from the summary. Keep answers short, specific, and \
grounded."""

_PROMPT_TEMPLATE = """Learner's question: {question}

RUN SUMMARY
{context_block}

Answer the learner's question briefly and specifically, using only the \
information in the summary above.
"""


def _rounded_minutes(minutes: float) -> int:
    """Round a duration to a whole minute, half-up (17.6 -> 18, 2.5 -> 3)."""
    return int(float(minutes) + 0.5)


def _build_summary(trace: Trace) -> str:
    """Assemble the whole-run, learner-facing digest the phrasing model is
    grounded in. Pulls from `trace.data` and drops anything internal: no
    transcripts, evidence snippets, accuracy flags, numeric utility/confidence,
    or the pipeline's `warnings` (which spell out transcript availability)."""
    data = trace.data
    inp = data.get("input") or {}
    uc = inp.get("user_context") or {}
    out = data.get("output") or {}
    scope = data.get("scope_check") or {}

    lines: list[str] = []

    # -- goal & learner (always -- it frames every other answer) ------------
    goal = str(inp.get("goal") or "").strip() or "(no goal recorded)"
    lines.append("Learner's goal: " + goal)
    budget = inp.get("time_budget_minutes")
    if budget is not None:
        lines.append(f"Time budget: {budget} minutes")
    if (uc.get("background") or "").strip():
        lines.append("Background: " + str(uc["background"]).strip())
    known = uc.get("known") or []
    if known:
        lines.append("Already knows: " + ", ".join(known))
    unknown = uc.get("unknown") or []
    if unknown:
        lines.append("Wants to learn: " + ", ".join(unknown))
    if (uc.get("constraints") or "").strip():
        lines.append("Preferences: " + str(uc["constraints"]).strip())

    # -- out-of-scope runs declined at the gate: answer "why no plan?" -------
    if scope and scope.get("in_scope") is False:
        lines.append("")
        lines.append("THIS RUN WAS DECLINED AS OUT OF SCOPE")
        if scope.get("reason"):
            lines.append(str(scope["reason"]))
        return "\n".join(lines)

    # -- ordered plan --------------------------------------------------------
    curriculum = out.get("curriculum") or []
    if curriculum:
        total = out.get("total_minutes")
        total_part = f", about {int(float(total))} minutes" if total is not None else ""
        lines.append("")
        lines.append(f"YOUR PLAN (in order{total_part})")
        for item in curriculum:
            vid = item.get("video_id")
            title = item.get("title") or "(untitled)"
            dur = item.get("duration_minutes")
            dur_part = f" (~{_rounded_minutes(dur)} min)" if dur is not None else ""
            reason = item.get("reason") or ""
            extra = f" -- {reason}" if reason else ""
            id_part = f" [ID: {vid}]" if vid else ""
            lines.append(f"{item.get('order', '?')}. {title}{id_part}{dur_part}{extra}")
    else:
        lines.append("")
        lines.append("This run did not produce a plan.")

    # -- the selection logic (static) ----------------------------------------
    lines.append("")
    lines.append("HOW YOUR PLAN WAS CHOSEN")
    lines.append(_HOW_CHOSEN)

    # -- every video this run looked at (the "whole pool") --------------------
    scored = data.get("scored_candidates") or []
    understanding_by_id: dict[str, dict] = {}
    for sc in scored:
        candidate = sc.get("candidate") or {}
        vid = candidate.get("video_id")
        if vid is not None:
            understanding_by_id[vid] = sc.get("understanding") or {}

    order_by_id = {item["video_id"]: item.get("order") for item in curriculum}
    dropped_by_id = {d.get("video_id"): d for d in out.get("considered_and_dropped") or []}

    candidates = data.get("candidates") or []
    if candidates:
        lines.append("")
        lines.append(
            "EVERY VIDEO THIS RUN LOOKED AT "
            "(topics are listed only where this run established what the video teaches)"
        )
        for c in candidates:
            vid = c.get("video_id")
            title = c.get("title") or "(untitled)"
            dur = c.get("duration_minutes")
            channel = c.get("channel") or ""
            where = f"~{_rounded_minutes(dur)} min" if dur is not None else ""
            where = f"{where}, {channel}" if where and channel else (where or channel)
            id_part = f" [ID: {vid}]" if vid else ""
            header = f"- {title}{id_part}" + (f" ({where})" if where else "")

            topics = understanding_by_id.get(vid, {}).get("covers_topics") or []
            if vid in order_by_id:
                outcome = f"in your plan (#{order_by_id[vid]})"
            elif vid in dropped_by_id:
                reason = (dropped_by_id[vid].get("reason_dropped") or "").strip()
                outcome = f"not chosen: {reason}" if reason else "not chosen"
            else:
                outcome = "not chosen: no analysis recorded"

            pieces = [header]
            if topics:
                pieces.append("covers: " + ", ".join(topics))
            pieces.append(outcome)
            lines.append(" — ".join(pieces))

    # -- goal coverage ---------------------------------------------------------
    coverage = out.get("goal_coverage") or {}
    if coverage:
        lines.append("")
        lines.append("GOAL COVERAGE")
        for topic, status in coverage.items():
            lines.append(f"- {topic}: {status}")

    # -- self-review ------------------------------------------------------------
    review = out.get("review") or {}
    iterations = review.get("iterations", 0) or 0
    lines.append("")
    lines.append("SELF-REVIEW")
    if iterations:
        approved = review.get("approved")
        status = "approved" if approved else "not approved"
        lines.append(f"the plan went through {iterations} review round(s) and was {status}")
        for issue in review.get("unresolved_blocking_issues") or []:
            if issue.get("issue"):
                lines.append(f"- {issue['issue']}")
    else:
        lines.append("no separate self-review pass was run on this plan")

    return "\n".join(lines)


def _prompt_for(trace: Trace, question: str) -> str:
    """Wrap the learner's question with the whole-run summary. Unlike the
    earlier single-video record, this hands the model the full pool so it can
    answer "which video covers X", "why is Y in my plan", or "what was the
    selection logic" from one grounded digest."""
    return _PROMPT_TEMPLATE.format(question=question, context_block=_build_summary(trace))


def answer_question(trace: Trace, question: str) -> str:
    """Non-streamed entrypoint: one schema-validated call returning the full
    answer string. ui/app.py now uses answer_question_stream for the typewriter
    effect; this stays as the plain-return API (forced tool call, so the answer
    is a guaranteed single string with no preamble)."""
    result = call_structured(
        _prompt_for(trace, question),
        FollowupAnswer,
        tier="strong",
        system=_SYSTEM,
        max_tokens=1024,
        trace=trace,
        trace_label="followup",
    )
    return result.answer


def answer_question_stream(trace: Trace, question: str) -> Iterator[str]:
    """Streamed chat entrypoint (ui/app.py): builds the same whole-run summary
    as answer_question, then yields the answer token-by-token from a plain-text
    completion so the UI can paint it as it arrives. Same `_SYSTEM` grounding
    prompt; logs the call into `trace` on completion."""
    yield from stream_text(
        _prompt_for(trace, question),
        system=_SYSTEM,
        tier="strong",
        max_tokens=1024,
        trace=trace,
        trace_label="followup",
    )