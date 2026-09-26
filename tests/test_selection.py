"""agent/selection.py -- pure code, no LLM, no count target. Assert on the
exact subset/order that comes out (HLD.md §4)."""
from agent.selection import select_and_sequence
from tests.factories import make_scored


def _ids(scored_list):
    return [sc.candidate.video_id for sc in scored_list]


def test_greedy_skips_redundant_cluster_and_sequences_by_phase():
    setup1 = make_scored("setup1", 30, 1.0, cluster_id=1, matches_unknown=["Vite tooling"], phase="setup")
    concept1 = make_scored("concept1", 60, 1.5, cluster_id=2, matches_unknown=["React"], phase="concept")
    concept1_dup = make_scored(
        "concept1_dup", 60, 1.0, cluster_id=2, matches_unknown=["React"], phase="concept"
    )
    project1 = make_scored(
        "project1", 90, 2.0, cluster_id=3, matches_unknown=["React hooks"], phase="hands-on-project"
    )

    selected, dropped, warnings = select_and_sequence(
        [setup1, concept1, concept1_dup, project1],
        budget_minutes=200,
        unknown_topics=["Vite tooling", "React", "React hooks"],
    )

    assert _ids(selected) == ["setup1", "concept1", "project1"]
    assert _ids(dropped) == ["concept1_dup"]
    assert warnings == []


def test_coverage_check_swaps_in_a_pick_that_closes_a_gap():
    x1 = make_scored("x1", 10, 5.0, cluster_id=1, matches_unknown=["A"], phase="setup")
    x2 = make_scored("x2", 10, 5.0, cluster_id=2, matches_unknown=["B"], phase="concept")
    filler = make_scored("filler", 15, 2.5, cluster_id=3, matches_unknown=[], phase="concept")
    x3 = make_scored("x3", 15, 2.0, cluster_id=4, matches_unknown=["C"], phase="hands-on-project")

    selected, dropped, warnings = select_and_sequence(
        [x1, x2, filler, x3], budget_minutes=40, unknown_topics=["A", "B", "C"]
    )

    # Greedy alone would pick x1, x2, filler (bang-per-minute: 0.5, 0.5,
    # 0.167 > x3's 0.133) and leave "C" uncovered; the coverage check then
    # swaps filler out for x3, since filler is the lowest-utility pick and
    # dropping it (rather than x1 or x2) doesn't reopen any other gap.
    assert _ids(selected) == ["x1", "x2", "x3"]
    assert _ids(dropped) == ["filler"]
    assert warnings == []


def test_infeasible_budget_produces_empty_curriculum_with_honest_warning():
    only_candidate = make_scored("only1", 500, 3.0, cluster_id=1, matches_unknown=["A"])

    selected, dropped, warnings = select_and_sequence(
        [only_candidate], budget_minutes=100, unknown_topics=["A"]
    )

    assert selected == []
    assert _ids(dropped) == ["only1"]
    assert any("budget too small" in w for w in warnings)


def test_never_exceeds_budget():
    a = make_scored("a", 40, 2.0, cluster_id=1, matches_unknown=["A"])
    b = make_scored("b", 40, 2.0, cluster_id=2, matches_unknown=["B"])
    c = make_scored("c", 40, 2.0, cluster_id=3, matches_unknown=["C"])

    selected, _, _ = select_and_sequence([a, b, c], budget_minutes=90, unknown_topics=["A", "B", "C"])

    assert sum(sc.candidate.duration_minutes for sc in selected) <= 90
