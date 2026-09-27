# Learning Curriculum Builder

An agent that takes `(goal, time_budget_minutes, user_context)` and produces a
sequenced set of real YouTube videos — **however many the goal and budget
actually call for, not a fixed count** — grounded in their actual transcripts,
aimed at *this* learner rather than a generic one.

This README is the orientation + runbook + results. The reasoning lives
elsewhere: [`APPROACH.md`](APPROACH.md) (the decisions and why),
[`HLD.md`](HLD.md) (the full design: pipeline, data contracts, algorithms,
evaluation, test set, cost model), [`SKILLS.md`](SKILLS.md) (a running log of
every non-obvious call made while building). Read those for depth; read this
one to know what was done, what it scored, and how to run it.

---

## What we solved

The spec's hard problem, restated: **YouTube search rank and view counts
optimize for popularity, not for whether a video actually gets *this*
learner to *this* goal.** A beginner React video and an advanced React
deep-dive both rank well for "React"; metadata can't tell them apart. The
agent therefore has to read what a video *actually teaches* — its transcript,
not its title — and weigh that against what the learner already knows,
doesn't know, and explicitly doesn't want, within a hard time budget.

The answer is a single-process, LangGraph-wired pipeline of named stages:

```
scope-check gate → query planning → discovery → transcript fetch →
content understanding (grounded) → dedup clustering → deterministic scoring →
constrained selection + sequencing → narration → review loop (≤3) → render
```

Every stage is a typed, independently-unit-testable module. The parts that
must be *correct* (budget arithmetic, selection, dedup) are deterministic
code; the parts that must be *smart* (extraction, narration, review) are LLM
calls. The full reasoning trace — every candidate, every score, every review
iteration, why anything was cut — is a first-class output artifact.

## How we solved it

The load-bearing design decisions, in one line each (fuller reasoning in
`APPROACH.md` / `HLD.md`):

- **No fixed video count.** Selection is a greedy coverage pass against the
  budget, not "pick N videos." A tiny budget legitimately yields 2 picks with
  a flagged gap; a broad one can yield 10.
- **Deterministic where it matters, generative where it helps.** Budget
  arithmetic, set selection, and dedup clustering are code. LLMs extract
  (what does this video teach) and narrate (explain a decision already
  made) — they never do silent arithmetic, and the reviewer can't override a
  deterministic metric failure with opinion.
- **Grounded over generic.** Every inclusion reason must cite a real
  transcript excerpt. No transcript → `grounded: false` + lowered confidence,
  never silently upgraded to a confident claim.
- **A bounded reviewer loop, not "until satisfied."** A second Claude call
  (`agent/review.py`) reviews the draft against the same deterministic
  metrics the eval uses, routes targeted fixes back to selection or
  narration, and is hard-capped at 3 iterations by a counter in graph state —
  never by LangGraph's `recursion_limit`.
- **Reviewer and judge share one rubric** (`agent/critique.py`) — no second
  quality system. The reviewer gates one live run; the offline judge grades
  the test set. Same prompt, two call sites.
- **`yt-dlp` is used strictly download-free.** Search/metadata via
  `extract_info(download=False)`, captions via `skip_download=True`. Video
  and audio bytes are never fetched — a hard constraint, not a convenience.
- **One reviewer on/off flag** (`--no-reviewer`) gives a first-class ablation
  (`--compare-reviewer`) instead of two codebases.
- **No infra that has no concurrent consumer.** No queues, no workers, no
  managed services. One process, one CLI entry point, an optional Streamlit
  viewer and an optional Docker wrapper on top of the identical code.

## How to run it

### Prerequisites

