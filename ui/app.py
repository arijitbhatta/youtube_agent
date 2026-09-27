"""Streamlit front-end (HLD.md §5.2) -- a thin wrapper, no pipeline logic
of its own (CLAUDE.md non-goals): run/load a trace, render it with
`agent/render.py`'s existing Markdown, and chat over it via
`agent/followup.py`'s `answer_question`. Single-user, local, in-process --
same scope as the CLI, a different front end (APPROACH.md non-goals: no
accounts, no multi-session state).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

# Streamlit only puts this script's own directory (ui/) on sys.path, not the
# repo root -- so `from agent... import ...` below fails with
# ModuleNotFoundError unless the repo root happens to already be on the path
# (e.g. it was the invocation cwd). Make it robust to invocation directory.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import streamlit as st

from agent import progress, recommendations, run_cache
from agent.config import ENABLE_REVIEWER_DEFAULT, OUTPUTS_DIR, REPO_ROOT, api_key_present
from agent.followup import answer_question_stream
from agent.render import render_markdown_lean
from agent.schemas import CurriculumOutput, OutOfScopeOutput, PersonaInput, UserContext
from agent.trace import Trace
from run import run_from_payload

TEST_SET_DIR = REPO_ROOT / "test_set"

# The UI no longer exposes a time-budget field; new-persona runs use a fixed
# default budget instead (the selected set is an outcome of it, not an input
# the reader is meant to tune from here).
DEFAULT_TIME_BUDGET_MINUTES = 120

# How long a run may take before we surface "also consider" book/course
# recommendations -- surfaced *mid-run* (not after), so the learner has
# something to look at while the pipeline is still working. That needs a
# worker thread for the pipeline (see _RunJob / _start_run below), because a
# synchronous run_from_payload would block the whole page.
FALLBACK_DELAY_SECONDS = 10.0

st.set_page_config(page_title="Learning Curriculum Builder", layout="wide")
st.title("Learning Curriculum Builder")

if "trace" not in st.session_state:
    st.session_state.trace = None
    st.session_state.output = None
    st.session_state.chat_history = []
    st.session_state.recommendations = None
    st.session_state._run_job = None


def _load_trace_and_render(trace: Trace) -> None:
    """Shared by both the "run a new persona" and "load an existing trace"
    paths -- one place decides what goes into session_state, so the render
    + chat panels below don't care which path produced the trace."""
    st.session_state.trace = trace
    st.session_state.chat_history = []
    st.session_state.recommendations = None
    output_data = trace.data.get("output")
    if output_data is None:
        st.session_state.output = None
    elif "message" in output_data and "curriculum" not in output_data:
        st.session_state.output = OutOfScopeOutput.model_validate(output_data)
    else:
        st.session_state.output = CurriculumOutput.model_validate(output_data)


