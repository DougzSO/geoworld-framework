"""
src/processors/ghg_abatement_calculator.py
==========================================
Phase 7: GHG Abatement, Carbon Intensity & Net Zero

SCOPE: ELECTRICITY TRANSITION (Electricity Generation Sector ONLY)

Calculates the technical/economic substitution of fossil thermal generation
(coal, gas, oil) with renewable sources (solar, wind, biomass).

penetration_factor semantics (v2.4):
    penetration_factor        = TARGET renewable share of total national grid [0-1]
    existing_renewable_share  = current renewable share (IEA/IRENA stat) [0-1]
    grid_total_gwh            = total national generation baseline (GWh/yr)

    Substitution volume:
        renewable_gap = penetration_factor - existing_renewable_share
        gwh_to_add    = grid_total_gwh × max(0, renewable_gap)
        subst_gwh     = min(gwh_to_add, total_th_gwh, renew_total_gwh)

    If existing_renewable_share >= penetration_factor the country already
    meets its target; a minimum technical floor (_MIN_TECH_SUBSTITUTION) is
    applied.
    If grid_total_gwh = 0 (not configured) the module falls back to legacy
    behaviour: subst_gwh = min(total_th_gwh, renew_total_gwh) × penetration.

Parameter architecture:
    parameters.json → CF, LUF, density, threshold (via CountryParams)
    settings.yaml   → infrastructure, paths, scenario offsets only

References:
    IPCC AR6 WG3 (2022) — Lifecycle emission factors
    IEA WEO 2024 — SRMC baselines
    IRENA (2024) — Grid penetration limits
    UNFCCC NDC Registry / CAT (2024)
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

from src.core.config_loader import ConfigLoader
from src.core.constants import TECH_ORDER
from src.utils.map_styling import GeoWorldStyler

try:
    from src.utils.abatement_plots import (
        TECH_LABELS,
        plot_geography,
        plot_macc_curve,
        plot_substitution,
        plot_carbon_intensity,
        plot_net_zero,
    )
    _ABATEMENT_PLOTS_AVAILABLE = True
except ImportError:
    _ABATEMENT_PLOTS_AVAILABLE = False
    TECH_LABELS: Dict[str, str] = {
        "solar":   "Solar PV",
        "wind":    "Wind Onshore",
        "biomass": "Biomass / Bioenergy",
    }
    plot_geography = plot_macc_curve = plot_substitution = None
    plot_carbon_intensity = plot_net_zero = None

logger = logging.getLogger("geoworld.processors.GHGAbatementCalculator")


# ═══════════════════════════════════════════════════════════════════════════
# Timer context manager
# ═══════════════════════════════════════════════════════════════════════════

@contextmanager
def _timer(label: str, timings: Dict[str, float]):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        timings[label] = round(elapsed, 2)
        logger.info("  [%s] completed in %.1fs", label, elapsed)


# ═══════════════════════════════════════════════════════════════════════════
# Module-level constants
# ═══════════════════════════════════════════════════════════════════════════

GLOBAL_THERMAL_FALLBACK: Dict[str, Dict[str, float]] = {
    "coal": {"ef": 820.0, "cf": 0.55, "fuel_mc": 35.0},
    "gas":  {"ef": 490.0, "cf": 0.45, "fuel_mc": 48.0},
    "oil":  {"ef": 750.0, "cf": 0.35, "fuel_mc": 90.0},
}

GLOBAL_RENEW_LIFECYCLE_FALLBACK: Dict[str, float] = {
    "solar":   48.0,
    "wind":    11.0,
    "biomass": 230.0,
}

# Lifecycle EF for existing renewable mix used in ci_before calculation.
# Hydro dominates most grids with high existing_renewable_share.
# Value: IPCC AR6 reservoir median (gCO₂/kWh).
EXISTING_RENEW_LIFECYCLE_G: float = 24.0

# IRENA-typical LCOE fallbacks by technology (USD/MWh)
_LCOE_FALLBACK: Dict[str, float] = {
    "solar":   50.0,
    "wind":    65.0,
    "biomass": 60.0,
}

GLOBAL_RENEWABLE_CF_FALLBACK: Dict[str, float] = {
    "solar":   0.20,
    "wind":    0.30,
    "biomass": 0.75,
}

FUEL_ALIASES: Dict[str, str] = {
    "gas": "gas", "natural gas": "gas", "gas (combined cycle)": "gas",
    "ccgt": "gas", "ocgt": "gas", "gas turbine": "gas", "gas/oil": "gas",
    "lng": "gas", "cng": "gas",
    "coal": "coal", "hard coal": "coal", "lignite": "coal",
    "brown coal": "coal", "sub-bituminous coal": "coal",
    "bituminous coal": "coal", "anthracite": "coal",
    "coal (conventional)": "coal", "coal (cogeneration)": "coal",
    "oil": "oil", "fuel oil": "oil", "heavy fuel oil": "oil",
    "diesel": "oil", "hfo": "oil", "light fuel oil": "oil",
    "petroleum": "oil", "distillate": "oil", "kerosene": "oil",
}

# Minimum thermal substitution fraction applied when the country already
# meets or exceeds its penetration target.
_MIN_TECH_SUBSTITUTION: float = 0.05

# HTTP timeout for external API calls (seconds).
_HTTP_TIMEOUT: int = 10


# ═══════════════════════════════════════════════════════════════════════════
# Pure functions (unchanged, kept for completeness)
# ═══════════════════════════════════════════════════════════════════════════

def calc_thermal_fleet(
    plants_df: pd.DataFrame,
    thermal_params: Dict[str, Dict[str, float]],
    cap_override: Optional[Dict[str, float]] = None,
) -> pd.DataFrame:
    """Builds the thermal generation baseline normalising capacities."""
    rows: List[Dict] = []
    valid_fuels = set(thermal_params.keys())
    plants_hits: Dict[str, float] = {}
    override_hits: Dict[str, float] = {}

    plants_available = plants_df is not None and not plants_df.empty
    labels_found: List[str] = []

    if plants_available:
        df = plants_df.copy()
        df.columns = [c.lower().strip() for c in df.columns]
        fc = next((c for c in ["primary_fuel", "fuel1"] if c in df.columns), None)
        cc = next((c for c in ["capacity_mw", "capacity"] if c in df.columns), None)

        if fc and cc:
            df[cc] = pd.to_numeric(df[cc], errors="coerce").fillna(0.0)
            df["_fuel_norm"] = (
                df[fc].astype(str).str.lower().str.strip()
                .map(lambda x: FUEL_ALIASES.get(x, x))
            )
            labels_found = sorted(
                df[fc].astype(str).str.lower().str.strip().unique().tolist()
            )
            for fuel in valid_fuels:
                cap = float(df.loc[df["_fuel_norm"] == fuel, cc].sum())
                if cap > 0:
                    plants_hits[fuel] = cap

    for fuel, p in thermal_params.items():
        if fuel in plants_hits and plants_hits[fuel] > 0:
            rows.append(_thermal_row(fuel, plants_hits[fuel], p))
        elif cap_override and fuel in cap_override and float(cap_override[fuel]) > 0:
            override_hits[fuel] = float(cap_override[fuel])
            rows.append(_thermal_row(fuel, float(cap_override[fuel]), p))

    if not plants_hits and not override_hits:
        logger.warning(
            "No thermal power plants established. "
            "plants_df available: %s | labels found: %s",
            plants_available,
            labels_found[:10] if labels_found else "None",
        )

    if not rows:
        return pd.DataFrame(columns=[
            "fuel", "capacity_mw", "gen_gwh", "co2_kt",
            "co2_mt", "fuel_mc", "ef", "cf",
        ])
    return pd.DataFrame(rows)


def _thermal_row(fuel: str, cap: float, p: Dict) -> Dict:
    gen = cap * p["cf"] * 8760.0 / 1000.0
    co2 = gen * p["ef"]
    return {
        "fuel": fuel,
        "capacity_mw": cap,
        "gen_gwh": gen,
        "co2_kt": co2 / 1000.0,
        "co2_mt": co2 / 1e6,
        "fuel_mc": p["fuel_mc"],
        "ef": p["ef"],
        "cf": p["cf"],
    }


def _derive_subst_gwh(
    total_th_gwh: float,
    renew_total_gwh: float,
    penetration_target: float,
    existing_renewable_share: float,
    grid_total_gwh: float,
) -> Tuple[float, str]:
    """
    Derives the GWh volume to substitute given the penetration semantics.

    Returns
    -------
    subst_gwh : float
    mode      : str  — description for logging/report
    """
    if grid_total_gwh > 0.0:
        renewable_gap = penetration_target - existing_renewable_share

        if renewable_gap <= 0.0:
            subst_gwh = total_th_gwh * _MIN_TECH_SUBSTITUTION
            mode = (
                f"target already met (existing {existing_renewable_share:.0%} ≥ "
                f"target {penetration_target:.0%}); "
                f"minimum floor {_MIN_TECH_SUBSTITUTION:.0%} applied"
            )
        else:
            gwh_to_add = grid_total_gwh * renewable_gap
            subst_gwh = min(gwh_to_add, total_th_gwh, renew_total_gwh)
            mode = (
                f"gap-based: target {penetration_target:.0%} − "
                f"existing {existing_renewable_share:.0%} = "
                f"{renewable_gap:.1%} × {grid_total_gwh:,.0f} GWh = "
                f"{gwh_to_add:,.0f} GWh to add"
            )
    else:
        # Legacy fallback: grid_total_gwh not configured
        subst_gwh = min(total_th_gwh, renew_total_gwh) * penetration_target
        mode = (
            f"legacy mode (grid_total_gwh=0): "
            f"{penetration_target:.0%} × min(thermal, renew)"
        )

    return float(subst_gwh), mode


def calc_macc(
    fleet_df: pd.DataFrame,
    renew_gwh: Dict[str, float],
    renew_lcoe: Dict[str, float],
    carbon_price: float,
    penetration: float = 0.80,
    existing_renewable_share: float = 0.0,
    grid_total_gwh: float = 0.0,
) -> Dict[str, Any]:
    """
    Calculates substitution curves and Marginal Abatement Costs.
    """
    renew_total_gwh = sum(float(v or 0.0) for v in renew_gwh.values())
    total_th_gwh = float(fleet_df["gen_gwh"].sum()) if not fleet_df.empty else 0.0

    if fleet_df.empty or renew_total_gwh <= 0 or total_th_gwh <= 0:
        return _empty_macc()

    penetration = min(max(float(penetration), 0.0), 1.0)
    existing_renewable_share = min(max(float(existing_renewable_share), 0.0), 1.0)
    grid_total_gwh = max(float(grid_total_gwh), 0.0)

    srmc_avg = float((fleet_df["fuel_mc"] * fleet_df["gen_gwh"]).sum() / total_th_gwh)
    ef_avg = float((fleet_df["ef"] * fleet_df["gen_gwh"]).sum() / max(total_th_gwh, 1e-9))

    fdf = fleet_df.copy()
    fdf["lrmc"] = fdf["fuel_mc"] + fdf["ef"] * carbon_price / 1000.0
    lrmc_avg = float((fdf["lrmc"] * fdf["gen_gwh"]).sum() / total_th_gwh)

    # Per-technology MAC entries
    by_tech: List[Dict[str, Any]] = []
    for tech in TECH_ORDER:
        gwh = float(renew_gwh.get(tech, 0.0) or 0.0)
        lcoe = float(renew_lcoe.get(tech, 0.0) or 0.0)
        if gwh > 0 and lcoe > 0:
            mac = (lcoe - srmc_avg) / max(ef_avg, 1e-9) * 1000.0
            by_tech.append({
                "tech": tech,
                "generation_gwh": gwh,
                "lcoe_usd_mwh": lcoe,
                "mac_usd_tco2e": mac,
                "bcp_usd_tco2e": max(0.0, mac),
                "competitive": lcoe <= srmc_avg,
                "competitive_lrmc": lcoe <= lrmc_avg,
                "subst_gwh": 0.0,
            })

    if not by_tech:
        return _empty_macc()

    # Derive substitution volume
    subst_gwh, subst_mode = _derive_subst_gwh(
        total_th_gwh, renew_total_gwh,
        penetration, existing_renewable_share, grid_total_gwh,
    )
    logger.info("Substitution mode: %s → %.0f GWh", subst_mode, subst_gwh)

    # Merit-order renewable dispatch (cheapest MAC first)
    by_tech = sorted(by_tech, key=lambda x: x["mac_usd_tco2e"])
    rem_renew = subst_gwh
    for t in by_tech:
        take = min(rem_renew, t["generation_gwh"])
        t["subst_gwh"] = take
        rem_renew -= take
        if rem_renew <= 0:
            break

    # Displace dirtiest thermal first
    fdf = fdf.sort_values("ef", ascending=False).copy()
    rem_therm = subst_gwh
    fdf["subst_gwh"] = 0.0
    fdf["co2_avoided"] = 0.0
    for idx in fdf.index:
        take = min(rem_therm, float(fdf.loc[idx, "gen_gwh"]))
        fdf.loc[idx, "subst_gwh"] = take
        fdf.loc[idx, "co2_avoided"] = take * float(fdf.loc[idx, "ef"]) / 1e6
        rem_therm -= take
        if rem_therm <= 0:
            break

    co2_avoided = float(fdf["co2_avoided"].sum())

    total_built = sum(t["generation_gwh"] for t in by_tech)
    lcoe_avg = (
        sum(t["lcoe_usd_mwh"] * t["generation_gwh"] for t in by_tech)
        / max(total_built, 1.0)
    )
    mac_global = (lcoe_avg - srmc_avg) / max(ef_avg, 1e-9) * 1000.0

    operating_savings_b = max(0.0, (srmc_avg - lcoe_avg) * subst_gwh * 1000.0 / 1e9)
    carbon_value_b = co2_avoided * 1e6 * carbon_price / 1e9

    return {
        "subst_gwh": subst_gwh,
        "subst_pct": subst_gwh / total_th_gwh * 100.0,
        "subst_mode": subst_mode,
        "co2_avoided_mt": co2_avoided,
        "carbon_value_b": carbon_value_b,
        "fuel_savings_b": operating_savings_b,
        "operating_savings_b": operating_savings_b,
        "lrmc_savings_b": max(0.0, (lrmc_avg - lcoe_avg) * subst_gwh * 1000.0 / 1e9),
        "total_value_b": operating_savings_b + carbon_value_b,
        "mac_global": mac_global,
        "bcp_global": max(0.0, mac_global),
        "srmc_avg": srmc_avg,
        "lrmc_avg": lrmc_avg,
        "lcoe_avg_renew": lcoe_avg,
        "ef_avg": ef_avg,
        "carbon_price": carbon_price,
        "penetration": penetration,
        "existing_renewable_share": existing_renewable_share,
        "grid_total_gwh": grid_total_gwh,
        "competitive_gwh": sum(
            t["generation_gwh"] for t in by_tech if t["competitive_lrmc"]
        ),
        "by_tech": by_tech,
        "fleet_df": fdf,
        "total_thermal_gwh": total_th_gwh,
        "total_thermal_co2": float(fleet_df["co2_mt"].sum()),
    }


def calc_carbon_intensity(
    fleet_df: pd.DataFrame,
    renew_gwh: Dict[str, float],
    result: Dict[str, Any],
    renew_lifecycle_ef: Dict[str, float],
    existing_renewable_share: float = 0.0,
    grid_total_gwh: float = 0.0,
) -> Dict[str, Any]:
    """
    Calculates grid carbon intensity before and after transition.

    ci_before reflects the FULL national grid (thermal + existing renewables).
    """
    total_th_gwh = float(result["total_thermal_gwh"])
    total_th_co2_mt = float(result["total_thermal_co2"])

    # Thermal-only reference (always computed)
    ci_thermal_only_g = (total_th_co2_mt * 1e6) / max(total_th_gwh, 1.0)

    existing_renewable_share = min(max(float(existing_renewable_share), 0.0), 1.0)
    grid_total_gwh = max(float(grid_total_gwh), 0.0)

    if existing_renewable_share > 0.0 and grid_total_gwh > 0.0:
        existing_renew_gwh = grid_total_gwh * existing_renewable_share
        existing_renew_co2_mt = existing_renew_gwh * EXISTING_RENEW_LIFECYCLE_G / 1e6
        total_grid_co2_mt = total_th_co2_mt + existing_renew_co2_mt
        total_grid_gwh = total_th_gwh + existing_renew_gwh
        ci_before_g = (total_grid_co2_mt * 1e6) / max(total_grid_gwh, 1.0)
    else:
        existing_renew_gwh = 0.0
        existing_renew_co2_mt = 0.0
        total_grid_gwh = total_th_gwh
        ci_before_g = ci_thermal_only_g

    # Post-transition
    renew_co2_lifecycle_mt = 0.0
    renew_gen_built_gwh = 0.0
    for t in result["by_tech"]:
        gwh_built = t.get("subst_gwh", 0.0)
        if gwh_built > 0:
            ef_g = float(
                renew_lifecycle_ef.get(
                    t["tech"],
                    GLOBAL_RENEW_LIFECYCLE_FALLBACK.get(t["tech"], 50.0),
                )
            )
            renew_co2_lifecycle_mt += gwh_built * ef_g / 1e6
            renew_gen_built_gwh += gwh_built

    fdf = result["fleet_df"]
    residual_co2_mt = float(
        ((fdf["gen_gwh"] - fdf["subst_gwh"]) * fdf["ef"]).sum() / 1e6
    )
    residual_thermal_gwh = max(0.0, total_th_gwh - result["subst_gwh"])

    total_gen_after = residual_thermal_gwh + renew_gen_built_gwh + existing_renew_gwh
    total_co2_after = residual_co2_mt + renew_co2_lifecycle_mt + existing_renew_co2_mt

    ci_after_g = (total_co2_after * 1e6) / max(total_gen_after, 1.0)
    ci_renew_avg_g = (renew_co2_lifecycle_mt * 1e6) / max(renew_gen_built_gwh, 1.0)

    logger.info(
        "CI before (full grid): %.1f gCO₂/kWh  "
        "[thermal-only: %.1f | existing renew: %.0f GWh]",
        ci_before_g, ci_thermal_only_g, existing_renew_gwh,
    )

    return {
        "ci_before_g_kwh": ci_before_g,
        "ci_after_g_kwh": ci_after_g,
        "ci_reduction_pct": (1.0 - ci_after_g / max(ci_before_g, 1.0)) * 100.0,
        "ci_thermal_only_g_kwh": ci_thermal_only_g,
        "ci_renew_avg_g_kwh": ci_renew_avg_g,
        "renew_lifecycle_co2_mt": renew_co2_lifecycle_mt,
        "residual_thermal_co2_mt": residual_co2_mt,
        "existing_renew_co2_mt": existing_renew_co2_mt,
        "total_co2_after_mt": total_co2_after,
        "total_gen_after_gwh": total_gen_after,
        "existing_renew_gwh": existing_renew_gwh,
        "benchmark_eu_g_kwh": 295.0,
        "benchmark_world_g_kwh": 459.0,
        "benchmark_netzero_g_kwh": 24.0,
    }


def calc_carbon_footprint(
    fleet_df: pd.DataFrame,
    renew_gwh: Dict[str, float],
    result: Dict[str, Any],
    country_co2_total_mt: float,
    ci_after_g_kwh: float,
    renew_lifecycle_ef: Dict[str, float],
) -> Dict[str, Any]:
    """GHG Protocol Scope 1/2/3 decomposition for the electricity sector."""
    fdf = result["fleet_df"]
    scope1_after_mt = float(
        ((fdf["gen_gwh"] - fdf["subst_gwh"]) * fdf["ef"]).sum() / 1e6
    )
    renew_gen_added_gwh = sum(float(t.get("subst_gwh", 0)) for t in result["by_tech"])
    scope2_after_mt = renew_gen_added_gwh * ci_after_g_kwh / 1e6
    scope3_renew_mt = sum(
        float(t.get("subst_gwh", 0))
        * float(renew_lifecycle_ef.get(t["tech"], 50.0)) / 1e6
        for t in result["by_tech"]
    )

    return {
        "scope1_before_mt": result["total_thermal_co2"],
        "scope1_after_mt": scope1_after_mt,
        "scope1_reduction": result["total_thermal_co2"] - scope1_after_mt,
        "scope2_after_mt": scope2_after_mt,
        "scope3_renew_mt": scope3_renew_mt,
        "reported_scopes_total_mt": scope1_after_mt + scope2_after_mt + scope3_renew_mt,
        "total_after_mt": scope1_after_mt + scope2_after_mt + scope3_renew_mt,
        "country_total_mt": float(country_co2_total_mt or 0.0),
        "sector_share_pct": (
            result["total_thermal_co2"]
            / max(float(country_co2_total_mt or 0.0), 1.0) * 100.0
        ),
    }


def _empty_macc() -> Dict:
    return {
        "subst_gwh": 0, "subst_pct": 0, "subst_mode": "empty — no data",
        "co2_avoided_mt": 0, "carbon_value_b": 0,
        "fuel_savings_b": 0, "operating_savings_b": 0,
        "lrmc_savings_b": 0, "total_value_b": 0,
        "mac_global": 0, "bcp_global": 0,
        "srmc_avg": 0, "lrmc_avg": 0, "lcoe_avg_renew": 0, "ef_avg": 0,
        "carbon_price": 0, "penetration": 0.0,
        "existing_renewable_share": 0.0, "grid_total_gwh": 0.0,
        "competitive_gwh": 0, "by_tech": [],
        "fleet_df": pd.DataFrame(),
        "total_thermal_gwh": 0, "total_thermal_co2": 0,
    }


def calc_net_zero(
    country_code: str,
    result: Dict[str, Any],
    ci_result: Dict[str, Any],
    footprint: Dict[str, Any],
    renew_gwh: Dict[str, float],
    db: Dict[str, Any],
) -> Dict[str, Any]:
    """Aligns transition results with national NDC and net-zero targets."""
    total_now_mt = float(db.get("total_co2_mt_2022", 0.0) or 0.0)
    base_mt = db.get("base_co2_mt")
    base_year = db.get("base_year")
    ndc_pct = db.get("ndc_2030_pct")
    ndc_intensity_pct = db.get("ndc_2030_intensity_pct")
    nz_year = int(db.get("net_zero_year") or 2050)
    ndc_horizon = int(db.get("ndc_horizon_year") or 2030)

    ndc_type = (
        "absolute" if ndc_pct is not None else
        "intensity" if ndc_intensity_pct is not None else
        "unknown"
    )

    co2_avoided = float(result["co2_avoided_mt"])
    renew_lifecycle = float(ci_result["renew_lifecycle_co2_mt"])
    net_avoided = max(0.0, co2_avoided - renew_lifecycle)
    residual_thermal = float(ci_result["residual_thermal_co2_mt"])

    fossil_thermal_mt = float(result.get("total_thermal_co2", 0.0))
    elec_coverage_pct = (
        min(100.0, net_avoided / fossil_thermal_mt * 100.0)
        if fossil_thermal_mt > 0 else 0.0
    )
    elec_sector_reduction = (
        net_avoided / fossil_thermal_mt * 100.0 if fossil_thermal_mt > 0 else 0.0
    )
    national_contribution_pct = (
        net_avoided / total_now_mt * 100.0 if total_now_mt > 0 else 0.0
    )

    if ndc_type == "absolute" and ndc_pct is not None:
        effective_base = float(base_mt) if base_mt is not None else total_now_mt
        target_ndc_mt = effective_base * (1.0 - float(ndc_pct) / 100.0)
        current_gap_mt = max(0.0, total_now_mt - target_ndc_mt)
    else:
        target_ndc_mt = np.nan
        current_gap_mt = 0.0

    owid_scope_warning = False
    coverage_pct = np.nan
    residual_gap_ndc = np.nan

    if ndc_type == "absolute" and pd.notna(target_ndc_mt):
        if current_gap_mt > 1.0:
            coverage_pct = min(100.0, net_avoided / current_gap_mt * 100.0)
            residual_gap_ndc = max(0.0, (total_now_mt - net_avoided) - target_ndc_mt)
        else:
            owid_scope_warning = True
            coverage_pct = np.nan
            residual_gap_ndc = np.nan
            logger.warning(
                "[%s] NDC gap ≈ 0 (total_now_mt=%.1f ≤ target=%.1f). "
                "OWID data is fossils-only; LULUCF emissions missing. "
                "National gap undetermined.",
                country_code, total_now_mt, target_ndc_mt,
            )

    total_after = max(0.0, total_now_mt - net_avoided)

    emission_balance = {
        "current_total_mt": total_now_mt,
        "fossil_thermal_mt": fossil_thermal_mt,
        "other_sectors_mt": max(0.0, total_now_mt - fossil_thermal_mt),
        "co2_avoided_thermal_mt": co2_avoided,
        "renew_lifecycle_mt": renew_lifecycle,
        "net_avoided_mt": net_avoided,
        "residual_thermal_mt": residual_thermal,
        "total_after_mt": total_after,
        "target_ndc_mt": target_ndc_mt,
        "residual_gap_mt": residual_gap_ndc,
    }

    return {
        "db": db,
        "base_year": base_year,
        "base_mt": base_mt,
        "ndc_pct": ndc_pct,
        "ndc_intensity_pct": ndc_intensity_pct,
        "ndc_type": ndc_type,
        "ndc_horizon_year": ndc_horizon,
        "target_2030_mt": target_ndc_mt,
        "target_ndc_mt": target_ndc_mt,
        "current_gap_mt": current_gap_mt,
        "coverage_pct": coverage_pct,
        "residual_gap_mt": residual_gap_ndc,
        "owid_scope_warning": owid_scope_warning,
        "fossil_thermal_mt": fossil_thermal_mt,
        "elec_coverage_pct": elec_coverage_pct,
        "elec_sector_reduction": elec_sector_reduction,
        "national_contribution_pct": national_contribution_pct,
        "current_total_mt": total_now_mt,
        "co2_avoided_mt": co2_avoided,
        "renew_lifecycle_mt": renew_lifecycle,
        "net_avoided_mt": net_avoided,
        "total_after_mt": total_after,
        "residual_thermal_mt": residual_thermal,
        "net_zero_year": nz_year,
        "annual_reduction_needed": (
            total_now_mt / max(1, nz_year - datetime.now().year)
            if total_now_mt > 0 else 0.0
        ),
        "emission_balance": emission_balance,
        "source": db.get("source", "parameters.json / fallback"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Main class
# ═══════════════════════════════════════════════════════════════════════════

class GHGAbatementCalculator:
    """Processor to calculate GHG abatement for local power market transitions."""

    def __init__(self, cfg: ConfigLoader, outputs_dir: Path):
        self.cfg = cfg
        self.outputs_dir = Path(outputs_dir)
        self.styler = GeoWorldStyler(cfg.system.get("visualization", {}))
        self._session = requests.Session()

        # Cache for OWID data (avoid re-downloading for multiple countries)
        self._owid_df_cache: Optional[pd.DataFrame] = None
        self._nz_db_cache: Optional[Dict[str, Any]] = None

    # ─────────────────────────────────────────────────────────────────────
    # Data recovery helpers
    # ─────────────────────────────────────────────────────────────────────

    def _normalize_potential_dir(self, data: Any, country_code: str) -> Path:
        if isinstance(data, (str, Path)):
            p = Path(data)
            if p.exists():
                return p
        return self.outputs_dir / country_code / "potential"

    def _normalize_lcoe_dir(self, data: Any, country_code: str) -> Path:
        if isinstance(data, (str, Path)):
            p = Path(data)
            if p.exists():
                return p
        return self.outputs_dir / country_code / "lcoe"

    # ─────────────────────────────────────────────────────────────────────
    # Main entry point
    # ─────────────────────────────────────────────────────────────────────

    def run(
        self,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        potential_dir: Any,
        lcoe_dir: Any,
        plants_df: pd.DataFrame,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        admin_gdf: Optional[gpd.GeoDataFrame] = None,
        **kwargs,
    ) -> Dict[str, Any]:

        t0 = datetime.now()
        ts_report = t0.strftime("%Y-%m-%d %H:%M:%S")
        timings: Dict[str, float] = {}
        out_base = self.outputs_dir / country_code / "abatement"

        logger.info("=" * 62)
        logger.info("  GHG ABATEMENT CALCULATOR — %s (%s)", country_name, country_code)
        logger.info("  Output: %s", out_base)
        logger.info("=" * 62)

        # ── 1. Resolve directories ──────────────────────────────────────
        potential_dir = self._normalize_potential_dir(potential_dir, country_code)
        lcoe_dir = self._normalize_lcoe_dir(lcoe_dir, country_code)

        if not potential_dir.exists():
            logger.warning("  Potential directory not found: %s", potential_dir)
        if not lcoe_dir.exists():
            logger.warning("  LCOE directory not found: %s", lcoe_dir)

        # ── 2. Log input stats ──────────────────────────────────────────
        for tech in TECH_ORDER:
            p_csv = potential_dir / "data" / f"{country_code}_{tech}_balanced_zonal.csv"
            if p_csv.exists():
                try:
                    df = pd.read_csv(p_csv)
                    if "generation_twh" in df.columns:
                        logger.info("  Input %-12s: %.2f TWh (balanced)", tech, df["generation_twh"].sum())
                    elif "capacity_mw_sum" in df.columns:
                        logger.info("  Input %-12s: %.2f GW (balanced)", tech, df["capacity_mw_sum"].sum() / 1000.0)
                except Exception as exc:
                    logger.warning("  Could not read stats for %s: %s", tech, exc)

        # ── 3. Output directories ──────────────────────────────────────
        out_fig = out_base / "figures"
        out_data = out_base / "data"
        out_rep = out_base / "reports"
        for d in (out_fig, out_data, out_rep):
            d.mkdir(parents=True, exist_ok=True)

        # ── 4. Load parameters ──────────────────────────────────────────
        with _timer("load_params", timings):
            params = self._load_params(country_code)

        # ── 5. Build thermal fleet ──────────────────────────────────────
        with _timer("thermal_fleet", timings):
            fleet_df = calc_thermal_fleet(
                plants_df,
                thermal_params=params["thermal_params"],
                cap_override=params["cap_override"],
            )

        # ── 6. Load renewable data ──────────────────────────────────────
        with _timer("renewable_data", timings):
            renew_gwh, renew_lcoe, zonal_dfs = self._load_renewable_data(
                potential_dir, lcoe_dir, country_code, params["renewable_cf"]
            )

        # ── 7. Fetch net-zero DB ────────────────────────────────────────
        with _timer("net_zero_db", timings):
            db_entry = self._fetch_net_zero_data(country_code)

        # ── 8. Core calculations ────────────────────────────────────────
        with _timer("calc_macc", timings):
            result = calc_macc(
                fleet_df, renew_gwh, renew_lcoe,
                carbon_price=params["carbon_price"],
                penetration=params["penetration"],
                existing_renewable_share=params["existing_renewable_share"],
                grid_total_gwh=params["grid_total_gwh"],
            )

        with _timer("calc_ci", timings):
            ci_result = calc_carbon_intensity(
                fleet_df, renew_gwh, result,
                renew_lifecycle_ef=params["renew_lifecycle_ef"],
                existing_renewable_share=params["existing_renewable_share"],
                grid_total_gwh=params["grid_total_gwh"],
            )
            result["ci_after_g_kwh"] = ci_result["ci_after_g_kwh"]

        with _timer("calc_footprint", timings):
            footprint = calc_carbon_footprint(
                fleet_df, renew_gwh, result,
                country_co2_total_mt=db_entry.get("total_co2_mt_2022", 0.0),
                ci_after_g_kwh=ci_result["ci_after_g_kwh"],
                renew_lifecycle_ef=params["renew_lifecycle_ef"],
            )

        with _timer("calc_nz", timings):
            nz_result = calc_net_zero(
                country_code, result, ci_result, footprint,
                renew_gwh, db_entry,
            )

        # ── 9. Plots ────────────────────────────────────────────────────
        if _ABATEMENT_PLOTS_AVAILABLE:
            with _timer("plots", timings):
                try:
                    plot_geography(
                        self.styler, result, plants_df, zonal_dfs,
                        mainland_gdf, context_gdf, admin_gdf, country_name,
                        out_fig / f"{country_code}_abatement_maps.png",
                        params["thermal_params"], params["renewable_cf"],
                    )
                    plot_macc_curve(
                        self.styler, result, country_name,
                        out_fig / f"{country_code}_macc_curve.png",
                    )
                    plot_substitution(
                        self.styler, result, renew_gwh, country_name,
                        out_fig / f"{country_code}_substitution_curves.png",
                        params["renewable_cf"],
                    )
                    plot_carbon_intensity(
                        self.styler, ci_result, renew_gwh, result, country_name,
                        out_fig / f"{country_code}_carbon_intensity.png",
                        params["thermal_params"], params["renew_lifecycle_ef"],
                    )
                    plot_net_zero(
                        self.styler, nz_result, ci_result, footprint, result,
                        country_name,
                        out_fig / f"{country_code}_net_zero.png",
                    )
                except Exception as exc:
                    logger.error("  Plot generation failed: %s", exc)
        else:
            logger.warning("  abatement_plots module not available — skipping visualisations.")

        # ── 10. Text report ──────────────────────────────────────────────
        with _timer("report", timings):
            report = self._format_report(
                result, ci_result, footprint, nz_result,
                fleet_df, renew_gwh, renew_lcoe,
                country_name, country_code,
                params["renewable_cf"], params, ts_report,
            )
            report_file = out_rep / f"{country_code}_abatement_{t0.strftime('%Y%m%d_%H%M%S')}.txt"
            report_file.write_text(report, encoding="utf-8")

        # ── 11. Summary log ──────────────────────────────────────────────
        flag = "self-financing" if result["mac_global"] <= 0 else f"needs {result['mac_global']:.1f} USD/tCO₂"
        ndc_coverage = f"{nz_result['coverage_pct']:.1f}%" if pd.notna(nz_result["coverage_pct"]) else "N/D"
        logger.info(
            "[%s] MAC %.1f USD/tCO₂e (%s) | Net Avoided: %.2f MtCO₂/yr | "
            "Value: %.2f B USD | Substituted: %.1f TWh | NDC coverage: %s",
            country_code,
            result["mac_global"], flag,
            nz_result["net_avoided_mt"],
            result["total_value_b"],
            result["subst_gwh"] / 1000,
            ndc_coverage,
        )

        elapsed = round((datetime.now() - t0).total_seconds(), 1)
        logger.info("[%s] Phase 7 completed in %.1fs.", country_code, elapsed)

        # ── 12. Completeness check ──────────────────────────────────────
        expected_plots = [
            f"{country_code}_abatement_maps.png",
            f"{country_code}_macc_curve.png",
            f"{country_code}_substitution_curves.png",
            f"{country_code}_carbon_intensity.png",
            f"{country_code}_net_zero.png",
        ]
        if _ABATEMENT_PLOTS_AVAILABLE:
            missing = [p for p in expected_plots if not (out_fig / p).exists()]
            if missing:
                logger.warning("  Missing plots: %s", ", ".join(missing))
            else:
                logger.info("  All expected plots generated successfully.")

        # ── 13. Build serialisable result ────────────────────────────────
        serializable_result: Dict[str, Any] = {
            "country": country_code,
            "timestamp": t0.isoformat(),
            "elapsed": elapsed,
            "available": True,
            "mac_usd_tco2e": result["mac_global"],
            "mac_global": result["mac_global"],
            "bcp_global": result["bcp_global"],
            "carbon_price": params["carbon_price"],
            "co2_avoided_mt": result["co2_avoided_mt"],
            "net_avoided_mt": nz_result["net_avoided_mt"],
            "total_value_b": result["total_value_b"],
            "operating_savings_b": result["operating_savings_b"],
            "carbon_value_b": result["carbon_value_b"],
            "subst_gwh": result["subst_gwh"],
            "subst_pct": result["subst_pct"],
            "ci_before": ci_result["ci_before_g_kwh"],
            "ci_after": ci_result["ci_after_g_kwh"],
            "ci_thermal_only": ci_result["ci_thermal_only_g_kwh"],
            "ci_reduction_pct": ci_result["ci_reduction_pct"],
            "existing_renewable_share": params["existing_renewable_share"],
            "penetration_target": params["penetration"],
            "grid_total_gwh": params["grid_total_gwh"],
            "ndc_coverage_pct": nz_result["coverage_pct"] if pd.notna(nz_result["coverage_pct"]) else None,
            "elec_coverage_pct": nz_result["elec_coverage_pct"],
            "national_contribution_pct": nz_result["national_contribution_pct"],
            "net_zero_year": nz_result["net_zero_year"],
            "owid_scope_warning": nz_result.get("owid_scope_warning", False),
            "capex_total_b": (
                result["lcoe_avg_renew"]
                * (result["subst_gwh"] / (max(params["renewable_cf"].get("solar", 0.2), 0.01) * 8760))
                / 1e6
                if result["subst_gwh"] > 0 else 0.0
            ),
        }

        # ── 14. Artifact persistence ──────────────────────────────────────
        from src.io.artifact_manager import ArtifactManager
        artifact_mgr = ArtifactManager(self.outputs_dir, country_code)
        phase_dir = artifact_mgr.phase_dir("abatement")

        artifact_mgr.save_result(phase_dir, serializable_result, serializer="pickle")

        files: Dict[str, str] = {}
        for d in (out_fig, out_data, out_rep):
            for p in d.rglob("*"):
                if p.is_file():
                    files[str(p.relative_to(phase_dir))] = str(p)

        artifact_mgr.save_manifest(
            phase_dir, "abatement",
            files=files,
            parameters={
                "country": country_code,
                "penetration": params["penetration"],
                "carbon_price": params["carbon_price"],
                "thermal_types": list(params["thermal_params"].keys()),
            },
        )

        return serializable_result

    # ─────────────────────────────────────────────────────────────────────
    # Parameter loading — usando cfg._params (única fonte)
    # ─────────────────────────────────────────────────────────────────────

    def _load_params(self, code: str) -> Dict[str, Any]:
        """
        Load all abatement parameters from parameters.json (via cfg._params).

        Uses nested structure: country.{tech}.capacity_factor.
        """
        # T8_18: acesso via cfg._params (atributo privado existente)
        params = getattr(self.cfg, "_params", {}) or {}
        abat_default = params.get("abatement_defaults", {}).get("default", {})
        country_cfg = params.get("countries", {}).get(code, {})
        abat_cfg = country_cfg.get("abatement", {})
        fallback = params.get("fallback_logic", {})

        wb_meta = self._fetch_country_metadata(code)
        region = self._normalize_region(country_cfg.get("region") or wb_meta.get("region"))
        income_level = country_cfg.get("income_level") or wb_meta.get("income_level")
        grid_mix = country_cfg.get("grid_mix_dominant", "Mixed")

        region_defaults = fallback.get("region_defaults", {}).get(region, {})
        income_defaults = fallback.get("income_level_defaults", {}).get(income_level, {})
        grid_defaults = fallback.get("grid_mix_defaults", {}).get(grid_mix, {})
        global_defaults = fallback.get("global_average", {})

        # Helper para cascata de parâmetros
        def _get_param(key: str, sources: List[Tuple[Dict, str]], ultimate: float) -> float:
            for d, _ in sources:
                if key in d:
                    return float(d[key])
            return float(ultimate)

        carbon_price = _get_param(
            "carbon_price_usd_tco2e",
            [(abat_cfg, "country"), (region_defaults, "region"),
             (global_defaults, "global"), (abat_default, "default")],
            80.0,
        )

        penetration = min(max(_get_param(
            "penetration_factor",
            [(abat_cfg, "country"), (grid_defaults, "grid"),
             (region_defaults, "region"), (income_defaults, "income"),
             (global_defaults, "global"), (abat_default, "default")],
            0.60,
        ), 0.0), 1.0)

        existing_renewable_share = min(max(_get_param(
            "existing_renewable_share",
            [(abat_cfg, "country"), (region_defaults, "region"),
             (income_defaults, "income"), (global_defaults, "global"),
             (abat_default, "default")],
            0.0,
        ), 0.0), 1.0)

        grid_total_gwh = max(_get_param(
            "grid_total_gwh",
            [(abat_cfg, "country"), (global_defaults, "global"),
             (abat_default, "default")],
            0.0,
        ), 0.0)

        # ── Thermal parameters ──────────────────────────────────────────
        thermal_types = abat_default.get("thermal_types", ["coal", "gas", "oil"])
        thermal_params: Dict[str, Dict[str, float]] = {}
        for fuel in thermal_types:
            fb = GLOBAL_THERMAL_FALLBACK.get(fuel, {"ef": 500.0, "cf": 0.45, "fuel_mc": 50.0})
            thermal_params[fuel] = {
                "ef": float(abat_cfg.get("thermal_ef", {}).get(fuel, fb["ef"])),
                "cf": float(
                    abat_cfg.get("thermal_cf", {}).get(
                        fuel, region_defaults.get("thermal_cf", {}).get(
                            fuel, abat_default.get("thermal_cf", {}).get(fuel, fb["cf"])
                        )
                    )
                ),
                "fuel_mc": float(
                    abat_cfg.get("thermal_srmc", {}).get(
                        fuel, region_defaults.get("thermal_srmc", {}).get(
                            fuel, abat_default.get("thermal_marginal_cost", {}).get(fuel, fb["fuel_mc"])
                        )
                    )
                ),
            }

        cap_override = {
            k: float(v)
            for k, v in abat_cfg.get("thermal_capacity_mw", {}).items()
            if k in thermal_params and not k.startswith("_")
        }

        # ── Renewable CFs & lifecycle EFs ──────────────────────────────
        lc_map = abat_default.get("renewable_lifecycle_gco2_kwh", {})
        renewable_cf: Dict[str, float] = {}
        renew_lifecycle_ef: Dict[str, float] = {}

        for tech in TECH_ORDER:
            # Nested lookup: country_cfg[tech]["capacity_factor"]
            tech_nested = country_cfg.get(tech, {})
            cf_from_params = tech_nested.get("capacity_factor") if isinstance(tech_nested, dict) else None

            if cf_from_params is not None and float(cf_from_params) > 0:
                renewable_cf[tech] = float(cf_from_params)
            else:
                renewable_cf[tech] = float(
                    region_defaults.get(f"renew_capacity_factor_{tech}")
                    or income_defaults.get(f"renew_capacity_factor_{tech}")
                    or global_defaults.get(f"renew_capacity_factor_{tech}")
                    or GLOBAL_RENEWABLE_CF_FALLBACK.get(tech, 0.25)
                )

            renew_lifecycle_ef[tech] = float(
                abat_cfg.get("renewable_lifecycle_ef", {}).get(
                    tech,
                    region_defaults.get("renewable_lifecycle_ef", {}).get(
                        tech,
                        lc_map.get(tech, GLOBAL_RENEW_LIFECYCLE_FALLBACK.get(tech, 50.0)),
                    ),
                )
            )

        logger.info(
            "[%s] Params: penetration=%.0f%% | existing_renew=%.0f%% | "
            "grid=%.0f GWh | carbon_price=%.0f USD/tCO₂e | "
            "CF solar=%.3f wind=%.3f biomass=%.3f",
            code,
            penetration * 100, existing_renewable_share * 100,
            grid_total_gwh, carbon_price,
            renewable_cf.get("solar", 0), renewable_cf.get("wind", 0),
            renewable_cf.get("biomass", 0),
        )

        return {
            "carbon_price": carbon_price,
            "cap_override": cap_override,
            "penetration": penetration,
            "existing_renewable_share": existing_renewable_share,
            "grid_total_gwh": grid_total_gwh,
            "thermal_params": thermal_params,
            "renewable_cf": renewable_cf,
            "renew_lifecycle_ef": renew_lifecycle_ef,
            "region": region,
            "income_level": income_level,
            "grid_mix_dominant": grid_mix,
        }

    # ─────────────────────────────────────────────────────────────────────
    # Region normalisation
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_region(region: Optional[str]) -> Optional[str]:
        if not region:
            return None
        r = str(region).strip().lower()
        if "eastern europe" in r: return "Eastern Europe"
        if "europe" in r: return "Europe"
        if "north america" in r: return "North America"
        if "latin america" in r or "south america" in r: return "South America"
        if "north africa" in r: return "North Africa"
        if "sub-saharan" in r or "subsaharan" in r: return "Sub-Saharan Africa"
        if "africa" in r: return "Africa"
        if "middle east" in r: return "Middle East"
        if "south asia" in r: return "South Asia"
        if "east asia" in r: return "East Asia"
        if "asia" in r or "pacific" in r or "oceania" in r: return "Asia Pacific"
        return region

    # ─────────────────────────────────────────────────────────────────────
    # External data fetching (short timeouts — non-blocking offline)
    # ─────────────────────────────────────────────────────────────────────

    def _fetch_country_metadata(self, code: str) -> Dict[str, Optional[str]]:
        url = f"https://api.worldbank.org/v2/country/{code}?format=json"
        try:
            resp = self._session.get(url, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, list) and len(payload) > 1 and payload[1]:
                row = payload[1][0]
                return {
                    "region": row.get("region", {}).get("value"),
                    "income_level": row.get("incomeLevel", {}).get("value"),
                }
        except Exception as exc:
            logger.debug("[%s] World Bank metadata: %s", code, exc)
        return {"region": None, "income_level": None}

    def _fetch_world_bank_total_co2_mt(self, code: str) -> Tuple[Optional[float], Optional[int]]:
        url = f"https://api.worldbank.org/v2/country/{code}/indicator/EN.ATM.CO2E.KT?format=json&mrv=5"
        try:
            resp = self._session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, list) and len(payload) > 1 and payload[1]:
                for row in payload[1]:
                    if row.get("value") is not None:
                        return float(row["value"]) / 1000.0, int(row.get("date", 0))
        except Exception as exc:
            logger.debug("[%s] World Bank CO2: %s", code, exc)
        return None, None

    def _fetch_owid_total_co2_mt(self, code: str) -> Tuple[Optional[float], Optional[int]]:
        if self._owid_df_cache is None:
            url = "https://raw.githubusercontent.com/owid/co2-data/master/owid-co2-data.csv"
            try:
                resp = self._session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
                resp.raise_for_status()
                self._owid_df_cache = pd.read_csv(
                    StringIO(resp.text),
                    usecols=["iso_code", "year", "co2"],
                    dtype={"iso_code": str, "year": int, "co2": float},
                )
            except Exception as exc:
                logger.debug("[%s] OWID download: %s", code, exc)
                return None, None

        try:
            cdf = self._owid_df_cache[self._owid_df_cache["iso_code"] == code.upper()].dropna(subset=["co2"])
            if not cdf.empty:
                row = cdf.sort_values("year", ascending=False).iloc[0]
                return float(row["co2"]), int(row["year"])
        except Exception as exc:
            logger.debug("[%s] OWID cache: %s", code, exc)
        return None, None

    def _build_net_zero_fallback(self, code: str) -> Dict[str, Any]:
        db_raw = self._load_net_zero_db()
        db = {k: v for k, v in db_raw.get(code, {}).items() if v is not None}

        params = getattr(self.cfg, "_params", {}) or {}
        nz_cfg = params.get("countries", {}).get(code, {}).get("net_zero_target", {})
        mapping = [
            ("base_year", "base_year"),
            ("base_co2_mt", "base_co2_mt"),
            ("ndc_2030_reduction_pct", "ndc_2030_pct"),
            ("ndc_2030_intensity_reduction_pct", "ndc_2030_intensity_pct"),
            ("net_zero_year", "net_zero_year"),
            ("source", "source"),
        ]
        for src, dst in mapping:
            if nz_cfg.get(src) is not None:
                db[dst] = nz_cfg[src]

        db.setdefault("country_code", code)
        db.setdefault("total_co2_mt_2022", 0.0)
        db.setdefault("net_zero_year", 2050)
        db.setdefault("source", "net_zero_db.json / fallback")
        return db

    def _load_net_zero_db(self) -> Dict[str, Any]:
        if self._nz_db_cache is not None:
            return self._nz_db_cache

        db_path = Path.cwd() / "configs" / "net_zero_db.json"
        if not db_path.exists():
            found = list(Path.cwd().rglob("net_zero_db.json"))
            if found:
                db_path = found[0]

        if db_path.exists():
            try:
                with open(db_path, "r", encoding="utf-8") as f:
                    self._nz_db_cache = json.load(f).get("countries", {})
                logger.info("Net Zero DB loaded — %s", db_path.name)
                return self._nz_db_cache
            except Exception as exc:
                logger.error("Failed to load net_zero_db.json: %s", exc)

        self._nz_db_cache = {}
        return self._nz_db_cache

    def _fetch_net_zero_data(self, code: str) -> Dict[str, Any]:
        db = self._build_net_zero_fallback(code)

        wb_total, wb_year = self._fetch_world_bank_total_co2_mt(code)
        if wb_total is not None:
            db["total_co2_mt_2022"] = wb_total
            db["source"] = f"{db.get('source', '')}; World Bank CO2 ({wb_year})".strip("; ")
            logger.info("[%s] Emissions via World Bank API → %.1f Mt (%s)", code, wb_total, wb_year)
        else:
            owid_total, owid_year = self._fetch_owid_total_co2_mt(code)
            if owid_total is not None:
                db["total_co2_mt_2022"] = owid_total
                db["source"] = f"{db.get('source', '')}; OWID/GCP CO2 ({owid_year})".strip("; ")
                logger.info("[%s] Emissions via OWID/GCP → %.1f Mt (%s)", code, owid_total, owid_year)

        return db

    # ─────────────────────────────────────────────────────────────────────
    # Renewable data loading
    # ─────────────────────────────────────────────────────────────────────

    def _load_renewable_data(
        self,
        potential_dir: Path,
        lcoe_dir: Path,
        code: str,
        renewable_cf: Dict[str, float],
    ) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, pd.DataFrame]]:
        """
        Load renewable generation (GWh) and LCOE (USD/MWh) from Phase 4/5 CSVs.
        """
        renew_gwh: Dict[str, float] = {}
        renew_lcoe: Dict[str, float] = {}
        zonal_dfs: Dict[str, pd.DataFrame] = {}

        for tech in TECH_ORDER:
            # ── Generation (Phase 4 CSV) ──────────────────────────────
            p = potential_dir / "data" / f"{code}_{tech}_balanced_zonal.csv"
            gwh = 0.0
            if p.exists():
                try:
                    df = pd.read_csv(p)
                    zonal_dfs[tech] = df

                    if "generation_twh" in df.columns:
                        gwh = float(df["generation_twh"].sum() * 1000.0)
                    elif "generation_gwh" in df.columns:
                        gwh = float(df["generation_gwh"].sum())
                    elif "capacity_mw_sum" in df.columns:
                        gwh = float(df["capacity_mw_sum"].sum() * renewable_cf.get(tech, 0.25) * 8760.0 / 1000.0)
                    else:
                        logger.warning("  [%s] No generation column found in %s", tech, p.name)
                except Exception as exc:
                    logger.debug("[%s-%s] Potential CSV error: %s", code, tech, exc)

            renew_gwh[tech] = gwh

            # ── LCOE (Phase 5 CSV) ──────────────────────────────────
            lcoe_path = lcoe_dir / "data" / f"{code}_{tech}_lcoe_zonal.csv"
            avg_lcoe = _LCOE_FALLBACK.get(tech, 55.0)

            if lcoe_path.exists():
                try:
                    df_lcoe = pd.read_csv(lcoe_path)
                    lcoe_col = next(
                        (c for c in ["lcoe_mean", "lcoe_usd_mwh", "mean_lcoe", "lcoe"] if c in df_lcoe.columns),
                        None,
                    )
                    weight_col = next(
                        (c for c in ["pixel_count", "count", "n_pixels", "area_km2"] if c in df_lcoe.columns),
                        None,
                    )
                    if lcoe_col:
                        valid = df_lcoe[df_lcoe[lcoe_col] > 0]
                        if not valid.empty:
                            avg_lcoe = float(
                                (valid[lcoe_col] * valid[weight_col]).sum() / valid[weight_col].sum()
                                if weight_col
                                else valid[lcoe_col].mean()
                            )
                except Exception as exc:
                    logger.warning("[%s-%s] LCOE CSV error, using fallback %.1f: %s", code, tech, avg_lcoe, exc)

            renew_lcoe[tech] = avg_lcoe
            logger.info("  [%s] renew: %.0f GWh  |  LCOE: %.1f USD/MWh", tech, gwh, avg_lcoe)

        return renew_gwh, renew_lcoe, zonal_dfs

    # ─────────────────────────────────────────────────────────────────────
    # Text report (unchanged, kept for completeness)
    # ─────────────────────────────────────────────────────────────────────

    def _format_report(
        self,
        result: Dict[str, Any],
        ci_result: Dict[str, Any],
        footprint: Dict[str, Any],
        nz: Dict[str, Any],
        fleet_df: pd.DataFrame,
        renew_gwh: Dict[str, float],
        renew_lcoe: Dict[str, float],
        country_name: str,
        code: str,
        renewable_cf: Dict[str, float],
        params: Dict[str, Any],
        ts_report: str,
    ) -> str:

        total_r = max(sum(renew_gwh.values()), 1)

        fleet_rows = "\n".join(
            f"  {r['fuel'].capitalize():<6} {r['capacity_mw']:>8,.0f} "
            f"{r['cf']:>5.0%} {r['gen_gwh']:>12,.0f} "
            f"{r['ef']:>13,.0f} {r['fuel_mc']:>11.1f} {r['co2_mt']:>14.3f}"
            for _, r in fleet_df.iterrows()
        ) if not fleet_df.empty else "  (no thermal fleet)"

        renew_rows = "\n".join(
            f"  {TECH_LABELS.get(t['tech'], t['tech']):<14} "
            f"{t['generation_gwh']:>12,.0f} "
            f"{t['generation_gwh'] / (renewable_cf.get(t['tech'], 0.25) * 8760):>6.1f} "
            f"{t['generation_gwh'] / total_r * 100:>8.1f} "
            f"{t['lcoe_usd_mwh']:>12.1f} {t['mac_usd_tco2e']:>12.1f}"
            + (" ✓ vs LRMC" if t["competitive_lrmc"] else (" ✓ vs SRMC" if t["competitive"] else ""))
            for t in result["by_tech"]
        ) if result["by_tech"] else "  (no renewable data)"

        subst_rows = (
            "\n".join(
                f"    {r['fuel'].capitalize():<6} : "
                f"{r['subst_gwh']:>8,.0f} GWh substituted  | "
                f"{r['co2_avoided']:.3f} MtCO₂e avoided"
                for _, r in fleet_df.iterrows()
            )
            if not fleet_df.empty and "subst_gwh" in fleet_df.columns
            else "    N/A"
        )

        financing_status = (
            "    → SELF-FINANCING (renewables already cheaper than thermal SRMC)"
            if result["mac_global"] <= 0 else
            f"    → Carbon proxy at {result['carbon_price']:.0f} USD/tCO₂ "
            f"{'COVERS' if result['carbon_price'] >= result['mac_global'] else 'below'} breakeven"
        )

        existing_pct = params["existing_renewable_share"] * 100.0
        target_pct = params["penetration"] * 100.0
        gap_pct = max(0.0, target_pct - existing_pct)

        penetration_block = (
            f"  Existing renewable share (grid)       : {existing_pct:.1f}%\n"
            f"  Penetration target (renewable share)  : {target_pct:.1f}%\n"
            f"  Renewable gap to close                : {gap_pct:.1f}%\n"
            f"  Grid baseline                         : {params['grid_total_gwh']:,.0f} GWh/yr\n"
            f"  Substitution logic                    : {result.get('subst_mode', 'N/A')}"
        )

        if nz.get("ndc_type") == "absolute":
            ndc_block = (
                f"  NDC type                               : absolute reduction target\n"
                f"  NDC base year / base emissions         : {nz['base_year']} / {float(nz.get('base_mt') or 0):.1f} MtCO₂e/yr\n"
                f"  NDC 2030 commitment                    : −{float(nz.get('ndc_pct') or 0):.0f}% vs {nz['base_year']}\n"
                f"  NDC 2030 target (absolute cap)         : <= {nz.get('target_2030_mt', 0):.1f} MtCO₂e/yr\n"
                f"  Current total vs target gap            : {nz.get('current_gap_mt', 0):.1f} MtCO₂e/yr\n"
                f"  Electricity transition contribution    : {nz['net_avoided_mt']:.2f} MtCO₂e/yr net avoided\n"
                f"    → covers {nz.get('coverage_pct', float('nan')):.1f}% of the 2030 gap\n"
                f"  Residual gap after this transition     : {nz.get('residual_gap_mt', float('nan')):.1f} MtCO₂e/yr\n"
                f"  Source: {nz.get('source', '')}"
            )
        else:
            ndc_block = (
                f"  NDC 2030 commitment type               : intensity-based / unavailable absolute target\n"
                f"  Reported intensity reduction target    : {float(nz.get('ndc_intensity_pct') or 0):.0f}%\n"
                f"  Net avoided by this transition         : {nz['net_avoided_mt']:.2f} MtCO₂e/yr\n"
                f"  Source: {nz.get('source', '')}"
            )

        fleet_total_cap = fleet_df["capacity_mw"].sum() if not fleet_df.empty else 0
        fleet_total_gen = fleet_df["gen_gwh"].sum() if not fleet_df.empty else 0
        fleet_total_co2 = fleet_df["co2_mt"].sum() if not fleet_df.empty else 0

        return (
            f"{'='*72}\n"
            f"GHG ABATEMENT SYNTHESIS REPORT — Phase 7\n"
            f"{country_name} ({code})\n"
            f"Generated: {ts_report}\n"
            f"{'='*72}\n\n"
            f"{'='*72}\nEXECUTIVE SUMMARY\n{'='*72}\n"
            f"MAC (Marginal Abatement Cost)      : {result['mac_global']:.1f} USD/tCO₂e\n"
            f"Breakeven Carbon Price (BCP)       : {result['bcp_global']:.1f} USD/tCO₂e\n"
            f"CO₂ Avoided (annual)               : {result['co2_avoided_mt']:.2f} MtCO₂e/yr\n"
            f"Total Economic Value               : {result['total_value_b']:.2f} Billion USD/yr\n"
            f"Grid Substitution Volume           : {result['subst_gwh']/1000:.1f} TWh ({result['subst_pct']:.1f}% of modelled thermal)\n"
            f"Carbon Intensity (full grid)       : {ci_result['ci_before_g_kwh']:.0f} → {ci_result['ci_after_g_kwh']:.0f} gCO₂/kWh\n"
            f"  [thermal-only reference]         : {ci_result['ci_thermal_only_g_kwh']:.0f} gCO₂/kWh\n"
            f"{'='*72}\n\n"
            f"SECTION 0 — PENETRATION & RENEWABLE CONTEXT\n"
            f"{'-'*60}\n{penetration_block}\n\n"
            f"SECTION 1 — THERMAL FLEET (BEFORE TRANSITION)\n"
            f"{'-'*60}\n"
            f"  Fuel     Cap MW    CF   Gen GWh/yr   EF tCO₂/GWh  SRMC $/MWh   CO₂ MtCO₂/yr\n"
            f"  {'-'*60}\n"
            f"{fleet_rows}\n"
            f"  TOTAL  {fleet_total_cap:>8,.0f}       {fleet_total_gen:>12,.0f}                        {fleet_total_co2:>14.3f}\n\n"
            f"SECTION 2 — RENEWABLE POTENTIAL (Balanced scenario)\n"
            f"{'-'*60}\n"
            f"  Tech           Gen GWh/yr     GW  Share %   LCOE $/MWh   MAC $/tCO₂\n"
            f"  {'-'*60}\n"
            f"{renew_rows}\n\n"
            f"SECTION 3 — MAC / SUBSTITUTION RESULTS\n"
            f"{'-'*60}\n"
            f"  Carbon proxy applied                   : {result['carbon_price']:.0f} USD/tCO₂e\n"
            f"  SRMC thermal (gen-weighted)            : {result['srmc_avg']:.1f} USD/MWh\n"
            f"  LRMC thermal (with carbon)             : {result['lrmc_avg']:.1f} USD/MWh\n"
            f"  Avg renewable LCOE                     : {result['lcoe_avg_renew']:.1f} USD/MWh\n"
            f"  ► MAC (global, gen-weighted)           : {result['mac_global']:.2f} USD/tCO₂e\n"
            f"  ► BCP (breakeven carbon price)         : {result['bcp_global']:.2f} USD/tCO₂e\n"
            f"{financing_status}\n"
            f"  Substitution volume                    : {result['subst_gwh']:,.0f} GWh/yr ({result['subst_pct']:.1f}% of modelled thermal)\n"
            f"{subst_rows}\n"
            f"  CO₂ avoided                            : {result['co2_avoided_mt']:.3f} MtCO₂e/yr\n"
            f"  TOTAL economic value created           : {result['total_value_b']:.3f} Billion USD/yr\n\n"
            f"SECTION 4 — CARBON INTENSITY (gCO₂eq/kWh)\n"
            f"{'-'*60}\n"
            f"  Before transition (full national grid) : {ci_result['ci_before_g_kwh']:.1f} gCO₂/kWh\n"
            f"    [thermal fleet only, reference]      : {ci_result['ci_thermal_only_g_kwh']:.1f} gCO₂/kWh\n"
            f"  After transition (post-subst mix)      : {ci_result['ci_after_g_kwh']:.1f} gCO₂/kWh\n"
            f"  Reduction                              : {ci_result['ci_reduction_pct']:.1f}%\n"
            f"  New renewables avg lifecycle           : {ci_result['ci_renew_avg_g_kwh']:.1f} gCO₂/kWh\n"
            f"  Existing renewables assumed lifecycle  : {EXISTING_RENEW_LIFECYCLE_G:.1f} gCO₂/kWh (IPCC AR6)\n\n"
            f"SECTION 5 — CARBON FOOTPRINT BY SCOPE (GHG Protocol)\n"
            f"{'-'*60}\n"
            f"  Scope 1 (direct thermal, after subst.) : {footprint['scope1_after_mt']:.3f} MtCO₂e/yr\n"
            f"  Scope 2 (indirect — new renew/grid)    : {footprint['scope2_after_mt']:.3f} MtCO₂e/yr\n"
            f"  Scope 3 (renew lifecycle: upstreams)   : {footprint['scope3_renew_mt']:.3f} MtCO₂e/yr\n"
            f"  Total Scope 1+2+3 (post-transition)    : {footprint['total_after_mt']:.3f} MtCO₂e/yr\n\n"
            f"SECTION 6 — NET ZERO ANALYSIS\n"
            f"{'-'*60}\n"
            f"  Country net-zero target year           : {nz['net_zero_year']}\n"
            f"  Current total CO₂ (all sectors, API)   : {nz['current_total_mt']:.1f} MtCO₂e/yr\n"
            f"{'-'*60}\n{ndc_block}\n\n"
            f"SECTION 7 — EMISSION BALANCE DECOMPOSITION\n"
            f"{'-'*60}\n"
            f"  Current national footprint (all sectors): +{nz['current_total_mt']:.2f} MtCO₂e/yr\n"
            f"    of which modelled thermal sector      :  {result['total_thermal_co2']:.2f} MtCO₂e/yr\n"
            f"  − CO₂ avoided (thermal displacement)    : −{result['co2_avoided_mt']:.3f} MtCO₂e/yr\n"
            f"  + Lifecycle CO₂ (new renewables built)  : +{nz['renew_lifecycle_mt']:.3f} MtCO₂e/yr\n"
            f"  + Residual thermal CO₂ kept for grid    : +{ci_result['residual_thermal_co2_mt']:.3f} MtCO₂e/yr\n"
            f"  = Post-transition national proxy        :  {nz['total_after_mt']:.2f} MtCO₂e/yr\n"
            f"{'='*72}\n"
        )