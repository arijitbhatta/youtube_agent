"""Exact-match run cache (ui/app.py only, HLD.md §5.2) -- an identical
(input_payload, enable_reviewer) submission through the Streamlit UI
re-loads the previous trace instead of re-running the whole pipeline.
Same sqlite file as agent/cache.py's understanding cache (one cache file,
a second table), same key-fingerprint pattern.

Deliberately exact-match only (SKILLS.md #15): the user asked for exact
caching now and named similarity/fuzzy caching as a later, separate
request -- not built here. `run.py`'s CLI is untouched; this is UI-only
orchestration (CLAUDE.md: "ui/app.py is a thin layer"), not new pipeline
logic.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

from agent.config import CACHE_PATH
from agent.schemas import PersonaInput

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_cache (
    cache_key TEXT PRIMARY KEY,
    trace_path TEXT NOT NULL,
    cached_at REAL NOT NULL
)
"""


def _connect() -> sqlite3.Connection:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(CACHE_PATH)
    conn.execute(_SCHEMA)
    return conn


def make_key(input_payload: PersonaInput, enable_reviewer: bool) -> str:
    """Exact-match fingerprint over every field that can change the
    pipeline's output -- the full input payload (not just the persona
    fields agent/cache.py's understanding-cache key uses: the free-text
    `goal` is exactly what makes two runs "the same query" here) plus the
    reviewer flag, since that alone changes what the pipeline does."""
    fingerprint = json.dumps(
        {"input": input_payload.model_dump(), "enable_reviewer": enable_reviewer},
        sort_keys=True,
    )
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:32]


def get_cached_trace_path(input_payload: PersonaInput, enable_reviewer: bool) -> str | None:
    key = make_key(input_payload, enable_reviewer)
    with _connect() as conn:
        row = conn.execute(
            "SELECT trace_path FROM run_cache WHERE cache_key = ?", (key,)
        ).fetchone()
    return row[0] if row else None


def set_cached_trace_path(input_payload: PersonaInput, enable_reviewer: bool, trace_path: str) -> None:
    key = make_key(input_payload, enable_reviewer)
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO run_cache (cache_key, trace_path, cached_at) VALUES (?, ?, ?)",
            (key, str(trace_path), time.time()),
        )
        conn.commit()
