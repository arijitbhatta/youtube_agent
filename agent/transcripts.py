"""Metadata + transcript fetch (HLD.md §3.3) -- download-free as a hard
constraint (CLAUDE.md), not an implementation detail. yt-dlp is only ever
used in extract_info(download=False) mode here, for real title/channel/
duration/upload_date/description -- none of which the transcript library
below provides. The transcript itself comes from `youtube-transcript-api`
(SKILLS.md #15), which talks to YouTube's caption endpoint directly rather
than through yt-dlp's caption-URL indirection; still no video/audio bytes
touched either way.
"""
from __future__ import annotations

import random
import time

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import CouldNotRetrieveTranscript

from agent.schemas import Candidate

MAX_TRANSCRIPT_CHARS = 12_000  # even spread across the video, not a truncation

_transcript_api = YouTubeTranscriptApi()


def _ydl_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        # yt-dlp's own default can hang far longer than is tolerable when a
        # single candidate's connection stalls (seen live: one bad request
        # blocked the whole sequential fetch loop for the better part of an
        # hour) -- bound both the per-attempt timeout and the retry count.
        "socket_timeout": 15,
        "extractor_retries": 1,
        # YouTube's web client increasingly bot-checks yt-dlp's default
        # requests ("Sign in to confirm you're not a bot") -- seen live,
        # repeatedly, across a real search batch. The android client
        # skips that check without needing cookies/login.
        "extractor_args": {"youtube": {"player_client": ["android"]}},
    }


def _fetch_transcript_text(video_id: str) -> str | None:
    try:
        # Jittered 1.5-5s pause before each caption-CDN request. The throttle
        # is IP-level and persistent (SKILLS.md #15), so this doesn't clear
        # the block by itself -- it just spaces requests out instead of
        # hammering the endpoint back-to-back.
        time.sleep(random.uniform(1.5, 5.0))
        fetched = _transcript_api.fetch(video_id, languages=["en"])
    except CouldNotRetrieveTranscript:
        # Covers every documented failure mode of this library (disabled
        # captions, no English track, unavailable video, and IP-level
        # blocking -- the same caption-CDN throttling previously seen via
        # yt-dlp's raw VTT fetch, still present here under a different
        # exception type; see SKILLS.md #15). Degrades the same way a
        # genuinely missing transcript does (grounded: false downstream).
        return None
    except Exception:
        return None
    text = " ".join(snippet.text for snippet in fetched if snippet.text.strip())
    return text or None


def _downsample(text: str, max_chars: int) -> str:
    """Sample contiguous, readable chunks spread evenly across the whole
    video -- a long tutorial's last third matters as much as its first, but
    single scattered words (a naive stride sample) would read as scrambled
    word-salad and defeat grounding (evidence snippets need a real quotable
    excerpt, not a bag of words)."""
    if len(text) <= max_chars:
        return text
    words = text.split()
    total = len(words)
    target_words = max(1, int(max_chars / 6))  # rough chars-per-word estimate
    if target_words >= total:
        return text
    num_chunks = min(8, target_words)
    chunk_size = max(1, target_words // num_chunks)
    chunks = []
    for i in range(num_chunks):
        start = int(i * total / num_chunks)
        end = min(total, start + chunk_size)
        chunks.append(" ".join(words[start:end]))
    return " ... ".join(chunks)


def fetch_metadata_and_transcript(candidate: Candidate) -> Candidate | None:
    """Returns a new Candidate with real duration/description/upload_date
    and transcript filled in (transcript_available set either way), or
    None if yt-dlp can't extract this video at all -- a real edge case
    from flat search results that were never validated as playable
    (unreleased premieres, private/deleted/region-blocked videos). Seen
    live: a real search surfaced a "Premieres in 214 days" video."""
    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(candidate.url, download=False)
    except yt_dlp.utils.DownloadError:
        return None

    transcript_text = _fetch_transcript_text(candidate.video_id)
    if transcript_text:
        transcript_text = _downsample(transcript_text, MAX_TRANSCRIPT_CHARS)

    return candidate.model_copy(
        update={
            "title": info.get("title", candidate.title),
            "channel": info.get("channel") or info.get("uploader") or candidate.channel,
            "duration_minutes": ((info.get("duration") or 0) / 60.0) or candidate.duration_minutes,
            "upload_date": info.get("upload_date"),
            "description": info.get("description") or "",
            "transcript_available": bool(transcript_text),
            "transcript": transcript_text,
        }
    )
