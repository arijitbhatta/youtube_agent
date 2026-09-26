"""Dedup clustering (HLD.md §3.5) -- a small local sentence-embedding
model (free, offline, no extra API calls), simple union-find over pairs
above a cosine-similarity threshold. No clustering library needed at the
~40-80 candidate scale this pipeline operates at.
"""
from __future__ import annotations

import numpy as np
from sentence_transformers import SentenceTransformer

from agent.schemas import UnderstandingRecord

SIMILARITY_THRESHOLD = 0.82
_MODEL_NAME = "all-MiniLM-L6-v2"

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def embed(texts: list[str]) -> np.ndarray:
    """Public wrapper so eval/run_eval.py's independent embedding cross-
    check (HLD §6.1 method (b)) reuses this module's already-loaded local
    model instead of instantiating a second SentenceTransformer."""
    return _get_model().encode(texts, normalize_embeddings=True)


def _summary_text(record: UnderstandingRecord) -> str:
    topics = ", ".join(record.covers_topics)
    return f"{topics} -- {record.primary_style}, {record.depth} depth, {record.phase} phase"


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def cluster_candidates(records: list[UnderstandingRecord]) -> dict[str, int]:
    """Returns video_id -> cluster_id. Every record gets a cluster, even a
    singleton one -- scoring.py's novelty bonus and selection.py's
    redundancy handling both rely on every candidate having a cluster_id,
    not just the ones with duplicates."""
    if not records:
        return {}
    texts = [_summary_text(r) for r in records]
    embeddings = _get_model().encode(texts, normalize_embeddings=True)
    uf = _UnionFind(len(records))
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            similarity = float(np.dot(embeddings[i], embeddings[j]))
            if similarity >= SIMILARITY_THRESHOLD:
                uf.union(i, j)
    # Normalize root indices to a compact 0..k-1 range for readability in
    # the trace/output rather than leaking arbitrary union-find root ids.
    roots: dict[int, int] = {}
    cluster_ids: dict[str, int] = {}
    for idx, record in enumerate(records):
        root = uf.find(idx)
        if root not in roots:
            roots[root] = len(roots)
        cluster_ids[record.video_id] = roots[root]
    return cluster_ids
