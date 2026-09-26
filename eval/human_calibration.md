# Human calibration (HLD §6.3)

For the reference persona plus two more real scenarios (including
`05_git_basics_high_duplication`, the most direct test of dedup), I read
the rendered Markdown output directly — blind, before consulting
`eval/metrics.py`'s automated scores or the judge's verdict — then
compared. This file records where human and automated judgment agree and,
more importantly, where they disagree and why. Disagreements are
documented as limitations of the metric, not tightened away.

## Scenario 1 — `01_weekend_react_dev` (reference persona)

**Blind read:** The curriculum's picks were plausibly sequenced
foundational → core → advanced, and the narrative reasons read as
grounded rather than generic. However, the run went through all 3 review
iterations and shipped `approved: False`, with several named blocking
issues (redundancy between two picks covering nearly the same ground, one
pick pushing meaningfully over budget) still listed as unresolved in the
final "Review" section — visible directly in the rendered output, not
something I had to dig for.

**Automated comparison:** The rendered "Review" section's unresolved
issues matched `output.warnings` and `review_summary.unresolved_blocking_issues`
in the saved trace exactly — no daylight between what I read as a human
and what the pipeline itself reported as unresolved. Where this run
complicates a prior expectation: `SKILLS.md` #12 predicted the reviewer
would likely approve-on-iteration-1 for legitimate input like this one.
It didn't — it used its full budget and still shipped with issues named.
That's the honest finding, not a result to explain away.

**Disagreement found:** None between my blind read and the trace's own
self-report. The one noteworthy gap is against the *a-priori expectation*
in `SKILLS.md`, not against the automated metrics themselves.

## Scenario 2 — `02_docker_tiny_budget`

**Blind read:** A short, tightly-scoped curriculum appropriate for a small
budget. Reading the picks against the stated goal, coverage looked
genuinely thin in places — a couple of unknowns were touched on only
briefly by a single video rather than substantively covered. Also hit the
full 3-iteration review cap, `approved: False`, 6 unresolved blocking
issues surfaced in the final output.

**Automated comparison** (against `outputs/eval_smoke2.json`):
- `redundancy`: 1 duplicate cluster detected, `passes: false` — consistent
  with my own sense that coverage was thin/overlapping rather than crisp.
- `judge`: `approved_rate: 0.0`, issue count 7 — the judge's verdict lines
  up directionally with my own read (not approved, real issues present).
- **Disagreement, and it's a real one:** `unknown_topic_coverage_llm`
  reported `fraction_covered: 1.0` (the LLM self-report says every unknown
  topic is covered), while `unknown_topic_coverage_embedding` reported
  `fraction_covered: 0.0`, flagging all 3 topics as disagreements
  (best similarity scores 0.011–0.109, far below the 0.35 threshold). My
  own blind read sits between these two extremes — thin coverage, not zero
  coverage. Root-caused this session (see `README.md`'s "Known operational
  characteristics"): the YouTube caption-CDN 429 left most/all transcripts
  empty, so the embedding cross-check ended up comparing against empty
  strings — this isn't two topics of genuine semantic disagreement, it's
  the same transcript-fetch failure surfacing in a second metric. The
  metric's `0.0` is not more trustworthy than the LLM's `1.0` here; both
  are degraded, in opposite directions, by the same missing input.
- `grounding_rate`: `checked: 0, rate: null` — same root cause, no
  transcripts to check grounding against.

**Takeaway:** the redundancy and judge signals were directionally
trustworthy and matched my blind read; the embedding-based coverage
cross-check was not informative this session and should be read alongside
`grounding_rate: null` as one signal (transcript fetch failure), not two
independent findings.

## Scenario 5 — `05_git_basics_high_duplication` (the direct dedup stress test)

**Blind read:** The shipped curriculum (11 videos, 89/90 min used) reads as
heavily redundant even without consulting any metric — branching is
covered twice (videos 2, 5), merging/conflicts across five videos (3, 4,
6, 7, 11), and PR/remote workflow across four (1, 8, 9, 10). The learner's
constraint ("not another overview, I already use git day to day") is
plainly violated by video 1, a basic "invite team members to GitHub"
video placed first. The review's own "Review" section names all of this
explicitly and still ships it — the clearest live example yet of the
3-iteration cap being exhausted with real, specific, correctly-diagnosed
issues left unresolved (see the cross-scenario note below).

**A second, more interesting disagreement, found by cross-checking
"Goal coverage" against the curriculum itself:** the rendered output
marks `working with a shared remote/PRs: uncovered` — despite three
selected videos (#8 "create a pull request," #9 "clone, branch, and open
a PR," #10 "multi-developer GitHub workflow: forking, PRs, code review,
merging PRs") whose own narrative "Why" text explicitly describes PR/
remote-workflow content. A human skimming the curriculum would reasonably
call this topic covered. Traced to root cause in the saved trace:

- `render.py`'s `_goal_coverage()` and `selection.py`'s coverage-forcing
  swap both key off one field — `understanding.matches_unknown` — an
  LLM judgment made per-candidate, in isolation, during the understanding
  stage. Checking that field directly: video #9 (`M2FoZvRl0jE`) was tagged
  `['branching']` only; video #10 (`k5D37W6h56o`) was tagged `['merging',
  'resolving conflicts']`; video #8 (`nCKdihvneS0`) was tagged `[]` —
  **none** of the three videos whose narrative reasoning cites PR/remote
  workflow were ever tagged with that exact topic string by the earlier,
  independent understanding-stage call. The understanding and narrative
  stages disagree with each other about the same candidates, and the
  coverage check trusts only the (more conservative) earlier one.
- Of the 80 discovered candidates, exactly 2 were tagged with the exact
  `"working with a shared remote/PRs"` string (`mcWsX_setW4`,
  `LTRI8rI4BsY`). Both were excluded by the review loop across iterations
  1-2 — for legitimate, separately-correct reasons (ungrounded, duplicate,
  constraint-violating) — before `_sel` re-ran on iteration 2. Once both
  were gone, the coverage-forcing swap in `selection.py` (`candidates_for_topic`
  empty → `continue`, line 70-71) had nothing left to force in, and
  silently no-op'd for that topic. The infeasibility check never fired
  either, because it only looks at aggregate coverage fraction
  (3 of 4 topics = 0.75, well above the 0.34 threshold) — losing one whole
  topic isn't enough to trip an aggregate-only check.

**This is a real gap, not a metric-display quirk:** the review loop can
improve grounding/redundancy at the cost of silently regressing topic
coverage, and nothing in the pipeline surfaces that trade-off except the
final "Goal coverage" table — which itself under-reports because it's
keyed to a single conservative LLM field that disagrees with the
pipeline's own later narrative reasoning about the same videos. Written up
in `README.md`'s "Known operational characteristics" and flagged in "What
I'd do with more time."

## Cross-scenario observation

Both real runs inspected hit the full 3-iteration review cap with
`approved: False` rather than approving on iteration 1. `SKILLS.md` #12's
a-priori expectation was that legitimate input would mostly approve on the
first pass, with the reviewer earning its keep mainly on adversarial/
infeasible input. The two scenarios actually read here don't fit that
expectation — reported here as measured, not massaged toward the earlier
prediction, per HLD §6.3's own instruction to report disagreements rather
than tighten them away.
