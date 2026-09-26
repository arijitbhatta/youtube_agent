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
take-home?

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
360-minute budget. A metric that flags the brief's own worked example as
a failure is wrong, not the example.

**Decision:** grade budget compliance asymmetrically. Overage is a hard
fail (the selection algorithm makes it structurally impossible, so any
overage means a bug, not a debatable call). Undershooting is graded
loosely, up to ~25%, reasoned directly from the brief's own sketch rather
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

### 10. There is no 4–6 constraint — that was the sample's number, not the brief's

**Trigger (explicit correction):** "There is no constraint of 4-6 videos
btw. It was provided for the sample example. Don't put any constraint on
this."

**What this exposed:** the design had quietly treated the reference
persona's example output size as a system requirement — a "floor check"
in §4 step 3 that wouldn't let a curriculum fall below 4 picks, a greedy
pass in step 1 that capped out at 6, an eval metric ("count sanity")
literally graded against the range 4–6, and the same number repeated as
if it were given in `APPROACH.md`'s problem statement and `CLAUDE.md`'s
one-line description. None of that came from the brief; it came from
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
well as mentioned in assignment pdf. At the same time keep a streamlit
based frontend so that I can chat with the output like why some videos have
been skipped etc. Think if docker container can be used here."

**Three separate decisions, one trigger:**

- *Two versions, compared.* Not two codebases — one `enable_reviewer` run
  flag. When off, `agent/graph.py` wires `NARR → OUT` directly and the
  `REVIEW` node (§3.9) never runs — zero extra calls, not "runs and gets
  ignored." `eval/run_eval.py --compare-reviewer` runs the seven test-set
  scenarios under both settings and diffs §6.1's metrics, §6.2's judge
  score, and an extended §6.3 human calibration between them. This is
  explicitly where the brief's "surface where the eval and human judgment
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
