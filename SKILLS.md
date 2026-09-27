# Design decision log

`APPROACH.md` and `HLD.md` are the finished design. This file is the
record of *how* they got there — the specific judgment calls made along
the way, what triggered each one, and why it was resolved the way it was.
It exists so the reasoning behind a decision doesn't have to be
reverse-engineered from the final document later, by me or anyone else
reviewing this.

Each entry: what was questioned or requested, what the actual problem
turned out to be, what was decided, and where it landed.

---

### 1. Borrow discipline, not infrastructure

**Question:** how much of a larger, previously-built RAG system's
engineering pattern should transfer to a single-user, no-concurrency
project?

**Resolution:** transfer the *habits* — named, independently-testable
pipeline stages; a cheap model for high-volume extraction and a stronger
one reserved for judgment calls; a thin instrumented LLM-call wrapper
kept separate from business logic; cost/scaling reasoning done on paper.
Do not transfer the *infrastructure* — no queue, no worker fleet, no
managed database. This task has no concurrent consumers, so nothing here
needs to survive a process crash independently of anything else, and
building as though it does would be exactly the over-engineering the
brief warns against.

**Where:** `APPROACH.md` principle 5, "Engineering discipline this design
leans on."

---

### 2. Where is the budget actually defined, and does the tolerance hold up?

**Trigger:** "in the HLD section 4 it talks about the budget, where this
budget is defined?"

**What the question exposed:** §4 already treated `time_budget_minutes`
as a hard ceiling (never exceed it). But the evaluation section had a
*symmetric* ±15% tolerance around that budget — which would fail the
brief's own reference sketch, since that sketch lands ~17% *under* a
360-minute budget. A metric that flags the spec's own worked example as
a failure is wrong, not the example.

**Decision:** grade budget compliance asymmetrically. Overage is a hard
fail (the selection algorithm makes it structurally impossible, so any
overage means a bug, not a debatable call). Undershooting is graded
loosely, up to ~25%, reasoned directly from the spec's own sketch rather
than picked arbitrarily.

**Where:** `HLD.md` §4 (budget definition) and §6.1 (asymmetric tolerance
and why).

---

### 3. No pub/sub, no queue

**Trigger:** "why don't you use pubsub or queue here?"

**Decision:** deliberately not used. No concurrent request path exists in
this brief — one CLI invocation, one process, one run. A queue solves a
problem (multiple producers/consumers needing to coordinate) that doesn't
exist here; adding one would be solving a problem for credit rather than
because the system has it.

**Where:** `APPROACH.md` principle 5 and the discipline table's last row.

---

### 4. Is there a way to judge content quality, not just topical fit?

**Trigger:** "Is there any way of understanding content quality in the
whole pipeline?"

