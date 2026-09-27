#!/usr/bin/env python3
"""Eval harness (HLD.md §6, build order rows 11-12/14) -- runs every
`test_set/*.json` scenario through the real pipeline (real yt-dlp
discovery, real Claude calls, per this project's "full verification, never
mocked" rule), then grades each run with §6.1's automated metrics and
§6.2's LLM-judge (`eval/judge.py`, 3x for variance). `--compare-reviewer`
(§6.6) runs every scenario with the review loop on and off and diffs the
two -- specifically reporting whether the reviewer changed anything, not
just producing two scorecards side by side.

This is the harness itself, not a report generator disconnected from real
calls -- every number here comes from an actual completed run.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

from agent import dedup
from agent.config import OUTPUTS_DIR, REPO_ROOT, api_key_present
from agent.graph import run_pipeline
from agent.schemas import CurriculumOutput, OutOfScopeOutput, PersonaInput, ScoredCandidate
from agent.trace import Trace
from eval import judge, metrics

TEST_SET_DIR = REPO_ROOT / "test_set"
DEFAULT_REPORT_PATH = OUTPUTS_DIR / "eval_report.json"

# Cheap independent heuristic for "surface intro"-style content (§6.1) --
# cross-checked against the LLM's own constraint_violations, never trusted
# alone, since that would just be re-asking the same model that made the
# pick whether it made a good pick.
_SURFACE_INTRO_PATTERN = re.compile(
    r"in \d+ ?(seconds|minutes)|crash course|quick(start| intro)|100 seconds|in a nutshell",
    re.IGNORECASE,
)
_SURFACE_INTRO_MAX_MINUTES = 5.0
# NOT live-recalibrated for text-embedding-3-small (unlike dedup.py's
# SIMILARITY_THRESHOLD, which was): this compares a short topic phrase
# against a full transcript, not two similar-length summaries, and every
# attempt to gather real topic-vs-transcript similarity data hit the same
# persistent caption-CDN 429 already documented in README.md -- every
# candidate across every saved trace in outputs/ has an empty transcript.
# 0.30 is a reasoned placeholder (OpenAI's embedding space is known to run
# lower/flatter than sentence-transformers' for short-query-vs-passage
# comparisons; the old 0.35 was tuned for MiniLM and doesn't transfer
# either). Recalibrate the same way SIMILARITY_THRESHOLD was -- against
# real topic-vs-transcript pairs -- once transcripts are actually fetchable.
_EMBEDDING_COVERAGE_THRESHOLD = 0.30


def _load_scenarios(names: list[str] | None) -> list[tuple[str, PersonaInput]]:
    paths = sorted(TEST_SET_DIR.glob("*.json"))
    if names:
        wanted = set(names)
        paths = [p for p in paths if p.stem in wanted or p.name in wanted]
    return [(p.stem, PersonaInput.model_validate(json.loads(p.read_text()))) for p in paths]


def _scored_pool(trace: Trace) -> list[ScoredCandidate]:
    return [ScoredCandidate.model_validate(d) for d in trace.data.get("scored_candidates", [])]


def _picks_in_order(output: CurriculumOutput, pool: list[ScoredCandidate]) -> list[ScoredCandidate]:
    by_id = {sc.candidate.video_id: sc for sc in pool}
    return [by_id[item.video_id] for item in output.curriculum if item.video_id in by_id]


def _transcripts_by_id(trace: Trace) -> dict[str, str]:
    return {c["video_id"]: c["transcript"] for c in trace.data.get("candidates", []) if c.get("transcript")}


def _embedding_topic_coverage(
    unknown_topics: list[str], picks: list[ScoredCandidate], *, trace=None
) -> dict:
    """§6.1 method (b): embedding similarity between the topic string and a
    pick's transcript, computed independently of the LLM's own
    `matches_unknown` self-report (method (a), eval/metrics.py). Reports
    disagreement between the two rather than picking one as correct --
    both are proxies (HLD §6.4).

    Only picks with a real fetched transcript are usable here -- the
    hosted embeddings API (unlike the old local model) hard-rejects an
    empty string (400 "Input is empty") rather than silently returning a
    junk vector, so an empty transcript must be filtered out before the
    call, not passed through. When the persistent caption-CDN 429
    documented elsewhere in this project (README.md) has blocked every
    transcript fetch, no pick has real text and this returns
    fraction_covered=None rather than a fabricated number."""
    picks = [sc for sc in picks if sc.candidate.transcript]
    if not unknown_topics or not picks:
        return {"fraction_covered": None, "disagreements": []}
    transcripts = [sc.candidate.transcript for sc in picks]
    topic_emb = dedup.embed(unknown_topics, trace=trace, trace_label="eval_embedding_coverage")
    pick_emb = dedup.embed(transcripts, trace=trace, trace_label="eval_embedding_coverage")
    covered_flags = []
    disagreements = []
    for i, topic in enumerate(unknown_topics):
        best_sim = max(float(np.dot(topic_emb[i], pick_emb[j])) for j in range(len(picks)))
        embedding_covered = best_sim >= _EMBEDDING_COVERAGE_THRESHOLD
        llm_covered = any(topic in sc.understanding.matches_unknown for sc in picks)
        covered_flags.append(embedding_covered)
        if embedding_covered != llm_covered:
            disagreements.append(
                {
                    "topic": topic,
                    "embedding_says_covered": embedding_covered,
                    "llm_says_covered": llm_covered,
                    "best_similarity": round(best_sim, 3),
                }
            )
    return {
        "fraction_covered": sum(covered_flags) / len(covered_flags),
        "disagreements": disagreements,
    }


def _constraint_heuristic_flags(picks: list[ScoredCandidate]) -> list[str]:
    return [
        sc.candidate.video_id
        for sc in picks
        if _SURFACE_INTRO_PATTERN.search(sc.candidate.title)
        and sc.candidate.duration_minutes < _SURFACE_INTRO_MAX_MINUTES
    ]


def _cost_summary(calls: list[dict]) -> dict:
    return {
        "llm_call_count": len(calls),
        "total_input_tokens": sum(c["input_tokens"] for c in calls),
        "total_output_tokens": sum(c["output_tokens"] for c in calls),
        "total_latency_seconds": round(sum(c["latency_seconds"] for c in calls), 2),
    }


def run_scenario(
    name: str, input_payload: PersonaInput, *, enable_reviewer: bool, judge_runs: int
) -> dict:
    output, trace = run_pipeline(input_payload, enable_reviewer=enable_reviewer)
    trace_path = trace.save(OUTPUTS_DIR)
    pipeline_cost = _cost_summary(trace.data["llm_calls"])

    if isinstance(output, OutOfScopeOutput):
        return {
            "scenario": name,
            "enable_reviewer": enable_reviewer,
            "out_of_scope": True,
            "scope_message": output.message,
            "trace_path": str(trace_path),
            "pipeline_cost": pipeline_cost,
        }

    pool = _scored_pool(trace)
    picks = _picks_in_order(output, pool)
    transcripts = _transcripts_by_id(trace)

    # A separate, unsaved trace just to capture eval-time-only LLM/embedding
    # calls' own cost (the judge below, and the embedding cross-check just
    # above it) -- these must never be merged back into trace_path's saved
    # file (that file is the record of what running the agent once, for
    # real, actually cost).
    judge_trace = Trace(persona_id=name)

    report: dict = {
        "scenario": name,
        "enable_reviewer": enable_reviewer,
        "out_of_scope": False,
        "trace_path": str(trace_path),
        "pick_count": len(output.curriculum),
        "curriculum_video_ids": [item.video_id for item in output.curriculum],
        "budget_compliance": metrics.budget_compliance(output),
        "curriculum_shape_sanity": metrics.curriculum_shape_sanity(output, len(pool)),
        "unknown_topic_coverage_llm": metrics.unknown_topic_coverage(output),
        "unknown_topic_coverage_embedding": _embedding_topic_coverage(
            input_payload.user_context.unknown, picks, trace=judge_trace
        ),
        "known_topic_leakage": metrics.known_topic_leakage(picks),
        "constraint_compliance": metrics.constraint_compliance(picks),
        "constraint_heuristic_flags": _constraint_heuristic_flags(picks),
        "redundancy": metrics.redundancy(picks),
        "grounding_rate": metrics.grounding_rate(output, transcripts),
        "sequencing_sanity": metrics.sequencing_sanity(picks),
        "content_quality": metrics.content_quality(picks, pool),
        "review_summary": output.review.model_dump(),
    }

    judge_verdicts = judge.judge_output(
        input_payload, output, picks, pool, runs=judge_runs, trace=judge_trace
    )
    approved_count = sum(1 for v in judge_verdicts if v.approved)
    report["judge"] = {
        "runs": len(judge_verdicts),
        "approved_rate": approved_count / len(judge_verdicts) if judge_verdicts else None,
        "issue_counts": [len(v.issues) for v in judge_verdicts],
    }
    report["pipeline_cost"] = pipeline_cost
    report["judge_cost"] = _cost_summary(judge_trace.data["llm_calls"])
    return report


def _diff_reviewer_effect(with_reviewer: dict, without_reviewer: dict) -> dict:
    """§6.6's actual causal question: did the reviewer change the shipped
    output, or run, cost something, and approve the first draft unchanged?

    Known confound (live-observed, not just theoretical): each variant
    calls run_pipeline independently, and live discovery is NOT
    deterministic -- two independent calls for the identical persona were
    seen to return only ~48% overlapping candidates in one live check.
    HLD §8 assumes "nothing about discovery/understanding differs between
    the two variants," which holds for understanding (cached by
    video_id+persona, agent/cache.py) but not for discovery's candidate
    SET itself. A cleaner ablation would run discovery/understanding once
    and replay SEL->NARR->REVIEW->OUT twice from the same scored pool;
    that's a real second harness-only entrypoint duplicating graph.py's
    node sequencing outside LangGraph, judged out of scope for this
    build (see README) -- so a curriculum-set difference between the two
    arms below may be partly discovery noise, not purely the reviewer's
    effect. Report this caveat alongside the diff, don't hide it."""
    if with_reviewer.get("out_of_scope") or without_reviewer.get("out_of_scope"):
        return {"changed_output": False, "reason": "scenario is out-of-scope; reviewer never runs"}
    with_ids = with_reviewer["curriculum_video_ids"]
    without_ids = without_reviewer["curriculum_video_ids"]
    changed = with_ids != without_ids
    return {
        "changed_output": changed,
        "with_reviewer_picks": with_ids,
        "without_reviewer_picks": without_ids,
        "review_iterations": with_reviewer["review_summary"]["iterations"],
        "reviewer_extra_llm_calls": (
            with_reviewer["pipeline_cost"]["llm_call_count"]
            - without_reviewer["pipeline_cost"]["llm_call_count"]
        ),
        "judge_approved_rate_with": with_reviewer["judge"]["approved_rate"],
        "judge_approved_rate_without": without_reviewer["judge"]["approved_rate"],
    }


