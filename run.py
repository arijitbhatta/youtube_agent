#!/usr/bin/env python3
"""Single entrypoint (HLD.md §9): `python run.py --input <persona.json>`.
Runs the full graph, saves the reasoning trace + rendered output under
outputs/, and prints the Markdown view. ui/app.py imports run_from_payload
directly rather than reimplementing any of this.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Callable

from agent.config import ENABLE_REVIEWER_DEFAULT, OUTPUTS_DIR, api_key_present
from agent.graph import run_pipeline
from agent.render import render_markdown
from agent.schemas import CurriculumOutput, OutOfScopeOutput, PersonaInput


def run_from_payload(
    input_payload: PersonaInput,
    *,
    enable_reviewer: bool = ENABLE_REVIEWER_DEFAULT,
    progress_cb: Callable[[str, str], None] | None = None,
):
    """Returns (output, trace_path). Shared by the CLI below and ui/app.py.

    `progress_cb`, when given, is invoked with `(stage, detail)` at each
    pipeline node boundary -- ui/app.py uses it for the live progress panel;
    the CLI leaves it `None`."""
    output, trace = run_pipeline(input_payload, enable_reviewer=enable_reviewer, progress_cb=progress_cb)
    trace_path = trace.save(OUTPUTS_DIR)
    return output, trace_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Learning Curriculum Builder")
    parser.add_argument("--input", required=True, help="Path to a persona JSON file")
    parser.add_argument(
        "--no-reviewer",
        action="store_true",
        help="Skip the review loop entirely (HLD §6.6 ablation) -- zero extra calls/latency.",
    )
    args = parser.parse_args()

    if not api_key_present():
        print(
            "OPENROUTER_API_KEY is not set -- copy .env.example to .env and fill it in.",
            file=sys.stderr,
        )
        return 1

    with open(args.input) as f:
        raw = json.load(f)
    input_payload = PersonaInput.model_validate(raw)

    output, trace_path = run_from_payload(input_payload, enable_reviewer=not args.no_reviewer)

    if isinstance(output, OutOfScopeOutput):
        print(output.message)
    else:
        assert isinstance(output, CurriculumOutput)
        print(render_markdown(output))
    print(f"\n(trace saved to {trace_path})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
