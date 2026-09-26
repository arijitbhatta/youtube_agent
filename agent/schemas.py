"""Data contracts (HLD.md §2). Every stage boundary in the pipeline passes
one of these — no stage exchanges a bare dict with another.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PrimaryStyle = Literal[
    "project-based", "theory-lecture", "quick-intro", "reference-walkthrough"
]
Depth = Literal["surface", "intro", "intermediate", "advanced"]
Phase = Literal["setup", "concept", "hands-on-project", "advanced-followup"]
Severity = Literal["blocking", "minor"]
StageToFix = Literal["selection", "narrative"]


# --- §2.1 Input -------------------------------------------------------------


class UserContext(BaseModel):
    background: str
    known: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    constraints: str = ""


class PersonaInput(BaseModel):
    persona_id: str
    goal: str
    time_budget_minutes: int
    user_context: UserContext


# --- §0 Scope check ----------------------------------------------------------


class ScopeCheckResult(BaseModel):
    in_scope: bool
    reason: str


# --- §2.2 Candidate (post-discovery, pre-understanding) --------------------


class Candidate(BaseModel):
    video_id: str
    title: str
    channel: str
    url: str
    duration_minutes: float
    upload_date: str | None = None
    description: str = ""
    transcript_available: bool = False
    transcript: str | None = None


# --- §2.3 Understanding record -----------------------------------------------


class QualitySignal(BaseModel):
    clarity: float = Field(ge=0.0, le=1.0)
    content_density: float = Field(ge=0.0, le=1.0)
    accuracy_flags: list[str] = Field(default_factory=list)


class UnderstandingRecord(BaseModel):
    video_id: str
    covers_topics: list[str]
    primary_style: PrimaryStyle
    depth: Depth
    phase: Phase
    matches_unknown: list[str]
    overlaps_known: list[str]
    constraint_violations: list[str] = Field(default_factory=list)
    evidence_snippet: str
    grounded: bool
    confidence: float = Field(ge=0.0, le=1.0)
    quality_signal: QualitySignal


# --- §2.4 Scored candidate (post dedup + scoring, pre-selection) -----------


class ScoredCandidate(BaseModel):
    candidate: Candidate
    understanding: UnderstandingRecord
    cluster_id: int
    utility: float


# --- §3.9 Review loop --------------------------------------------------------


class ReviewIssue(BaseModel):
    severity: Severity
    stage_to_fix: StageToFix
    target: str  # video_id or "overall"
    issue: str
    suggested_fix: str


class ReviewVerdict(BaseModel):
    approved: bool
    issues: list[ReviewIssue] = Field(default_factory=list)


# --- §2.5 Output --------------------------------------------------------------


class CurriculumItem(BaseModel):
    order: int
    video_id: str
    title: str
    url: str
    duration_minutes: float
    reason: str
    evidence_snippet: str
    confidence: float
    grounded: bool


class DroppedItem(BaseModel):
    video_id: str
    title: str
    reason_dropped: str
    would_have_ranked: int | None = None


class ReviewSummary(BaseModel):
    iterations: int
    approved: bool
    unresolved_blocking_issues: list[ReviewIssue] = Field(default_factory=list)


class CurriculumOutput(BaseModel):
    persona_id: str
    budget_minutes: int
    total_minutes: float
    curriculum: list[CurriculumItem]
    review: ReviewSummary
    considered_and_dropped: list[DroppedItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    goal_coverage: dict[str, str] = Field(default_factory=dict)


class OutOfScopeOutput(BaseModel):
    status: Literal["out_of_scope"] = "out_of_scope"
    message: str


# --- Narrative draft (§3.8), pre-review --------------------------------------


class NarrativeItem(BaseModel):
    video_id: str
    reason: str


class NarrativeDroppedItem(BaseModel):
    video_id: str
    reason_dropped: str


class NarrativeDraft(BaseModel):
    items: list[NarrativeItem]
    dropped: list[NarrativeDroppedItem] = Field(default_factory=list)


# --- §5.1 Follow-up Q&A -------------------------------------------------------


class FollowupAnswer(BaseModel):
    # One-field wrapper so the followup call can still go through
    # call_structured like every other LLM call site (CLAUDE.md: no
    # regex/manual JSON parsing of free text) rather than needing a second,
    # freeform completion code path in agent/llm_client.py just for this.
    answer: str
