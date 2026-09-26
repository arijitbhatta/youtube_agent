"""Instantiate every schema (HLD.md §2) with valid data. No network, no LLM
-- this only checks the contracts hold together as Python objects."""
from agent.schemas import (
    Candidate,
    CurriculumItem,
    CurriculumOutput,
    DroppedItem,
    NarrativeDraft,
    NarrativeDroppedItem,
    NarrativeItem,
    OutOfScopeOutput,
    PersonaInput,
    QualitySignal,
    ReviewIssue,
    ReviewSummary,
    ReviewVerdict,
    ScopeCheckResult,
    ScoredCandidate,
    UnderstandingRecord,
    UserContext,
)


def _make_input() -> PersonaInput:
    return PersonaInput(
        persona_id="weekend_react_dev",
        goal="Build a small React app this weekend, something like a habit tracker with local storage",
        time_budget_minutes=360,
        user_context=UserContext(
            background="Python backend engineer, 5 years experience",
            known=["JavaScript fundamentals", "HTTP, REST, JSON", "Git, npm"],
            unknown=["React", "JSX", "Vite tooling", "React hooks", "Component patterns"],
            constraints="I prefer project-based content. Avoid pure-theory lectures.",
        ),
    )


def _make_candidate() -> Candidate:
    return Candidate(
        video_id="abc123",
        title="Vite + React Setup From Scratch",
        channel="Some Channel",
        url="https://youtube.com/watch?v=abc123",
        duration_minutes=42.0,
        upload_date="20240101",
        description="A tutorial",
        transcript_available=True,
        transcript="today we set up vite and react ...",
    )


def _make_understanding(video_id: str = "abc123") -> UnderstandingRecord:
    return UnderstandingRecord(
        video_id=video_id,
        covers_topics=["Vite project scaffolding", "dev server config"],
        primary_style="project-based",
        depth="intro",
        phase="setup",
        matches_unknown=["Vite tooling"],
        overlaps_known=[],
        constraint_violations=[],
        evidence_snippet="today we set up vite and react",
        grounded=True,
        confidence=0.86,
        quality_signal=QualitySignal(clarity=0.8, content_density=0.7, accuracy_flags=[]),
    )


def test_input_roundtrip():
    payload = _make_input()
    assert PersonaInput.model_validate(payload.model_dump()) == payload


def test_scope_check_result():
    ScopeCheckResult(in_scope=True, reason="clear learning goal")
    ScopeCheckResult(in_scope=False, reason="goal field is empty/gibberish")


def test_candidate_and_understanding():
    candidate = _make_candidate()
    understanding = _make_understanding()
    assert understanding.video_id == candidate.video_id


def test_scored_candidate():
    scored = ScoredCandidate(
        candidate=_make_candidate(),
        understanding=_make_understanding(),
        cluster_id=0,
        utility=1.42,
    )
    assert scored.utility > 0


def test_review_verdict_approved_has_no_required_issues():
    verdict = ReviewVerdict(approved=True)
    assert verdict.issues == []


def test_review_verdict_blocking_issue():
    verdict = ReviewVerdict(
        approved=False,
        issues=[
            ReviewIssue(
                severity="blocking",
                stage_to_fix="selection",
                target="abc123",
                issue="duplicate of an already-picked cluster",
                suggested_fix="drop abc123, keep the higher-utility cluster-mate",
            )
        ],
    )
    assert verdict.issues[0].severity == "blocking"


def test_curriculum_output():
    output = CurriculumOutput(
        persona_id="weekend_react_dev",
        budget_minutes=360,
        total_minutes=347,
        curriculum=[
            CurriculumItem(
                order=1,
                video_id="abc123",
                title="Vite + React Setup From Scratch",
                url="https://youtube.com/watch?v=abc123",
                duration_minutes=42,
                reason="Covers Vite project scaffolding end to end, hands-on.",
                evidence_snippet="today we set up vite and react",
                confidence=0.86,
                grounded=True,
            )
        ],
        review=ReviewSummary(iterations=1, approved=True, unresolved_blocking_issues=[]),
        considered_and_dropped=[
            DroppedItem(
                video_id="def456",
                title="Another Vite Setup Video",
                reason_dropped="Same cluster as pick #1, lower utility.",
                would_have_ranked=2,
            )
        ],
        warnings=[],
        goal_coverage={"React": "covered", "Vite tooling": "covered"},
    )
    assert output.curriculum[0].order == 1


def test_out_of_scope_output():
    out = OutOfScopeOutput(message="That doesn't look like a learning goal.")
    assert out.status == "out_of_scope"


def test_narrative_draft():
    draft = NarrativeDraft(
        items=[NarrativeItem(video_id="abc123", reason="Covers Vite setup end to end.")],
        dropped=[NarrativeDroppedItem(video_id="def456", reason_dropped="Same cluster, lower utility.")],
    )
    assert len(draft.items) == 1