- Python 3.12+
- An [OpenRouter](https://openrouter.ai) API key. Calls are
  OpenAI-Chat-Completions-compatible and proxy to Anthropic's Claude models
  (`agent/llm_client.py`). No YouTube API key is needed — discovery and
  transcripts go through `yt-dlp` / `youtube-transcript-api` in download-free
  modes.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in OPENROUTER_API_KEY
```

### Run one persona

```bash
python run.py --input test_set/01_weekend_react_dev.json
# --no-reviewer skips the review loop entirely (HLD §6.6 ablation) — zero extra calls/latency
```

Prints the rendered Markdown curriculum to stdout and saves a full reasoning
trace (every candidate, score, review iteration, why anything was cut) to
`outputs/`.

### Run the eval harness

```bash
python -m eval.run_eval                          # all 7 test_set/ scenarios → outputs/eval_report.json
python -m eval.run_eval --scenario 01_weekend_react_dev   # one scenario (repeatable)
python -m eval.run_eval --judge-runs 3           # LLM-judge variance runs per scenario (default 3)
python -m eval.run_eval --compare-reviewer       # reviewer on vs. off diff (HLD §6.6)
```

Each number in the report comes from a **real completed run** — real `yt-dlp`
discovery, real API calls — never mocked. Expect ~20–26 pipeline LLM calls
and ~4–5 minutes wall-clock per in-scope scenario.

### Streamlit UI

A thin presentation layer over the same code (imports `run.py`'s
`run_from_payload` and `agent/followup.py` directly — no second pipeline):

```bash
streamlit run ui/app.py
```

### Tests

Deterministic core only — `scoring.py`, `selection.py`, `eval/metrics.py`,
plus the pure helpers — no network, no LLM, exact-behavior assertions:

```bash
pytest -q   # 73 passed
```

### Docker (optional convenience — not the primary path)

`Dockerfile` + `docker-compose.yml` package the same codebase into one image
with a `cli` service (one-shot) and a `ui` service (long-running), sharing an
`outputs/` volume so the sqlite cache and traces are visible from both:

```bash
docker compose run cli --input test_set/01_weekend_react_dev.json
docker compose up ui   # http://localhost:8501
```

`pip install` + `python run.py` remains the documented default — the spec's
"zero extra credentials" promise is a feature, not an accident.

---

## The evaluation 
### What it measures

`eval/run_eval.py` runs every `test_set/` scenario through the real pipeline
and grades it three independent ways:

1. **Automated, reference-free metrics** (`eval/metrics.py`) — budget
   compliance, curriculum-shape sanity, unknown-topic coverage, known-topic
   leakage, constraint compliance, redundancy, grounding rate, sequencing
   sanity, content quality. These are deterministic checks, several of which
   the runtime reviewer is anchored on.
2. **LLM-as-judge** (`eval/judge.py`) — one `agent/critique.py` rubric asked
   "would following this get the learner to their goal," run 3× per scenario
   for variance.
3. **Human calibration** (`eval/human_calibration.md`) — blind reads of
   rendered output, compared against the automated scores, disagreements
   documented rather than tightened away.

Plus a **reviewer ablation** (`--compare-reviewer`) that runs every scenario
with the review loop on and off, to answer "does that extra call earn its
cost" with data.

### Results (all live runs)

| Scenario | Picks | Over budget | LLM coverage | Redundancy (dup clusters) | Review (iters / approved) | Judge approved | Pipeline cost |
|---|---|---|---|---|---|---|---|
| 01 weekend_react_dev | 10 | no | 1.00 (5) | 0 | 3 / no | 0.0 (12,11,17) | 26 calls · 89k in / 32k out · 315s |
| 02 docker_tiny_budget | 5 | no | 1.00 (3) | 0 | 3 / no | 0.0 (4,4,5) | 18 calls · 65k / 22k · 237s |
| 03 pytorch_advanced_narrow | 9 | no | 0.75 (4) | 0 | 3 / no | 0.0 (13,11,12) | 21 calls · 66k / 25k · 262s |
| 04 ml_from_scratch_infeasible ✅ | 3 | no | 0.8 | 0 | 3 / no | 0.0 (5,4,5) | 13 calls · 26k / 7k · 90s |
| 05 git_basics_high_duplication | 11 | no | 0.75 (4) | 0 | 3 / no | 0.0 (6,7,7) | 25 calls · 78k / 26k · 273s |
| 06 accessible_content_constraint | 6 | no | **0.00** (4) | 0 | 3 / no | 0.0 (12,13,14) | 25 calls · 85k / 32k · 295s |
| 07 out_of_scope_injection | — | declined ✅ | — | — | — | — | 1 call · 0.9k / 0.1k · 2s |

*Coverage = fraction of the learner's `unknown` topics self-reported covered.
Rows 01–03, 05–07 are from the original full run
(`outputs/eval_report_full.json`); row 04 is the post-fix re-run
(`outputs/eval_report_scenario04_07_postfix.json`).*

**How to read this table.** Each row is one test-set input, written to stress
a specific failure mode; each column is a question the eval asks of whatever
shipped for that row.

- **Picks** — videos in the final curriculum. There is no target count
  anywhere in the design, so the spread (5–11) is the point, not noise. `—`
  means no curriculum was produced at all.
- **Over budget** — the hard structural guarantee (`total_minutes ≤ budget`).
  Any "yes" would be a code bug, not a judgment call; it held everywhere.
- **LLM coverage** — the model's *self-report* on how many of the learner's
  `unknown` gaps got addressed (`fraction_covered`, with the total topic count
  in parentheses). A claim, not a fact.
- **Redundancy** — near-duplicate clusters among picks. `0` = the cluster
  check found none; note it can still miss the semantic overlap the reviewer
  and the human both caught on row 5.
- **Review (iters / approved)** — the runtime reviewer (`agent/review.py`).
  `3 / no` = it burned its full 3-iteration cap and still shipped
  `approved: False`, with unresolved issues surfaced verbatim. Every in-scope
  row did this.
- **Judge approved** — the offline judge (`eval/judge.py`), 3× per scenario.
  `0.0 (a,b,c)` = rejected all three runs, with `a,b,c` the issue count from
  each independent run.
- **Pipeline cost** — measured, not estimated: calls · input/output tokens ·
  wall-clock. Row 7 (`1 call · ~2s`) is the scope-check gate short-circuiting
  the entire run — correctly, for the injection-shaped input.

### What the results actually say, honestly

**The structural guarantees held.** Budget was never exceeded in any of the
six in-scope scenarios (overage is structurally impossible by design, and
none occurred). Selection never produced a fragmented or one-dominant
curriculum, sequencing showed zero inversions, and dedup produced zero
duplicate clusters. The deterministic parts did what they were built to do.

**The quality signals are the weak part — and this run says so plainly,
not apologetically.** The judge's `approved_rate` was `0.0` in every
scenario, and the runtime reviewer exhausted its 3-iteration cap on every
in-scope run while still shipping `approved: False`. Constraint compliance
failed on scenarios 1, 3, and 6 (7, 2, and 6 violations — "no theory /
no surface intros" constraints being violated). This is a real finding: the
pipeline's extraction-and-review quality bar is not being met on the test
set, and the eval is reporting it rather than massaging it.

**The grounding metric went dark this session.** A persistent YouTube
caption-CDN **429** left most/all fetched transcripts empty (rate-limited,
not a code defect — `agent/transcripts.py` degrades to `transcript: None` as
designed), so `grounding_rate` is `None` everywhere — nothing to check
against.

**A real bug, found and fixed — and the table above reflects the fix.** On
the original full run, scenario 4 (`04_ml_from_scratch_infeasible`) — a
legitimate goal with a deliberately tiny 30-minute budget, designed to
exercise selection's *deterministic* infeasibility warning — was wrongly
**hard-declined by the scope-check gate**, with the model's own reason being
budget infeasibility. That was a defect: the gate's rubric is only "empty /
gibberish / unrelated / injection-shaped," and budget feasibility is supposed
to be handled downstream by code (`agent/selection.py`), not the gate. Fixed
with one explicit sentence in the scope-check prompt (`agent/scope_check.py`):
don't judge scope by budget feasibility.

The original decline is kept as as-found evidence in
`outputs/eval_report_full.json`; **row 04 above is the post-fix re-run**
(`outputs/eval_report_scenario04_07_postfix.json`). It now passes the gate,
runs the full pipeline, and ships a 3-video curriculum with the budget
respected (10.7% under) that degrades honestly, with the reviewer naming
the missing topics — instead of a hard decline.
`07_out_of_scope_injection` was re-run in the same pass as a regression check
and still declines correctly, so the prompt change did not loosen the
injection catch.

### Reviewer ablation (reviewer on vs. off, `outputs/eval_report_compare_reviewer.json`)

| Scenario | Picks with / without | Judge issues with | Judge issues without | Reviewer's extra LLM calls |
|---|---|---|---|---|
| 01_weekend_react_dev | 13 / 22 | 16, 14, 15 | 20, 20, 18 | 7 |
| 02_docker_tiny_budget | 4 / 6 | 3, 3, 4 | 7, 6, 7 | 1 |
| 05_git_basics_high_duplication | 11 / 8 | 8, 8, 9 | 11, 10, 9 | 7 |

**How to read this table.** Only three rows, because the fourth candidate
(`07_out_of_scope_injection`) is out-of-scope — the reviewer never runs in
either arm, so there is nothing to diff. Two of the columns must be read
differently:

- **Picks with / without** — *confounded, read as noise.* The counts also
  differ from Table 1 because each arm is a fresh run and discovery is
  non-deterministic, so a pick-set delta may be the reviewer's effect or just
  a different candidate pool. The direction isn't even consistent (the
  reviewer *trims* 01 and 02 but *adds* on 05) — don't read this as pure
  causal.
- **Judge issues with / without** — *the clean signal.* The judge runs
  identically against whatever each arm shipped, so its verdicts compare
  directly. It's lower with the reviewer on in every run, across every
  scenario.
- **Reviewer's extra LLM calls** — the price of that improvement (1–7 calls,
  scenario-dependent).

The reviewer reduces the judge's issue count in **every judge run, across
all three scenarios** — a clean signal that survives the one confound (each
arm runs discovery independently, and discovery is non-deterministic, so the
*pick-set* diffs are noise; the judge's issue count is not, since it's run
identically against whatever each arm shipped). Note the reviewer *still*
never drove `approved_rate` off `0.0` — it cuts issues but doesn't eliminate
them.

### Human calibration (highlights)

Blind reads of `01_weekend_react_dev`, `02_docker_tiny_budget`, and
`05_git_basics_high_duplication` matched the trace's own self-report closely,
and independently caught one thing the metrics miss: the review loop can
**silently regress topic coverage** while improving grounding/redundancy.
On the dedup stress scenario, `working with a shared remote/PRs` was marked
`uncovered` despite three selected videos that plainly cover it — traced to
the coverage check trusting a single conservative LLM field
(`matches_unknown`) that disagreed with the pipeline's own later narrative
reasoning. Full write-up in `eval/human_calibration.md` and "What I'd do with
more time" below.

---

## Directory / file map

```
youtube_agent/
├── run.py                     # CLI entry point: `python run.py --input <persona.json>`
├── requirements.txt           # deps; openai (OpenRouter), langgraph, yt-dlp, youtube-transcript-api, streamlit
├── .env.example               # template: OPENROUTER_API_KEY + model slugs + cache path (→ copy to .env, never committed)
├── .gitignore
├── Dockerfile                 # one image with the full agent + eval + ui
├── docker-compose.yml         # `cli` (one-shot) and `ui` (long-running) services sharing outputs/
├── APPROACH.md                # the decisions and why — read first
├── HLD.md                     # full design: pipeline, contracts, algorithms, eval, test set, cost model
├── SKILLS.md                  # running decision log of non-obvious calls
├── agent/                     # the pipeline, one module per stage
│   ├── schemas.py             #   §2 data contracts (pydantic) — also what graph state accumulates
│   ├── graph.py               #   LangGraph wiring only: nodes = stages, edges = §1 diagram + review's conditional edge
│   ├── scope_check.py         #   §0 guardrail — cheap in-scope/injection check, can short-circuit the run
│   ├── query_planning.py      #   §3.1 one Claude call: goal+context → 4–5 search queries
│   ├── discovery.py           #   §3.2 yt-dlp search (ytsearchN:), merge+dedupe+candidates hard-capped at 40
│   ├── transcripts.py         #   §3.3 metadata + transcript fetch — download-free (youtube-transcript-api for captions)
│   ├── understanding.py       #   §3.4 the grounding step — batched calls → per-video UnderstandingRecord (phase included)
│   ├── dedup.py               #   §3.5 embedding clustering (hosted text-embedding-3-small), union-find by cosine threshold
│   ├── scoring.py             #   §3.6 deterministic composite utility — pure arithmetic
│   ├── selection.py           #   §4 constrained selection + sequencing — greedy + coverage-forcing swap + infeasibility check
│   ├── narrative.py           #   §3.8 one strong-tier call narrating decisions already made
│   ├── critique.py            #   §3.9/§6.2 shared "is this good" rubric — single source of truth
│   ├── review.py              #   §3.9 bounded review loop (≤3, counter in state), calls critique.py
│   ├── trace.py               #   §2.6 reasoning-trace read/write, incl. review iterations
│   ├── followup.py            #   §5.1 "why not video X" Q&A over the trace
│   ├── render.py              #   §3.10 JSON + Markdown output from one source of truth
│   ├── llm_client.py          #   thin instrumented wrapper (tokens/latency), model tiering
│   ├── cache.py               #   sqlite understanding cache, keyed by (video_id, persona)
│   ├── run_cache.py           #   UI-only exact-match run cache (same sqlite file, second table)
│   ├── config.py              #   config-over-code seam: model slugs + cache path, read once from .env
│   ├── progress.py            #   UI-only: internal stage names → learner-facing labels
│   └── recommendations.py     #   UI-only: "while you waited" books (Google Books) + course search links
├── eval/
│   ├── metrics.py             # §6.1 automated metrics — also what review.py anchors blocking issues on
│   ├── judge.py               # §6.2 LLM-as-judge, 3× variance — imports agent/critique.py
│   ├── run_eval.py            # harness: runs test_set/, emits report; --compare-reviewer diffs reviewer on/off
│   └── human_calibration.md   # §6.3 blind reads + agreements/disagreements
├── test_set/                  # 7 scenarios (HLD §7), each targeting one failure mode
│   ├── 01_weekend_react_dev.json           # happy path (reference persona)
│   ├── 02_docker_tiny_budget.json          # beginner + tiny budget → honesty over padding
│   ├── 03_pytorch_advanced_narrow.json     # advanced narrow + post-2023 constraint
│   ├── 04_ml_from_scratch_infeasible.json  # deliberately infeasible budget → graceful degradation
│   ├── 05_git_basics_high_duplication.json # dedup stress: hundreds of near-identical topics
│   ├── 06_accessible_content_constraint.json # constraint beyond style (captions/accessibility)
│   └── 07_out_of_scope_injection.json      # scope-check gate: off-topic + injection-shaped input
├── tests/                     # 73 deterministic tests — no network, no LLM
│   ├── factories.py           #   shared test fixtures
│   ├── test_schemas.py, test_scoring.py, test_selection.py, test_metrics.py   # the provably-correct core
│   ├── test_cache.py, test_run_cache.py    # cache behavior
│   ├── test_followup.py, test_narrative.py, test_render.py, test_recommendations.py, test_progress.py
├── ui/
│   └── app.py                 # §5.2 Streamlit viewer — imports run.py + followup.py, no logic of its own
└── outputs/                   # gitignored — traces, rendered output, eval_report_*.json, cache.sqlite3
```

---

## What I'd do with more time

- **Split the cache key.** Understanding extraction (what does this video
  teach) is persona-independent and could be a pure `video_id` cache reused
  across runs; only relevance is persona-dependent. Splitting them would make
  the expensive half reusable across the whole test set.
- **Get past the caption-CDN 429** — authenticated caption requests or a
  longer backoff/retry — since it's the single biggest blind spot in this
  session's eval numbers.
- **Re-run coverage after review exclusions land**, and reconcile
  `understanding.matches_unknown` against the narrative stage's own reasoning,
  so losing one whole topic can't hide behind an aggregate-only check (the
  dedup-scenario finding).
- **Make the review loop's exit path visible sooner** — "iteration 2 of 3, N
  blocking issues remain" — since every real run hit the cap.
- **Seed/cache discovery across `--compare-reviewer`** so the ablation diff
  isolates the reviewer's effect instead of discovery noise.
- **Comment-mining as a second, third-party quality signal** — the one lever
  that would close the grounding/quality gap the rest of the pipeline can't.