**What the question exposed:** a real gap. The design up to that point
only modeled *fit* (does this video address this learner's gaps) — it had
no axis for whether the content itself was clear, dense, or accurate.
Fit and quality are genuinely different and can disagree: a perfectly
on-topic video can still be a rambling, low-density explanation.

**Decision:** add `quality_signal` (`clarity`, `content_density`,
`accuracy_flags`) to the per-video understanding record, read off the
same transcript in the same LLM call (no extra cost). Fold `clarity ×
content_density` into the selection utility as `quality_multiplier`.
Deliberately keep `accuracy_flags` *out* of the score — it's speculative,
and silently downranking on an unconfirmed hunch is worse than surfacing
it — so it's routed to output `warnings` instead. Add a matching eval
metric (picked-set vs. candidate-pool average quality) and an explicit
"what this still can't verify" caveat, since an LLM reading a transcript
can be fooled by a confident, clearly-narrated explanation of something
wrong exactly as easily as a correct one.

**Where:** `HLD.md` §2.3, §3.6, §6.1, §6.4.

---

### 5. Does discovery actually download video?

**Trigger:** "does yt-dlp download the video?" → confirmed as a
requirement: "I want yt-dlp to be download free."

**Decision:** made explicit as a hard constraint, not just an implied
implementation detail. Discovery (§3.2) and the fetch step (§3.3) use
`yt-dlp` exclusively in its no-download modes —
`extract_info(download=False)` for search/metadata, `skip_download=True` +
`writesubtitles`/`writeautomaticsub` for captions. No frame or audio
analysis exists anywhere in this pipeline, and no local disk usage scales
with video length — only with metadata and text.

**Where:** `HLD.md` §3.3 (stated inline as a hard constraint, with the
consequence for §8's cost model spelled out); already carried as a hard
constraint in `CLAUDE.md`.

---

### 6. Add a reviewer agent that critiques and revises before shipping

**Trigger (explicit request):** "In the agentic architecture I want to
keep a reviewer agent which will review and provide feedback to the
initial final outcome and that feedback will be incorporated as well
until the reviewer agent is satisfied."

**Tensions this created, and how each was resolved:**

- *§3.8 said narration "is explicitly not asked to re-decide anything" —
  doesn't a revise loop contradict that?* No: the narrator itself still
  never revises unprompted. The reviewer is a separate call that issues a
  structured, targeted instruction back to a *specific* stage
  (`stage_to_fix: "narrative"` or `"selection"`) — the narrator still only
  narrates what it's told, once per instruction.
- *"Until satisfied" has no stopping condition — that's an open-ended cost
  risk against a capped API key.* Replaced with a concrete rule: hard cap
  of 3 iterations; if issues remain at the cap, ship anyway and copy every
  unresolved blocking issue into the output's `warnings` rather than loop
  indefinitely or ship silently broken.
- *A pure-LLM reviewer can be talked out of a real defect by fluent
  prose.* Anchored the reviewer on the same deterministic metrics already
  built for evaluation (§6.1) — any deterministic failure (budget
  overage, uncovered topic, redundancy) is auto-injected as a `blocking`
  issue the LLM can't override; the LLM can only *add* issues on top for
  things the metrics can't see.
- *Isn't this a second "is this good" system, duplicating the existing
  LLM-judge?* No — factored the rubric into one shared module
  (`agent/critique.py`), called online by the reviewer (gates one run)
  and offline by `eval/judge.py` (grades the whole test set). One
  definition of "good," two call sites.
- *Does this violate "simple over impressive"?* Justified explicitly: no
  new infrastructure (a plain bounded loop in one process), no new
  judgment logic (reused rubric), and a small hard cap tied to the
  existing usage-cap concern.

**Where:** `HLD.md` §3.9 (the loop itself), §6.2/§6.4 (shared blind spot
with the judge), §8 (cost impact), §9/§10 (module + build order), §11
(loop-specific risk). `APPROACH.md` scope decisions.

---

### 7. Add a guardrail for off-topic or malicious input

**Trigger (explicit request):** "let's keep one more check at the
beginning if someone is asking unrelated questions a relatively small LLM
will tell the user politely that it's not the purpose."

**Decisions made in scoping this:**

- *Which model.* Reused the cheap tier already defined for the
  high-volume understanding step rather than introducing a new tier or a
  separate local model. Considered a fully local tiny model explicitly
  (zero Claude-budget cost for garbage traffic) and rejected it — a
  second inference stack for one narrow yes/no classification is more
  complexity than the risk it protects against.
- *Structured, not freeform, output.* The classifier returns
  `{in_scope, reason}`; the decline message is a fixed template with
  `reason` dropped in, not an open-ended generation — a cheap model asked
  to freely write a refusal can drift in tone; one asked to fill one slot
  in a fixed sentence can't.
- *Named the secondary benefit honestly, without overclaiming it.* Since
  `goal`/`background`/`constraints` are free text feeding every
  downstream prompt, this gate is also a first, best-effort line of
  defense against prompt injection — stated explicitly as best-effort,
  not as solved, since a determined injection phrased as a plausible
  learning goal could still pass, and nothing downstream specifically
  sanitizes injected text arriving inside a transcript either.
- *Cost framing.* Net-positive: one extra cheap call on every request,
  but it prevents the ~10–25 downstream calls entirely on the traffic
  it's meant to catch.
- Added a 7th test-set scenario specifically to exercise this gate (plain
  off-topic input and an injection-shaped one), since it's a distinct
  failure mode from the other six.

**Where:** `HLD.md` §0 (new), §1 (diagram), §7 (scenario 7), §8, §9, §10,
§11. `APPROACH.md` scope decisions.

---

### 8. Make the submitted docs stand on their own

**Trigger (explicit request):** remove references to the prior project
that inspired this design, since only this repo is being submitted.

**Decision:** every reference to the inspiration project's specific
version names was rewritten as a general engineering-discipline statement
(named stages, model tiering, config seams, cost-on-paper, no
infrastructure without a concurrent consumer) with no dependency on an
unshared repo to make sense. The real external references that remain
(`all-MiniLM-L6-v2`, YouTube Data API v3) are actual public tools/APIs,
not the private project, and were kept because the design genuinely
depends on knowing what they are.

**Where:** `APPROACH.md`, `HLD.md` — throughout.

---

### 9. Who actually assigns `phase`, for sequencing?

**Trigger:** "how the sequence of learning is mentioned because it's a
tutorial right . we need to make it structured" — asked while trying to
understand §3.6's scoring, which led to tracing where `phase` (the field
§4 sequences by) actually gets set.

**What the question exposed:** a real gap. §2.4 asserted a `phase` field
existed by the time §4 runs, and §4 step 4 sorted by it — but no stage in
§3 ever claimed to produce it. §2.3 (the understanding record) didn't
include it; §3.4 didn't mention setting it; §3.6 (scoring) only computed
`utility`. It was consumed without ever being assigned — the same shape of
gap as the bang-per-minute issue found while explaining §3.6, just in a
different part of the pipeline.

**Decision:** classify `phase` in the same LLM call that already produces
`primary_style`/`depth` (§3.4/§2.3), not as a separate pass or a
code-derived rule. Reasoning: "is this setup/tooling, a concept explainer,
a hands-on build, or an advanced extension" is the same *kind* of semantic
read of the transcript as the fields already produced there — and it
can't be derived from `matches_unknown` strings alone, since the persona's
`unknown` list (§2.1) is flat with no prerequisite structure. No new call,
no new cost. Also stated the resulting limitation honestly: this sequences
by broad pedagogical stage, not fine-grained inter-video prerequisites,
and — because it's now an LLM judgment, not deterministic code —
misclassification is a named risk (§11) that no existing metric can catch
(§6.1's sequencing check only verifies internal consistency with whatever
`phase` got assigned, not that the assignment was correct).

**Where:** `HLD.md` §2.3, §2.4, §3.4, §4 step 4, §6.1 (unchanged, but now
correctly scoped), §9, §10 (row 4), §11.

---

### 10. There is no 4–6 constraint — that was the sample's number, not the spec's

**Trigger (explicit correction):** "There is no constraint of 4-6 videos
btw. It was provided for the sample example. Don't put any constraint on
this."

**What this exposed:** the design had quietly treated the reference
persona's example output size as a system requirement — a "floor check"
in §4 step 3 that wouldn't let a curriculum fall below 4 picks, a greedy
pass in step 1 that capped out at 6, an eval metric ("count sanity")
literally graded against the range 4–6, and the same number repeated as
if it were given in `APPROACH.md`'s problem statement and `CLAUDE.md`'s
one-line description. None of that came from the spec; it came from
copying the reference sketch's output size into the spec.

**Decision:** remove every count target from the algorithm, not just the
prose describing it. §4's greedy pass now stops on two conditions only —
budget exhausted, or nothing left is genuinely non-redundant — with no
upper bound. The old "floor check" became an "infeasibility check": no
minimum pick count either, a single honest pick against a tight budget is
a valid output. The eval metric was rewritten from a fixed range into a
shape check (not zero, not fragmented, not one video swallowing the whole
budget while leaving most of `unknown` uncovered) plus the existing
infeasibility-warning path. Count is now purely an outcome of coverage and
budget, everywhere it's mentioned.

**Where:** `HLD.md` §4 (steps 1 and 3, rewritten), §6.1 ("Curriculum shape
sanity"), §7 (scenario 2), §11 (stale "six" scenario-count reference also
caught and fixed to seven while editing this section). `APPROACH.md`
(problem restatement, new "video count is an outcome" scope decision).
`CLAUDE.md` (one-line description).

---

### 11. Building it with LangGraph

**Trigger (explicit information):** "Btw I will be using langgraph to
build the agent."

**Why this needed a decision, not just a note:** several places in the
docs said the literal opposite — `CLAUDE.md` principle 5 said any
agentic piece "must be a plain, bounded function call loop, never an
orchestration framework"; `HLD.md` §3.9 said the review loop was "a plain
bounded `while` loop inside one process, not an orchestration framework."
LangGraph is an orchestration framework by name. Rather than assume how
deep the adoption goes, asked directly: just the graph/control-flow
engine, or also its checkpointing/state and tool-calling idioms. Answer:
just the control-flow engine.

**Decision:** LangGraph implements exactly the graph `HLD.md` §1 already
drew — nodes are the existing plain-function stages, the review loop
becomes one conditional edge. This doesn't reintroduce the
queues/workers/managed-infra `APPROACH.md` #5 rules out; it still runs in
one process. The one thing made explicit rather than left implicit: the
3-iteration cap is enforced by a counter carried in graph state and
checked inside the conditional edge's routing function — never by relying
on LangGraph's generic `recursion_limit` (which bounds the whole graph's
step count, not this specific loop) to happen to do the job. Flagged as
its own risk (§11) precisely because it would be an easy, silent way to
lose the cap in a later refactor.

