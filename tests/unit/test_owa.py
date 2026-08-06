"""
tests/unit/test_owa.py
=======================
Unit tests for src/utils/owa.py (Ordered Weighted Averaging).

Scope: pure-function behaviour of prepare_owa_weights() and owa_spatial()
on small, hand-verified inputs. No country fixtures, no raster I/O
(see QI-002 for integration-level tests).
"""

from __future__ import annotations

import numpy as np
import pytest

from src.utils.owa import OWA_DEFAULTS, owa_spatial, prepare_owa_weights


# ─────────────────────────────────────────────────────────────────────────
# prepare_owa_weights
# ─────────────────────────────────────────────────────────────────────────

def test_prepare_owa_weights_exact_length_normalizes():
    """OWA_DEFAULTS vectors already sum to 1.0 and must pass through
    unchanged (within float32 precision)."""
    for scenario, raw in OWA_DEFAULTS.items():
        w = prepare_owa_weights(raw, len(raw))
        assert w.sum() == pytest.approx(1.0, abs=1e-6), scenario
        assert np.allclose(w, raw, atol=1e-6), scenario


def test_prepare_owa_weights_unnormalized_input_is_normalized():
    w = prepare_owa_weights([1.0, 1.0, 1.0], 3)
    assert w.sum() == pytest.approx(1.0, abs=1e-6)
    assert np.allclose(w, [1 / 3, 1 / 3, 1 / 3], atol=1e-6)


def test_prepare_owa_weights_pads_shorter_vector_with_last_value():
    """A 2-element vector requested for 3 criteria repeats the last value,
    then renormalizes: [0.6, 0.4, 0.4] / 1.4."""
    w = prepare_owa_weights([0.6, 0.4], 3)
    expected = np.array([0.6, 0.4, 0.4]) / 1.4
    assert np.allclose(w, expected, atol=1e-6)
    assert w.sum() == pytest.approx(1.0, abs=1e-6)


def test_prepare_owa_weights_truncates_longer_vector():
    """A 4-element vector requested for 2 criteria truncates to the first
    2 elements, then renormalizes: [0.5, 0.3] / 0.8."""
    w = prepare_owa_weights([0.5, 0.3, 0.1, 0.1], 2)
    expected = np.array([0.5, 0.3]) / 0.8
    assert np.allclose(w, expected, atol=1e-6)
    assert len(w) == 2


def test_prepare_owa_weights_all_zero_falls_back_to_uniform():
    """total <= 0.0 is an explicit guarded branch: fall back to equal weights."""
    w = prepare_owa_weights([0.0, 0.0, 0.0], 3)
    assert np.allclose(w, [1 / 3, 1 / 3, 1 / 3], atol=1e-6)


def test_prepare_owa_weights_returns_float32():
    w = prepare_owa_weights([0.7, 0.2, 0.1], 3)
    assert w.dtype == np.float32


# ─────────────────────────────────────────────────────────────────────────
# owa_spatial
# ─────────────────────────────────────────────────────────────────────────

def test_owa_spatial_weight_length_mismatch_raises():
    criteria = {
        "a": np.array([[1.0, 0.0]], dtype=np.float32),
        "b": np.array([[1.0, 0.0]], dtype=np.float32),
    }
    mask = np.array([[True, True]])
    with pytest.raises(ValueError, match="criteria count"):
        owa_spatial(criteria, np.array([0.5, 0.3, 0.2], dtype=np.float32), mask)


