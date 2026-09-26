"""Pipeline wiring (HLD.md §1) -- LangGraph as control-flow only, no
LangServe, still one process. Every node body is a plain function imported
from another `agent/` module; no stage's actual logic lives here.

Two conditional edges (HLD §3.9/§6.6), both driven by explicit, checked
graph-state counters/flags rather than LangGraph defaults:
  - after NARR -> render_draft: `enable_reviewer` (state, defaults from
    config.ENABLE_REVIEWER_DEFAULT) picks REVIEW vs. OUT directly -- when
    false, REVIEW never runs: zero extra calls, zero extra latency.
  - after REVIEW: `review_iterations` (state, capped at
    review.MAX_REVIEW_ITERATIONS) plus the verdict route to SEL, NARR, or
    OUT (agent/review.py's route_after_review -- pure, unit-tested).
"""
from __future__ import annotations

import concurrent.futures
import sys
from typing import TypedDict

from langgraph.graph import END, StateGraph

from agent import (
    dedup,
    discovery,
    narrative,
    query_planning,
    render,
    review,
    scope_check,
    scoring,
    selection,
)
from agent.config import ENABLE_REVIEWER_DEFAULT, FETCH_MAX_WORKERS
from agent.schemas import (
    Candidate,
    CurriculumOutput,
    NarrativeDraft,
    OutOfScopeOutput,
    PersonaInput,
    ReviewSummary,
    ReviewVerdict,
    ScopeCheckResult,
    ScoredCandidate,
    UnderstandingRecord,
)
from agent.trace import Trace
from agent.transcripts import fetch_metadata_and_transcript
from agent.understanding import understand_candidates


class PipelineState(TypedDict, total=False):
    input: PersonaInput
    trace: Trace
    enable_reviewer: bool
    scope_result: ScopeCheckResult
    queries: list[str]
    candidates: list[Candidate]
    understanding: list[UnderstandingRecord]
    cluster_ids: dict[str, int]
    scored: list[ScoredCandidate]
    selected: list[ScoredCandidate]
    dropped: list[ScoredCandidate]
    selection_warnings: list[str]
    excluded_ids: set[str]
    narrative_draft: NarrativeDraft
    draft_output: CurriculumOutput
    review_verdict: ReviewVerdict
    review_iterations: int
    review_summary: ReviewSummary
    output: CurriculumOutput | OutOfScopeOutput


def _log(stage: str, detail: str = "") -> None:
    # Stages here are sequential network/LLM-bound calls that can each take
    # a while (yt-dlp metadata fetches especially) -- a completely silent
    # multi-minute run is indistinguishable from a hang, so each node
    # announces itself on stderr rather than only writing to the trace,
    # which isn't readable until the whole run finishes.
    print(f"[graph] {stage}{': ' + detail if detail else ''}", file=sys.stderr, flush=True)


def _gate(state: PipelineState) -> dict:
    _log("gate")
    result = scope_check.check_scope(state["input"], trace=state["trace"])
    state["trace"].set_scope_check(result.model_dump())
    return {"scope_result": result}


def _route_gate(state: PipelineState) -> str:
    return "qp" if state["scope_result"].in_scope else "decline"


def _decline(state: PipelineState) -> dict:
    _log("decline")
    message = scope_check.decline_message(state["scope_result"])
    output = OutOfScopeOutput(message=message)
    state["trace"].set_output(output.model_dump())
    return {"output": output}


def _qp(state: PipelineState) -> dict:
    _log("query_planning")
    queries = query_planning.plan_queries(state["input"], trace=state["trace"])
    state["trace"].set_queries(queries)
    return {"queries": queries}


def _disc(state: PipelineState) -> dict:
    _log("discovery", f"{len(state['queries'])} queries")
    candidates = discovery.discover_candidates(
        state["queries"], state["input"].time_budget_minutes
    )
    _log("discovery", f"{len(candidates)} candidates found")
    return {"candidates": candidates}


