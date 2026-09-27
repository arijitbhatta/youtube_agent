"""Deterministic-only tests for agent/followup.py's run-summary assembly
(HLD.md §5.1) -- no network, no LLM call. The summary is the grounding the
phrasing model answers from; these tests pin down what is in it (goal, plan,
topics, outcomes, selection logic) and, crucially, that internal noise
(transcripts, accuracy flags, raw scores) is kept OUT of it. answer_question
itself makes one real call and is exercised live in the Phase 7 checkpoint,
not here.
"""
from __future__ import annotations

from agent.followup import _build_summary, _prompt_for
from agent.trace import Trace


def _cand(video_id, title=None, duration=10.0, channel="Some Channel"):
    return {
        "video_id": video_id,
        "title": title or f"Video {video_id}",
        "channel": channel,
        "url": f"https://youtube.com/watch?v={video_id}",
        "duration_minutes": duration,
    }


def _scored(video_id, topics=(), duration=10.0, title=None, **understanding):
    base = {
        "video_id": video_id,
        "covers_topics": list(topics),
        "primary_style": "project-based",
        "depth": "intro",
        "phase": "concept",
        "matches_unknown": list(topics),
        "overlaps_known": [],
        "constraint_violations": [],
        "evidence_snippet": "evidence",
        "grounded": True,
        "confidence": 0.8,
        "quality_signal": {"clarity": 0.8, "content_density": 0.8, "accuracy_flags": []},
    }
    base.update(understanding)
    return {
        "candidate": _cand(video_id, title=title, duration=duration),
        "understanding": base,
        "cluster_id": 0,
        "utility": 0.5,
    }


def _item(order, video_id, title, reason="because it fits"):
    return {
        "order": order,
        "video_id": video_id,
        "title": title,
        "url": f"https://youtube.com/watch?v={video_id}",
        "duration_minutes": 10.0,
        "reason": reason,
        "evidence_snippet": "x",
        "confidence": 0.8,
        "grounded": True,
    }


def _trace_with(inp=None, candidates=None, scored=None, output=None, scope=None):
    trace = Trace(persona_id="test")
    if inp is not None:
        trace.set_input(inp, enable_reviewer=True)
    if candidates:
        trace.add_candidates(candidates)
    if scored:
        trace.set_scored_candidates(scored)
    if output is not None:
        trace.set_output(output)
    if scope is not None:
        trace.set_scope_check(scope)
    return trace


_INPUT = {
    "persona_id": "test",
    "goal": "learn machine learning",
    "time_budget_minutes": 120,
    "user_context": {
        "background": "college student",
        "known": ["math"],
        "unknown": ["ML algorithms", "python"],
        "constraints": "practical",
    },
}


def test_summary_includes_goal_budget_and_learner_context():
    s = _build_summary(_trace_with(inp=_INPUT))
    assert "learn machine learning" in s
    assert "Time budget: 120 minutes" in s
    assert "Background: college student" in s
    assert "Already knows: math" in s
    assert "Wants to learn: ML algorithms, python" in s
    assert "Preferences: practical" in s


def test_summary_includes_ordered_curriculum():
    out = {
        "curriculum": [_item(1, "aaa", "First Video"), _item(2, "bbb", "Second Video")],
        "total_minutes": 20.0,
        "considered_and_dropped": [],
    }
    s = _build_summary(_trace_with(inp=_INPUT, output=out))
    assert "YOUR PLAN (in order, about 20 minutes)" in s
    assert "1. First Video [ID: aaa] (~10 min) -- because it fits" in s
    assert "2. Second Video" in s
    assert s.index("1. First Video") < s.index("2. Second Video")


def test_summary_includes_dropped_reason():
    out = {
        "curriculum": [],
        "considered_and_dropped": [
            {"video_id": "aaa", "title": "Dup Video", "reason_dropped": "superseded by a better one"}
        ],
    }
    s = _build_summary(_trace_with(
        inp=_INPUT, candidates=[_cand("aaa", "Dup Video")], output=out
    ))
    assert "not chosen: superseded by a better one" in s


def test_summary_lists_topics_for_videos():
    cands = [_cand("aaa", "Gradient Vid"), _cand("bbb", "Python Vid")]
    scored = [_scored("aaa", topics=["Gradient Descent"], title="Gradient Vid")]
    s = _build_summary(_trace_with(inp=_INPUT, candidates=cands, scored=scored))
    assert "covers: Gradient Descent" in s


def test_summary_marks_unscored_candidate_without_topics():
    cands = [_cand("aaa", "Discovered Only")]
    s = _build_summary(_trace_with(inp=_INPUT, candidates=cands, output={"curriculum": []}))
    assert "Discovered Only" in s
    assert "not chosen: no analysis recorded" in s
    assert "covers:" not in s


def test_summary_includes_video_ids_for_matching():
    out = {"curriculum": [_item(3, "aaa", "Picked Video")], "considered_and_dropped": []}
    s = _build_summary(_trace_with(
        inp=_INPUT, candidates=[_cand("aaa", "Picked Video")], output=out
    ))
    assert "[ID: aaa]" in s


def test_summary_marks_plan_membership():
    out = {"curriculum": [_item(3, "aaa", "Picked Video")], "considered_and_dropped": []}
    s = _build_summary(_trace_with(
        inp=_INPUT, candidates=[_cand("aaa", "Picked Video")], output=out
    ))
    assert "in your plan (#3)" in s


def test_summary_includes_how_chosen_logic():
    s = _build_summary(_trace_with(inp=_INPUT, output={"curriculum": []}))
    assert "HOW YOUR PLAN WAS CHOSEN" in s
    assert "value-per-minute" in s


def test_summary_hides_internal_noise():
    noisy_scored = _scored(
        "aaa",
        title="Metadata Only",
        grounded=False,
        evidence_snippet="(no transcript available -- metadata only)",
        quality_signal={
            "clarity": 0.5,
            "content_density": 0.5,
            "accuracy_flags": ["no_transcript_available_inferred_from_metadata_only"],
        },
    )
    out = {
        "curriculum": [],
        "considered_and_dropped": [],
        "warnings": ["no_transcript_available", "selected without a grounded transcript"],
    }
    cand = _cand("aaa", "Metadata Only")
    cand["transcript_available"] = False
    cand["transcript"] = "a raw transcript that must never surface"
    s = _build_summary(_trace_with(inp=_INPUT, candidates=[cand], scored=[noisy_scored], output=out))
    lowered = s.lower()
    assert "transcript" not in lowered
    assert "accuracy_flags" not in lowered
    assert "no_transcript" not in lowered
    assert "evidence" not in lowered


def test_summary_out_of_scope_declined():
    scope = {"in_scope": False, "reason": "not a genuine learning objective"}
    s = _build_summary(_trace_with(inp=_INPUT, scope=scope))
    assert "DECLINED AS OUT OF SCOPE" in s
    assert "not a genuine learning objective" in s
    assert "YOUR PLAN" not in s


def test_prompt_for_wraps_question_and_summary():
    trace = _trace_with(inp=_INPUT, output={"curriculum": []})
    prompt = _prompt_for(trace, "which video covers python?")
    assert "which video covers python?" in prompt
    assert "RUN SUMMARY" in prompt
    assert "learn machine learning" in prompt