**Where:** `CLAUDE.md` (principle 5, repo map), `APPROACH.md` (new scope
decision, discipline table), `HLD.md` §1, §2, §3.9, §9, §10 (steps 8-9),
§11.

---

### 12. Reviewer ablation, a Streamlit viewer, and optional Docker packaging

**Trigger (explicit request):** "come up with two versions one without LLM
judge reviewer in the pipeline and other one with judge. compare them as
well as mentioned in the plan. At the same time keep a streamlit
based frontend so that I can chat with the output like why some videos have
been skipped etc. Think if docker container can be used here."

**Three separate decisions, one trigger:**

- *Two versions, compared.* Not two codebases — one `enable_reviewer` run
  flag. When off, `agent/graph.py` wires `NARR → OUT` directly and the
  `REVIEW` node (§3.9) never runs — zero extra calls, not "runs and gets
  ignored." `eval/run_eval.py --compare-reviewer` runs the seven test-set
  scenarios under both settings and diffs §6.1's metrics, §6.2's judge
  score, and an extended §6.3 human calibration between them. This is
  explicitly where the spec's "surface where the eval and human judgment
  disagree" line (asked about separately, same session) gets applied to a
  concrete design choice rather than only to curriculum output — and the
  honest expectation is stated up front: on legitimate test-set input the
  reviewer likely approves-on-iteration-1 most of the time (cost paid,
  nothing changed), earning its keep mainly on adversarial/malformed input.
  That's reported as the finding if the data shows it, not massaged toward
  "the reviewer is always worth it."
