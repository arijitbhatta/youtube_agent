"""Pure helper for turning the pipeline's internal stage names (emitted by
`agent/graph.py`'s `_log` → `ProgressFn` callback) into short learner-facing
phrases -- owned here (not in graph.py, which is pipeline wiring, and not in
ui/app.py, which is untested) so it stays unit-testable. UI-only: the CLI and
trace never use this.
"""
from __future__ import annotations

# stage names are the exact strings `_log` is called with in agent/graph.py.
STAGE_LABELS: dict[str, str] = {
    "gate": "Checking your request",
    "decline": "Finishing up",
    "query_planning": "Planning search queries",
    "discovery": "Searching YouTube",
    "fetch": "Fetching transcripts",
    "understanding": "Understanding videos",
    "dedup": "Grouping near-duplicates",
    "score": "Scoring candidates",
    "select": "Selecting & sequencing",
    "narrative": "Writing your plan",
    "review": "Reviewing",
    "render": "Rendering your plan",
}


def format_progress(stage: str, detail: str = "") -> str:
    """Friendly one-line status from a (stage, detail) progress event. The
    detail carries the live count ("25/80") for `fetch` and similar; unknown
    stage names fall back to the raw string rather than a KeyError, and an
    empty stage (the instant before the first node announces itself) reads as
    "Starting…". """
    if not stage:
        return "Starting…"
    label = STAGE_LABELS.get(stage, stage)
    return f"{label}{f' — {detail}' if detail else ''}"