def _print_summary(reports: list[dict], compare: bool) -> None:
    for r in reports:
        if compare:
            with_r, without_r = r["with_reviewer"], r["without_reviewer"]
            print(f"\n=== {r['scenario']} (compare-reviewer) ===")
            if with_r.get("out_of_scope"):
                print("  out-of-scope scenario -- reviewer never runs either way")
                continue
            print(f"  picks: with={len(with_r['curriculum_video_ids'])} without={len(without_r['curriculum_video_ids'])}")
            print(f"  reviewer changed output: {r['reviewer_effect']['changed_output']}")
            print(
                f"  judge approved-rate: with={with_r['judge']['approved_rate']:.2f} "
                f"without={without_r['judge']['approved_rate']:.2f}"
            )
            print(f"  extra LLM calls from reviewer: {r['reviewer_effect']['reviewer_extra_llm_calls']}")
        else:
            print(f"\n=== {r['scenario']} ===")
            if r.get("out_of_scope"):
                print(f"  out-of-scope: {r['scope_message'][:80]}")
                continue
            print(f"  picks: {r['pick_count']}  budget_over: {r['budget_compliance']['over_budget']}")
            print(f"  shape_ok: {r['curriculum_shape_sanity']['passes']}  redundancy_ok: {r['redundancy']['passes']}")
            print(f"  grounding_rate: {r['grounding_rate']['rate']}  sequencing_ok: {r['sequencing_sanity']['passes']}")
            print(f"  review approved: {r['review_summary']['approved']} (iterations={r['review_summary']['iterations']})")
            print(f"  judge approved-rate: {r['judge']['approved_rate']}")
            print(f"  llm calls: pipeline={r['pipeline_cost']['llm_call_count']} judge={r['judge_cost']['llm_call_count']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the eval harness against test_set/ scenarios")
    parser.add_argument("--scenario", action="append", help="Limit to one scenario stem/filename (repeatable)")
    parser.add_argument("--compare-reviewer", action="store_true", help="Run every scenario with the review loop on and off, and diff (HLD §6.6)")
    parser.add_argument("--judge-runs", type=int, default=judge.JUDGE_RUNS, help="LLM-judge runs per scenario (default 3, HLD §6.2)")
    parser.add_argument("--out", type=Path, default=DEFAULT_REPORT_PATH, help="Where to write the JSON report")
    args = parser.parse_args()

    if not api_key_present():
        print("OPENROUTER_API_KEY is not set -- copy .env.example to .env and fill it in.", file=sys.stderr)
        return 1

    scenarios = _load_scenarios(args.scenario)
    if not scenarios:
        print("No matching scenarios found under test_set/.", file=sys.stderr)
        return 1

    reports: list[dict] = []
    for name, input_payload in scenarios:
        print(f"[eval] running {name}...", file=sys.stderr)
        if args.compare_reviewer:
            with_r = run_scenario(name, input_payload, enable_reviewer=True, judge_runs=args.judge_runs)
            without_r = run_scenario(name, input_payload, enable_reviewer=False, judge_runs=args.judge_runs)
            reports.append(
                {
                    "scenario": name,
                    "with_reviewer": with_r,
                    "without_reviewer": without_r,
                    "reviewer_effect": _diff_reviewer_effect(with_r, without_r),
                }
            )
        else:
            reports.append(run_scenario(name, input_payload, enable_reviewer=True, judge_runs=args.judge_runs))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"compare_reviewer": args.compare_reviewer, "reports": reports}, indent=2, default=str))
    _print_summary(reports, args.compare_reviewer)
    print(f"\n(full report written to {args.out})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