- *Streamlit frontend.* Directly contradicted existing "no web app... no UI
  beyond rendered Markdown/JSON" language in `APPROACH.md` and `CLAUDE.md`
  — resolved by scoping it as a strictly thin layer: `ui/app.py` imports
  `run.py`'s entrypoint and `agent/followup.py` directly, adding no new
  prompts or pipeline logic. It reuses §5's existing bonus Q&A mechanism
  (already a read path over the trace) and adds a reviewer-on/off toggle
  tying it to the ablation above. The CLI stays the primary interface;
  Streamlit is additive, not a replacement the eval harness depends on.
- *Docker.* An open question ("think if docker can be used"), not a
  request — decided as optional convenience, explicitly weighed against
  the existing "zero extra credentials, `pip install` and run" scope
  decision rather than silently overriding it. One Dockerfile, one
  `docker-compose.yml` with two services (`cli`, `ui`) from the same image,
  sharing a volume for `outputs/` and the sqlite cache. `pip install`
  remains the documented default; Docker is offered because there are now
  two entry points sharing one dependency set, not because this task has a
  concurrency/deployment need it didn't have before.

**Where:** `HLD.md` §5.2 (new), §6.6 (new), §8, §9, §10 (steps 14-15), §11
(ablation small-N caveat). `APPROACH.md` (interface scope decision
rewritten, two new scope decisions, non-goals). `CLAUDE.md` (repo map,
non-goals).

