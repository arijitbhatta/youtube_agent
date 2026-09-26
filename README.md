# Learning Curriculum Builder

An agent that takes `(goal, time_budget_minutes, user_context)` and produces
a sequenced set of real YouTube videos — however many the goal and budget
actually call for, not a fixed count — grounded in their actual transcripts,
aimed at *this* learner rather than a generic one.

Full design rationale lives in [`APPROACH.md`](APPROACH.md) (the decisions
and why) and [`HLD.md`](HLD.md) (pipeline, data contracts, algorithms,
evaluation methodology, test set, cost model). [`SKILLS.md`](SKILLS.md) is
a running decision log of every non-obvious call made while building —
referenced throughout this README instead of repeated verbatim.

## Setup

Requires Python 3.12+ and an [OpenRouter](https://openrouter.ai) API key
(calls are OpenAI-Chat-Completions-compatible and proxy to Anthropic's
Claude models — see `agent/llm_client.py`). No YouTube API key is needed;
discovery and transcripts go through `yt-dlp` in download-free modes only
(`extract_info(download=False)`, caption fetch with `skip_download=True`).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in OPENROUTER_API_KEY
```

### Docker (optional convenience — not the primary path)

`Dockerfile` + `docker-compose.yml` package the same codebase into one
image with a `cli` service (one-shot) and a `ui` service (long-running),
sharing an `outputs/` volume so the sqlite understanding-cache and traces
are visible from both containers and the host:

```bash
docker compose run cli --input test_set/01_weekend_react_dev.json
docker compose up ui   # http://localhost:8501
```

**Honesty note:** Docker is not installed on the machine this was built on,
so this path is written to the same spec as the CLI/UI it wraps but was
**not** live-verified end-to-end the way everything else in this repo was.
The `pip install` + `python run.py` path above is the one that was actually
run, repeatedly, against the live API — treat it as the documented default,
per `APPROACH.md`'s "zero extra credentials, zero extra infra" promise.

## Usage

**Run one persona:**

```bash
python run.py --input test_set/01_weekend_react_dev.json
# --no-reviewer skips the review loop entirely (HLD §6.6 ablation) — zero extra calls/latency
```

Prints the rendered Markdown curriculum to stdout and saves a full
reasoning trace (every candidate, score, review iteration, and why
anything was cut) to `outputs/`.

**Run the eval harness against all seven `test_set/` scenarios:**

```bash
python -m eval.run_eval                          # full report → outputs/eval_report.json
python -m eval.run_eval --scenario 01_weekend_react_dev   # one scenario (repeatable flag)
python -m eval.run_eval --compare-reviewer        # reviewer on vs. off diff, HLD §6.6
python -m eval.run_eval --judge-runs 3            # LLM-judge variance runs per scenario (default 3)
```

**Streamlit UI** (thin wrapper only — imports `run.py`'s `run_from_payload`
and `agent/followup.py`'s `answer_question` directly; no second pipeline
implementation):

```bash
streamlit run ui/app.py
```

Run a new persona, run an existing `test_set/` scenario, or load a previous
trace from `outputs/`; then chat about the result ("why wasn't video X
picked?") in the same panel. Live-verified this way: the process was
actually launched (`streamlit run ui/app.py --server.headless true`) and
polled over HTTP, confirming a clean 200 response and no import errors.

**Tests** (deterministic core only — `scoring.py`, `selection.py`,
`eval/metrics.py` — no network, no LLM, exact-behavior asserts):

```bash
pytest -q   # 43 passed
```

## Design decisions worth knowing about

The full record is `SKILLS.md`; the ones most likely to raise a "why did
you do it that way" question:

- **No fixed video count.** Selection (`agent/selection.py`) is a greedy
  pass against the time budget with a coverage check and an infeasibility
  check, not "pick N videos." A tiny budget can legitimately produce a
  2-video curriculum with a flagged gap rather than padding to a count.
- **Asymmetric budget tolerance.** Slightly under budget is fine;
  meaningfully over budget is not — the greedy pass is tuned accordingly
  rather than treating both directions symmetrically.
- **`phase` (foundational/core/advanced) drives sequencing, not scoring.**
  It's assigned during understanding extraction and used to order the
  final selection, kept separate from the relevance/quality score so it
  can't distort what gets picked, only how picks are ordered.
- **`quality_signal` (view count, likes, channel size) is surfaced but
  deliberately excluded from the scoring formula.** It's a weak, gameable
  proxy for "this actually teaches the goal well" — shown to the learner
  as extra context, never allowed to outrank a better-grounded, lower-view
  video.
- **`yt-dlp` is used strictly download-free.** `extract_info(download=False)`
  for search/metadata, `skip_download=True` + `writesubtitles`/
  `writeautomaticsub` for captions — never video/audio bytes. This is a
  hard constraint (see `CLAUDE.md`), not a default that happened to be
  convenient.
- **The reviewer (`agent/review.py`) is a bounded, 3-iteration conditional
  edge in the graph, not an open "until satisfied" loop.** Every iteration
  is counted explicitly in graph state; the loop exits via that counter,
  never via LangGraph's `recursion_limit` (a framework default is not
  allowed to stand in for a business rule — see `CLAUDE.md` principle 5
  and `SKILLS.md` #11).
- **The scope-check gate (`agent/scope_check.py`) is a best-effort first
  filter on untrusted free text, not a guarantee.** `goal`,
  `user_context.background`, and `user_context.constraints` flow into every
  downstream prompt; the gate catches obviously off-topic/injection-shaped
  input (test scenario 7) but isn't treated as having solved prompt
  injection risk.
- **LangGraph is used as a control-flow engine only** — one process, no
  LangServe, no separate service. `graph.py` is wiring: it imports plain,
  independently-unit-testable functions from every other `agent/` module
  as node bodies and defines the edges, including the review loop's
  conditional one.
- **Caching (`agent/cache.py`) is keyed by a persona fingerprint, not just
  `video_id`**, which is a deliberate deviation from HLD's stated ideal
  (see "With more time" below).
- **The reviewer ablation (`--no-reviewer` / `--compare-reviewer`), the
  Streamlit UI, and Docker packaging were all done from a single "unblock
  and finish everything" trigger** (`SKILLS.md` #12) rather than three
  separate asks — recorded there so the reasoning behind bundling them
  isn't lost.
- **Fetch parallelization is a bounded thread pool** (`agent/config.py`'s
  `FETCH_MAX_WORKERS`), not an unbounded `ThreadPoolExecutor()` or asyncio
  rewrite — `yt-dlp` calls are I/O-bound and blocking, and a bounded pool
  was the smallest change that got real wall-clock improvement without a
  concurrency-model rewrite (`SKILLS.md` #13).

## What was actually verified, live

Per `CLAUDE.md` principle 4 ("evaluate what's verifiable; say plainly what
isn't"), this section is about what was *run*, not what was *designed*:

- Every pipeline stage (gate → query_planning → discovery → fetch →
  understanding → dedup → score → select → narrative → review → render)
  was exercised against the real OpenRouter/Claude API and real YouTube
  metadata/captions via `yt-dlp`, repeatedly, across all seven test-set
  scenarios plus the reference persona, not mocked at any point.
- The full 7-scenario eval report (`eval/run_eval.py`, `outputs/eval_report_full.json`)
  was run to completion against the live pipeline. Example measured cost
  from one scenario (`02_docker_tiny_budget`, a real completed run):
  27 pipeline LLM calls, ~82K input / ~26K output tokens, ~250s pipeline
  latency; the offline judge added 1 call, ~2.4K/1.2K tokens, ~15s — in
  the range `HLD.md` §8 estimated (~11-19 calls typical, ~25 worst case).
- Human-calibration reads (`HLD.md` §6.3): two real completed runs
  (`weekend_react_dev`, `docker_tiny_budget`) were read directly as
  rendered Markdown — blind, before consulting automated scores — and
  compared against `eval/metrics.py`'s §6.1 numbers and the judge's §6.2
  verdict. Agreement was strong on what shipped; the disagreements found
  are listed below because they're more informative than a bare "it
  matched."
- 43 unit tests (`scoring.py`, `selection.py`, `eval/metrics.py`) pass with
  zero network calls, asserting exact set/order behavior — the one part
  of the pipeline the design insists must be provably correct rather than
  merely plausible.
- Docker packaging was written to spec but **not** run — see the Setup
  section above.

## A real bug, found and fixed during the full eval run

Running the full 7-scenario eval report surfaced one genuine defect, not
a design tradeoff: `04_ml_from_scratch_infeasible` — a completely
legitimate goal ("learn ML from scratch") paired with a deliberately tiny
30-minute budget, specifically designed (`HLD.md` §7) to exercise
`selection.py`'s deterministic infeasibility warning — was instead being
hard-declined by the scope-check gate as out of scope, with the model's
own stated reason being budget infeasibility:

> "The learning goal is not achievable within the stated time budget...
> This represents an infeasible scope mismatch between ambition and
> available time."

That's a real defect, not the gate working as designed:
`agent/scope_check.py`'s prompt never asked the model to judge budget
feasibility at all — its rubric was only "empty / gibberish / unrelated /
injection-shaped." The model added an unrequested feasibility judgment on
its own, and because the gate's decision short-circuits the whole
pipeline, that judgment pre-empted the actual mechanism designed for
exactly this case: `selection.py`'s deterministic infeasibility check,
which is supposed to proceed and produce an honest warning rather than a
decline (`CLAUDE.md` principle 2: budget arithmetic is code, never LLM
judgment).

**Fix:** added one explicit sentence to the scope-check prompt: don't
judge scope by budget feasibility, an ambitious goal with a small budget
is still in scope, a separate step handles feasibility on its own. Fixed
in `agent/scope_check.py`.

**Verified live, before and after, not just re-read as code:**
- *Before:* `outputs/eval_report_full.json` (the original full run) shows
  `04_ml_from_scratch_infeasible` declined outright — `out_of_scope: true`,
  1 LLM call, no curriculum produced.
- *After:* re-running the same scenario through the real API
  (`outputs/eval_report_scenario04_postfix.json`) now reaches selection
  and ships a 4-video, budget-compliant curriculum with the intended
  honest signal instead: `unknown_topic_coverage_llm.fraction_covered:
  0.5`, and the reviewer's own unresolved blocking issues explicitly name
  the missing topics ("probability/statistics and Python for ML are not
  covered at all... clearly scope the curriculum to acknowledge these are
  out of budget"). This is exactly the graceful-degradation behavior the
  design intends for an infeasible budget, now actually reachable.
- 43 unit tests still pass after the fix (the change is a prompt-text
  edit only, no logic touched).

The rest of the original full report (`outputs/eval_report_full.json`) is
kept as-is rather than silently overwritten — it's the as-found evidence
for this bug and wasn't affected by the fix (no other scenario's `goal`/
`time_budget_minutes` combination was ambitious enough to trigger the
same false decline).

## Known operational characteristics (found by running the real thing)

These aren't bugs — each was traced to a specific, understood cause — but
they're real, live-observed behaviors worth knowing about rather than
discovering by surprise:

**A persistent YouTube caption-CDN 429 degrades two different checks via
the same root cause.** During this development/eval session, transcript
fetches for most/all selected videos returned HTTP 429 from YouTube's
caption CDN (rate-limited, not a code defect — `agent/transcripts.py`
already handles the failure by leaving `transcript: None` and flagging
`grounded: false`, exactly as designed). Two consequences follow from that
one root cause, not two independent problems:
1. `eval/run_eval.py`'s `grounding_rate` metric reads `None` (nothing to
   check against).
2. The same module's `_embedding_topic_coverage()` cross-check falls back
   to `sc.candidate.transcript or ""` when no transcript exists — so it
   ends up computing similarity against an empty string. Observed on a
   real run: the LLM self-reported `fraction_covered: 1.0` while the
   embedding cross-check reported `fraction_covered: 0.0` with every topic
   flagged as a disagreement (best similarities 0.011–0.109, far below the
   0.35 threshold). That's not two topics of genuine semantic disagreement
   — it's the same transcript-fetch failure showing up in a second metric.
   Read `grounding_rate: null` and a large embedding/LLM coverage gap
   together as one signal, not two.

**The reviewer's 3-iteration cap can be exhausted before every blocking
issue is resolved.** `agent/review.py`'s `route_after_review()` exits the
loop once `iterations >= 3` regardless of what the final verdict still
says — by design (`HLD.md` §3.9's "run ships anyway" rule: graceful,
transparent degradation over an open-ended "until satisfied" loop that
could burn arbitrary API budget on one run). The unresolved issues aren't
silently dropped — they surface verbatim in the rendered "Review" section
and `output.warnings` — but the curriculum does ship with them still
present. Concretely, both real completed runs inspected during human
calibration (`weekend_react_dev`, `docker_tiny_budget`) hit the full cap
with `approved: False`, not an iteration-1 approval. `SKILLS.md` entry #12
predicted the reviewer would "likely approve on iteration 1 most of the
time on legitimate input" — the two real runs actually observed complicate
that expectation, so this reports what was measured rather than what was
predicted.

**`--compare-reviewer`'s diff is confounded by discovery non-determinism.**
Running the same scenario twice (reviewer on vs. off) can hit a different
YouTube search result set each time, since discovery isn't seeded/cached
across the two runs — already noted as a caveat in
`eval/run_eval.py`'s `_diff_reviewer_effect()` docstring. A diff between the
two runs can therefore reflect "discovery found different candidates" as
much as "the reviewer changed the outcome"; read the ablation report below
with that in mind rather than as a clean causal comparison.

## Reviewer ablation, measured (`--compare-reviewer`, HLD §6.6)

Ran live (`outputs/eval_report_compare_reviewer.json`) across
`01_weekend_react_dev`, `02_docker_tiny_budget`,
`05_git_basics_high_duplication`, and `07_out_of_scope_injection` (the
fourth confirms the reviewer never runs at all for an out-of-scope
decline — cheap to verify, no diff to report). Given the discovery-noise
confound above, the picked-video-set diffs aren't a clean causal signal by
themselves, but one number *is* clean regardless of which candidates
discovery happened to find: the offline judge's own issue count, run
identically against whatever each arm shipped.

| scenario | picks with/without | judge issues with | judge issues without | reviewer's extra LLM calls |
|---|---|---|---|---|
| 01_weekend_react_dev | 13 / 22 | 16, 14, 15 | 20, 20, 18 | 7 |
| 02_docker_tiny_budget | 4 / 6 | 3, 3, 4 | 7, 6, 7 | 1 |
| 05_git_basics_high_duplication | 11 / 8 | 8, 8, 9 | 11, 10, 9 | 7 |

The judge's issue count is lower with the reviewer on, in every judge run,
across all three scenarios — a consistent effect even though it never
moved `judge.approved_rate` off `0.0` in either arm (both variants still
had issues left for the judge to flag; see the review-cap finding above).
The pick-count direction isn't consistent (fewer picks with the reviewer
on for 01/02, more for 05) — expected, given 05's `05_git_basics_high_duplication`
review-exclusion finding above shows the reviewer actively trading picks
in and out, not just trimming. So: the reviewer measurably reduces
judge-visible issues on every scenario tried, at a real and scenario-
dependent extra-call cost (1-7 calls here), without being confounded by
which candidates discovery happened to surface — that part of the
question has a clean answer even though the exact picked-video-set diff
does not.

**The review loop's exclusion mechanism can silently regress topic
coverage, and nothing downstream catches it.** Found on the dedup stress
scenario (`05_git_basics_high_duplication`): the rendered "Goal coverage"
table marked `working with a shared remote/PRs: uncovered` despite three
selected videos whose own narrative reasoning clearly describes PR/
remote-workflow content. Root cause, traced through the saved trace: the
coverage check (both `selection.py`'s coverage-forcing swap and
`render.py`'s `_goal_coverage()`) trusts only one field —
`understanding.matches_unknown`, an LLM judgment made per-candidate, in
isolation, during the earlier understanding stage — which disagreed with
the later, independent narrative-stage reasoning about the exact same
videos. Only 2 of 80 discovered candidates were ever tagged with that
exact topic string, and the review loop excluded *both* of them across
iterations 1-2, for otherwise-legitimate reasons (ungrounded, duplicate,
constraint-violating). Once gone, the coverage-forcing swap had nothing
left to force in and silently no-op'd; the infeasibility check never
fired either, since it only looks at aggregate coverage across all topics
(3 of 4 = 0.75, well above the 0.34 threshold) — losing one whole topic
isn't enough to trip an aggregate-only check. Net effect: the review loop
traded topic coverage for grounding/redundancy quality, and the only place
that trade-off becomes visible is a table a reader has to actively notice
and cross-reference against the curriculum by hand, as was done here.

**A fuzzy title-match false positive was found and fixed in
`agent/followup.py`.** The chat follow-up's "why wasn't video X picked"
path matches on video title; an early version's matching was loose enough
to match an unrelated video sharing a common word. Fixed with a
word-overlap check before falling back to substring matching.

**Caching is keyed by a persona fingerprint, not just `video_id`.**
`HLD.md` §8's stated ideal is a pure `video_id → understanding record`
cache, reusable across any persona that discovers the same video. The
shipped `agent/cache.py` keys on `(video_id, persona fingerprint)` instead
— see "With more time" below for why, and what the fix looks like.

## What I'd do with more time

- **Split the cache key.** Understanding extraction (what does this video
  teach, is it grounded) doesn't depend on the persona and could be a pure
  `video_id`-keyed cache reused across every run, matching `HLD.md` §8's
  original design. The persona-dependent part (relevance to *this* goal)
  is what actually needs to vary per persona. Splitting these into two
  cache tables would let the expensive, persona-independent half of
  understanding be reused across the whole test set instead of recomputed
  per scenario.
- **Comment-mining / non-YouTube discovery sources**, flagged in
  `APPROACH.md`'s non-goals as the first thing to add — comment sentiment
  as a second, independent quality signal alongside the transcript-grounded
  one.
- **Re-run the coverage check after review exclusions land, using the
  full remaining candidate pool rather than trusting `matches_unknown`
  alone.** The dedup-scenario finding above is fixable two ways that could
  be combined: (a) have the infeasibility check flag a *per-topic* drop to
  zero, not just an aggregate-fraction floor, so losing one whole topic
  can't hide behind the others still being covered; (b) reconcile
  `understanding.matches_unknown` against the narrative stage's own
  reasoning about the same candidate before treating a topic as
  uncovered, since the two stages visibly disagreed on this run.
- **Make the review loop's exit path visible sooner.** Given both real
  runs inspected hit the 3-iteration cap, it's worth surfacing "iteration
  2 of 3, N blocking issues remain" as a warning during the run rather
  than only after the fact, so exhausting the cap reads as a known risk
  in progress rather than a silent final-state discovery.
- **Seed or cache discovery results across a `--compare-reviewer` run**
  so the ablation diff isolates the reviewer's effect instead of being
  confounded by a different candidate set on each pass.
- **Get past the YouTube caption-CDN 429** — either with authenticated
  caption requests or a longer backoff/retry policy — since it's currently
  the single biggest blind spot in this session's own eval numbers
  (`grounding_rate` and the embedding-coverage cross-check both go dark
  under it, as documented above).
