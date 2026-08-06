"""
src/utils/owa.py
================
Ordered Weighted Averaging (OWA) — Yager, 1988.

Provides functions for normalising OWA weight vectors and performing
spatially explicit OWA aggregation.

References
----------
[1] Yager, R.R. (1988). On ordered weighted averaging operators.
    IEEE Trans SMC 18(1):183–190.
[2] McKenna et al. (2020). Energy Policy 147:111693.
"""

from __future__ import annotations

import logging
from typing import Dict, List

import numpy as np

logger = logging.getLogger("geoworld.utils.owa")

# Default OWA weight vectors for three scenarios.
# Vectors must sum to 1.0 and be non‑increasing (best → worst criterion).
OWA_DEFAULTS: Dict[str, List[float]] = {
    "optimistic":   [0.70, 0.20, 0.10],
    "balanced":     [0.34, 0.33, 0.33],
    "conservative": [0.50, 0.30, 0.20],
}


def prepare_owa_weights(
    raw_weights: List[float],
    n_criteria: int,
) -> np.ndarray:
    """Normalise and resize an OWA weight vector to match ``n_criteria``.

    If the raw vector is shorter than ``n_criteria``, the last element is
    repeated and the result re‑normalised.  If longer, it is truncated and
    re‑normalised.

    Args:
        raw_weights: OWA weights from ``parameters.json`` (any length ≥ 1).
        n_criteria:  Number of criteria actually present for the technology.

    Returns:
        Normalised float32 array of length ``n_criteria``.
    """
    w = list(raw_weights)
    if len(w) < n_criteria:
        # Pad with the last value (conservative extension)
        w += [w[-1]] * (n_criteria - len(w))
    w = np.array(w[:n_criteria], dtype=np.float64)
    total = w.sum()
    if total <= 0.0:
        w = np.ones(n_criteria, dtype=np.float64)
        total = float(n_criteria)
    return (w / total).astype(np.float32)


def owa_spatial(
    criteria_arrays: Dict[str, np.ndarray],
    owa_weights: np.ndarray,
    valid_mask: np.ndarray,
    chunk_size: int = 500_000,
) -> np.ndarray:
    """Spatially explicit OWA (Ordered Weighted Averaging) aggregation.

    For each valid pixel *p* with criterion values :math:`x_{p,1} \\ldots x_{p,n}`:

    1. Sort values in **descending** order:
       :math:`x_{p,(1)} \\geq x_{p,(2)} \\geq \\ldots \\geq x_{p,(n)}`
    2. Apply OWA weights:
       :math:`S_p = \\sum_{k=1}^{n} w_k \\cdot x_{p,(k)}`

    Args:
        criteria_arrays: Criterion name → float32 array, values in [0, 1].
        owa_weights:     Normalised OWA weight vector of length n_criteria.
        valid_mask:      Boolean mask — True for pixels to score.
        chunk_size:      Pixels per iteration for memory control.

    Returns:
        2D float32 array of OWA scores in [0, 1]; NaN for invalid pixels.

    Raises:
        ValueError: If ``len(owa_weights) != len(criteria_arrays)``.
    """
    names = list(criteria_arrays.keys())
    n_crit = len(names)
    if len(owa_weights) != n_crit:
        raise ValueError(
            f"OWA weight vector length {len(owa_weights)} ≠ "
            f"criteria count {n_crit}."
        )

    height, width = next(iter(criteria_arrays.values())).shape
    flat_valid = valid_mask.ravel()
    n_valid    = int(flat_valid.sum())

    if n_valid == 0:
        return np.full((height, width), np.nan, dtype=np.float32)

    # Cache valid pixel indices once — avoids repeated boolean indexing
    valid_indices = np.where(flat_valid)[0]

    score_valid = np.empty(n_valid, dtype=np.float32)

    for start in range(0, n_valid, chunk_size):
        end = min(start + chunk_size, n_valid)
        idx = valid_indices[start:end]
        chunk_len = end - start

        # Assemble (chunk_len × n_crit) matrix for this chunk
        chunk = np.empty((chunk_len, n_crit), dtype=np.float32)
        for k, nm in enumerate(names):
            chunk[:, k] = criteria_arrays[nm].ravel()[idx]

        # Sort each row in descending order — in‑place for memory efficiency
        chunk.sort(axis=1)
        chunk = chunk[:, ::-1]  # reverse to descending

        # Weighted sum: (chunk_len × n_crit) × (n_crit,) → (chunk_len,)
        score_valid[start:end] = chunk @ owa_weights

        del chunk

    score_flat = np.full(height * width, np.nan, dtype=np.float32)
    score_flat[flat_valid] = score_valid

    return score_flat.reshape(height, width)