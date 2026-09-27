"""Pure tests for agent/progress.py -- the stage -> learner-facing phrase
mapping and detail formatting used by the UI's live progress panel."""
from __future__ import annotations

from agent.progress import STAGE_LABELS, format_progress


def test_known_stages_map_to_friendly_labels():
    assert format_progress("fetch") == "Fetching transcripts"
    assert format_progress("query_planning") == "Planning search queries"
    assert format_progress("understanding") == "Understanding videos"
    assert format_progress("narrative") == "Writing your plan"
    assert format_progress("review") == "Reviewing"
    assert format_progress("render") == "Rendering your plan"


def test_format_progress_appends_detail_when_present():
    assert format_progress("fetch", "25/80") == "Fetching transcripts — 25/80"
    assert format_progress("discovery", "5 queries") == "Searching YouTube — 5 queries"


def test_format_progress_empty_detail_matches_bare_label():
    assert format_progress("fetch", "") == format_progress("fetch")
    assert format_progress("gate") == STAGE_LABELS["gate"]


def test_format_progress_unknown_stage_falls_back_to_raw_name():
    assert format_progress("some_future_stage") == "some_future_stage"
    assert format_progress("some_future_stage", "x") == "some_future_stage — x"


def test_format_progress_empty_stage_reads_as_starting():
    assert format_progress("") == "Starting…"
    assert format_progress("", "5/80") == "Starting…"


def test_every_emitted_stage_has_a_label():
    # The stage names _log emits in agent/graph.py must all have a friendly
    # mapping -- a missing one would silently fall back to a raw snake_case
    # name in the learner's UI.
    emitted = {
        "gate", "decline", "query_planning", "discovery", "fetch", "understanding",
        "dedup", "score", "select", "narrative", "review", "render",
    }
    assert emitted <= set(STAGE_LABELS)