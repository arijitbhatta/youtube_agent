"""Streamlit front-end (HLD.md §5.2) -- a thin wrapper, no pipeline logic
of its own (CLAUDE.md non-goals): run/load a trace, render it with
`agent/render.py`'s existing Markdown, and chat over it via
`agent/followup.py`'s `answer_question`. Single-user, local, in-process --
same scope as the CLI, a different front end (APPROACH.md non-goals: no
accounts, no multi-session state).
"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from agent.config import ENABLE_REVIEWER_DEFAULT, OUTPUTS_DIR, REPO_ROOT, api_key_present
from agent.followup import answer_question
from agent.render import render_markdown
from agent.schemas import CurriculumOutput, OutOfScopeOutput, PersonaInput, UserContext
from agent.trace import Trace
from run import run_from_payload

TEST_SET_DIR = REPO_ROOT / "test_set"

st.set_page_config(page_title="Learning Curriculum Builder", layout="wide")
st.title("Learning Curriculum Builder")

if "trace" not in st.session_state:
    st.session_state.trace = None
    st.session_state.output = None
    st.session_state.chat_history = []


def _load_trace_and_render(trace: Trace) -> None:
    """Shared by both the "run a new persona" and "load an existing trace"
    paths -- one place decides what goes into session_state, so the render
    + chat panels below don't care which path produced the trace."""
    st.session_state.trace = trace
    st.session_state.chat_history = []
    output_data = trace.data.get("output")
    if output_data is None:
        st.session_state.output = None
    elif "message" in output_data and "curriculum" not in output_data:
        st.session_state.output = OutOfScopeOutput.model_validate(output_data)
    else:
        st.session_state.output = CurriculumOutput.model_validate(output_data)


with st.sidebar:
    st.header("Run or load")
    mode = st.radio("Source", ["New persona", "Existing test_set scenario", "Previous run (outputs/)"])

    if mode == "New persona":
        with st.form("new_persona_form"):
            persona_id = st.text_input("Persona ID", value="ui_session")
            goal = st.text_area("Goal", value="")
            time_budget_minutes = st.number_input("Time budget (minutes)", min_value=1, value=120)
            background = st.text_area("Background", value="")
            known = st.text_input("Already known (comma-separated)", value="")
            unknown = st.text_input("Wants to learn / unknown (comma-separated)", value="")
            constraints = st.text_input("Constraints", value="")
            enable_reviewer = st.checkbox("Enable reviewer loop", value=ENABLE_REVIEWER_DEFAULT)
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
                    time_budget_minutes=int(time_budget_minutes),
                    user_context=UserContext(
                        background=background,
                        known=[s.strip() for s in known.split(",") if s.strip()],
                        unknown=[s.strip() for s in unknown.split(",") if s.strip()],
                        constraints=constraints,
                    ),
                )
                with st.spinner("Running the pipeline (real discovery + LLM calls)..."):
                    _, trace_path = run_from_payload(input_payload, enable_reviewer=enable_reviewer)
                    _load_trace_and_render(Trace.load(trace_path))
                st.success(f"Done -- trace saved to {trace_path}")

    elif mode == "Existing test_set scenario":
        scenario_paths = sorted(TEST_SET_DIR.glob("*.json"))
        chosen = st.selectbox("Scenario", scenario_paths, format_func=lambda p: p.stem)
        enable_reviewer = st.checkbox("Enable reviewer loop", value=ENABLE_REVIEWER_DEFAULT)
        if st.button("Run this scenario"):
            if not api_key_present():
                st.error("OPENROUTER_API_KEY is not set -- copy .env.example to .env and fill it in.")
            else:
                input_payload = PersonaInput.model_validate(json.loads(chosen.read_text()))
                with st.spinner("Running the pipeline (real discovery + LLM calls)..."):
                    _, trace_path = run_from_payload(input_payload, enable_reviewer=enable_reviewer)
                    _load_trace_and_render(Trace.load(trace_path))
                st.success(f"Done -- trace saved to {trace_path}")

    else:
        trace_paths = sorted(OUTPUTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        chosen_trace_path = st.selectbox("Trace file", trace_paths, format_func=lambda p: p.name)
        if st.button("Load"):
            _load_trace_and_render(Trace.load(chosen_trace_path))
            st.success(f"Loaded {chosen_trace_path.name}")

if st.session_state.trace is None:
    st.info("Run a new persona, run a test_set scenario, or load a previous run's trace from the sidebar.")
else:
    output = st.session_state.output
    if isinstance(output, OutOfScopeOutput):
        st.warning(f"Out of scope: {output.message}")
    elif output is not None:
        st.markdown(render_markdown(output))
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
        if not api_key_present():
            answer = "OPENROUTER_API_KEY is not set -- copy .env.example to .env and fill it in."
        else:
            with st.spinner("Thinking..."):
                answer = answer_question(st.session_state.trace, question)
        st.session_state.chat_history.append(("assistant", answer))
        with st.chat_message("assistant"):
            st.write(answer)
