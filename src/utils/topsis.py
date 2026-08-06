"""
src/utils/topsis.py
===================
TOPSIS (Technique for Order of Preference by Similarity to Ideal Solution).

Spatially explicit TOPSIS evaluation for raster criteria arrays.

References
----------
[1] Hwang, C.L., & Yoon, K. (1981). Multiple Attribute Decision Making. Springer.
[2] Al Garni, H.Z., & Awasthi, A. (2017). Renew. Sustain. Energy Rev. 76:641–662.
"""

from __future__ import annotations

import logging
from typing import Dict

import numpy as np

logger = logging.getLogger("geoworld.utils.topsis")

# Numerical tolerance for weight-sum validation.
WEIGHT_SUM_TOLERANCE: float = 1e-6

# Score assigned to hard-excluded pixels inside the country boundary.
# Kept as 0.0 so that downstream phases can distinguish them from outside-country NaN.
TOPSIS_EXCLUSION_SCORE: float = 0.0


def topsis_spatial(
    criteria_arrays: Dict[str, np.ndarray],
    weights: Dict[str, float],
    valid_mask: np.ndarray,
    chunk_size: int = 500_000,
) -> np.ndarray:
    """Memory-optimized, spatially explicit TOPSIS evaluation.

    Mathematical Formulation
    ------------------------
    1. **Vector normalisation** per criterion *j* over all valid pixels:

       .. math:: r_{ij} = x_{ij} / \\sqrt{\\sum_i x_{ij}^2}

    2. **Weight application**:

       .. math:: v_{ij} = w_j \\cdot r_{ij}

    3. **Positive/Negative Ideal Solutions** (PIS / NIS):

       .. math::
           A^+ = \\{\\max_i v_{ij}\\}_{j=1}^{n}, \\quad
           A^- = \\{\\min_i v_{ij}\\}_{j=1}^{n}

    4. **Separation distances**:

       .. math::
           d_i^+ = \\sqrt{\\sum_j (v_{ij} - A^+_j)^2}, \\quad
           d_i^- = \\sqrt{\\sum_j (v_{ij} - A^-_j)^2}

    5. **Relative Closeness Score**:

       .. math:: S_i = d_i^- / (d_i^+ + d_i^-)

    Memory Architecture
    -------------------
    Column norms, PIS, and NIS are computed sequentially per criterion
    (O(N) peak memory).  Valid-pixel indices are cached once via
    ``np.where(flat_valid)[0]`` and reused across chunks, avoiding repeated
    boolean indexing (O(N) index cost paid once instead of per chunk).

    Args:
        criteria_arrays: Criterion name → float32 array clipped to [0, 1].
        weights:         AHP-derived weights (must sum to 1.0 ± tolerance).
        valid_mask:      Boolean mask — True for pixels to evaluate.
        chunk_size:      Pixels per iteration for memory control.

    Returns:
        2D float32 array of TOPSIS scores in [0, 1]; NaN for invalid pixels.

    Raises:
        ValueError: If weights do not sum to 1.0 within ``WEIGHT_SUM_TOLERANCE``.
    """
    weight_sum = sum(weights.values())
    if abs(weight_sum - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise ValueError(
            f"AHP weights must sum to 1.0 (got {weight_sum:.6f}). "
            "Ensure compute_ahp_weights() was called correctly."
        )

    names  = list(criteria_arrays.keys())
    height, width = next(iter(criteria_arrays.values())).shape
    flat_valid    = valid_mask.ravel()
    n_valid       = int(flat_valid.sum())

    if n_valid == 0:
        return np.full((height, width), np.nan, dtype=np.float32)

    # ── Pre-compute column norms, PIS, NIS over the full valid set ─────
    col_norms = np.empty(len(names), dtype=np.float32)
    pis       = np.empty(len(names), dtype=np.float32)
    nis       = np.empty(len(names), dtype=np.float32)

    for i, nm in enumerate(names):
        vec  = criteria_arrays[nm].ravel()[flat_valid].astype(np.float32)
        norm = float(np.sqrt(np.sum(vec ** 2)))
        norm = norm if norm > 0.0 else 1.0
        col_norms[i] = norm

        vec_norm = (vec / norm) * weights[nm]
        pis[i]   = float(vec_norm.max())
        nis[i]   = float(vec_norm.min())
        del vec, vec_norm

    # ── Chunked evaluation ─────────────────────────────────────────────
    valid_indices = np.where(flat_valid)[0]
    w_array       = np.array([weights[nm] for nm in names], dtype=np.float32)
    score_valid   = np.empty(n_valid, dtype=np.float32)

    for start in range(0, n_valid, chunk_size):
        end       = min(start + chunk_size, n_valid)
        idx       = valid_indices[start:end]
        chunk_len = end - start

        chunk = np.empty((chunk_len, len(names)), dtype=np.float32)
        for i, nm in enumerate(names):
            chunk[:, i] = (
                criteria_arrays[nm].ravel()[idx] / col_norms[i]
            ) * w_array[i]

        d_pos = np.sqrt(((chunk - pis) ** 2).sum(axis=1))
        d_neg = np.sqrt(((chunk - nis) ** 2).sum(axis=1))
        denom = d_pos + d_neg
        score_valid[start:end] = np.where(denom > 0.0, d_neg / denom, 0.0)
        del chunk

    score_flat = np.full(height * width, np.nan, dtype=np.float32)
    score_flat[flat_valid] = score_valid

    return score_flat.reshape(height, width)