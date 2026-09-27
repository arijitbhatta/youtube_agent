# Project context

**Learning Curriculum Builder** — an agent that takes `(goal,
time_budget_minutes, user_context)` and produces a sequenced set of
YouTube videos — however many the goal and budget actually call for, not
a fixed count (see `HLD.md` §4) — grounded in their actual transcripts,
that would plausibly get *this* learner (not a generic one) to the goal.

Read `APPROACH.md` first (the decisions and why), then `HLD.md` (the full
design: pipeline, data contracts, algorithms, evaluation, test set, cost
model, repo layout, build order). Both are finished. **No code exists yet.**
Build order is §10 of `HLD.md` — follow it; each step is chosen so it can
be checked by watching something concrete happen before the next step
depends on it.

## Non-negotiable principles (see `APPROACH.md` for the reasoning)

1. **Grounded over generic.** Every inclusion reason must trace to a real
   transcript excerpt. No transcript → flag `grounded: false`, lower
   `confidence`. Never silently upgrade a metadata guess to a confident
   claim.
2. **Deterministic where it matters, generative where it helps.** Budget
   arithmetic, set selection, and dedup clustering are code, never LLM
   output. LLMs extract (what does this video teach) and narrate (explain
   a decision already made) — they never do silent arithmetic, and the
   reviewer (§3.9 of `HLD.md`) never overrides a deterministic metric
   failure with its own opinion.
3. **Explainable by construction.** The full reasoning trace (every
   candidate, every score, every review iteration, why anything was cut)
   is a first-class output artifact, not reconstructed after the fact.
4. **Evaluate what's verifiable; say plainly what isn't.** The eval is
   weighted most heavily. Measure real things; state blind spots in the
   doc rather than papering over them with a single LLM-judge score.
5. **Simple over impressive.** No queues, workers, or managed infra — there
   is no concurrency requirement here. The pipeline is built as a
   **LangGraph graph** (nodes = §3's stages, matching `HLD.md` §1's
   diagram exactly), but only as the control-flow engine — no LangServe,
   no separate service, still one process. Any agentic-looking piece (the
   reviewer loop, the scope-check gate) must stay a bounded conditional
   edge with an explicit, checked iteration count in graph state — never
   relying on a framework default (e.g. `recursion_limit`) to enforce a
   business rule — and must justify its call cost against the usage cap
   below.

## Hard constraints

- **Claude API key has a usage cap.** Every new call site needs a reason —
  batch where possible (understanding calls: ~5 candidates/call), cache
  per `video_id`, and cap any loop (`agent/review.py`: hard max 3
  iterations, no "until satisfied" open loop).
- **`yt-dlp` is used strictly in download-free modes** —
  `extract_info(download=False)` for search/metadata,
  `skip_download=True` + `writesubtitles`/`writeautomaticsub` for
  captions. Never download video or audio bytes. If this constraint ever
  needs to be broken, that's a design conversation, not a one-line patch.
- **`agent/critique.py` is the single source of truth for "is this
  curriculum good."** `agent/review.py` (online, gates one run) and
  `eval/judge.py` (offline, grades the test set) both import it. Do not
  write a second, separate quality-judgment prompt.
- **Free-text input fields (`goal`, `user_context.background`,
  `user_context.constraints`) are untrusted.** They flow into every
  downstream prompt. The scope-check gate (`agent/scope_check.py`, HLD §0)
  is a best-effort first filter, not a guarantee — don't treat it as
  having solved prompt-injection risk.

## Repo map (once built — see HLD §9 for the full layout)

```
agent/       schemas, graph (LangGraph wiring — nodes + conditional edges,
             matches HLD §1's diagram 1:1; enable_reviewer=false skips the
             REVIEW node entirely, HLD §6.6), scope_check, query_planning,
             discovery, transcripts, understanding, dedup, scoring,
             selection, narrative, critique, review, trace, followup,
             llm_client, cache, render
eval/        metrics.py (shared with review.py's anchoring), judge.py
             (imports agent/critique.py), run_eval.py (--compare-reviewer
             runs the test set with the flag both ways, HLD §6.6),
             human_calibration.md
test_set/    7 scenarios (HLD §7) — includes an out-of-scope/injection-
             shaped input, not just "hard but legitimate" cases
ui/          app.py — Streamlit front-end (HLD §5.2): imports run.py's
             entrypoint + agent/followup.py, no logic of its own
outputs/     gitignored run artifacts
Dockerfile, docker-compose.yml   optional packaging (HLD §9) — `pip
             install` + `python run.py` stays the documented default
```

`graph.py` is wiring only — it imports the plain functions in every other
`agent/` module as node bodies and defines the edges (including the
review loop's conditional one). No stage's actual logic lives in
`graph.py` itself, so every module is still unit-testable exactly as
described below, independent of LangGraph. `ui/app.py` is the same kind of
exception: a thin presentation layer, not a second implementation of any
pipeline logic.

## Non-goals

No UI beyond a thin Streamlit viewer/chat layer over data the CLI already
produces (HLD §5.2) — no accounts, no multi-session state, no pipeline
logic reachable only from the UI. No real deployment — cost/latency at
10K users/day is reasoned about on paper only (HLD §8), nothing is built to
serve that traffic. No comment-mining or non-YouTube discovery in the base
agent (flagged as first thing to add with more time).

## Working conventions

- Don't add error handling or fallbacks for scenarios that can't happen.
  Trust the schemas (`agent/schemas.py`) at internal boundaries; validate
  only at real system edges (raw `yt-dlp` output, raw LLM completions).
- Every stage is a typed function, unit-testable without a network call
  wherever the stage isn't inherently the LLM/network call itself
  (`scoring.py` and `selection.py` especially — these must be provably
  correct, not just plausible).
- If a change would add a new call site, a new model, a new dependency, or
  loosen a cap (iteration count, budget ceiling), check `SKILLS.md` first —
  it records why the current bound was chosen, so a change either has a
  new reason or it's scope creep.
