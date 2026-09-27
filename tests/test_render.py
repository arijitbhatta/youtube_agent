"""Lean learner-facing render (render_markdown_lean): keeps title/link/why and
the budget summary, drops every internal surface -- warnings, confidence,
grounded, evidence, review, goal coverage, and the ranked dropped list."""
from __future__ import annotations

from agent.render import render_markdown_lean
from agent.schemas import CurriculumItem, CurriculumOutput, ReviewSummary


def _item(order: int, vid: str, title: str, dur: float, why: str) -> CurriculumItem:
    return CurriculumItem(
        order=order,
        video_id=vid,
        title=title,
        url=f"https://youtube.com/watch?v={vid}",
        duration_minutes=dur,
        reason=why,
        evidence_snippet="verbatim quote",
        confidence=0.91,
        grounded=False,
    )


def _output(total_minutes: float = 45.0) -> CurriculumOutput:
    return CurriculumOutput(
        persona_id="probe",
        budget_minutes=120,
        total_minutes=total_minutes,
        curriculum=[
            _item(1, "a", "Git in One Hour", 25.0, "Covers git init and first commits."),
            _item(2, "b", "Branches and Merging", 20.0, "Covers branches and merging."),
        ],
        review=ReviewSummary(iterations=3, approved=False, unresolved_blocking_issues=[]),
        considered_and_dropped=[],
        warnings=["b: selected without a grounded transcript"],
        goal_coverage={"git init and first commits": "covered"},
    )


def test_lean_keeps_learner_essentials():
    md = render_markdown_lean(_output())
    assert "Git in One Hour" in md
    assert "https://youtube.com/watch?v=a" in md
    assert "Covers git init and first commits." in md
    assert "of 120 min" in md
    assert "[Watch on YouTube]" in md


def test_lean_omits_internal_surfaces():
    md = render_markdown_lean(_output())
    for token in (
        "Confidence",
        "Grounded",
        "Warnings",
        "Review",
        "Goal coverage",
        "considered and dropped",
        "evidence",
    ):
        assert token not in md


def test_lean_over_budget_note_only_when_over():
    assert "over your budget" not in render_markdown_lean(_output(total_minutes=45.0))
    assert "over your budget" in render_markdown_lean(_output(total_minutes=130.0))