def _fetch(state: PipelineState) -> dict:
    # Each candidate's yt-dlp extract_info + caption-CDN fetch is I/O-bound
    # (network wait dominates), so a bounded thread pool turns 80 sequential
    # round-trips into ~80/FETCH_MAX_WORKERS -- was the single slowest stage
    # in a live run before this. Results are written back by original index
    # so downstream stages see the same candidate order regardless of which
    # request happened to finish first.
    total = len(state["candidates"])
    _log("fetch", f"fetching metadata/transcripts for {total} candidates")
    results: list = [None] * total
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_MAX_WORKERS) as pool:
        future_to_index = {
            pool.submit(fetch_metadata_and_transcript, c): i
            for i, c in enumerate(state["candidates"])
        }
        for future in concurrent.futures.as_completed(future_to_index):
            results[future_to_index[future]] = future.result()
            completed += 1
            if completed % 5 == 0 or completed == total:
                _log("fetch", f"{completed}/{total}")
    candidates = [c for c in results if c is not None]
    _log("fetch", f"{len(candidates)}/{total} survived extraction")
    state["trace"].add_candidates([c.model_dump() for c in candidates])
    return {"candidates": candidates}


def _und(state: PipelineState) -> dict:
    _log("understanding", f"{len(state['candidates'])} candidates")
    records = understand_candidates(state["candidates"], state["input"], trace=state["trace"])
    state["trace"].add_understanding([r.model_dump() for r in records])
    return {"understanding": records}


def _dedup(state: PipelineState) -> dict:
    _log("dedup")
    cluster_ids = dedup.cluster_candidates(state["understanding"])
    state["trace"].set_dedup_clusters(cluster_ids)
    return {"cluster_ids": cluster_ids}


def _score(state: PipelineState) -> dict:
    _log("score")
    records = state["understanding"]
    cluster_ids = state["cluster_ids"]
    utility_by_id = scoring.score_candidates(
        records, state["input"].user_context.unknown, cluster_ids
    )
    candidates_by_id = {c.video_id: c for c in state["candidates"]}
    scored = [
        ScoredCandidate(
            candidate=candidates_by_id[r.video_id],
            understanding=r,
            cluster_id=cluster_ids[r.video_id],
            utility=utility_by_id[r.video_id],
        )
        for r in records
        if r.video_id in candidates_by_id
    ]
    state["trace"].set_scored_candidates([sc.model_dump() for sc in scored])
    return {"scored": scored}


def _sel(state: PipelineState) -> dict:
    _log("select")
    excluded = review.excluded_ids_after(state.get("review_verdict"), state.get("excluded_ids", set()))
    scored = state["scored"]
    if excluded:
        _log("select", f"excluding {len(excluded)} candidate(s) flagged by review")
        scored = [sc for sc in scored if sc.candidate.video_id not in excluded]
    selected, dropped, warnings = selection.select_and_sequence(
        scored, state["input"].time_budget_minutes, state["input"].user_context.unknown
    )
    state["trace"].add_selection_step(
        {
            "selected": [sc.candidate.video_id for sc in selected],
            "dropped": [sc.candidate.video_id for sc in dropped],
            "warnings": warnings,
            "excluded_by_review": sorted(excluded),
        }
    )
    return {"selected": selected, "dropped": dropped, "selection_warnings": warnings, "excluded_ids": excluded}


def _narr(state: PipelineState) -> dict:
    _log("narrative", f"{len(state['selected'])} selected")
    notable = narrative.notable_dropped_for(state["selected"], state["dropped"])
    feedback = review.narrative_feedback(state.get("review_verdict"))
    draft = narrative.generate_narrative(
        state["input"], state["selected"], notable, feedback=feedback, trace=state["trace"]
    )
    return {"narrative_draft": draft}


def _render_draft(state: PipelineState) -> dict:
    # Deterministic, no LLM call -- safe to (re)run on every pass through
    # the loop. Feeds REVIEW a real CurriculumOutput to run §6.1's checks
    # against; the final render in _out replaces this draft's placeholder
    # ReviewSummary with the loop's actual outcome.
    placeholder = ReviewSummary(iterations=0, approved=True, unresolved_blocking_issues=[])
    draft = render.render_output(
        state["input"],
        state["selected"],
        state["dropped"],
        state["selection_warnings"],
        state["narrative_draft"],
        placeholder,
    )
    return {"draft_output": draft}


