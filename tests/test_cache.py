"""Network-free tests for agent/cache.py's sqlite understanding cache
(HLD.md §8) -- no LLM call, a temp sqlite file per test via monkeypatching
agent.config.CACHE_PATH."""
from __future__ import annotations

from agent import cache
from agent.schemas import PersonaInput, UserContext
from tests.factories import make_understanding


def _persona(known=None, unknown=None, constraints="") -> PersonaInput:
    return PersonaInput(
        persona_id="p",
        goal="learn things",
        time_budget_minutes=60,
        user_context=UserContext(background="x", known=known or [], unknown=unknown or [], constraints=constraints),
    )


def _use_temp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_PATH", tmp_path / "test_cache.sqlite3")


def test_set_then_get_round_trips(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    record = make_understanding("vid00000001")
    key = cache.make_key("vid00000001", _persona())
    cache.set_many({key: record})
    result = cache.get_many([key])
    assert result[key].video_id == "vid00000001"


def test_get_many_misses_return_empty(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    assert cache.get_many(["nonexistent:abc"]) == {}


def test_different_persona_unknown_lists_get_different_keys(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    key_a = cache.make_key("vid00000001", _persona(unknown=["hooks"]))
    key_b = cache.make_key("vid00000001", _persona(unknown=["routing"]))
    assert key_a != key_b


def test_same_persona_fields_get_same_key_regardless_of_goal_text(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    persona_a = PersonaInput(
        persona_id="a",
        goal="learn React this weekend",
        time_budget_minutes=60,
        user_context=UserContext(background="x", known=["js"], unknown=["hooks"], constraints=""),
    )
    persona_b = PersonaInput(
        persona_id="b",
        goal="a completely different goal string",
        time_budget_minutes=999,
        user_context=UserContext(background="y", known=["js"], unknown=["hooks"], constraints=""),
    )
    assert cache.make_key("vid00000001", persona_a) == cache.make_key("vid00000001", persona_b)
