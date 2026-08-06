"""
src/utils/economics.py
======================
Financial modelling utilities for renewable energy LCOE and supply curve analysis.

Provides:
  - Capital Recovery Factor (CRF) computation
  - Levelized Cost of Energy (LCOE) calculation
  - Supply curve (merit order) generation
  - Capacity factor modulation and clamping

Used by:
  - lcoe_calculator.py (Phase 5)
  - potential_calculator.py (Phase 4)
  - sensitivity_analyzer.py (Phase 8)
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("geoworld.utils.economics")


# ═══════════════════════════════════════════════════════════════════════════
# Core Financial Computations
# ═══════════════════════════════════════════════════════════════════════════


def capital_recovery_factor(rate: float, lifetime: int) -> float:
    """
    Capital Recovery Factor (CRF) mapping upfront CAPEX to annualised series.

    The CRF converts a lump-sum capital investment into an equivalent annual
    payment stream over the asset lifetime at a given discount rate.

    Mathematical definition:
        CRF = [r(1+r)^n] / [(1+r)^n - 1]

    where:
      - r = discount rate (e.g., 0.06 for 6%)
      - n = asset lifetime (years)

    Args:
        rate: Annual discount rate as decimal (e.g., 0.06 for 6%).
              Must be ≥ 0. If ≤ 0, returns 1/lifetime (no discounting).
        lifetime: Asset lifetime in years. Must be ≥ 1.

    Returns:
        CRF as a dimensionless coefficient (typ. 0.06–0.15 for 25-yr assets).

    Examples:
        >>> capital_recovery_factor(0.06, 25)
        0.0643...  # 6.43% annual payment of CAPEX

        >>> capital_recovery_factor(0.0, 25)
        0.04  # No discounting: simple 1/25
    """
    if rate <= 0.0:
        return 1.0 / max(lifetime, 1)

    factor = (1.0 + rate) ** lifetime
    return rate * factor / (factor - 1.0)


def compute_lcoe(
    capex_usd_kw: float,
    opex_usd_kw_yr: float,
    crf: float,
    capacity_factor: float,
    hours_year: int = 8760,
) -> float:
    """
    Levelized Cost of Energy (LCOE) computation.

    LCOE represents the average cost (USD/MWh) of electricity generated over
    the lifetime of the plant, accounting for:
      - Upfront capital costs (amortised via CRF)
      - Annual operating costs
      - Capacity factor (actual output / theoretical maximum)

    Mathematical definition:
        LCOE = [CAPEX × CRF + OPEX] / [CF × hours_year] × 1000

    where CF is in per-unit (0–1) and the result is in USD/MWh.

    Args:
        capex_usd_kw: Capital expenditure (USD/kW installed).
        opex_usd_kw_yr: Annual operating expenditure (USD/kW/yr).
        crf: Capital Recovery Factor (dimensionless).
        capacity_factor: Capacity factor in per-unit (0–1).
        hours_year: Hours per year (default 8760 = 365.25 days).

    Returns:
        LCOE in USD/MWh, or np.nan if inputs are invalid (CF ≤ 0, CRF ≤ 0).

    Examples:
        >>> compute_lcoe(
        ...     capex_usd_kw=760,
        ...     opex_usd_kw_yr=13,
        ...     crf=0.0643,
        ...     capacity_factor=0.19,
        ... )
        46.8...  # ~47 USD/MWh for typical solar PV

        >>> compute_lcoe(760, 13, 0.0643, 0.0)  # No generation
        nan
    """
    if capacity_factor <= 0.0 or crf <= 0.0:
        return np.nan

    annual_cost_kw = capex_usd_kw * crf + opex_usd_kw_yr
    return (annual_cost_kw / (capacity_factor * hours_year)) * 1000.0


# ═══════════════════════════════════════════════════════════════════════════
# Capacity Factor Modulation & Clamping
# ═══════════════════════════════════════════════════════════════════════════


def modulate_capacity_factor(
    base_cf: float,
    source_array: np.ndarray,
    source_mean: float,
    cf_floor: float,
    cf_ceiling: float,
) -> np.ndarray:
    """
    Compute spatially-varying capacity factor via linear modulation.

    The local CF at each pixel is derived by scaling the base CF by the
    ratio of local resource value to mean resource value, then clamped
    to floor/ceiling bounds.

    Formula:
        CF_local = clamp(CF_base × (resource_local / resource_mean),
                         CF_floor, CF_ceiling)

    Args:
        base_cf: Baseline capacity factor (0–1) for the technology.
        source_array: Spatial source array (resource or suitability proxy).
                      Shape (H, W). NaN values are preserved.
        source_mean: Mean of valid source values (must be > 0).
                     Typically computed as np.mean(source_array[valid_mask]).
        cf_floor: Lower clamp for CF (e.g., 0.0 for solar, 0.5 for biomass).
        cf_ceiling: Upper clamp for CF (e.g., 0.95 for biomass, 1.8×base for others).

    Returns:
        Modulated CF array of same shape as source_array.
        NaN values are preserved (not clamped).

    Examples:
        >>> base_cf = 0.19
        >>> source = np.array([[0.5, 1.0], [1.5, np.nan]])
        >>> modulate_capacity_factor(base_cf, source, 1.0, 0.0, 0.95)
        array([[0.095, 0.19 ], [0.285, nan  ]])
    """
    if source_mean <= 0:
        logger.warning(
            "modulate_capacity_factor: source_mean=%.4f <= 0 — "
            "returning base_cf everywhere.",
            source_mean,
        )
        return np.full_like(source_array, base_cf, dtype=np.float32)

    # Modulate: CF_local = base_cf * (resource / resource_mean)
    modulated = np.where(
        np.isfinite(source_array),
        base_cf * (source_array / source_mean),
        np.nan,
    )

    # Clamp to [floor, ceiling]
    clamped = np.clip(modulated, cf_floor, cf_ceiling).astype(np.float32)

    # Restore NaN for originally invalid pixels
    result = np.where(np.isfinite(source_array), clamped, np.nan).astype(np.float32)

    return result


def compute_cf_bounds(
    base_cf: float,
    tech: str,
    cf_ceiling_override: Optional[float] = None,
    cf_floor_override: Optional[float] = None,
    cf_absolute_ceiling: float = 0.95,
    relative_range: Tuple[float, float] = (0.40, 1.80),
) -> Tuple[float, float]:
    """
    Compute technology-specific capacity factor bounds for clamping.

    Precedence:
      1. Explicit overrides (cf_ceiling_override, cf_floor_override)
      2. Technology-specific hardcoded rules
      3. Relative range bounds (floor = base_cf × 0.40, ceiling = base_cf × 1.80)
      4. Absolute global ceiling

    Args:
        base_cf: Baseline capacity factor (0–1).
        tech: Technology identifier ('solar', 'wind', 'biomass').
        cf_ceiling_override: Explicit upper bound, if available.
        cf_floor_override: Explicit lower bound, if available.
        cf_absolute_ceiling: Global hard cap (e.g., 0.95 for biomass, physics limit).
        relative_range: Tuple (floor_mult, ceiling_mult) for relative scaling
                        of base_cf (default: 0.4×base to 1.8×base for solar/wind).

    Returns:
        Tuple (cf_floor, cf_ceiling) with sensible bounds.

    Examples:
        >>> compute_cf_bounds(0.19, 'solar')
        (0.076, 0.342)

        >>> compute_cf_bounds(0.73, 'biomass', cf_ceiling_override=0.90)
        (0.51, 0.90)
    """
    # ── Explicit overrides take precedence ──────────────────────────────
    if cf_floor_override is not None:
        floor = float(cf_floor_override)
    elif tech == "biomass":
        floor = max(base_cf * 0.70, 0.0)  # Biomass narrower range
    else:
        floor = base_cf * relative_range[0]  # Solar/wind wider range

    if cf_ceiling_override is not None:
        ceiling = float(cf_ceiling_override)
    elif tech == "biomass":
        ceiling = min(base_cf * 0.95, cf_absolute_ceiling)
    else:
        ceiling = min(base_cf * relative_range[1], cf_absolute_ceiling)

    return float(floor), float(ceiling)


# ═══════════════════════════════════════════════════════════════════════════
# Supply Curve (Merit Order) Generation
# ═══════════════════════════════════════════════════════════════════════════


def compute_supply_curve(
    lcoe_map: np.ndarray,
    cap_map: np.ndarray,
) -> pd.DataFrame:
    """
    Compute economic merit order curve via LCOE sorting and cumulative capacity.

    The supply curve orders all sitable pixels from lowest to highest LCOE,
    accumulating installed capacity (in GW) at each LCOE level.
    This allows answering: "How much capacity is available below USD 50/MWh?"

    Args:
        lcoe_map: Spatial LCOE array (USD/MWh). Shape (H, W).
                  NaN and non-positive values are excluded.
        cap_map: Spatial capacity array (MW). Shape (H, W).
                 Must match lcoe_map shape.

    Returns:
        DataFrame with columns:
          - 'lcoe_usd_mwh': sorted LCOE values
          - 'capacity_mw': capacity at each pixel
          - 'cum_capacity_gw': cumulative capacity (GW) from lowest LCOE

        Empty DataFrame if no valid (lcoe, cap) pairs exist.

    Examples:
        >>> lcoe = np.array([[50, 100], [80, 120]])
        >>> cap = np.array([[10, 5], [20, 8]])
        >>> sc = compute_supply_curve(lcoe, cap)
        >>> sc
             lcoe_usd_mwh  capacity_mw  cum_capacity_gw
        0             50           10            0.010
        1             80           20            0.030
        2            100            5            0.035
        3            120            8            0.043
    """
    # Identify valid (lcoe, cap) pairs
    valid = (
        np.isfinite(lcoe_map)
        & np.isfinite(cap_map)
        & (lcoe_map > 0)
        & (cap_map > 0)
    )

    if not valid.any():
        logger.warning(
            "compute_supply_curve: no valid (lcoe, cap) pairs. "
            "Returning empty DataFrame."
        )
        return pd.DataFrame(
            columns=["lcoe_usd_mwh", "capacity_mw", "cum_capacity_gw"]
        )

    # Extract valid values and sort by LCOE
    lcoe_vals = lcoe_map[valid]
    cap_vals = cap_map[valid]
    order = np.argsort(lcoe_vals)

    # Compute cumulative capacity in GW
    cum_cap_gw = np.cumsum(cap_vals[order]) / 1000.0

    # Validate cumulative capacity
    if cum_cap_gw[-1] <= 0:
        logger.warning(
            "compute_supply_curve: cumulative capacity = 0 GW. "
            "cap_map may be all-zero or all-NaN."
        )

    return pd.DataFrame({
        "lcoe_usd_mwh": lcoe_vals[order].astype(np.float32),
        "capacity_mw": cap_vals[order].astype(np.float32),
        "cum_capacity_gw": cum_cap_gw.astype(np.float32),
    })


def extract_supply_curve_thresholds(
    supply_curve: pd.DataFrame,
    lcoe_thresholds: Optional[list] = None,
) -> Dict[float, float]:
    """
    Extract available capacity at each LCOE threshold from a supply curve.

    Useful for answering questions like: "What % of capacity is below 50 USD/MWh?"

    Args:
        supply_curve: DataFrame from compute_supply_curve().
        lcoe_thresholds: List of LCOE values to query (USD/MWh).
                        Defaults to [40, 50, 60, 70, 80, 100, 120, 150].

    Returns:
        Dict mapping LCOE threshold → available capacity (GW).
        Missing thresholds return 0.0 GW.

    Examples:
        >>> sc = compute_supply_curve(lcoe, cap)
        >>> extract_supply_curve_thresholds(sc, [50, 100])
        {50: 0.010, 100: 0.035}
    """
    if lcoe_thresholds is None:
        lcoe_thresholds = [40, 50, 60, 70, 80, 100, 120, 150]

    if supply_curve.empty:
        return {thr: 0.0 for thr in lcoe_thresholds}

    result = {}
    for thr in lcoe_thresholds:
        sub = supply_curve[supply_curve["lcoe_usd_mwh"] <= thr]
        capacity_gw = (
            float(sub["cum_capacity_gw"].max())
            if not sub.empty
            else 0.0
        )
        result[thr] = capacity_gw

    return result