"""Follow-up Q&A over a finished run's trace (HLD.md §5.1). Not a second
retrieval or judgment pass -- a read path over data the pipeline already
computed. Resolving which video a question is about is deterministic
(exact video_id/URL, else fuzzy title match against everything this run
ever saw); the one Claude call that follows only phrases the already-
assembled record it's handed, and is told never to guess beyond it.

`ui/app.py` (HLD §5.2) wraps `answer_question` directly as its chat layer --
no new pipeline logic there either.
"""
from __future__ import annotations

import re

from agent.llm_client import call_structured
from agent.schemas import FollowupAnswer
from agent.trace import Trace

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|\b)([A-Za-z0-9_-]{11})\b")

# Seen live: raw character-level similarity (difflib.SequenceMatcher) between
# a whole question and a short title clears a naive 0.35 threshold on pure
# noise -- common short words ("the", "in", "to"...) contribute matching
# runs even when the question is about a completely different video. Word-
# overlap against the TITLE's distinctive words only is much harder to
# false-positive on, since a title has few enough words that noise doesn't
# accumulate the way character n-grams do.
_STOPWORDS = {
    "the", "a", "an", "in", "on", "to", "of", "is", "are", "was", "wasn't",
    "did", "didn't", "does", "doesn't", "do", "you", "it", "that", "this",
    "not", "why", "how", "for", "and", "or", "with", "at", "by", "i", "have",
    "has", "had", "include", "included", "pick", "picked", "choose", "chose",
    "video", "about",
}

# Fraction of the TITLE's significant words that must appear in the
# question. Below this, a fuzzy match is more likely noise than signal --
# the question gets treated as "never discovered" instead of guessing.
_MATCH_THRESHOLD = 0.6


def _significant_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS and len(w) > 2}

_SYSTEM = """You answer a learner's question about a curriculum-building \
run that has already finished. You are given the one structured record \
relevant to their question -- everything this run knows about that video: \
its score, its cluster, whether it was selected, dropped, or excluded by \
review, and why. Phrase a direct answer from that record only. Never \
invent a reason, a score, or an outcome that isn't in the record. If the \
record says the video was never found by this run's search, say exactly \
that -- do not speculate about why it might have been excluded."""

_PROMPT_TEMPLATE = """Learner's question: {question}

{context_block}

Answer in 2-4 sentences, grounded only in the record above.
"""


def _all_titles(trace: Trace) -> dict[str, str]:
    """video_id -> title over every candidate this run ever saw, discovered
    or not -- the full universe fuzzy title matching searches against."""
    return {c["video_id"]: c["title"] for c in trace.data.get("candidates", [])}


def resolve_video_id(trace: Trace, question: str) -> str | None:
    """Deterministic only (HLD §5.1): exact video_id/URL match first, then
    the closest fuzzy title match if it clears _MATCH_THRESHOLD. Never asks
    an LLM to guess which video a vague question refers to."""
    titles = _all_titles(trace)
    for match in _VIDEO_ID_RE.finditer(question):
        if match.group(1) in titles:
            return match.group(1)

    q_words = _significant_words(question)
    best_id, best_overlap = None, 0.0
    for video_id, title in titles.items():
        title_words = _significant_words(title)
        # A title with <2 distinctive words can't be matched reliably --
        # any question sharing just one common-ish word would "match".
        if len(title_words) < 2:
            continue
        overlap = len(title_words & q_words) / len(title_words)
        if overlap > best_overlap:
            best_id, best_overlap = video_id, overlap
    return best_id if best_overlap >= _MATCH_THRESHOLD else None


def _record_for(trace: Trace, video_id: str) -> dict:
    """Assembles everything the trace recorded about one video_id -- score,
    cluster-mates, and its selection/review outcome -- into one dict so the
    phrasing call downstream sees the whole picture, never a partial one
    that would nudge it toward filling gaps itself."""
    candidate = next(
        (c for c in trace.data.get("candidates", []) if c["video_id"] == video_id), None
    )
    scored = next(
        (s for s in trace.data.get("scored_candidates", []) if s["candidate"]["video_id"] == video_id),
        None,
    )
    cluster_id = trace.data.get("dedup_clusters", {}).get(video_id)
    cluster_mates = [
        vid
        for vid, cid in trace.data.get("dedup_clusters", {}).items()
        if cid == cluster_id and vid != video_id
    ] if cluster_id is not None else []

    output = trace.data.get("output") or {}
    in_curriculum = next(
        (item for item in output.get("curriculum", []) if item["video_id"] == video_id), None
    )
    considered_and_dropped = next(
        (item for item in output.get("considered_and_dropped", []) if item["video_id"] == video_id),
        None,
    )
    excluded_by_review = any(
        video_id in step.get("excluded_by_review", [])
        for step in trace.data.get("selection_steps", [])
    )
    review_issues = [
        issue
        for iteration in trace.data.get("review_iterations", [])
        for issue in iteration.get("verdict", {}).get("issues", [])
        if issue.get("target") == video_id
    ]

    return {
        "video_id": video_id,
        "title": candidate["title"] if candidate else None,
        "understanding": scored["understanding"] if scored else None,
        "utility": scored["utility"] if scored else None,
        "cluster_id": cluster_id,
        "cluster_mates": cluster_mates,
        "made_final_curriculum": in_curriculum,
        "considered_and_dropped_as": considered_and_dropped,
        "excluded_by_review": excluded_by_review,
        "review_issues_naming_it": review_issues,
    }


def answer_question(trace: Trace, question: str) -> str:
    """The one entrypoint the CLI bonus-ask path and ui/app.py's chat both
    call. Returns plain text; logs the phrasing call into `trace` like any
    other LLM call site (agent/trace.py's cost/latency bookkeeping)."""
    video_id = resolve_video_id(trace, question)
    if video_id is None:
        context_block = (
            "No video matching this question was ever discovered by this "
            "run's search -- checked against every candidate this run saw, "
            "whether it was ultimately selected, dropped, or excluded."
        )
    else:
        context_block = f"Record for video_id {video_id}:\n{_record_for(trace, video_id)}"

    prompt = _PROMPT_TEMPLATE.format(question=question, context_block=context_block)
    result = call_structured(
        prompt,
        FollowupAnswer,
        tier="strong",
        system=_SYSTEM,
        max_tokens=1024,
        trace=trace,
        trace_label="followup",
    )
    return result.answer
