"""Metadata + transcript fetch (HLD.md §3.3) -- download-free as a hard
constraint (CLAUDE.md), not an implementation detail. yt-dlp is only ever
used in extract_info(download=False)/skip_download=True modes here; the
caption *file* itself is small text, fetched directly over HTTP once
yt-dlp has told us its URL -- no video/audio bytes ever touched.
"""
from __future__ import annotations

import re
import time
import urllib.error
import urllib.request

import yt_dlp

from agent.schemas import Candidate

MAX_TRANSCRIPT_CHARS = 12_000  # even spread across the video, not a truncation

# YouTube's caption/timedtext CDN (distinct from the main video-info
# endpoint) rate-limits with a plain 429 under sustained request volume --
# seen live, repeatedly, during this session's own testing (a fresh,
# unrelated, extremely popular video's caption download still 429'd, so
# it's IP-level throttling, not a per-video quirk). A short bounded backoff
# is the canonical response to a 429; this is not the open-ended retry
# CLAUDE.md warns against -- it's 2 extra attempts, then give up and flag
# grounded: false same as a genuinely missing transcript.
_CAPTION_FETCH_RETRIES = 2
_CAPTION_FETCH_BACKOFF_SECONDS = (2, 5)


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


def _clean_vtt(raw: str) -> str:
    """Strip WebVTT timestamps/cues down to plain spoken text."""
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        if "-->" in line:
            continue
        if re.match(r"^\d+$", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line:
            lines.append(line)
    # auto-captions repeat the previous line as a rolling-window artifact
    deduped = []
    for line in lines:
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return " ".join(deduped)


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

    transcript_text = None
    subs = {**(info.get("subtitles") or {}), **(info.get("automatic_captions") or {})}
    en_tracks = subs.get("en") or []
    vtt_track = next((t for t in en_tracks if t.get("ext") == "vtt"), None)
    if vtt_track:
        raw_vtt = None
        for attempt in range(_CAPTION_FETCH_RETRIES + 1):
            try:
                with urllib.request.urlopen(vtt_track["url"], timeout=15) as response:
                    raw_vtt = response.read().decode("utf-8", errors="replace")
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < _CAPTION_FETCH_RETRIES:
                    time.sleep(_CAPTION_FETCH_BACKOFF_SECONDS[attempt])
                    continue
                break
            except Exception:
                break
        if raw_vtt:
            transcript_text = _downsample(_clean_vtt(raw_vtt), MAX_TRANSCRIPT_CHARS)

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
