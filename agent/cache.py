"""Understanding-record cache (HLD.md §8/§9) -- sqlite, local, free. A
video's content doesn't change between one run and the next, so re-running
the same persona (Phase 9's own checkpoint: run the eval suite twice and
watch cost drop) or `--compare-reviewer`'s two variants of the SAME persona
(HLD §8: "nothing about discovery/understanding differs between the two
variants and the cache carries over") should skip the LLM call entirely on
a repeat.

Design note (not fully HLD-specified): `UnderstandingRecord.matches_unknown`,
`.overlaps_known`, and `.constraint_violations` are genuinely
persona-dependent -- they're computed against THIS learner's own
known/unknown/constraints strings, not just the video's content. Caching by
`video_id` alone would silently serve a wrong record to a differently-shaped
persona asking about the same video. Rather than splitting the record into
content-only vs. persona-dependent fields and recomputing the latter
deterministically (a real optimization, left as a "with more time" item --
see README), this cache keys on video_id + the exact persona fields that
feed those three outputs, which is correctness-first and still hits on
every case this project's own checkpoints actually exercise.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from agent.config import CACHE_PATH
from agent.schemas import PersonaInput, UnderstandingRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS understanding_cache (
    cache_key TEXT PRIMARY KEY,
    record_json TEXT NOT NULL
)
"""


def _connect() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_PATH)
    conn.execute(_SCHEMA)
    return conn


def make_key(video_id: str, input_payload: PersonaInput) -> str:
    """video_id + the exact persona fields that can change the record's
    persona-dependent fields (matches_unknown/overlaps_known/
    constraint_violations) -- not the free-text goal, which doesn't feed
    any UnderstandingRecord field directly."""
    persona_fingerprint = json.dumps(
        {
            "known": sorted(input_payload.user_context.known),
            "unknown": sorted(input_payload.user_context.unknown),
            "constraints": input_payload.user_context.constraints,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(persona_fingerprint.encode()).hexdigest()[:16]
    return f"{video_id}:{digest}"


def get_many(keys: list[str]) -> dict[str, UnderstandingRecord]:
    if not keys:
        return {}
    with _connect() as conn:
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"SELECT cache_key, record_json FROM understanding_cache WHERE cache_key IN ({placeholders})",
            keys,
        ).fetchall()
    return {key: UnderstandingRecord.model_validate_json(record_json) for key, record_json in rows}


def set_many(records_by_key: dict[str, UnderstandingRecord]) -> None:
    if not records_by_key:
        return
    with _connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO understanding_cache (cache_key, record_json) VALUES (?, ?)",
            [(key, record.model_dump_json()) for key, record in records_by_key.items()],
        )
        conn.commit()
