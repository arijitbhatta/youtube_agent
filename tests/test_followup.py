"""Deterministic-only tests for agent/followup.py's video resolution and
record assembly (HLD.md §5.1) -- no network, no LLM call. answer_question
itself makes one real call and is exercised live in the Phase 7 checkpoint,
not here.
"""
from __future__ import annotations

from agent.followup import _record_for, resolve_video_id
from agent.trace import Trace
from tests.factories import make_candidate, make_scored


def _trace_with(candidates, scored=None, cluster_ids=None, output=None, selection_steps=None, review_iterations=None):
    trace = Trace(persona_id="test")
    trace.add_candidates([c.model_dump() for c in candidates])
    if scored:
        trace.set_scored_candidates([sc.model_dump() for sc in scored])
    if cluster_ids:
        trace.set_dedup_clusters(cluster_ids)
    if output:
        trace.set_output(output)
    for step in selection_steps or []:
        trace.add_selection_step(step)
    for it in review_iterations or []:
        trace.add_review_iteration(it)
    return trace


def test_resolve_exact_video_id_in_question():
    trace = _trace_with([make_candidate("abc12345678", 10, title="Learn React Hooks")])
    assert resolve_video_id(trace, "why not abc12345678?") == "abc12345678"


def test_resolve_video_url_in_question():
    trace = _trace_with([make_candidate("abc12345678", 10, title="Learn React Hooks")])
    q = "why didn't you pick https://youtube.com/watch?v=abc12345678"
    assert resolve_video_id(trace, q) == "abc12345678"


def test_resolve_fuzzy_title_match():
    trace = _trace_with([
        make_candidate("aaaaaaaaaaa", 10, title="Complete React Hooks Crash Course"),
        make_candidate("bbbbbbbbbbb", 10, title="Docker for Beginners"),
    ])
    assert resolve_video_id(trace, "why wasn't the React Hooks Crash Course included?") == "aaaaaaaaaaa"


def test_resolve_returns_none_when_never_discovered():
    trace = _trace_with([make_candidate("aaaaaaaaaaa", 10, title="Docker for Beginners")])
    assert resolve_video_id(trace, "why not the video about quantum computing?") is None


def test_record_for_reports_cluster_mates_and_final_status():
    selected_sc = make_scored("selid1234aa", 10, 0.9, cluster_id=1)
    dropped_sc = make_scored("dropid1234a", 10, 0.5, cluster_id=1)
    trace = _trace_with(
        [selected_sc.candidate, dropped_sc.candidate],
        scored=[selected_sc, dropped_sc],
        cluster_ids={"selid1234aa": 1, "dropid1234a": 1},
        output={
            "curriculum": [{"video_id": "selid1234aa", "order": 1}],
            "considered_and_dropped": [
                {"video_id": "dropid1234a", "title": "x", "reason_dropped": "superseded"}
            ],
        },
    )
    record = _record_for(trace, "dropid1234a")
    assert record["cluster_mates"] == ["selid1234aa"]
    assert record["made_final_curriculum"] is None
    assert record["considered_and_dropped_as"]["reason_dropped"] == "superseded"
    assert record["excluded_by_review"] is False


def test_record_for_flags_excluded_by_review():
    sc = make_scored("revid12345a", 10, 0.5, cluster_id=1)
    trace = _trace_with(
        [sc.candidate],
        scored=[sc],
        cluster_ids={"revid12345a": 1},
        output={"curriculum": [], "considered_and_dropped": []},
        selection_steps=[{"selected": [], "dropped": ["revid12345a"], "warnings": [], "excluded_by_review": ["revid12345a"]}],
        review_iterations=[
            {"iteration": 1, "verdict": {"approved": False, "issues": [
                {"severity": "blocking", "stage_to_fix": "selection", "target": "revid12345a", "issue": "bad", "suggested_fix": "drop it"}
            ]}}
        ],
    )
    record = _record_for(trace, "revid12345a")
    assert record["excluded_by_review"] is True
    assert len(record["review_issues_naming_it"]) == 1
