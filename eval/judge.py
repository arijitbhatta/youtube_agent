"""Offline LLM-as-judge (HLD.md §6.2). Reuses `agent/critique.py`'s shared
rubric -- CLAUDE.md: "do not write a second, separate quality-judgment
prompt." `agent/review.py` calls the same `evaluate()` online to gate one
run; this module calls it offline, in batch, against a finished run's
output, purely to grade it -- never to change it.

Run 3x per scenario (§6.2): the judge is a Claude call, not a deterministic
metric, so a single verdict would misrepresent it as ground truth. Variance
across the 3 runs is itself part of what run_eval.py reports.
"""
from __future__ import annotations

from agent import critique
from agent.schemas import CurriculumOutput, PersonaInput, ReviewVerdict, ScoredCandidate

JUDGE_RUNS = 3


def judge_output(
    input_payload: PersonaInput,
    output: CurriculumOutput,
    picks_in_order: list[ScoredCandidate],
    candidate_pool: list[ScoredCandidate],
    *,
    runs: int = JUDGE_RUNS,
    trace=None,
) -> list[ReviewVerdict]:
    """One verdict per run. Deliberately independent Claude calls (not a
    single call asked for 3 opinions) -- sampling variance is the thing
    being measured here."""
    return [
        critique.evaluate(input_payload, output, picks_in_order, candidate_pool, trace=trace)
        for _ in range(runs)
    ]
