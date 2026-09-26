"""agent/scoring.py -- pure arithmetic, no network, no LLM."""
import pytest

from agent.scoring import score_candidates
from tests.factories import make_understanding


def test_topic_weight_diminishes_for_later_coverer():
    strong = make_understanding("a", matches_unknown=["React"], confidence=0.9)
    weak = make_understanding("b", matches_unknown=["React"], confidence=0.5)
    utility = score_candidates([strong, weak], ["React"], {"a": 0, "b": 1})
    assert utility["a"] > utility["b"]


def test_known_overlap_penalty_reduces_utility():
    clean = make_understanding("a", matches_unknown=["React"])
    overlapping = make_understanding(
        "b", matches_unknown=["React"], overlaps_known=["JavaScript fundamentals"]
    )
    utility = score_candidates([clean, overlapping], ["React"], {"a": 0, "b": 1})
    assert utility["b"] < utility["a"]


def test_constraint_violation_penalizes():
    clean = make_understanding("a", matches_unknown=["React"])
    violating = make_understanding(
        "b", matches_unknown=["React"], constraint_violations=["surface_intro"]
    )
    utility = score_candidates([clean, violating], ["React"], {"a": 0, "b": 1})
    assert utility["b"] < utility["a"]


def test_quality_multiplier_scales_utility():
    high_quality = make_understanding("a", matches_unknown=["React"], clarity=0.9, content_density=0.9)
    low_quality = make_understanding("b", matches_unknown=["React"], clarity=0.2, content_density=0.2)
    utility = score_candidates([high_quality, low_quality], ["React"], {"a": 0, "b": 1})
    assert utility["a"] > utility["b"]


def test_novelty_bonus_only_for_best_in_cluster():
    a = make_understanding("a", matches_unknown=["React"])
    b = make_understanding("b", matches_unknown=["Vite"])
    # Equal pre-bonus utility (each uniquely covers one of two unknown
    # topics with identical confidence/quality) -- only "a" should get the
    # +0.05 novelty bonus, as the first-encountered best-in-cluster.
    utility = score_candidates([a, b], ["React", "Vite"], {"a": 0, "b": 0})
    assert utility["a"] - utility["b"] == pytest.approx(0.05)


def test_no_unknown_topics_zeroes_relevance():
    record = make_understanding("a", matches_unknown=[])
    utility = score_candidates([record], [], {"a": 0})
    assert utility["a"] == 0.0