def test_owa_spatial_known_pixel_matches_manual_calculation():
    """Pixel (0, 0): values a=1.0, b=0.0, c=0.5 -- sorted descending
    [1.0, 0.5, 0.0], weighted by the real 'optimistic' OWA vector
    [0.7, 0.2, 0.1]: 0.7*1.0 + 0.2*0.5 + 0.1*0.0 = 0.80."""
    criteria = {
        "a": np.array([[1.0, 0.2], [0.5, np.nan]], dtype=np.float32),
        "b": np.array([[0.0, 0.8], [0.5, 0.9]], dtype=np.float32),
        "c": np.array([[0.5, 0.5], [0.5, 0.1]], dtype=np.float32),
    }
    mask = np.array([[True, True], [True, False]])
    weights = prepare_owa_weights(OWA_DEFAULTS["optimistic"], 3)
    out = owa_spatial(criteria, weights, mask)
    assert out[0, 0] == pytest.approx(0.80, abs=1e-5)
    # Pixel (0, 1): values a=0.2, b=0.8, c=0.5 -- sorted [0.8, 0.5, 0.2]:
    # 0.7*0.8 + 0.2*0.5 + 0.1*0.2 = 0.68
    assert out[0, 1] == pytest.approx(0.68, abs=1e-5)
    # Pixel (1, 0): all three criteria equal 0.5 -- order-independent, S=0.5
    assert out[1, 0] == pytest.approx(0.5, abs=1e-5)
    # Pixel (1, 1) masked out
    assert np.isnan(out[1, 1])


def test_owa_spatial_equal_weights_reduce_to_arithmetic_mean():
    """With perfectly equal weights, order doesn't matter -- OWA collapses
    to the plain arithmetic mean of the criteria at each pixel."""
    criteria = {
        "a": np.array([[0.9, 0.1]], dtype=np.float32),
        "b": np.array([[0.1, 0.9]], dtype=np.float32),
        "c": np.array([[0.5, 0.5]], dtype=np.float32),
    }
    mask = np.array([[True, True]])
    weights = np.array([1 / 3, 1 / 3, 1 / 3], dtype=np.float32)
    out = owa_spatial(criteria, weights, mask)
    assert out[0, 0] == pytest.approx(0.5, abs=1e-5)
    assert out[0, 1] == pytest.approx(0.5, abs=1e-5)


def test_owa_spatial_invalid_pixels_are_nan():
    criteria = {
        "a": np.array([[1.0, 0.0]], dtype=np.float32),
        "b": np.array([[1.0, 0.0]], dtype=np.float32),
    }
    mask = np.array([[True, False]])
    out = owa_spatial(criteria, np.array([0.5, 0.5], dtype=np.float32), mask)
    assert not np.isnan(out[0, 0])
    assert np.isnan(out[0, 1])


def test_owa_spatial_all_invalid_mask_returns_all_nan():
    criteria = {
        "a": np.array([[1.0, 0.0], [0.3, 0.7]], dtype=np.float32),
        "b": np.array([[1.0, 0.0], [0.3, 0.7]], dtype=np.float32),
    }
    mask = np.full((2, 2), False)
    out = owa_spatial(criteria, np.array([0.5, 0.5], dtype=np.float32), mask)
    assert out.shape == (2, 2)
    assert np.all(np.isnan(out))


def test_owa_spatial_optimistic_scenario_favors_best_criterion():
    """The real 'optimistic' vector [0.70, 0.20, 0.10] heavily overweights
    the best-scoring criterion, so its OWA score must exceed the plain
    arithmetic mean whenever criteria disagree."""
    criteria = {
        "a": np.array([[0.9]], dtype=np.float32),
        "b": np.array([[0.1]], dtype=np.float32),
        "c": np.array([[0.1]], dtype=np.float32),
    }
    mask = np.array([[True]])
    weights = prepare_owa_weights(OWA_DEFAULTS["optimistic"], 3)
    out = owa_spatial(criteria, weights, mask)
    arithmetic_mean = (0.9 + 0.1 + 0.1) / 3.0
    assert out[0, 0] > arithmetic_mean


def test_owa_spatial_chunking_does_not_change_result():
    rng = np.random.default_rng(11)
    h, w = 15, 15
    criteria = {
        "a": rng.random((h, w)).astype(np.float32),
        "b": rng.random((h, w)).astype(np.float32),
        "c": rng.random((h, w)).astype(np.float32),
    }
    mask = np.full((h, w), True)
    weights = prepare_owa_weights(OWA_DEFAULTS["balanced"], 3)

    out_single_chunk = owa_spatial(criteria, weights, mask, chunk_size=1_000_000)
    out_tiny_chunks = owa_spatial(criteria, weights, mask, chunk_size=7)

    assert np.allclose(out_single_chunk, out_tiny_chunks, equal_nan=True)
