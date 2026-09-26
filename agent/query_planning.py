"""Query planning (HLD.md §3.1) -- one Claude call: given goal + context,
produce targeted YouTube search queries decomposed by `unknown` item rather
than searching the goal string alone.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from agent.llm_client import call_structured
from agent.schemas import PersonaInput

_PROMPT_TEMPLATE = """Given this learner's goal and context, produce 4 to 8 \
targeted YouTube search queries that would surface videos to build a \
curriculum from. Decompose by the learner's `unknown` items -- one query \
per unknown item where that makes sense -- plus one or two queries phrased \
around the goal itself (a capstone/project search). Do not just search the \
goal alone; setup/prerequisite content matters as much as capstone content.

goal: {goal}
background: {background}
known: {known}
unknown: {unknown}
constraints: {constraints}
"""


class QueryPlan(BaseModel):
    queries: list[str] = Field(min_length=4, max_length=8)


def plan_queries(input_payload: PersonaInput, *, trace=None) -> list[str]:
    prompt = _PROMPT_TEMPLATE.format(
        goal=input_payload.goal,
        background=input_payload.user_context.background,
        known=", ".join(input_payload.user_context.known) or "(none stated)",
        unknown=", ".join(input_payload.user_context.unknown) or "(none stated)",
        constraints=input_payload.user_context.constraints or "(none stated)",
    )
    plan = call_structured(
        prompt, QueryPlan, tier="cheap", trace=trace, trace_label="query_planning"
    )
    return plan.queries
