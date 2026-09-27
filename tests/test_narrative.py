"""Network-free tests for agent/narrative.py's incremental-regeneration
logic (HLD.md §3.9 follow-up) -- monkeypatches call_structured so any
unexpected LLM call fails the test loudly, rather than being invisible."""
from __future__ import annotations

import pytest

from agent import narrative
from agent.schemas import (
    NarrativeDraft,
    NarrativeDroppedItem,
    NarrativeItem,
    PersonaInput,
    ReviewIssue,
    UserContext,
)
from tests.factories import make_scored


def _persona() -> PersonaInput:
    return PersonaInput(
        persona_id="p",
        goal="learn things",
        time_budget_minutes=60,
        user_context=UserContext(background="x", known=[], unknown=["hooks"], constraints=""),
    )


def _no_llm_calls_allowed(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("call_structured should not have been called")

    monkeypatch.setattr(narrative, "call_structured", _boom)


def test_fully_reusable_pass_makes_no_llm_call(monkeypatch):
    _no_llm_calls_allowed(monkeypatch)
    sc1 = make_scored("vid00000001", 10, 0.9, cluster_id=1)
    sc2 = make_scored("vid00000002", 10, 0.8, cluster_id=2)
    previous_items = [
        NarrativeItem(video_id="vid00000001", reason="r1"),
        NarrativeItem(video_id="vid00000002", reason="r2"),
    ]

    draft = narrative.generate_narrative(
        _persona(),
        [sc1, sc2],
        [],
        previous_items=previous_items,
        previous_dropped=[],
    )

    assert {i.video_id: i.reason for i in draft.items} == {
        "vid00000001": "r1",
        "vid00000002": "r2",
    }


def test_new_pick_only_generates_for_the_new_subset(monkeypatch):
    sc_old = make_scored("vid00000001", 10, 0.9, cluster_id=1)
    sc_new = make_scored("vid00000002", 10, 0.8, cluster_id=2)
    previous_items = [NarrativeItem(video_id="vid00000001", reason="old reason")]

    seen_prompts = []

    def _fake_call_structured(prompt, schema, **kwargs):
        seen_prompts.append(prompt)
        return NarrativeDraft(items=[NarrativeItem(video_id="vid00000002", reason="new reason")], dropped=[])

    monkeypatch.setattr(narrative, "call_structured", _fake_call_structured)

    draft = narrative.generate_narrative(
        _persona(),
        [sc_old, sc_new],
        [],
        previous_items=previous_items,
        previous_dropped=[],
    )

    assert len(seen_prompts) == 1
    assert "vid00000001" not in seen_prompts[0]  # only the new pick's block went to the LLM
    assert {i.video_id: i.reason for i in draft.items} == {
        "vid00000001": "old reason",
        "vid00000002": "new reason",
    }


def test_feedback_targeted_pick_is_not_reused_even_if_unchanged(monkeypatch):
    sc = make_scored("vid00000001", 10, 0.9, cluster_id=1)
    previous_items = [NarrativeItem(video_id="vid00000001", reason="stale reason")]
    feedback = [
        ReviewIssue(
            severity="blocking",
            stage_to_fix="narrative",
            target="vid00000001",
            issue="reason doesn't cite evidence",
            suggested_fix="cite the evidence snippet",
        )
    ]

    def _fake_call_structured(prompt, schema, **kwargs):
        return NarrativeDraft(items=[NarrativeItem(video_id="vid00000001", reason="fixed reason")], dropped=[])

    monkeypatch.setattr(narrative, "call_structured", _fake_call_structured)

    draft = narrative.generate_narrative(
        _persona(),
        [sc],
        [],
        feedback=feedback,
        previous_items=previous_items,
        previous_dropped=[],
    )

    assert draft.items[0].reason == "fixed reason"


def test_no_previous_draft_generates_everything(monkeypatch):
    sc = make_scored("vid00000001", 10, 0.9, cluster_id=1)
    called = []

    def _fake_call_structured(prompt, schema, **kwargs):
        called.append(True)
        return NarrativeDraft(items=[NarrativeItem(video_id="vid00000001", reason="fresh")], dropped=[])

    monkeypatch.setattr(narrative, "call_structured", _fake_call_structured)

    draft = narrative.generate_narrative(_persona(), [sc], [])

    assert called == [True]
    assert draft.items[0].reason == "fresh"


def test_reused_dropped_items_pass_through(monkeypatch):
    _no_llm_calls_allowed(monkeypatch)
    sc_selected = make_scored("vid00000001", 10, 0.9, cluster_id=1)
    sc_dropped = make_scored("vid00000002", 10, 0.5, cluster_id=1)
    previous_items = [NarrativeItem(video_id="vid00000001", reason="r1")]
    previous_dropped = [NarrativeDroppedItem(video_id="vid00000002", reason_dropped="superseded")]

    draft = narrative.generate_narrative(
        _persona(),
        [sc_selected],
        [(sc_dropped, "vid00000001")],
        previous_items=previous_items,
        previous_dropped=previous_dropped,
    )

    assert draft.dropped[0].video_id == "vid00000002"
    assert draft.dropped[0].reason_dropped == "superseded"
