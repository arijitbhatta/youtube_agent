"""Candidate discovery (HLD.md §3.2) -- yt-dlp search only, always
download-free. Cheap pre-filters (duration, live/unavailable) run here so
the expensive understanding stage (§3.4) isn't handed obviously-irrelevant
candidates.
"""
from __future__ import annotations

import yt_dlp

from agent.schemas import Candidate

SEARCH_RESULTS_PER_QUERY = 40
MIN_DURATION_MINUTES = 1.5  # drops shorts / zero-duration live streams

# HLD §8's cost model assumes ~40-80 filtered candidates feeding the
# expensive per-candidate yt-dlp fetch + batched understanding stages (seen
# live: with a generous budget the duration-based cheap filter barely
# narrows anything, so 7 queries x 40 results/query produced 245 unique
# candidates -- 245 sequential yt-dlp detail fetches and ~49 understanding
# batches instead of the intended ~8-16). Capping here, not by lowering
# SEARCH_RESULTS_PER_QUERY, so each query still gets a fair, deep look
# before the round-robin interleave below decides which of its results
# actually survive to the expensive stages.
MAX_TOTAL_CANDIDATES = 80


def _ydl_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 15,
        "extractor_retries": 1,
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }


def search_query(query: str, max_results: int = SEARCH_RESULTS_PER_QUERY) -> list[dict]:
    """One yt-dlp flat-extract search -- never downloads video/audio bytes."""
    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        info = ydl.extract_info(f"ytsearch{max_results}:{query}", download=False)
    return info.get("entries", []) if info else []


def _duration_minutes(entry: dict) -> float:
    return (entry.get("duration") or 0) / 60.0


def _passes_cheap_filters(entry: dict, budget_minutes: float) -> bool:
    duration = _duration_minutes(entry)
    if duration <= 0:
        return False  # live stream / unavailable duration
    if duration < MIN_DURATION_MINUTES:
        return False
    if duration > budget_minutes:
        return False  # can't fit on its own
    return True


def discover_candidates(queries: list[str], budget_minutes: float) -> list[Candidate]:
    per_query_entries = [search_query(query) for query in queries]

    by_video_id: dict[str, Candidate] = {}
    # Round-robin across queries (rank 0 from every query, then rank 1, ...)
    # rather than exhausting query 1 before touching query 2 -- otherwise the
    # MAX_TOTAL_CANDIDATES cap would silently starve later queries (e.g. the
    # ones covering topics later in `unknown`) whenever an earlier query's 40
    # results alone exceeded the cap.
    max_len = max((len(entries) for entries in per_query_entries), default=0)
    for rank in range(max_len):
        if len(by_video_id) >= MAX_TOTAL_CANDIDATES:
            break
        for entries in per_query_entries:
            if len(by_video_id) >= MAX_TOTAL_CANDIDATES:
                break
            if rank >= len(entries):
                continue
            entry = entries[rank]
            video_id = entry.get("id")
            if not video_id or video_id in by_video_id:
                continue
            if not _passes_cheap_filters(entry, budget_minutes):
                continue
            by_video_id[video_id] = Candidate(
                video_id=video_id,
                title=entry.get("title", ""),
                channel=entry.get("channel") or entry.get("uploader") or "",
                url=entry.get("url") or f"https://www.youtube.com/watch?v={video_id}",
                duration_minutes=_duration_minutes(entry),
                transcript_available=False,
            )
    return list(by_video_id.values())
