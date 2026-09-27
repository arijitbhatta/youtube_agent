"""Content understanding -- the grounding step (HLD.md §3.4). Batched
Claude calls (BATCH_SIZE candidates/call) turn each transcript (or bare
metadata, if no transcript) into the structured UnderstandingRecord from
§2.3 -- this answers the spec's actual hard problem: not "does this look
relevant" but "what does this video actually teach, at what depth, in
what style," including the `phase` label §4 later sequences by.
"""
from __future__ import annotations

import concurrent.futures

from pydantic import BaseModel, Field

from agent import cache
from agent.config import UNDERSTAND_MAX_WORKERS
from agent.llm_client import call_structured
from agent.schemas import Candidate, PersonaInput, UnderstandingRecord

BATCH_SIZE = 5

_SYSTEM = """You are grading YouTube videos for how well they teach specific \
topics to a specific learner, grounded strictly in the transcript excerpt \
given -- never in the title, description, or your own general knowledge of \
the topic. If a transcript is unavailable, say so honestly (grounded: \
false, lower confidence) rather than guessing from metadata."""

_PROMPT_TEMPLATE = """Learner's goal: {goal}
Learner's background: {background}
Already known: {known}
Wants to learn (unknown): {unknown}
Constraints: {constraints}

For each candidate video below, produce one UnderstandingRecord (video_id \
must exactly match the one given). Ground every claim in the transcript \
excerpt -- if no transcript is available, set grounded: false, lower \
confidence, and don't invent detail metadata alone couldn't tell you.

Fields: `covers_topics` (what it actually teaches), `matches_unknown` \
(subset of the learner's unknown list above, exact strings, that this \
video teaches), `overlaps_known` (subset of the known list this video \
mostly re-teaches), `primary_style` -- the video's FORMAT, must be \
EXACTLY one of these four strings, no others: "project-based", \
"theory-lecture", "quick-intro", "reference-walkthrough", `depth` \
(surface / intro / intermediate / advanced), `phase` -- a DIFFERENT \
field from primary_style: the pedagogical ROLE this video plays for THIS \
learner specifically, must be EXACTLY one of these four strings: \
"setup" (tooling/environment/prerequisites), "concept" (explains a \
technique/API), "hands-on-project" (builds something end-to-end), \
"advanced-followup" (extends/optimizes past the basics) -- do not put a \
`phase` value into `primary_style` or vice versa, \
`constraint_violations` (does it violate a constraint the learner stated \
above -- e.g. "avoid pure-theory lectures" and this IS one), \
`evidence_snippet` (a real quote copied verbatim from the transcript \
excerpt, not paraphrased), `grounded` (true only if evidence_snippet is a \
real transcript quote), `confidence` (0-1), and `quality_signal` (clarity \
0-1, content_density 0-1, accuracy_flags -- speculative only, never \
verified, empty list if none).

`matches_unknown` and `overlaps_known` must contain ONLY exact strings \
copied verbatim from the "wants to learn" / "already known" lists above -- \
never a placeholder, paraphrase, or explanation. If you genuinely can't \
tell (e.g. no transcript available), leave the list EMPTY rather than \
inventing an entry.

Candidates:
{candidates_block}
"""


class _BatchResult(BaseModel):
    records: list[UnderstandingRecord] = Field(min_length=1)


def _format_candidate(candidate: Candidate) -> str:
    transcript = candidate.transcript or "(no transcript available -- metadata only)"
    return (
        f"video_id: {candidate.video_id}\n"
        f"title: {candidate.title}\n"
        f"channel: {candidate.channel}\n"
        f"duration_minutes: {candidate.duration_minutes:.1f}\n"
        f"description: {candidate.description[:500]}\n"
        f"transcript_available: {candidate.transcript_available}\n"
        f"transcript_excerpt: {transcript}\n"
    )


def understand_candidates(
    candidates: list[Candidate], input_payload: PersonaInput, *, trace=None
) -> list[UnderstandingRecord]:
    """Batched (BATCH_SIZE per call) grounded extraction -- see §3.4. A
    candidate the model drops from its batch response (rare, but the kind
    of thing a forced-tool call can still get wrong) simply doesn't get an
    understanding record and falls out of consideration downstream -- no
    retry-until-complete loop, matching CLAUDE.md's one-bounded-retry
    stance on LLM flakiness.

    Cache lookup (agent/cache.py, §8) happens first, keyed by video_id +
    the persona fields that can change the record -- a cache hit skips the
    LLM call for that candidate entirely, so a repeat run of the same
    persona (--compare-reviewer's two variants, or simply re-running the
    same scenario) pays for understanding once, not once per run."""
    keys_by_video_id = {c.video_id: cache.make_key(c.video_id, input_payload) for c in candidates}
    cached = cache.get_many(list(keys_by_video_id.values()))

    records: list[UnderstandingRecord] = []
    to_fetch: list[Candidate] = []
    for c in candidates:
        hit = cached.get(keys_by_video_id[c.video_id])
        if hit is not None:
            records.append(hit)
        else:
            to_fetch.append(c)

    def _understand_batch(batch: list[Candidate]) -> list[UnderstandingRecord]:
        candidates_block = "\n---\n".join(_format_candidate(c) for c in batch)
        prompt = _PROMPT_TEMPLATE.format(
            goal=input_payload.goal,
            background=input_payload.user_context.background,
            known=", ".join(input_payload.user_context.known) or "(none stated)",
            unknown=", ".join(input_payload.user_context.unknown) or "(none stated)",
            constraints=input_payload.user_context.constraints or "(none stated)",
            candidates_block=candidates_block,
        )
        result = call_structured(
            prompt,
            _BatchResult,
            tier="cheap",
            system=_SYSTEM,
            max_tokens=4096,
            trace=trace,
            trace_label="understanding",
        )
        valid_ids = {c.video_id for c in batch}
        out: list[UnderstandingRecord] = []
        for record in result.records:
            if record.video_id not in valid_ids:
                continue
            # Defensive boundary validation on a real LLM completion
            # (CLAUDE.md: validate at real system edges) -- these two
            # fields must be an exact subset of the persona's own lists;
            # an occasional invented/placeholder entry (seen live: a model
            # writing "<UNKNOWN - no transcript available>" instead of an
            # empty list) must not leak into scoring/trace.
            record.matches_unknown = [
                t for t in record.matches_unknown if t in input_payload.user_context.unknown
            ]
            record.overlaps_known = [
                k for k in record.overlaps_known if k in input_payload.user_context.known
            ]
            out.append(record)
        return out

    new_records: list[UnderstandingRecord] = []
    batches = [to_fetch[i : i + BATCH_SIZE] for i in range(0, len(to_fetch), BATCH_SIZE)]
    if batches:
        # The batched calls are I/O-bound (OpenRouter round-trips), so run them
        # concurrently over a bounded pool (config.UNDERSTAND_MAX_WORKERS) --
        # this collapses the stage from sum(latencies) to roughly max(latency).
        # Futures are gathered in submission order so the returned record order
        # still matches the candidate order (as the old sequential loop did).
        with concurrent.futures.ThreadPoolExecutor(max_workers=UNDERSTAND_MAX_WORKERS) as pool:
            futures = [pool.submit(_understand_batch, b) for b in batches]
            for future in futures:
                new_records.extend(future.result())

    cache.set_many({keys_by_video_id[r.video_id]: r for r in new_records})
    records.extend(new_records)
    return records
