"""
src/utils/ahp.py
================
Analytic Hierarchy Process (AHP) — Saaty scale.

Provides functions to build pairwise comparison matrices, compute weights
via the geometric mean method, and log consistency metrics.

References
----------
[1] Saaty, T.L. (1980). The Analytic Hierarchy Process. McGraw-Hill.
[2] Al Garni, H.Z., & Awasthi, A. (2017). Renew. Sustain. Energy Rev. 76:641–662.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

import numpy as np

from src.core.constants import (
    AHP_CR_THRESHOLD,
    AHP_RANDOM_INDEX,
    AHP_SCALE_TABLE,
)

logger = logging.getLogger("geoworld.utils.ahp")


def build_saaty_matrix(
    criteria: List[str],
    priority_order: List[str],
    intensity: str = "moderate",
) -> np.ndarray:
    """Construct an AHP pairwise comparison matrix using the Saaty Scale.

    Mathematical Semantics
    ----------------------
    ``rank[c]`` is the position of criterion ``c`` in ``priority_order``
    (0 = highest priority).  For a pair (ci, cj):

    .. code-block:: text

        diff = rank[cj] - rank[ci]

        diff > 0  →  cj appears later  →  ci has higher priority
                  →  mat[i, j] = scale[|diff|]  (> 1)

        diff < 0  →  cj appears earlier →  cj has higher priority
                  →  mat[i, j] = 1 / scale[|diff|]  (< 1)

        diff = 0  →  i == j, diagonal = 1

    The matrix is guaranteed reciprocal: mat[i, j] * mat[j, i] = 1.

    Args:
        criteria:       Criterion names present in the dataset.
        priority_order: Ordered list — first element has highest priority.
        intensity:      Saaty scale level (``"subtle"``/``"moderate"``/``"strong"``).

    Returns:
        Square float64 pairwise comparison matrix of shape (n, n).
    """
    scale = AHP_SCALE_TABLE.get(intensity, AHP_SCALE_TABLE["moderate"])
    n = len(criteria)
    # Criteria absent from priority_order are appended at the end (lowest priority).
    full_order = priority_order + [c for c in criteria if c not in priority_order]
    rank = {c: i for i, c in enumerate(full_order)}

    mat = np.ones((n, n), dtype=np.float64)
    for i, ci in enumerate(criteria):
        for j, cj in enumerate(criteria):
            if i == j:
                continue
            # diff > 0  →  cj has lower priority  →  ci is more important
            # diff < 0  →  cj has higher priority  →  ci is less important
            diff = rank[cj] - rank[ci]
            dist = abs(diff)
            val  = float(scale.get(dist, 9))
            mat[i, j] = val if diff > 0 else 1.0 / val

    return mat


def ahp_weights(
    matrix: np.ndarray,
) -> Tuple[np.ndarray, float, float, float]:
    """Calculate AHP weights using the geometric mean method.

    Mathematical Formulation
    ------------------------
    For a pairwise comparison matrix :math:`A` of size :math:`n \\times n`:

    .. math::
        w_i = \\frac{\\left( \\prod_{j=1}^{n} a_{ij} \\right)^{1/n}}
                     {\\sum_{k=1}^{n} \\left( \\prod_{j=1}^{n} a_{kj} \\right)^{1/n}}

    Consistency Index (CI) and Consistency Ratio (CR):

    .. math::
        \\lambda_{\\max} = \\sum_{j} \\left( \\sum_{i} a_{ij} \\right) w_j,
        \\quad
        CI = \\frac{\\lambda_{\\max} - n}{n - 1},
        \\quad
        CR = \\frac{CI}{RI_n}

    where :math:`RI_n` is the Random Index for matrix size *n*.

    Args:
        matrix: Square pairwise comparison matrix (n × n).

    Returns:
        Tuple of (weights array, lambda_max, CI, CR).
    """
    n = matrix.shape[0]
    geo_mean = np.exp(np.log(matrix + 1e-12).mean(axis=1))
    weights  = geo_mean / geo_mean.sum()

    col_sum = matrix.sum(axis=0)
    lam_max = float((col_sum * weights).sum())

    ci = (lam_max - n) / max(n - 1, 1)
    ri = AHP_RANDOM_INDEX.get(n, 1.59)
    cr = ci / ri if ri > 0 else 0.0

    return weights, lam_max, ci, cr


def compute_ahp_weights(
    criteria: List[str],
    priority_order: List[str],
    intensity: str = "moderate",
) -> Tuple[Dict[str, float], float, float, float]:
    """High-level interface: compute AHP weights from an ordinal priority list.

    Args:
        criteria:       Available criterion names.
        priority_order: Ordered list — first element has highest priority.
        intensity:      Saaty scale intensity.

    Returns:
        Tuple of (weights dict keyed by criterion name, lambda_max, CI, CR).
    """
    mat = build_saaty_matrix(criteria, priority_order, intensity)
    w, lam, ci, cr = ahp_weights(mat)
    return {c: float(w[i]) for i, c in enumerate(criteria)}, lam, ci, cr


def log_ahp_result(
    tech: str,
    weights: Dict[str, float],
    lam_max: float,
    ci: float,
    cr: float,
    intensity: str,
) -> None:
    """Log AHP consistency metrics and per-criterion weights."""
    status = (
        "[OK] Consistent" if cr <= AHP_CR_THRESHOLD else "[WARNING] INCONSISTENT"
    )
    logger.info("  [%s] AHP Saaty (%s) — %s", tech, intensity, status)
    logger.info("  [%s]   λ_max=%.4f | CI=%.4f | CR=%.4f", tech, lam_max, ci, cr)
    for name, w in sorted(weights.items(), key=lambda x: -x[1]):
        logger.info(
            "  [%s]   %-30s: %.4f  (%.1f%%)", tech, name, w, w * 100
        )