class _RunJob:
    """Thread-safe holder for one in-flight pipeline run. The pipeline worker
    thread and the recommendations worker thread both write into it under a
    lock; the main script thread reads snapshots in the live poll loop. No
    `st.*` calls happen on the worker threads, so none of them need
    Streamlit's script-run context (add_script_run_ctx)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.start = time.monotonic()
        self._stage = ""
        self._detail = ""
        self._recs = None
        self._output = None
        self._trace_path = None
        self._error = None
        self._done = False

    # -- writers (worker threads only) --
    def set_progress(self, stage: str, detail: str = "") -> None:
        with self._lock:
            self._stage = stage
            self._detail = detail

    def set_recs(self, recs) -> None:
        with self._lock:
            self._recs = recs

    def finish(self, output, trace_path) -> None:
        with self._lock:
            self._output = output
            self._trace_path = trace_path
            self._done = True

    def fail(self, error: BaseException) -> None:
        with self._lock:
            self._error = error
            self._done = True

    # -- readers (main thread only) --
    def snapshot(self) -> tuple[str, str, object, float]:
        with self._lock:
            return self._stage, self._detail, self._recs, time.monotonic() - self.start

    def done(self) -> bool:
        with self._lock:
            return self._done

    def result(self):
        with self._lock:
            return self._output, self._trace_path, self._error

    def recs(self):
        with self._lock:
            return self._recs


def _run_pipeline_job(input_payload: PersonaInput, enable_reviewer: bool, job: _RunJob) -> None:
    """Worker-thread body: run the full pipeline (emitting progress into the
    job), cache the trace path for a future exact-match hit, and record the
    outcome. sqlite writes here stay on this single thread, as before."""
    try:
        output, trace_path = run_from_payload(
            input_payload, enable_reviewer=enable_reviewer, progress_cb=job.set_progress
        )
        run_cache.set_cached_trace_path(input_payload, enable_reviewer, str(trace_path))
        job.finish(output, trace_path)
    except BaseException as exc:  # surface in the UI, never crash the app
        job.fail(exc)


def _run_recommendations_job(input_payload: PersonaInput, job: _RunJob) -> None:
    """Worker-thread body: fetch books/courses in parallel with the pipeline
    (started at t=0 so they're ready by FALLBACK_DELAY_SECONDS). get_recommendations
    already degrades to empty lists internally; the guard is belt-and-suspenders."""
    try:
        recs = recommendations.get_recommendations(
            input_payload.goal, input_payload.user_context.unknown
        )
    except BaseException:
        recs = None
    job.set_recs(recs)


def _start_run(input_payload: PersonaInput, enable_reviewer: bool) -> None:
    """Exact-match cache check (agent/run_cache.py) first; a hit re-loads the
    previous trace. Otherwise kick off the pipeline + recommendations on worker
    threads and return immediately -- the render area below polls the job live
    (stage text + the ~10s book/course panel) until it finishes."""
    cached_path = run_cache.get_cached_trace_path(input_payload, enable_reviewer)
    if cached_path and Path(cached_path).exists():
        _load_trace_and_render(Trace.load(cached_path))
        st.success("Loaded your previous result for the same request.")
        return

    job = st.session_state.get("_run_job")
    if job is not None and not job.done():
        st.warning("A run is already in progress.")
        return

    job = _RunJob()
    st.session_state._run_job = job
    threading.Thread(target=_run_pipeline_job, args=(input_payload, enable_reviewer, job), daemon=True).start()
    threading.Thread(target=_run_recommendations_job, args=(input_payload, job), daemon=True).start()


def _live_wait_and_finalize(job: _RunJob) -> None:
    """Poll an in-flight job: paint the live stage line in place (every ~0.75s)
    and, once FALLBACK_DELAY_SECONDS has elapsed and the recommendations are
    ready, render the books/courses panel below it -- so the learner sees
    something while the pipeline finishes. When the worker is done, load the
    trace into session_state (reusing _load_trace_and_render's validation) so
    the rest of the script renders the final result."""
    status = st.empty()
    recs_slot = st.container()
    recs_shown = False
    while not job.done():
        stage, detail, recs, elapsed = job.snapshot()
        status.markdown(f"**{progress.format_progress(stage, detail)}** · {elapsed:.0f}s")
        # Show the books/courses panel once, at/after the 10s mark, as soon as
        # the (t=0-started) recommendations thread has them. Re-checked every
        # poll if they've somehow not landed yet, so no flicker is needed.
        if not recs_shown and elapsed >= FALLBACK_DELAY_SECONDS and recs is not None:
            with recs_slot:
                _render_recommendations(recs)
            recs_shown = True
        time.sleep(0.75)

    output, trace_path, error = job.result()
    st.session_state._run_job = None
    status.empty()

    if error is not None:
        st.error(f"The run failed: {error}")
        return

    _load_trace_and_render(Trace.load(trace_path))
    # Recommendations shown mid-run stay on screen (recs_slot persists for this
    # script pass); only promote them below the finished result if they were
    # fetched but the run finished before 10s. A cache-hit / loaded-trace path
    # leaves recommendations None via _load_trace_and_render.
    st.session_state.recommendations = None if recs_shown else job.recs()
    st.success("Done.")


def _render_recommendations(recs) -> None:
    """User-facing "while you waited" panel -- real books from Google Books,
    LLM-suggested courses with clearly-labeled search links (never fabricated
    deep links)."""
    st.divider()
    st.subheader("While you waited — books & courses to also consider")
    if recs.books:
        st.markdown("**Books**")
        for b in recs.books:
            authors = ", ".join(b.authors)
            st.markdown(f"- [{b.title}]({b.link})" + (f" — {authors}" if authors else ""))
    if recs.courses:
        st.markdown("**Online courses**")
        for c in recs.courses:
            site = "Udemy" if c.platform == "udemy" else "Coursera"
            st.markdown(
                f"- **{c.title}** ({site}) — {c.why}  \n  [search on {site}]({c.search_link})"
            )
    if not recs.books and not recs.courses:
        st.caption("Couldn't load recommendations this time.")


with st.sidebar:
    st.header("Run or load")
    mode = st.radio("Source", ["New persona", "Existing test_set scenario", "Previous run (outputs/)"])
    enable_reviewer = st.checkbox("Enable reviewer loop", value=ENABLE_REVIEWER_DEFAULT)

    if mode == "New persona":
        with st.form("new_persona_form"):
            persona_id = st.text_input("Persona ID", value="ui_session")
            goal = st.text_area("Goal", value="")
            background = st.text_area("Background", value="")
            known = st.text_input("Already known (comma-separated)", value="")
            unknown = st.text_input("Wants to learn / unknown (comma-separated)", value="")
            constraints = st.text_input("Constraints", value="")
            submitted = st.form_submit_button("Run")

        if submitted:
            if not api_key_present():
                st.error("OPENROUTER_API_KEY is not set -- copy .env.example to .env and fill it in.")
            elif not goal.strip():
                st.error("Goal is required.")
            else:
                input_payload = PersonaInput(
                    persona_id=persona_id or "ui_session",
                    goal=goal,
                    time_budget_minutes=DEFAULT_TIME_BUDGET_MINUTES,
                    user_context=UserContext(
                        background=background,
                        known=[s.strip() for s in known.split(",") if s.strip()],
                        unknown=[s.strip() for s in unknown.split(",") if s.strip()],
                        constraints=constraints,
                    ),
                )
                _start_run(input_payload, enable_reviewer)

    elif mode == "Existing test_set scenario":
        scenario_paths = sorted(TEST_SET_DIR.glob("*.json"))
        chosen = st.selectbox("Scenario", scenario_paths, format_func=lambda p: p.stem)
        if st.button("Run this scenario"):
            if not api_key_present():
                st.error("OPENROUTER_API_KEY is not set -- copy .env.example to .env and fill it in.")
            else:
                input_payload = PersonaInput.model_validate(json.loads(chosen.read_text()))
                _start_run(input_payload, enable_reviewer)

    else:
        trace_paths = sorted(OUTPUTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        chosen_trace_path = st.selectbox("Trace file", trace_paths, format_func=lambda p: p.name)
        if st.button("Load"):
            _load_trace_and_render(Trace.load(chosen_trace_path))
            st.success(f"Loaded {chosen_trace_path.name}")

job = st.session_state.get("_run_job")
if job is not None:
    _live_wait_and_finalize(job)

if st.session_state.trace is None:
    st.info("Run a new persona, run a test_set scenario, or load a previous run's trace from the sidebar.")
else:
    output = st.session_state.output
    if isinstance(output, OutOfScopeOutput):
        st.warning(f"Out of scope: {output.message}")
    elif output is not None:
        st.markdown(render_markdown_lean(output))
        if st.session_state.recommendations:
            _render_recommendations(st.session_state.recommendations)
    else:
        st.warning("This trace has no output recorded.")

    st.divider()
    st.header("Chat about this run")
    for role, text in st.session_state.chat_history:
        with st.chat_message(role):
            st.write(text)

    question = st.chat_input("Ask about this run (e.g. \"why wasn't video X picked?\")")
    if question:
        st.session_state.chat_history.append(("user", question))
        with st.chat_message("user"):
            st.write(question)
        with st.chat_message("assistant"):
            if not api_key_present():
                answer = "OPENROUTER_API_KEY is not set -- copy .env.example to .env and fill it in."
                st.write(answer)
            else:
                answer = st.write_stream(answer_question_stream(st.session_state.trace, question))
        st.session_state.chat_history.append(("assistant", answer))
