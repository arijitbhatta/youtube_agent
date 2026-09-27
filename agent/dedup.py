"""Dedup clustering (HLD.md §3.5) -- embeddings via a hosted proprietary
model over OpenRouter (agent/llm_client.call_embeddings), simple union-find
over pairs above a cosine-similarity threshold. No clustering library
needed at the ~40-80 candidate scale this pipeline operates at.

Originally a local sentence-transformers model (`all-MiniLM-L6-v2` -- free,
offline, no extra API calls). Swapped to a hosted embedding model because
installing torch made the Docker image's first `docker compose build` take
>20 minutes on this machine, entirely from the torch wheel -- see
SKILLS.md #14. The swap reuses the OpenRouter key every chat call already
requires rather than adding a second dependency/runtime.

SIMILARITY_THRESHOLD was recalibrated live against real `covers_topics`
pairs from a high-duplication trace (36 non-degenerate records from
outputs/git_basics_high_duplication-*.json) -- `text-embedding-3-small`'s
cosine-similarity space is not the same as MiniLM's, so the old 0.82 could
not just carry over. Sweeping 0.80-0.88: at 0.80-0.82 union-find's
transitive closure chains through weakly-related intermediates into one
9-17-member megacluster mixing merge-conflict videos with branching,
DevOps-practices, and IDE-tooling videos -- a chaining artifact, not real
duplication. At 0.86-0.88, genuine triples (e.g. three distinct "GitHub
collaboration setup" videos) start splitting apart, losing real
duplicates. 0.84 is the sweet spot with neither problem -- see SKILLS.md
#14 for the full numbers.
"""
from __future__ import annotations

import numpy as np

from agent.llm_client import call_embeddings
from agent.schemas import UnderstandingRecord

SIMILARITY_THRESHOLD = 0.84


def embed(texts: list[str], *, trace=None, trace_label: str | None = None) -> np.ndarray:
    """Public wrapper so eval/run_eval.py's independent embedding cross-
    check (HLD §6.1 method (b)) reuses this module's one call site instead
    of duplicating the OpenRouter embeddings call."""
    return call_embeddings(texts, trace=trace, trace_label=trace_label)


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


def cluster_candidates(records: list[UnderstandingRecord], *, trace=None) -> dict[str, int]:
    """Returns video_id -> cluster_id. Every record gets a cluster, even a
    singleton one -- scoring.py's novelty bonus and selection.py's
    redundancy handling both rely on every candidate having a cluster_id,
    not just the ones with duplicates."""
    if not records:
        return {}
    texts = [_summary_text(r) for r in records]
    embeddings = embed(texts, trace=trace, trace_label="dedup_embed")
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
