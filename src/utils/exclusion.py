"""
src/utils/exclusion.py
======================
Hard exclusion logic for renewable energy siting.

Applies per‑criterion thresholds, slope limits, and land‑cover class
exclusions to a base validity mask.

The module also defines ``TechnologyConfig`` (dataclass) and
``ExclusionResult`` (dataclass) used throughout Phase 3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

import numpy as np

logger = logging.getLogger("geoworld.utils.exclusion")


# ═══════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class TechnologyConfig:
    """Configuration container for a single renewable technology.

    Attributes:
        label:                Human-readable name (e.g. "Solar PV").
        color:                Hex colour for visualisation.
        intensity:            AHP Saaty scale intensity
                              ("subtle" / "moderate" / "strong").
        priority_order:       Criterion names ordered highest → lowest priority.
                              Used by ``build_saaty_matrix`` to construct the
                              AHP pairwise comparison matrix.
        hard_exclusions:      Mapping criterion_name → minimum allowable score.
                              Pixels below this score are hard-excluded before
                              MCDA scoring.
        slope_max_deg:        Maximum slope in degrees; derived from
                              ``country_params["slope_threshold_deg"]`` +
                              ``_SLOPE_OFFSET_DEG[tech]``.
        lc_exclusion_classes: ESA WorldCover class codes that are excluded
                              entirely (e.g. 50=Built-up, 10=Forest if policy
                              active).
    """

    label: str
    color: str
    intensity: str
    priority_order: List[str]
    hard_exclusions: Dict[str, float]
    slope_max_deg: float
    lc_exclusion_classes: Set[int]


@dataclass
class ExclusionResult:
    """Encapsulates the output of the hard exclusion filtering stage.

    Attributes:
        valid_mask:       Boolean array — True for pixels that survived all
                          exclusion rules and are eligible for MCDA scoring.
        n_excluded_total: Total pixel count removed by all exclusion rules
                          combined (criterion + slope + land cover).
        log:              Per-criterion exclusion events with threshold and
                          count metadata; useful for audit and debugging.
        slope_excluded:   Pixels removed specifically by the slope threshold.
        lc_excluded:      Pixels removed by land-cover class rules.
    """

    valid_mask: np.ndarray
    n_excluded_total: int
    log: List[Dict[str, Any]] = field(default_factory=list)
    slope_excluded: int = 0
    lc_excluded: int = 0


# ═══════════════════════════════════════════════════════════════════════════
# EXCLUSION LOGIC
# ═══════════════════════════════════════════════════════════════════════════


def apply_hard_exclusions(
    base_valid_mask: np.ndarray,
    all_criteria: Dict[str, np.ndarray],
    cfg_tech: TechnologyConfig,
    lc_data: Optional[np.ndarray],
    slope_data: Optional[np.ndarray],
) -> ExclusionResult:
    """Apply all hard exclusion constraints and return the updated validity mask.

    Pure transformation — no side effects; suitable for isolated unit testing.

    Exclusion order:
      1. Criterion score below per-criterion threshold (``hard_exclusions``).
      2. Slope above ``slope_max_deg`` (from country base + tech offset).
      3. Land-cover class in ``lc_exclusion_classes`` (ESA WorldCover codes).

    Args:
        base_valid_mask: Initial boolean mask (True = eligible pixel).
        all_criteria:    All criterion arrays loaded by Phase 3.
        cfg_tech:        Technology configuration (thresholds, slope, LC).
        lc_data:         Aligned land-cover raster; native class codes, not
                         clipped to [0, 1] (loaded via ``_load_aux_raster``).
        slope_data:      Aligned slope raster in degrees; not normalised.

    Returns:
        :class:`ExclusionResult` with updated mask and per-rule statistics.
    """
    valid = base_valid_mask.copy()
    n_before = int(valid.sum())
    exclusion_log: List[Dict[str, Any]] = []

    # ── 1. Criterion-score thresholds ──────────────────────────────────
    for crit_name, threshold in cfg_tech.hard_exclusions.items():
        if crit_name not in all_criteria:
            continue
        crit_arr = all_criteria[crit_name]
        excl_mask = np.isfinite(crit_arr) & (crit_arr < threshold)
        n_excl = int(excl_mask.sum())
        valid &= ~excl_mask
        exclusion_log.append(
            {"criterion": crit_name, "threshold": threshold, "n_excluded": n_excl}
        )

    # ── 2. Slope threshold ─────────────────────────────────────────────
    slope_excluded = 0
    if slope_data is not None:
        slope_excl = np.isfinite(slope_data) & (slope_data > cfg_tech.slope_max_deg)
        slope_excluded = int(slope_excl.sum())
        valid &= ~slope_excl

    # ── 3. Land-cover class exclusion ──────────────────────────────────
    lc_excluded = 0
    if lc_data is not None and cfg_tech.lc_exclusion_classes:
        lc_safe = np.where(np.isfinite(lc_data), lc_data, -1.0)
        lc_excl = (
            np.isin(
                np.round(lc_safe).astype(np.int32),
                list(cfg_tech.lc_exclusion_classes),
            )
            & np.isfinite(lc_data)
        )
        lc_excluded = int(lc_excl.sum())
        valid &= ~lc_excl

    return ExclusionResult(
        valid_mask=valid,
        n_excluded_total=n_before - int(valid.sum()),
        log=exclusion_log,
        slope_excluded=slope_excluded,
        lc_excluded=lc_excluded,
    )