---

### 13. Parallelize the fetch stage, but bounded, not open-ended

**Trigger (explicit request):** "the yt-dlp process needs to be async or
multiprocessing" -- the sequential fetch loop over ~80 candidates
(`agent/graph.py`'s `_fetch`) was, in practice, the single slowest stage
of a live run: 80 round-trips to yt-dlp's `extract_info` plus the
caption-CDN request, one after another.

**Why threads, not multiprocessing or asyncio:** yt-dlp's own calls are
synchronous I/O (urllib underneath), not CPU-bound, and release the GIL
while waiting on the network -- a `ThreadPoolExecutor` gets the same
wall-clock win as multiprocessing without process-spawn overhead or
needing every `Candidate` round-trip through pickling. yt-dlp has no
native async API, so "async" would just mean wrapping the same blocking
call in a thread via `asyncio.to_thread` -- strictly more moving parts for
the same result.

**Why bounded, not "one thread per candidate":** `agent/transcripts.py`
already documents a live-observed finding -- YouTube's caption CDN
429s under sustained request volume, an IP-level throttle, not a
per-video quirk. Firing 80 requests at once would make that worse, not
just faster. `FETCH_MAX_WORKERS` (`agent/config.py`, default 8, env-
overridable per the existing config-over-code seam) keeps this a real
concurrency win while staying well under whatever triggered that 429
originally.

**Where:** `agent/config.py` (`FETCH_MAX_WORKERS`), `agent/graph.py`
(`_fetch`, now a bounded `ThreadPoolExecutor` keyed by original index so
downstream candidate order is unchanged regardless of completion order).

---

### 14. Swap the local dedup embedding model for a hosted one over OpenRouter

**Trigger (explicit request):** "since sentence-transformer is the only
open source model and taking a lot of time, replace it with any
proprietary embedding model from openrouter" -- made while `docker
compose build cli` was still running past 20 minutes, all of it spent
compiling/installing `sentence-transformers`'s torch dependency. Verified
via `docker buildx du`/`docker images` that the build was genuinely still
making progress, not hung -- the local embedding model was just a heavy
runtime for what §3.5 needed (embed a few dozen short summary strings per
run, cluster with union-find).

**What was verified before committing to the swap:** OpenRouter's
`/embeddings` endpoint is OpenAI-Chat-Completions-compatible, same as the
chat endpoint already in use -- a live test call against
`openai/text-embedding-3-small` returned real 1536-dim vectors at
negligible cost (`Usage(prompt_tokens=5, total_tokens=5, cost=1e-07)`).
This reuses the one `OPENROUTER_API_KEY` every chat call already requires;
no second credential, no second client, no separate runtime dependency.
`agent/llm_client.py` gained one new function, `call_embeddings()`,
alongside the existing `call_structured()` -- same client, same trace
logging pattern (`tier="embedding"`, an untyped string field at the trace
layer, so no schema change was needed).

**What did NOT change:** the union-find clustering logic and the
cosine-similarity threshold *comparison* in `agent/dedup.py` are still
plain deterministic code (CLAUDE.md principle #2) -- only the embedding
*vector generation* moved from a local model to a hosted API call. This
is a runtime/dependency change, not an architecture change to how
dedup decides what's a duplicate.

**Recalibration was necessary, not optional:** cosine-similarity
distributions are model-space-specific. The old `SIMILARITY_THRESHOLD`
(0.82) was tuned for `all-MiniLM-L6-v2` and had no reason to transfer to
`text-embedding-3-small`'s embedding space. Recalibrated live against 36
real (non-degenerate) `UnderstandingRecord`s from a saved
`git_basics_high_duplication` trace -- filtering out the ~44 records
whose `covers_topics` was empty due to the persistent caption-CDN 429
(README.md), since those produce near-identical, spuriously-inflated
"duplicate" summary strings that would calibrate against noise, not
content. Swept 0.80-0.88 and inspected the actual resulting clusters at
each cut point, not just the similarity numbers:
  - 0.80-0.82: union-find's transitive closure chains through weakly-
    related intermediates into one 9-17-member megacluster mixing
    merge-conflict videos with branching, DevOps-practices, and IDE-
    tooling videos -- a chaining artifact, not real duplication.
  - 0.86-0.88: genuine triples (e.g. three distinct "GitHub collaboration
    setup" videos) start splitting into disconnected pairs, losing real
    duplicates the lower thresholds correctly caught.
  - 0.84: no chaining artifact, every remaining multi-member cluster is
    topically tight. Set as the final value.

**What could not be recalibrated the same way, and why that's disclosed
rather than papered over:** `eval/run_eval.py`'s
`_EMBEDDING_COVERAGE_THRESHOLD` (§6.1 method (b), topic-string-vs-pick-
transcript similarity) needed different calibration data -- a short
phrase against a full transcript, not two comparable-length summaries.
Every attempt to gather that data hit the same persistent caption-CDN 429
already documented in this project: every candidate in every saved trace
under `outputs/` has an empty transcript. Left at a reasoned placeholder
(0.30, down from the old 0.35 -- OpenAI's embedding space is known to run
flatter for short-query-vs-passage similarity than sentence-transformers')
with a comment saying plainly it's not live-verified and how to
recalibrate it once transcripts are actually fetchable, per this
project's "evaluate what's verifiable, say plainly what isn't" rule
(CLAUDE.md principle #4) -- rather than inventing synthetic transcript
text to force a number.

**A real bug this swap surfaced, not just a config change:** the old
local model silently embedded empty strings (a transcript-less pick)
into some garbage-but-valid vector, silently producing low, meaningless
similarity scores. OpenAI's hosted endpoint hard-rejects an empty string
(`400: Input is empty`). `_embedding_topic_coverage` now filters out
picks with no transcript before embedding, and reports
`fraction_covered: None` rather than crashing or fabricating a number
when no pick has one -- which, given the 429 above, is every scenario run
in this environment right now.

**Where:** `agent/config.py` (`MODEL_EMBEDDING`), `agent/llm_client.py`
(`call_embeddings`), `agent/dedup.py` (`SIMILARITY_THRESHOLD = 0.84`,
rewritten to call the hosted embedding via `embed()`),
`eval/run_eval.py` (`_EMBEDDING_COVERAGE_THRESHOLD = 0.30`,
`_embedding_topic_coverage`'s empty-transcript filter), `agent/graph.py`
(`_dedup` passes `trace` through), `requirements.txt` (removed
`sentence-transformers`, `openai>=1.50` now serves both chat and
embeddings), `.env.example` (`MODEL_EMBEDDING`).

### 15. Latency/cost round: transcript-source swap, tighter query fan-out, cheap-tier online judge + incremental narrative, Streamlit exact-match cache

**Trigger (explicit request):** "use yt-dlp for searching and
youtube-transcript-api for getting the transcript / for query planning
get 4-5 different queries and then choose top 40 / for Judge narrative
regeneration for everyone is not necessary and Skip the review loop's
narrative rewrite entirely when the only blocking issues are
selection-stage, also shift to a very lightweight model for the judge. /
On streamlit side implement exact caching... later will incorporate the
similarity caching." Four independent sub-changes.

**Transcript source: `youtube-transcript-api` instead of the raw VTT
fetch, `yt-dlp` still owns search/metadata.** A second, genuinely
different attempt at dodging the caption-CDN 429 documented under entry
14 and in README.md -- a dedicated library talking to YouTube's caption
endpoint directly, not `yt-dlp`'s caption-URL indirection.
**Live-verified result: still blocked, same root cause, different code
path.** A direct call against two well-known, definitely-captioned video
IDs (`dQw4w9WgXcQ`, `jNQXAC9IVRw`) both raised `IpBlocked` from the
library itself -- YouTube is blocking the request origin at the IP
level, which no client-library swap fixes. Re-confirmed a second time in
this same round of work; the finding is stable, not a one-off. Disclosed
plainly rather than claimed fixed: `agent/transcripts.py` is a strictly
better code path (a maintained library instead of hand-rolled VTT
parsing/retry logic) but does not resolve the underlying blocker in this
environment. `NoTranscriptFound`/`TranscriptsDisabled`/`VideoUnavailable`
plus a bare `Exception` fallback all degrade to
`transcript_available=False` exactly as before.

**Query planning: 4-5 queries, not 4-8; candidate cap 40, not 80.**
`QueryPlan.queries` tightened to `min_length=4, max_length=5`;
`agent/discovery.py::MAX_TOTAL_CANDIDATES` dropped from 80 to 40. Fewer,
more targeted queries need less of a cap to protect against fan-out.
**Live-verified twice** (`git_basics_high_duplication`,
`docker_tiny_budget`): both real runs produced exactly 5 queries and
capped at exactly 40 candidates.

**Online judge moved to the cheap tier; offline eval judge untouched.**
`agent/critique.py`'s `llm_judgment()`/`evaluate()` gained a `tier: Tier
= "strong"` parameter threaded into `call_structured`, instead of a
second judgment prompt (CLAUDE.md: `agent/critique.py` stays the single
source of truth). `agent/review.py::run_review` -- the path every real
user request goes through -- passes `tier="cheap"` explicitly.
`eval/judge.py` keeps the untouched `"strong"` default, since CLAUDE.md
calls the eval "weighted most heavily." Which call site gets the
lightweight model was a judgment call made per that stated priority, not
asked about. **Live-verified**: every `critique`-labeled call in both
verification traces used `anthropic/claude-haiku-4.5`; every `narrative`
call stayed on `anthropic/claude-sonnet-5`.

**"Skip the review loop's narrative rewrite entirely" -- read literally
this breaks a newly-swapped-in pick's contract (it would ship with no
`reason` at all), so this is built as incremental narrative
regeneration, not a literal always-skip.** `agent/narrative.py::
generate_narrative` now takes `previous_items`/`previous_dropped` (the
prior pass's draft, threaded through `agent/graph.py`'s `_narr` node
from `state["narrative_draft"]`, which LangGraph already carries across
loop iterations). A video_id is reused verbatim -- no LLM involvement --
when it's present in the prior draft, not itself named by a
narrative-stage blocking issue, and no issue targets `"overall"`. Only
the genuinely-new/feedback-targeted subset is sent to the LLM; if that
subset is empty on both sides, the call is skipped entirely (0 LLM
calls) -- the literal "skip entirely" case, which holds whenever a
selection-only fix purely *excludes* a flagged pick with no replacement.
When a fix *swaps in* a replacement pick (the common case in adversarial
test scenarios, where exclusion frees budget that selection then spends
on a different candidate), that one new pick still gets narrated -- the
one case "entirely" can't be honored without leaving a pick unexplained.
**Live-verified** on `docker_tiny_budget` (3 review iterations, each
excluding a different flagged video and selection filling the freed
budget with a new pick): iteration 0's narrative call generated
everything (4085 input / 1847 output tokens); iterations 1 and 2 dropped
to 1525/166 and 1360/129 tokens respectively -- both calls happened
(a new pick appeared each round in this adversarial scenario, so the
zero-call branch didn't fire here), but each generated for exactly the
one new pick and reused everything else, matching the design.

**Streamlit exact-match run cache, deliberately not similarity/fuzzy
(named as a later, separate request by the user).** New
`agent/run_cache.py`: same sqlite file as `agent/cache.py`'s
understanding cache (`config.CACHE_PATH`), a second table. Key is a
sha256 of the *full* `PersonaInput` payload (including free-text `goal`
-- unlike the understanding-cache key, which deliberately excludes
`goal`) plus `enable_reviewer`, since both determine the pipeline's
output. `ui/app.py` checks the cache before calling `run_from_payload`
in both the "New persona" and "Existing test_set scenario" branches; a
hit re-renders the saved trace with zero pipeline calls, a miss runs
normally and populates the cache after. UI-only orchestration (CLAUDE.md:
"`ui/app.py` is a thin layer") -- `run.py`'s CLI path is untouched, per
the user's own scoping to "on streamlit side."

**Docker rebuild after adding `youtube-transcript-api`: initially blocked
in-shell, then re-verified once the binary was located -- and a second,
real bug caught right after, from only rebuilding half of it.** `docker`
was not on `PATH` in the shell this session ran in (`which docker`
failed); turned out the daemon (Docker Desktop) was installed and
running the whole time, just with its CLI at
`/Applications/Docker.app/Contents/Resources/bin/docker`, not symlinked
onto `PATH`. Re-ran with that path prepended: `docker compose build cli`
succeeded (~3m42s, `youtube-transcript-api` and `yt-dlp` both installed
cleanly in the image), `docker compose run --rm cli --input
test_set/02_docker_tiny_budget.json` ran the full pipeline for real
inside the container (3 review iterations, real curriculum output --
note the `entrypoint: ["python", "run.py"]` in `docker-compose.yml`
means the command is just `--input ...`, not `python run.py --input
...`).

**Real bug caught by the user, not by me:** I had only run `docker
compose build cli`, then separately brought up `ui` and health-checked
it -- but `docker-compose.yml`'s `cli` and `ui` services each have their
own `build: .` with no shared `image:` tag, so Compose keeps two
*independent* images from the same Dockerfile. Rebuilding `cli` never
touched `ui`'s image, so `docker compose up ui` was still serving the
pre-this-round code (80-candidate cap, 4-8 queries) even after the "full"
verification above -- a health-check 200 says the server started, not
that it's running current code. The user caught this by actually using
the app and noticing 80 candidates in the search log. Fixed by rebuilding
`ui` explicitly (`docker compose build ui`), then verifying inside the
*running* container this time, not just via HTTP health check:
`docker compose exec ui python -c "from agent import discovery;
print(discovery.MAX_TOTAL_CANDIDATES)"` → `40`, plus a full
`run_from_payload(...)` executed inside the container via `docker compose
exec ui python -c "..."` confirmed 5 queries / 40 candidates for real.
**Lesson: verifying a multi-service Compose file after a code change
means rebuilding (and probing the actual running code of) every service
whose image derives from the changed files, not just the one you happened
to `build`/`run` first — an HTTP 200 from a health endpoint is not
evidence the container is running the code you think it is.**

**Where:** `requirements.txt` (`youtube-transcript-api`),
`agent/transcripts.py`, `agent/query_planning.py`
(`QueryPlan.queries`), `agent/discovery.py`
(`MAX_TOTAL_CANDIDATES = 40`), `agent/critique.py` (`tier` param on
`llm_judgment`/`evaluate`), `agent/review.py` (`tier="cheap"`),
`agent/narrative.py` (`generate_narrative`'s reuse/splice logic),
`agent/graph.py` (`_narr` threading `previous_items`/`previous_dropped`),
`agent/run_cache.py` (new), `ui/app.py` (`_run_with_cache`),
`tests/test_narrative.py` (new), `tests/test_run_cache.py` (new),
`HLD.md` §8/§9, `APPROACH.md` scope decisions.