def _route_after_render_draft(state: PipelineState) -> str:
    return "review" if state.get("enable_reviewer", ENABLE_REVIEWER_DEFAULT) else "out"


def _review(state: PipelineState) -> dict:
    iteration = state.get("review_iterations", 0) + 1
    _log("review", f"iteration {iteration}")
    verdict = review.run_review(
        state["input"], state["draft_output"], state["selected"], state["scored"], trace=state["trace"]
    )
    state["trace"].add_review_iteration({"iteration": iteration, "verdict": verdict.model_dump()})
    return {"review_verdict": verdict, "review_iterations": iteration}


def _route_review(state: PipelineState) -> str:
    return review.route_after_review(state["review_verdict"], state["review_iterations"])


def _out(state: PipelineState) -> dict:
    _log("render")
    verdict = state.get("review_verdict")
    iterations = state.get("review_iterations", 0)
    if verdict is None:
        review_summary = ReviewSummary(iterations=0, approved=True, unresolved_blocking_issues=[])
    else:
        unresolved = [] if verdict.approved else [i for i in verdict.issues if i.severity == "blocking"]
        review_summary = ReviewSummary(
            iterations=iterations, approved=verdict.approved, unresolved_blocking_issues=unresolved
        )
    output = render.render_output(
        state["input"],
        state["selected"],
        state["dropped"],
        state["selection_warnings"],
        state["narrative_draft"],
        review_summary,
    )
    state["trace"].set_output(output.model_dump())
    return {"output": output, "review_summary": review_summary}


def build_graph():
    graph = StateGraph(PipelineState)
    graph.add_node("gate", _gate)
    graph.add_node("decline", _decline)
    graph.add_node("qp", _qp)
    graph.add_node("disc", _disc)
    graph.add_node("fetch", _fetch)
    graph.add_node("und", _und)
    graph.add_node("dedup", _dedup)
    graph.add_node("score", _score)
    graph.add_node("sel", _sel)
    graph.add_node("narr", _narr)
    graph.add_node("render_draft", _render_draft)
    graph.add_node("review", _review)
    graph.add_node("out", _out)

    graph.set_entry_point("gate")
    graph.add_conditional_edges("gate", _route_gate, {"qp": "qp", "decline": "decline"})
    graph.add_edge("decline", END)
    graph.add_edge("qp", "disc")
    graph.add_edge("disc", "fetch")
    graph.add_edge("fetch", "und")
    graph.add_edge("und", "dedup")
    graph.add_edge("dedup", "score")
    graph.add_edge("score", "sel")
    graph.add_edge("sel", "narr")
    graph.add_edge("narr", "render_draft")
    graph.add_conditional_edges(
        "render_draft", _route_after_render_draft, {"review": "review", "out": "out"}
    )
    graph.add_conditional_edges(
        "review", _route_review, {"sel": "sel", "narr": "narr", "out": "out"}
    )
    graph.add_edge("out", END)
    return graph.compile()


_compiled = None


def _get_compiled():
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


def run_pipeline(
    input_payload: PersonaInput, *, enable_reviewer: bool = ENABLE_REVIEWER_DEFAULT
) -> tuple[CurriculumOutput | OutOfScopeOutput, Trace]:
    trace = Trace(persona_id=input_payload.persona_id)
    trace.set_input(input_payload.model_dump(), enable_reviewer=enable_reviewer)
    initial_state: PipelineState = {
        "input": input_payload,
        "trace": trace,
        "enable_reviewer": enable_reviewer,
    }
    # Default recursion_limit (25) comfortably covers the worst case (3
    # review_iterations x the sel->narr->render_draft->review loop body,
    # ~20 node visits total) -- the actual cap enforced is
    # review.MAX_REVIEW_ITERATIONS via the explicit counter above, not
    # this framework default (CLAUDE.md principle #5).
    final_state = _get_compiled().invoke(initial_state)
    return final_state["output"], trace
