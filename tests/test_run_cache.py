"""Network-free tests for agent/run_cache.py's exact-match run cache
(ui/app.py only) -- same temp-sqlite-via-monkeypatch pattern as
tests/test_cache.py."""
from __future__ import annotations

from agent import run_cache
from agent.schemas import PersonaInput, UserContext


def _use_temp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(run_cache, "CACHE_PATH", tmp_path / "test_cache.sqlite3")


def _persona(goal="learn things", **kwargs) -> PersonaInput:
    defaults = dict(background="x", known=[], unknown=[], constraints="")
    defaults.update(kwargs)
    return PersonaInput(
        persona_id="p",
        goal=goal,
        time_budget_minutes=60,
        user_context=UserContext(**defaults),
    )


def test_miss_returns_none(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    assert run_cache.get_cached_trace_path(_persona(), True) is None


def test_set_then_get_round_trips(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    run_cache.set_cached_trace_path(_persona(), True, "outputs/run123.json")
    assert run_cache.get_cached_trace_path(_persona(), True) == "outputs/run123.json"


def test_different_goal_text_is_a_different_key(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    run_cache.set_cached_trace_path(_persona(goal="learn React"), True, "outputs/react.json")
    assert run_cache.get_cached_trace_path(_persona(goal="learn Vue"), True) is None


def test_different_enable_reviewer_is_a_different_key(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    run_cache.set_cached_trace_path(_persona(), True, "outputs/with_review.json")
    assert run_cache.get_cached_trace_path(_persona(), False) is None


def test_same_payload_and_flag_hits_regardless_of_field_order(tmp_path, monkeypatch):
    _use_temp_cache(tmp_path, monkeypatch)
    a = _persona(known=["a", "b"])
    b = _persona(known=["a", "b"])
    run_cache.set_cached_trace_path(a, False, "outputs/x.json")
    assert run_cache.get_cached_trace_path(b, False) == "outputs/x.json"
