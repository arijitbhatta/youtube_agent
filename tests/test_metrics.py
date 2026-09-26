"""eval/metrics.py -- pure functions, no LLM, no network (HLD.md §6.1)."""
from agent.schemas import CurriculumItem, CurriculumOutput, ReviewSummary
from eval.metrics import (
    budget_compliance,
    constraint_compliance,
    content_quality,
    curriculum_shape_sanity,
    grounding_rate,
    known_topic_leakage,
    redundancy,
    sequencing_sanity,
    unknown_topic_coverage,
)
from tests.factories import make_scored


def _output(total_minutes, budget_minutes, curriculum=None, warnings=None, goal_coverage=None):
    return CurriculumOutput(
        persona_id="p",
        budget_minutes=budget_minutes,
        total_minutes=total_minutes,
        curriculum=curriculum or [],
        review=ReviewSummary(iterations=1, approved=True, unresolved_blocking_issues=[]),
        considered_and_dropped=[],
        warnings=warnings or [],
        goal_coverage=goal_coverage or {},
    )


def _item(video_id, duration, evidence_snippet="s"):
    return CurriculumItem(
        order=1,
        video_id=video_id,
        title=video_id,
        url="https://x",
        duration_minutes=duration,
        reason="r",
        evidence_snippet=evidence_snippet,
        confidence=0.8,
        grounded=True,
    )


def test_budget_compliance_hard_fail_on_overage():
    result = budget_compliance(_output(120, 100))
    assert result["hard_fail"] is True


def test_budget_compliance_soft_flag_when_far_under():
    result = budget_compliance(_output(40, 100))  # 60% under
    assert result["hard_fail"] is False
    assert result["soft_flag"] is True


def test_budget_compliance_passes_within_tolerance():
    result = budget_compliance(_output(85, 100))  # 15% under
    assert result["hard_fail"] is False
    assert result["soft_flag"] is False


def test_shape_sanity_flags_zero_when_feasible():
    result = curriculum_shape_sanity(_output(0, 100, curriculum=[]), candidate_pool_size=10)
    assert result["zero_unjustified"] is True
    assert result["passes"] is False


def test_shape_sanity_ok_with_infeasibility_warning():
    output = _output(0, 100, curriculum=[], warnings=["budget too small to responsibly cover this goal"])
    result = curriculum_shape_sanity(output, candidate_pool_size=10)
    assert result["zero_unjustified"] is False
    assert result["passes"] is True


def test_shape_sanity_flags_one_video_dominating():
    output = _output(95, 100, curriculum=[_item("a", 95)])
    result = curriculum_shape_sanity(output, candidate_pool_size=10)
    assert result["one_dominates_without_warning"] is True
    assert result["passes"] is False


def test_unknown_topic_coverage():
    output = _output(50, 100, goal_coverage={"React": "covered", "Vite": "uncovered"})
    result = unknown_topic_coverage(output)
    assert result["fraction_covered"] == 0.5


def test_known_topic_leakage():
    picks = [make_scored("a", 10, 1.0, cluster_id=0)]
    picks[0].understanding.overlaps_known.append("JavaScript fundamentals")
    result = known_topic_leakage(picks)
    assert result["picks_with_overlap"] == 1


def test_constraint_compliance_passes_when_clean():
    clean = make_scored("a", 10, 1.0, cluster_id=0)
    result = constraint_compliance([clean])
    assert result["passes"] is True


def test_redundancy_flags_shared_cluster():
    a = make_scored("a", 10, 1.0, cluster_id=0)
    b = make_scored("b", 10, 1.0, cluster_id=0)
    result = redundancy([a, b])
    assert result["duplicate_cluster_count"] == 1
    assert result["passes"] is False


def test_grounding_rate_matches_real_substring():
    output = _output(10, 100, curriculum=[_item("a", 10, evidence_snippet="today we set up vite")])
    transcripts = {"a": "in this video today we set up vite and react from scratch"}
    result = grounding_rate(output, transcripts)
    assert result["rate"] == 1.0


def test_grounding_rate_flags_fabricated_snippet():
    output = _output(10, 100, curriculum=[_item("a", 10, evidence_snippet="this never appears anywhere")])
    transcripts = {"a": "in this video today we set up vite and react from scratch"}
    result = grounding_rate(output, transcripts)
    assert result["rate"] == 0.0


def test_sequencing_sanity_detects_inversion():
    setup = make_scored("a", 10, 1.0, cluster_id=0, phase="setup")
    concept = make_scored("b", 10, 1.0, cluster_id=1, phase="concept")
    result = sequencing_sanity([concept, setup])  # wrong order
    assert result["inversions"] == 1
    assert result["passes"] is False


def test_content_quality_flags_below_pool_average():
    picks = [make_scored("a", 10, 1.0, cluster_id=0)]
    picks[0].understanding.quality_signal.clarity = 0.2
    picks[0].understanding.quality_signal.content_density = 0.2
    pool = picks + [make_scored("b", 10, 1.0, cluster_id=1)]
    result = content_quality(picks, pool)
    assert result["below_pool_average"] is True
