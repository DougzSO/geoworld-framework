"""
tests/unit/test_topsis.py
==========================
Unit tests for src/utils/topsis.py (TOPSIS spatial suitability scoring).

Scope: pure-function behaviour of topsis_spatial() on small, hand-built
arrays with known analytical outcomes. No country fixtures, no raster
I/O (see QI-002 for integration-level tests).
"""

from __future__ import annotations

import numpy as np
import pytest

from src.utils.topsis import topsis_spatial


def test_topsis_spatial_weight_sum_validation():
    """Weights that don't sum to 1.0 (within WEIGHT_SUM_TOLERANCE) must raise."""
    criteria = {
        "a": np.array([[1.0, 0.0]], dtype=np.float32),
        "b": np.array([[1.0, 0.0]], dtype=np.float32),
    }
    mask = np.array([[True, True]])
    with pytest.raises(ValueError, match="must sum to 1.0"):
        topsis_spatial(criteria, {"a": 0.5, "b": 0.6}, mask)


def test_topsis_spatial_best_pixel_scores_near_one():
    """A pixel that is simultaneously maximal on every criterion is its own
    positive ideal solution: d+ = 0, so S = d- / (d+ + d-) = 1.0."""
    criteria = {
        "a": np.array([[1.0, 0.0], [0.5, 0.5]], dtype=np.float32),
        "b": np.array([[1.0, 0.0], [0.5, 0.5]], dtype=np.float32),
    }
    mask = np.full((2, 2), True)
    out = topsis_spatial(criteria, {"a": 0.5, "b": 0.5}, mask)
    assert out[0, 0] == pytest.approx(1.0, abs=1e-5)


def test_topsis_spatial_worst_pixel_scores_near_zero():
    """A pixel that is simultaneously minimal on every criterion is its own
    negative ideal solution: d- = 0, so S = 0 / (d+ + 0) = 0.0."""
    criteria = {
        "a": np.array([[1.0, 0.0], [0.5, 0.5]], dtype=np.float32),
        "b": np.array([[1.0, 0.0], [0.5, 0.5]], dtype=np.float32),
    }
    mask = np.full((2, 2), True)
    out = topsis_spatial(criteria, {"a": 0.5, "b": 0.5}, mask)
    assert out[0, 1] == pytest.approx(0.0, abs=1e-5)


def test_topsis_spatial_equidistant_pixel_scores_half():
    """A pixel equally far from both criteria's max and min sits exactly
    between PIS and NIS: S = 0.5."""
    criteria = {
        "a": np.array([[1.0, 0.0], [0.5, 0.5]], dtype=np.float32),
        "b": np.array([[1.0, 0.0], [0.5, 0.5]], dtype=np.float32),
    }
    mask = np.full((2, 2), True)
    out = topsis_spatial(criteria, {"a": 0.5, "b": 0.5}, mask)
    assert out[1, 0] == pytest.approx(0.5, abs=1e-5)
    assert out[1, 1] == pytest.approx(0.5, abs=1e-5)


def test_topsis_spatial_invalid_pixels_are_nan():
    criteria = {
        "a": np.array([[1.0, 0.0]], dtype=np.float32),
        "b": np.array([[1.0, 0.0]], dtype=np.float32),
    }
    mask = np.array([[True, False]])
    out = topsis_spatial(criteria, {"a": 0.5, "b": 0.5}, mask)
    assert np.isnan(out[0, 1])
    assert not np.isnan(out[0, 0])


def test_topsis_spatial_all_invalid_mask_returns_all_nan():
    criteria = {
        "a": np.array([[1.0, 0.0], [0.3, 0.7]], dtype=np.float32),
        "b": np.array([[1.0, 0.0], [0.3, 0.7]], dtype=np.float32),
    }
    mask = np.full((2, 2), False)
    out = topsis_spatial(criteria, {"a": 0.5, "b": 0.5}, mask)
    assert out.shape == (2, 2)
    assert np.all(np.isnan(out))


def test_topsis_spatial_scores_bounded_in_unit_interval():
    """Regardless of input spread, valid TOPSIS scores must lie in [0, 1]."""
    rng = np.random.default_rng(42)
    h, w = 20, 20
    criteria = {
        "solar_resource": rng.random((h, w)).astype(np.float32),
        "terrain_score": rng.random((h, w)).astype(np.float32),
        "grid_suitability": rng.random((h, w)).astype(np.float32),
    }
    mask = np.full((h, w), True)
    weights = {"solar_resource": 0.5, "terrain_score": 0.3, "grid_suitability": 0.2}
    out = topsis_spatial(criteria, weights, mask)
    finite = out[np.isfinite(out)]
    assert finite.size == h * w
    assert finite.min() >= 0.0
    assert finite.max() <= 1.0


def test_topsis_spatial_chunking_does_not_change_result():
    """The chunked evaluation loop must be numerically equivalent to a
    single-chunk pass -- chunk_size is purely a memory-management knob."""
    rng = np.random.default_rng(7)
    h, w = 15, 15
    criteria = {
        "a": rng.random((h, w)).astype(np.float32),
        "b": rng.random((h, w)).astype(np.float32),
        "c": rng.random((h, w)).astype(np.float32),
    }
    mask = np.full((h, w), True)
    weights = {"a": 0.4, "b": 0.35, "c": 0.25}

    out_single_chunk = topsis_spatial(criteria, weights, mask, chunk_size=1_000_000)
    out_tiny_chunks = topsis_spatial(criteria, weights, mask, chunk_size=7)

    assert np.allclose(out_single_chunk, out_tiny_chunks, equal_nan=True)
