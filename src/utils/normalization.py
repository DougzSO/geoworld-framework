"""
src/utils/normalization.py
==========================
Reusable array normalisation functions for spatial criteria computation.

Used by:
  - CriteriaBuilder  (Phase 2b)
  - LCOECalculator   (Phase 5)
  - SensitivityAnalyzer (Phase 8, future)
"""

from __future__ import annotations

import logging
import numpy as np

from src.core.constants import NODATA_FLOAT

logger = logging.getLogger("geoworld.utils.normalization")


def normalize_percentile(
    data: np.ndarray,
    valid: np.ndarray,
    p_low: float = 5.0,
    p_high: float = 95.0,
) -> np.ndarray:
    """
    Min-max normalisation with percentile clipping to suppress outliers.

    S(x) = clip((x − P_low) / (P_high − P_low), 0, 1)

    Args:
        data:   Raw input array (any dtype; cast to float64 internally).
        valid:  Boolean mask of valid pixels to consider for statistics.
        p_low:  Lower percentile clip (default 5).
        p_high: Upper percentile clip (default 95).

    Returns:
        Float32 score array in [0, 1]; NODATA_FLOAT for invalid pixels.
    """
    score = np.full(data.shape, NODATA_FLOAT, dtype=np.float32)
    if not valid.any():
        return score

    vals   = data[valid].astype(np.float64)
    v_low  = np.percentile(vals, p_low)
    v_high = np.percentile(vals, p_high)

    if v_high <= v_low:
        score[valid] = 0.5
        return score

    score[valid] = np.clip(
        (vals - v_low) / (v_high - v_low), 0.0, 1.0
    ).astype(np.float32)

    return score


def normalize_minmax(
    data: np.ndarray,
    valid: np.ndarray,
) -> np.ndarray:
    """
    Standard min-max normalisation without percentile clipping.

    S(x) = (x − min) / (max − min)

    Args:
        data:  Raw input array.
        valid: Boolean mask of valid pixels.

    Returns:
        Float32 score array in [0, 1]; NODATA_FLOAT for invalid pixels.
    """
    score = np.full(data.shape, NODATA_FLOAT, dtype=np.float32)
    if not valid.any():
        return score

    vals   = data[valid].astype(np.float64)
    v_min  = vals.min()
    v_max  = vals.max()

    if v_max <= v_min:
        score[valid] = 0.5
        return score

    score[valid] = np.clip(
        (vals - v_min) / (v_max - v_min), 0.0, 1.0
    ).astype(np.float32)

    return score


def normalize_invert(
    score: np.ndarray,
    nodata: float = NODATA_FLOAT,
) -> np.ndarray:
    """
    Invert a score array: S_inv(x) = 1 - S(x).

    Preserves NODATA values unchanged.

    Args:
        score:  Input float32 array in [0, 1].
        nodata: Nodata sentinel value (default NODATA_FLOAT).

    Returns:
        Inverted float32 array; nodata pixels unchanged.
    """
    out  = score.copy()
    mask = np.isfinite(score) & (score != nodata)
    out[mask] = np.clip(1.0 - score[mask], 0.0, 1.0).astype(np.float32)
    return out