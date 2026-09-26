"""Reasoning trace (HLD.md §2.6) -- one JSON file per run, superset of
everything the pipeline produces. Every stage appends to this as it runs
rather than the pipeline reconstructing a trace after the fact; it's the
only artifact eval/run_eval.py and agent/followup.py read.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from agent.config import OUTPUTS_DIR


def _new_run_id(persona_id: str) -> str:
    return f"{persona_id}-{int(time.time())}-{uuid.uuid4().hex[:6]}"


class Trace:
    def __init__(self, persona_id: str, run_id: str | None = None):
        self.run_id = run_id or _new_run_id(persona_id)
        self.data: dict[str, Any] = {
            "run_id": self.run_id,
            "persona_id": persona_id,
            "enable_reviewer": None,
            "input": None,
            "scope_check": None,
            "queries": [],
            "candidates": [],
            "understanding": [],
            "dedup_clusters": {},
            "scored_candidates": [],
            "selection_steps": [],
            "review_iterations": [],
            "llm_calls": [],
            "output": None,
        }

    # --- writers, one per stage boundary (HLD §1) --------------------------

    def set_input(self, input_payload: dict, enable_reviewer: bool) -> None:
        self.data["input"] = input_payload
        self.data["enable_reviewer"] = enable_reviewer

    def set_scope_check(self, result: dict) -> None:
        self.data["scope_check"] = result

    def set_queries(self, queries: list[str]) -> None:
        self.data["queries"] = queries

    def add_candidates(self, candidates: list[dict]) -> None:
        self.data["candidates"].extend(candidates)

    def add_understanding(self, records: list[dict]) -> None:
        self.data["understanding"].extend(records)

    def set_dedup_clusters(self, clusters: dict) -> None:
        self.data["dedup_clusters"] = clusters

    def set_scored_candidates(self, scored: list[dict]) -> None:
        self.data["scored_candidates"] = scored

    def add_selection_step(self, step: dict) -> None:
        self.data["selection_steps"].append(step)

    def add_review_iteration(self, iteration: dict) -> None:
        self.data["review_iterations"].append(iteration)

    def set_output(self, output: dict) -> None:
        self.data["output"] = output

    def log_llm_call(
        self,
        *,
        label: str,
        model: str,
        tier: str,
        input_tokens: int,
        output_tokens: int,
        latency_seconds: float,
        attempt: int,
    ) -> None:
        self.data["llm_calls"].append(
            {
                "label": label,
                "model": model,
                "tier": tier,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_seconds": round(latency_seconds, 3),
                "attempt": attempt,
                "timestamp": time.time(),
            }
        )

    # --- persistence ---------------------------------------------------------

    def save(self, outputs_dir: Path | str = OUTPUTS_DIR) -> Path:
        outputs_dir = Path(outputs_dir)
        outputs_dir.mkdir(parents=True, exist_ok=True)
        path = outputs_dir / f"{self.run_id}.json"
        path.write_text(json.dumps(self.data, indent=2, default=str))
        return path

    @classmethod
    def load(cls, path: Path | str) -> "Trace":
        path = Path(path)
        raw = json.loads(path.read_text())
        trace = cls(persona_id=raw.get("persona_id", "unknown"), run_id=raw.get("run_id"))
        trace.data = raw
        return trace
