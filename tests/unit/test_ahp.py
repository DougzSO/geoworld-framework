"""
tests/unit/test_ahp.py
=======================
Unit tests for src/utils/ahp.py (Analytic Hierarchy Process).

Scope: pure-function behaviour of the Saaty pairwise-matrix builder and
the geometric-mean weight/consistency-ratio solver. No country fixtures,
no I/O, no pipeline integration (see QI-002 for that).
"""

from __future__ import annotations

import numpy as np
import pytest

from src.core.constants import AHP_CR_THRESHOLD, AHP_RANDOM_INDEX
from src.utils.ahp import ahp_weights, build_saaty_matrix, compute_ahp_weights


# ─────────────────────────────────────────────────────────────────────────
# build_saaty_matrix
# ─────────────────────────────────────────────────────────────────────────

def test_build_saaty_matrix_diagonal_is_one():
    criteria = ["solar_resource", "terrain_score", "grid_suitability"]
    mat = build_saaty_matrix(criteria, criteria, "moderate")
    assert np.allclose(np.diag(mat), 1.0)


def test_build_saaty_matrix_is_reciprocal():
    """mat[i, j] * mat[j, i] == 1 for every pair, per the module's own contract."""
    criteria = ["solar_resource", "terrain_score", "grid_suitability", "road_suitability"]
    mat = build_saaty_matrix(criteria, criteria, "moderate")
    assert np.allclose(mat * mat.T, 1.0)


def test_build_saaty_matrix_higher_priority_gets_larger_value():
    """First-ranked criterion must dominate later-ranked ones (mat[i, j] > 1
    when criterion i outranks criterion j)."""
    criteria = ["a", "b", "c"]
    mat = build_saaty_matrix(criteria, priority_order=["a", "b", "c"], intensity="moderate")
    assert mat[0, 1] > 1.0   # a > b
    assert mat[0, 2] > 1.0   # a > c
    assert mat[1, 2] > 1.0   # b > c
    assert mat[1, 0] < 1.0   # b < a
    assert mat[2, 0] < 1.0   # c < a


def test_build_saaty_matrix_criteria_absent_from_priority_ranked_last():
    """Criteria not present in priority_order are appended (lowest priority)."""
    criteria = ["a", "b", "c"]
    mat = build_saaty_matrix(criteria, priority_order=["b"], intensity="moderate")
    # b (explicit, rank 0) must dominate both a and c (implicit, appended after).
    assert mat[1, 0] > 1.0
    assert mat[1, 2] > 1.0


# ─────────────────────────────────────────────────────────────────────────
# ahp_weights
# ─────────────────────────────────────────────────────────────────────────

def test_ahp_weights_sum_to_one():
    criteria = ["solar_resource", "terrain_score", "grid_suitability", "road_suitability"]
    mat = build_saaty_matrix(criteria, criteria, "moderate")
    w, _lam, _ci, _cr = ahp_weights(mat)
    assert w.sum() == pytest.approx(1.0, abs=1e-9)


def test_ahp_weights_ordering_matches_priority():
    """Weights must be strictly decreasing in the same order as priority_order,
    for a matrix built directly from that same order."""
    criteria = ["solar_resource", "terrain_score", "grid_suitability", "road_suitability"]
    mat = build_saaty_matrix(criteria, criteria, "moderate")
    w, _lam, _ci, _cr = ahp_weights(mat)
    assert w[0] > w[1] > w[2] > w[3]


def test_ahp_weights_consistent_matrix_is_below_threshold():
    """A Saaty matrix built from a strict, non-contradictory priority order
    should be well within the standard CR <= 0.10 acceptance threshold."""
    criteria = ["solar_resource", "terrain_score", "grid_suitability", "road_suitability"]
    mat = build_saaty_matrix(criteria, criteria, "moderate")
    _w, _lam, _ci, cr = ahp_weights(mat)
    assert cr < AHP_CR_THRESHOLD


def test_ahp_weights_flags_inconsistent_matrix_above_threshold():
    """A hand-built matrix with a circular dominance pattern (a > b > c > a,
    each by the maximum Saaty intensity) is a textbook-inconsistent case and
    must produce CR far above the 0.10 acceptance threshold."""
    inconsistent = np.array([
        [1.0, 9.0, 1.0 / 9.0],
        [1.0 / 9.0, 1.0, 9.0],
        [9.0, 1.0 / 9.0, 1.0],
    ])
    _w, _lam, _ci, cr = ahp_weights(inconsistent)
    assert cr > AHP_CR_THRESHOLD


def test_ahp_weights_single_criterion_is_trivially_consistent():
    """n=1: RI(1)=0.0, so the CR guard (ri > 0 else 0.0) must return CR=0.0
    and the sole criterion must receive full weight."""
    mat = np.array([[1.0]])
    w, lam, ci, cr = ahp_weights(mat)
    assert w[0] == pytest.approx(1.0)
    assert lam == pytest.approx(1.0)
    assert ci == pytest.approx(0.0)
    assert cr == pytest.approx(0.0)


def test_ahp_weights_two_criteria_always_zero_cr():
    """n=2: RI(2)=0.0 in AHP_RANDOM_INDEX, so CR is defined as 0.0 by the
    module's own guard regardless of the matrix's actual inconsistency."""
    assert AHP_RANDOM_INDEX[2] == 0.0
    mat = build_saaty_matrix(["a", "b"], ["a", "b"], "moderate")
    _w, _lam, _ci, cr = ahp_weights(mat)
    assert cr == pytest.approx(0.0, abs=1e-9)


# ─────────────────────────────────────────────────────────────────────────
# compute_ahp_weights (high-level interface)
# ─────────────────────────────────────────────────────────────────────────

def test_compute_ahp_weights_returns_dict_keyed_by_criterion():
    criteria = ["solar_resource", "terrain_score", "grid_suitability"]
    weights, lam, ci, cr = compute_ahp_weights(criteria, criteria, "moderate")
    assert set(weights.keys()) == set(criteria)
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-9)
    assert weights["solar_resource"] > weights["terrain_score"] > weights["grid_suitability"]
    assert lam >= len(criteria)  # lambda_max >= n always holds for a reciprocal PCM
    assert ci >= 0.0
    assert cr >= 0.0


@pytest.mark.parametrize("intensity", ["subtle", "moderate", "strong"])
def test_compute_ahp_weights_all_intensities_stay_consistent_for_small_n(intensity):
    """All three Saaty-scale intensities, applied to a small, strictly-ordered
    criteria set, should stay within the standard consistency threshold."""
    criteria = ["a", "b", "c"]
    _weights, _lam, _ci, cr = compute_ahp_weights(criteria, criteria, intensity)
    assert cr < AHP_CR_THRESHOLD
