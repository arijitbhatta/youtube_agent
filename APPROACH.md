# Learning Curriculum Builder — Approach

This file is the elevator pitch and the decisions; `HLD.md` is the detailed
design. Nothing here is code yet — this is the plan.

## The problem, restated

Given `(goal, time_budget_minutes, user_context)`, produce a sequenced set of
YouTube videos, totaling within the budget, that would actually get *this*
learner (not a generic one) to the goal. How many videos that takes is an
outcome of the goal's scope and the budget, not a fixed target — the spec's
own reference sketch happens to land on 4–6 for its example persona, which is
a property of that example, not a constraint on the agent (see "Scope
decisions" below). The hard part is explicit in the spec:
search rank and view count optimize for popularity, not fit. Fit requires
reading what a video actually teaches — transcript, not metadata — and
weighing that against what the learner already knows, doesn't know, and
explicitly doesn't want.

## Design principles, in priority order

1. **Grounded over generic.** Every inclusion reason must trace to something
   verifiably in the transcript, not a paraphrase of the title. If no
   transcript is available, the pick is still allowed but is flagged
   low-confidence and metadata-only — never silently upgraded to a confident
   claim.
2. **Deterministic where it matters, generative where it helps.** Budget
   arithmetic, set selection, and dedup clustering are code, not LLM output —
   LLMs are unreliable at "make these numbers sum to X." The LLM's job is
   extraction (what does this video actually cover) and narration (explain a
   decision that was already made deterministically), never silent
   arithmetic.
3. **Explainable by construction.** The full reasoning trace — every
   candidate considered, its score, and why it was cut — is a first-class
   output artifact, not something reconstructed after the fact for the
   README. This is also what makes the optional follow-up Q&A almost free.
4. **Evaluate what's verifiable; say plainly what isn't.** The eval is the
   deliverable weighted most heavily. It should measure real things
   (budget adherence, coverage, redundancy, grounding) and state its blind
   spots rather than paper over them with an LLM-judge score.
5. **Simple over impressive.** This is graded partly on *not*
   over-engineering. There is no concurrency or multi-tenant requirement
   here, so there are no queues, workers, or managed infra — that
   machinery earns its keep in systems with concurrent users and shared,
   expensive infrastructure to protect. This one has neither. Complexity
   budget goes into the evaluation, not the plumbing.

## Engineering discipline this design leans on

None of the following requires distributed infrastructure to justify it —
these are habits that make an LLM pipeline debuggable and cheap, applied at
whatever scale the task actually has:

| Discipline | Applied here | Why |
|---|---|---|
| Named, independently-testable stages | discovery → transcript fetch → understanding → dedup → selection → narration → review, as separate modules, each with a typed input/output, wired together as LangGraph nodes but individually unit-testable without the graph | The right way to make an LLM pipeline debuggable — a bad output should point at exactly which stage produced it, not require re-reading the whole run |
| Right-sized model per step | Cheaper/faster model for the high-volume per-video extraction step and the scope-check gate; reserve the stronger model for the one narration/judgment/review step | Capability should be spent where it's actually load-bearing, not applied uniformly to every call regardless of how hard the task is |
| Separate "the LLM call" from "the business logic around it" | `llm_client.py` is a thin, instrumented wrapper; nothing upstream of it does raw string parsing of a completion when a structured extraction would do | Don't let the slow, unreliable part hide inside logic that should be simple and testable |
| Cost/scaling reasoning, on paper | Applied only in the cost section of the HLD (caching per `video_id`, model tiering, 10K-users/day extrapolation) | There's no real traffic here; building actual cache/queue infra now would be exactly the over-engineering the spec warns against — reasoning about cost still matters even when nothing is deployed |
| Config-over-code seams | Discovery/transcript source is a swappable interface (`yt-dlp` now; YouTube Data API v3 documented as a drop-in alternative) selected by config, not a rewrite | The boundary that's most likely to need to move (search/caption reliability) is decided as a seam up front |
| No infrastructure without a concurrent consumer to justify it | No queue, no workers, no retries-across-processes | Nothing here has concurrent consumers that would need it. A plain `try`/retry-twice inside one function covers the actual failure mode (a flaky network call) |

The short version: the discipline (named stages, config seams, separating
unreliable calls from logic, thinking in cost-per-request) is worth having
regardless of scale; the infrastructure (workers, queues, managed
databases) is not, at this scale. This task's scope doesn't justify the
latter, and the spec says so directly ("clean simple code... not a clever
framework").

## Scope decisions

- **Discovery + metadata: `yt-dlp`. Transcript: `youtube-transcript-api`.**
  Zero extra credentials beyond the Claude key already provided — anyone
  can `pip install` and run with nothing else to provision. `yt-dlp` gives
  search (`ytsearchN:`) and full metadata; the transcript itself comes from
  `youtube-transcript-api`, a dedicated library talking to YouTube's caption
  endpoint directly rather than through `yt-dlp`'s caption-URL indirection —
  tried as a second attempt at dodging the caption-CDN throttling documented
  in `SKILLS.md`/`README.md`; live-confirmed to still hit the same IP-level
  block (`IpBlocked`), so this is a code-path swap, not a fix for that
  specific environment issue. The YouTube Data API v3 is documented in the
  HLD as a drop-in alternative behind the same interface, for if `yt-dlp`'s
  search proves rate-limited or unreliable in practice.
- **Output:** both a structured JSON (for the eval harness and any future
  UI) and a rendered Markdown curriculum (for a human to read). One run, two
  artifacts, same source of truth.
- **Interface:** a single CLI command, one process, no server — `python
  run.py --input test_set/weekend_react_dev.json` — remains the primary,
  always-available interface. A thin Streamlit front-end (`HLD.md` §5.2) is
  added on top of it, not instead of it: this is an explicit ask, not
  something the spec itself requires, so it's kept deliberately minimal —
  it imports `run.py`'s entrypoint and `agent/followup.py` directly, adds no
  new pipeline logic, and exists to make "chat with the output" (why was
  video X skipped, why is this ordered before that) interactive instead of
  one CLI invocation per question. Building a real web app with accounts,
  multi-session state, or a service boundary would still be exactly the
  over-engineering the grading criteria discount; a local single-session
  Streamlit view over data the CLI already produces isn't that.
- **Every run persists its reasoning trace to disk.** That trace is what the
  eval harness scores against, and it's the entire mechanism behind the
  optional follow-up Q&A (§5 of `HLD.md`) — no new retrieval needed to
  answer "why not video X," just a lookup into what was already computed.
- **A bounded reviewer loop, not a multi-agent framework.** Before the
  final output ships, a second Claude call reviews the draft curriculum
  against the same deterministic metrics the eval harness uses (§3.9 of
  `HLD.md`) and can send specific, targeted feedback back to selection or
  narration — capped at 3 iterations, never an open-ended "until
  satisfied" loop. It's built as one shared critique module reused by both
  the runtime reviewer and the offline `eval/judge.py`, not two separate
  quality systems, and as a plain bounded loop in one process, not an
  agent-orchestration layer — keeping it consistent with principle 5
  despite adding a genuinely agentic piece to the pipeline.
- **LangGraph as the control-flow engine, not a new architecture.** The
  pipeline in `HLD.md` §1 was already drawn as a graph — named stages as
  nodes, the review loop as a conditional edge back to selection or
  narration. LangGraph implements exactly that graph; it doesn't add a
  queue, a worker, or a separate service — the graph still runs inside
  the one CLI process this design already commits to. The one thing this
  requires being explicit about: the review loop's 3-iteration cap
  (`HLD.md` §3.9) is enforced by a counter in the graph's own state,
  checked in the conditional edge, not by leaning on LangGraph's generic
  `recursion_limit` — that's a safety net against a genuinely broken
  graph, not the mechanism for a product-level bound.
- **Video count is an outcome, not a target.** The selection algorithm
  (§4 of `HLD.md`) has no minimum or maximum pick count anywhere in it —
  it stops adding videos when the budget runs out or nothing left is
  genuinely non-redundant, and it doesn't pad to reach a floor either. A
  narrow goal with a tight budget can validly produce two picks; a broad
  goal with a generous budget can validly produce ten. The reference
  persona's 4–6 is what that specific goal/budget combination happens to
  produce, not a rule baked into the algorithm.
- **A scope-check gate in front of everything (§0 of `HLD.md`).** One
  cheap Claude call — the same cheap tier already used for the
  high-volume understanding step, not a new model — classifies whether
  the input is a learning goal this tool can act on before anything else
  runs. Off-topic or malformed input gets a short, templated, polite
  decline instead of a hallucinated curriculum, and it costs nothing
  extra: the ~10-25 downstream calls simply never run. It doubles as a
  first, best-effort line of defense against prompt injection through the
  input's free-text fields, which flow directly into every later prompt —
  stated honestly as best-effort, not a guarantee.
- **A reviewer on/off comparison as a first-class eval deliverable
  (`HLD.md` §6.6).** Not two codebases — one `enable_reviewer` flag that
  removes the `REVIEW` node from the graph entirely when off (§3.9's loop
  never runs, zero extra calls). `eval/run_eval.py --compare-reviewer` runs
  the full test set under both settings and diffs §6.1's metrics, §6.2's
  judge score, and §6.3's human calibration between them — this is also
  where the spec's own ask to "surface places where the eval and human
  judgment disagree" gets applied to a concrete design choice (is the
  reviewer worth its cost) instead of only to the pipeline's output.
- **Docker as an optional packaging convenience, not a requirement.** The
  documented default remains `pip install -r requirements.txt` — that's
  what the zero-extra-credentials discovery decision above already
  promises, and a hard Docker dependency would undercut it. A `Dockerfile`
  + `docker-compose.yml` are provided as a secondary path (`docker compose
  run cli`, `docker compose up ui`), useful now that there are two entry
  points (CLI, Streamlit) sharing one dependency set and one on-disk cache —
  but both paths run the identical code, so nothing about correctness or
  grading depends on which one is used.

## Assumptions and open questions

- `yt-dlp`'s search ranking is good enough to surface relevant candidates
  within the first ~40–60 results per query; if not, the fix is more/better
  query expansion (cheap) before it's a reason to switch to the paid-quota
  Data API.
- Not every candidate has usable captions (auto-captions disabled, wrong
  language, or music/no-speech). Handled by degrading confidence and
  labeling the pick `grounded: false`, not by silently excluding videos that
  might otherwise be the best fit.
- "Grounded in actual content" is interpreted strictly: the per-video
  understanding step is only allowed to claim what appears in a transcript
  excerpt it can quote, and the eval harness spot-checks that the quote is
  real (a substring/near-match of the actual transcript), not fabricated.
- The Claude key has a usage cap. The design keeps call count low
  deliberately: extraction calls are batched (several candidates per call,
  not one call per video) and cached per `video_id` on disk, so re-running
  the eval suite repeatedly doesn't re-spend budget on videos already seen.

## Non-goals for this submission

- No video playback. No UI beyond a thin Streamlit viewer/chat layer over
  data the CLI already produces (`HLD.md` §5.2) — no accounts, no
  multi-session state, no pipeline logic reachable only from the UI.
- No actual deployment or multi-user concerns — the cost/latency bonus
  reasons about 10K users/day on paper; nothing here is built to serve that.
- No comment-mining or non-YouTube discovery sources in the base agent —
  flagged in the HLD as the first thing to add with more time, not
  something half-built now.
