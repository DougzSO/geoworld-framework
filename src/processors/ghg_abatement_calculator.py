"""
ghg_abatement_calculator.py — Phase 7: GHG Abatement, Carbon Intensity & Net Zero
==================================================================================
SCOPE: ELECTRICITY TRANSITION (Electricity Generation Sector ONLY)

Calculates the technical/economic substitution of fossil thermal generation 
(coal, gas, oil) with renewable sources (solar, wind, biomass). 
Does not cover transportation, heating, or process emissions.

Outputs:
1. MAC — Marginal Abatement Cost (USD/tCO₂e)
2. Carbon Intensity (gCO₂/kWh) — Before and after transition
3. Carbon Footprint (GHG Protocol Scopes 1, 2, 3) relative to power sector
4. Net Zero & NDC Coverage — Relative alignment with national targets
5. Emissions Balance — Net abated vs residual lifecycle emissions

References:
  - IPCC AR5/AR6 (2014, 2022): Lifecycle emission factors
  - IEA WEO (2023): SRMC baselines (coal 30 USD, gas 44 USD, oil 70 USD/MWh)
  - IRENA (2024): Grid penetration limits
  - UNFCCC NDC Registry / CAT (2024): Absolute/Intensity targets
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import numpy as np
import pandas as pd
import requests

from src.core.config_loader import ConfigLoader
from src.core.constants import TECH_ORDER
from src.utils.map_styling import GeoWorldStyler

# Defensive import for plotting dependencies
try:
    from src.utils.abatement_plots import (
        TECH_LABELS,
        plot_geography, plot_macc_curve,
        plot_substitution, plot_carbon_intensity, plot_net_zero,
    )
    _ABATEMENT_PLOTS_AVAILABLE = True
except ImportError:
    _ABATEMENT_PLOTS_AVAILABLE = False
    TECH_LABELS = {}
    plot_geography = plot_macc_curve = plot_substitution = None
    plot_carbon_intensity = plot_net_zero = None
    logging.getLogger("geoworld.processors.GHGAbatementCalculator").warning(
        "abatement_plots module not found. Visualizations are disabled."
    )

logger = logging.getLogger("geoworld.processors.GHGAbatementCalculator")

# ── Fallbacks ────────────────────────────────────────────────────────────────
GLOBAL_THERMAL_FALLBACK: Dict[str, Dict[str, float]] = {
    "coal": {"ef": 820.0, "cf": 0.55, "fuel_mc": 30.0},
    "gas":  {"ef": 490.0, "cf": 0.45, "fuel_mc": 44.0},
    "oil":  {"ef": 750.0, "cf": 0.50, "fuel_mc": 70.0},
}

GLOBAL_RENEW_LIFECYCLE_FALLBACK: Dict[str, float] = {
    "solar": 48.0,
    "wind": 11.0,
    "biomass": 230.0,
}

GLOBAL_RENEWABLE_CF_FALLBACK: Dict[str, float] = {
    "solar": 0.20,
    "wind": 0.30,
    "biomass": 0.75,
}

FUEL_ALIASES: Dict[str, str] = {
    "gas": "gas", "natural gas": "gas", "gas (combined cycle)": "gas", "ccgt": "gas",
    "ocgt": "gas", "gas turbine": "gas", "gas/oil": "gas", "lng": "gas", "cng": "gas",
    "coal": "coal", "hard coal": "coal", "lignite": "coal", "brown coal": "coal",
    "sub-bituminous coal": "coal", "bituminous coal": "coal", "anthracite": "coal",
    "coal (conventional)": "coal", "coal (cogeneration)": "coal",
    "oil": "oil", "fuel oil": "oil", "heavy fuel oil": "oil", "diesel": "oil",
    "hfo": "oil", "light fuel oil": "oil", "petroleum": "oil", "distillate": "oil",
    "kerosene": "oil",
}

# ─────────────────────────────────────────────────────────────────────────────
# PURE FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def calc_thermal_fleet(
    plants_df: pd.DataFrame,
    thermal_params: Dict[str, Dict[str, float]],
    cap_override: Optional[Dict[str, float]] = None,
) -> pd.DataFrame:
    """Builds the thermal generation baseline normalizing capacities."""
    rows: List[Dict] = []
    valid_fuels = set(thermal_params.keys())
    plants_hits, override_hits, zero_fuels = {}, {}, []
    
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
            labels_found = sorted(df[fc].astype(str).str.lower().str.strip().unique().tolist())

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
        else:
            zero_fuels.append(fuel)

    logger.debug(f"thermal_fleet generation details: plants_hits={plants_hits}, override={override_hits}")
    
    if not plants_hits and not override_hits:
        logger.warning(
            f"No thermal power plants established. plants_df available: {plants_available}, "
            f"Found labels: {labels_found[:10] if labels_found else 'None'}."
        )

    if not rows:
        return pd.DataFrame(columns=["fuel", "capacity_mw", "gen_gwh", "co2_kt", "co2_mt", "fuel_mc", "ef", "cf"])
    return pd.DataFrame(rows)


def _thermal_row(fuel: str, cap: float, p: Dict) -> Dict:
    gen = cap * p["cf"] * 8760 / 1000
    co2 = gen * p["ef"]
    return {
        "fuel": fuel, "capacity_mw": cap, "gen_gwh": gen,
        "co2_kt": co2 / 1000, "co2_mt": co2 / 1e6,
        "fuel_mc": p["fuel_mc"], "ef": p["ef"], "cf": p["cf"],
    }


def calc_macc(
    fleet_df: pd.DataFrame,
    renew_gwh: Dict[str, float],
    renew_lcoe: Dict[str, float],
    carbon_price: float,
    penetration: float = 0.80,
) -> Dict[str, Any]:
    """Calculates Substitution curves and Marginal Abatement Costs."""
    renew_total_gwh = sum(float(v or 0.0) for v in renew_gwh.values())
    total_th_gwh = float(fleet_df["gen_gwh"].sum()) if not fleet_df.empty else 0.0

    if fleet_df.empty or renew_total_gwh <= 0 or total_th_gwh <= 0:
        return _empty_macc()

    penetration = min(max(float(penetration), 0.0), 1.0)
    
    srmc_avg = float((fleet_df["fuel_mc"] * fleet_df["gen_gwh"]).sum() / total_th_gwh)
    ef_avg   = float((fleet_df["ef"] * fleet_df["gen_gwh"]).sum() / max(total_th_gwh, 1e-9))

    fdf = fleet_df.copy()
    fdf["lrmc"] = fdf["fuel_mc"] + fdf["ef"] * carbon_price / 1000.0
    lrmc_avg = float((fdf["lrmc"] * fdf["gen_gwh"]).sum() / total_th_gwh)

    by_tech = []
    for tech in TECH_ORDER:
        gwh = float(renew_gwh.get(tech, 0.0) or 0.0)
        lcoe = float(renew_lcoe.get(tech, 0.0) or 0.0)
        if gwh > 0 and lcoe > 0:
            mac = (lcoe - srmc_avg) / max(ef_avg, 1e-9) * 1000.0
            by_tech.append({
                "tech": tech, "generation_gwh": gwh, "lcoe_usd_mwh": lcoe,
                "mac_usd_tco2e": mac, "bcp_usd_tco2e": max(0.0, mac),
                "competitive": lcoe <= srmc_avg, "competitive_lrmc": lcoe <= lrmc_avg,
                "subst_gwh": 0.0 
            })

    if not by_tech:
        return _empty_macc()

    subst_gwh = min(total_th_gwh, renew_total_gwh) * penetration

    # Merit order renewables substitution
    by_tech = sorted(by_tech, key=lambda x: x["mac_usd_tco2e"])
    rem_renew = subst_gwh
    for t in by_tech:
        take = min(rem_renew, t["generation_gwh"])
        t["subst_gwh"] = take
        rem_renew -= take

    # Order thermal plants by dirtiest/most expensive to replace
    fdf = fdf.sort_values("ef", ascending=False).copy()
    rem_therm = subst_gwh
    fdf["subst_gwh"] = 0.0
    fdf["co2_avoided"] = 0.0

    for idx in fdf.index:
        take = min(rem_therm, float(fdf.loc[idx, "gen_gwh"]))
        fdf.loc[idx, "subst_gwh"] = take
        fdf.loc[idx, "co2_avoided"] = take * float(fdf.loc[idx, "ef"]) / 1e6
        rem_therm -= take
        if rem_therm <= 0: break

    co2_avoided = float(fdf["co2_avoided"].sum())
    
    total_built = sum(t["generation_gwh"] for t in by_tech)
    lcoe_avg = sum(t["lcoe_usd_mwh"] * t["generation_gwh"] for t in by_tech) / max(total_built, 1.0)
    mac_global = (lcoe_avg - srmc_avg) / max(ef_avg, 1e-9) * 1000.0

    operating_savings_b = max(0.0, (srmc_avg - lcoe_avg) * subst_gwh * 1000.0 / 1e9)
    carbon_value_b = co2_avoided * 1e6 * carbon_price / 1e9

    return {
        "subst_gwh": subst_gwh, "subst_pct": subst_gwh / total_th_gwh * 100.0,
        "co2_avoided_mt": co2_avoided, "carbon_value_b": carbon_value_b,
        "fuel_savings_b": operating_savings_b, "operating_savings_b": operating_savings_b,
        "lrmc_savings_b": max(0.0, (lrmc_avg - lcoe_avg) * subst_gwh * 1000.0 / 1e9),
        "total_value_b": operating_savings_b + carbon_value_b,
        "mac_global": mac_global, "bcp_global": max(0.0, mac_global),
        "srmc_avg": srmc_avg, "lrmc_avg": lrmc_avg, "lcoe_avg_renew": lcoe_avg,
        "ef_avg": ef_avg, "carbon_price": carbon_price, "penetration": penetration,
        "competitive_gwh": sum(t["generation_gwh"] for t in by_tech if t["competitive_lrmc"]),
        "by_tech": by_tech, "fleet_df": fdf,
        "total_thermal_gwh": total_th_gwh, "total_thermal_co2": float(fleet_df["co2_mt"].sum()),
    }


def calc_carbon_intensity(
    fleet_df: pd.DataFrame, renew_gwh: Dict[str, float],
    result: Dict[str, Any], renew_lifecycle_ef: Dict[str, float],
) -> Dict[str, Any]:
    total_th_gwh = float(result["total_thermal_gwh"])
    total_th_co2_mt = float(result["total_thermal_co2"])
    ci_before_g = (total_th_co2_mt * 1e6) / max(total_th_gwh, 1.0)

    renew_co2_lifecycle_mt = 0.0
    renew_gen_built_gwh = 0.0

    for t in result["by_tech"]:
        gwh_built = t.get("subst_gwh", 0.0) 
        if gwh_built > 0:
            ef_g = float(renew_lifecycle_ef.get(t["tech"], GLOBAL_RENEW_LIFECYCLE_FALLBACK.get(t["tech"], 50.0)))
            renew_co2_lifecycle_mt += gwh_built * ef_g / 1e6
            renew_gen_built_gwh += gwh_built

    fdf = result["fleet_df"]
    residual_co2_mt = float(((fdf["gen_gwh"] - fdf["subst_gwh"]) * fdf["ef"]).sum() / 1e6)
    residual_thermal_gwh = max(0.0, total_th_gwh - result["subst_gwh"])
    
    total_gen_after = residual_thermal_gwh + renew_gen_built_gwh
    total_co2_after = residual_co2_mt + renew_co2_lifecycle_mt

    return {
        "ci_before_g_kwh": ci_before_g,
        "ci_after_g_kwh": (total_co2_after * 1e6) / max(total_gen_after, 1.0),
        "ci_renew_avg_g_kwh": (renew_co2_lifecycle_mt * 1e6) / max(renew_gen_built_gwh, 1.0),
        "ci_reduction_pct": (1.0 - (total_co2_after * 1e6) / max(total_gen_after, 1.0) / max(ci_before_g, 1.0)) * 100.0,
        "renew_lifecycle_co2_mt": renew_co2_lifecycle_mt,
        "residual_thermal_co2_mt": residual_co2_mt,
        "total_co2_after_mt": total_co2_after,
        "total_gen_after_gwh": total_gen_after,
        "benchmark_eu_g_kwh": 295.0, "benchmark_world_g_kwh": 459.0, "benchmark_netzero_g_kwh": 24.0,
    }


def calc_carbon_footprint(
    fleet_df: pd.DataFrame, renew_gwh: Dict[str, float], result: Dict[str, Any],
    country_co2_total_mt: float, ci_after_g_kwh: float, renew_lifecycle_ef: Dict[str, float],
) -> Dict[str, Any]:
    fdf = result["fleet_df"]
    scope1_after_mt = float(((fdf["gen_gwh"] - fdf["subst_gwh"]) * fdf["ef"]).sum() / 1e6)

    renew_gen_added_gwh = sum(float(t.get("subst_gwh", 0)) for t in result["by_tech"])
    scope2_after_mt = renew_gen_added_gwh * ci_after_g_kwh / 1e6

    scope3_renew_mt = sum(
        float(t.get("subst_gwh", 0)) * float(renew_lifecycle_ef.get(t["tech"], 50.0)) / 1e6
        for t in result["by_tech"]
    )

    return {
        "scope1_before_mt":        result["total_thermal_co2"],
        "scope1_after_mt":         scope1_after_mt,
        "scope1_reduction":        result["total_thermal_co2"] - scope1_after_mt,
        "scope2_after_mt":         scope2_after_mt,
        "scope3_renew_mt":         scope3_renew_mt,
        "reported_scopes_total_mt": scope1_after_mt + scope2_after_mt + scope3_renew_mt,
        "total_after_mt":          scope1_after_mt + scope2_after_mt + scope3_renew_mt,
        "country_total_mt":        float(country_co2_total_mt or 0.0),
        "sector_share_pct":        result["total_thermal_co2"] / max(float(country_co2_total_mt or 0.0), 1.0) * 100.0,
    }


def _empty_macc() -> Dict:
    return {
        "subst_gwh": 0, "subst_pct": 0, "co2_avoided_mt": 0,
        "carbon_value_b": 0, "fuel_savings_b": 0, "operating_savings_b": 0,
        "lrmc_savings_b": 0, "total_value_b": 0, "mac_global": 0, "bcp_global": 0,
        "srmc_avg": 0, "lrmc_avg": 0, "lcoe_avg_renew": 0, "ef_avg": 0,
        "carbon_price": 0, "competitive_gwh": 0, "by_tech": [],
        "fleet_df": pd.DataFrame(), "total_thermal_gwh": 0, "total_thermal_co2": 0,
        "penetration": 0.8,
    }


def calc_net_zero(
    country_code: str,
    result: Dict[str, Any],
    ci_result: Dict[str, Any],
    footprint: Dict[str, Any],
    renew_gwh: Dict[str, float],
    db: Dict[str, Any],
) -> Dict[str, Any]:
    
    total_now_mt = float(db.get("total_co2_mt_2022", 0.0) or 0.0)
    base_mt      = db.get("base_co2_mt")
    base_year    = db.get("base_year")
    ndc_pct      = db.get("ndc_2030_pct")
    ndc_intensity_pct = db.get("ndc_2030_intensity_pct")
    nz_year      = int(db.get("net_zero_year") or 2050)
    ndc_horizon  = int(db.get("ndc_horizon_year") or 2030)

    ndc_type = "absolute" if ndc_pct is not None else ("intensity" if ndc_intensity_pct is not None else "unknown")

    logger.debug(
        f"[{country_code}] calc_net_zero state: total_now_mt={total_now_mt:.2f}, "
        f"ndc_type={ndc_type}, nz_year={nz_year}"
    )

    co2_avoided      = float(result["co2_avoided_mt"])
    renew_lifecycle  = float(ci_result["renew_lifecycle_co2_mt"])
    net_avoided      = max(0.0, co2_avoided - renew_lifecycle)
    residual_thermal = float(ci_result["residual_thermal_co2_mt"])

    fossil_thermal_mt = float(result.get("total_thermal_co2", 0.0))
    elec_coverage_pct = min(100.0, net_avoided / fossil_thermal_mt * 100.0) if fossil_thermal_mt > 0 else 0.0
    elec_sector_reduction = (net_avoided / fossil_thermal_mt * 100.0) if fossil_thermal_mt > 0 else 0.0

    national_contribution_pct = (net_avoided / total_now_mt * 100.0) if total_now_mt > 0 else 0.0

    if ndc_type == "absolute" and ndc_pct is not None:
        effective_base = float(base_mt) if base_mt is not None else total_now_mt
        target_ndc_mt  = effective_base * (1.0 - float(ndc_pct) / 100.0)
        current_gap_mt = max(0.0, total_now_mt - target_ndc_mt)
    else:
        target_ndc_mt  = np.nan
        current_gap_mt = 0.0

    owid_scope_warning = False
    coverage_pct       = np.nan
    residual_gap_ndc   = np.nan

    if ndc_type == "absolute" and pd.notna(target_ndc_mt):
        if current_gap_mt > 1.0:
            coverage_pct     = min(100.0, net_avoided / current_gap_mt * 100.0)
            residual_gap_ndc = max(0.0, (total_now_mt - net_avoided) - target_ndc_mt)
        else:
            owid_scope_warning = True
            coverage_pct       = np.nan  
            residual_gap_ndc   = np.nan
            logger.warning(
                f"[{country_code}] NDC gap appears zero (total_now_mt={total_now_mt:.1f} <= target={target_ndc_mt:.1f}). "
                f"OWID API data is fossils-only (missing large LULUCF emissions). National gap undetermined."
            )

    total_after = max(0.0, total_now_mt - net_avoided)

    emission_balance = {
        "current_total_mt":          total_now_mt,
        "fossil_thermal_mt":         fossil_thermal_mt,
        "other_sectors_mt":          max(0.0, total_now_mt - fossil_thermal_mt),
        "co2_avoided_thermal_mt":    co2_avoided,
        "renew_lifecycle_mt":        renew_lifecycle,
        "net_avoided_mt":            net_avoided,
        "residual_thermal_mt":       residual_thermal,
        "total_after_mt":            total_after,
        "target_ndc_mt":             target_ndc_mt,
        "residual_gap_mt":           residual_gap_ndc,
    }
    
    # Adicionando um info coeso com o summary final
    logger.info(
        f"[{country_code}] NDC Profile: Target Year {nz_year} | Type: {ndc_type} | Current Nat. Emissions: {total_now_mt:.1f} Mt"
    )

    return {
        "db":                         db,
        "base_year":                  base_year,
        "base_mt":                    base_mt,
        "ndc_pct":                    ndc_pct,
        "ndc_intensity_pct":          ndc_intensity_pct,
        "ndc_type":                   ndc_type,
        "ndc_horizon_year":           ndc_horizon,
        "target_2030_mt":             target_ndc_mt,
        "target_ndc_mt":              target_ndc_mt,
        "current_gap_mt":             current_gap_mt,
        "coverage_pct":               coverage_pct,
        "residual_gap_mt":            residual_gap_ndc,
        "owid_scope_warning":         owid_scope_warning,
        "fossil_thermal_mt":          fossil_thermal_mt,
        "elec_coverage_pct":          elec_coverage_pct,
        "elec_sector_reduction":      elec_sector_reduction,
        "national_contribution_pct":  national_contribution_pct,
        "current_total_mt":           total_now_mt,
        "co2_avoided_mt":             co2_avoided,
        "renew_lifecycle_mt":         renew_lifecycle,
        "net_avoided_mt":             net_avoided,
        "total_after_mt":             total_after,
        "residual_thermal_mt":        residual_thermal,
        "net_zero_year":              nz_year,
        "annual_reduction_needed":    (total_now_mt / max(1, nz_year - datetime.now().year)) if total_now_mt > 0 else 0.0,
        "emission_balance":           emission_balance,
        "source":                     db.get("source", "parameters.json / fallback"),
    }

# ─────────────────────────────────────────────────────────────────────────────
# MAIN CLASS
# ─────────────────────────────────────────────────────────────────────────────

class GHGAbatementCalculator:
    """Processor to calculate GHG abatement for local power market transitions."""

    def __init__(self, cfg: ConfigLoader, outputs_dir: Path):
        self.cfg = cfg
        self.outputs_dir = Path(outputs_dir)
        self.styler = GeoWorldStyler(cfg.system.get("visualization", {}))
        self._session = requests.Session()
        self._http_timeout = 20

    def run(
        self,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        potential_dir: Path,
        lcoe_dir: Path,
        plants_df: pd.DataFrame,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        admin_gdf: Optional[gpd.GeoDataFrame]   = None,
        **kwargs,
    ) -> Dict[str, Any]:

        t0 = datetime.now()
        ts_report = t0.strftime("%Y-%m-%d %H:%M:%S")
    
        out_base = self.outputs_dir / country_code / "abatement"
        
        logger.info("=" * 62)
        logger.info(f"  GHG ABATEMENT CALCULATOR — {country_name} ({country_code})")
        logger.info(f"  Saída: {out_base}")
        logger.info("=" * 62)
        
        for d in ["maps", "reports", "data"]:
            (out_base / d).mkdir(parents=True, exist_ok=True)

        params = self._load_params(country_code)
        
        fleet_df = calc_thermal_fleet(
            plants_df,
            thermal_params=params["thermal_params"],
            cap_override=params["cap_override"],
        )

        renew_gwh, renew_lcoe, zonal_dfs = self._load_renewable_data(
            potential_dir, lcoe_dir, country_code, params["renewable_cf"]
        )
        
        db_entry = self._fetch_net_zero_data(country_code)
        
        result = calc_macc(
            fleet_df, renew_gwh, renew_lcoe,
            carbon_price=params["carbon_price"],
            penetration=params["penetration"],
        )

        ci_result = calc_carbon_intensity(
            fleet_df, renew_gwh, result,
            renew_lifecycle_ef=params["renew_lifecycle_ef"],
        )
        result["ci_after_g_kwh"] = ci_result["ci_after_g_kwh"]

        footprint = calc_carbon_footprint(
            fleet_df, renew_gwh, result, db_entry.get("total_co2_mt_2022", 0.0),
            ci_after_g_kwh=ci_result["ci_after_g_kwh"],
            renew_lifecycle_ef=params["renew_lifecycle_ef"],
        )

        nz_result = calc_net_zero(country_code, result, ci_result, footprint, renew_gwh, db_entry)
        
        if _ABATEMENT_PLOTS_AVAILABLE:
            plot_geography(self.styler, result, plants_df, zonal_dfs, mainland_gdf,
                           context_gdf, admin_gdf, country_name,
                           out_base/"maps"/f"{country_code}_abatement_maps.png",
                           params["thermal_params"], params["renewable_cf"])
            plot_macc_curve(self.styler, result, country_name, out_base/"maps"/f"{country_code}_macc_curve.png")
            plot_substitution(self.styler, result, renew_gwh, country_name,
                              out_base/"maps"/f"{country_code}_substitution_curves.png", params["renewable_cf"])
            plot_carbon_intensity(self.styler, ci_result, renew_gwh, result, country_name,
                                  out_base/"maps"/f"{country_code}_carbon_intensity.png",
                                  params["thermal_params"], params["renew_lifecycle_ef"])
            plot_net_zero(self.styler, nz_result, ci_result, footprint, result, country_name,
                          out_base/"maps"/f"{country_code}_net_zero.png")
        
        # Build structured synthesis report
        report = self._format_report(
            result, ci_result, footprint, nz_result,
            fleet_df, renew_gwh, renew_lcoe, country_name, country_code,
            params["renewable_cf"], params, ts_report
        )
        
        report_file = out_base / "reports" / f"{country_code}_abatement_{t0.strftime('%Y%m%d_%H%M%S')}.txt"
        report_file.write_text(report, encoding="utf-8")

        # Concise summary logging
        flag = "self-financing" if result["mac_global"] <= 0 else f"needs {result['mac_global']:.1f} USD/tCO2"
        ndc_coverage = f"{nz_result['coverage_pct']:.1f}%" if pd.notna(nz_result["coverage_pct"]) else "N/D"
        logger.info(
            f"[{country_code}] MAC {result['mac_global']:.1f} USD/tCO₂e ({flag}) | "
            f"Net Avoided: {nz_result['net_avoided_mt']:.2f} MtCO₂/yr | Value: {result['total_value_b']:.2f} B USD | "
            f"Substituted: {result['subst_gwh'] / 1000:.1f} TWh | NDC coverage: {ndc_coverage}"
        )

        elapsed = round((datetime.now() - t0).total_seconds(), 1)
        logger.info(f"[{country_code}] Phase 7 completed in {elapsed}s.")
        
        return {
            "country":                   country_code,
            "mac_usd_tco2e":             result["mac_global"],
            "co2_avoided_mt":            result["co2_avoided_mt"],
            "total_value_b":             result["total_value_b"],
            "ci_before":                 ci_result["ci_before_g_kwh"],
            "ci_after":                  ci_result["ci_after_g_kwh"],
            "elec_coverage_pct":         nz_result["elec_coverage_pct"],
            "national_contribution_pct": nz_result["national_contribution_pct"],
            "ndc_coverage_pct":          nz_result["coverage_pct"] if pd.notna(nz_result["coverage_pct"]) else None,
            "owid_scope_warning":        nz_result.get("owid_scope_warning", False),
            "available":                 True,
            "elapsed":                   elapsed,
        }

   # ─── Loaders ─────────────────────────────────────────────────────────────

    def _load_params(self, code: str) -> Dict[str, Any]:
        params = getattr(self.cfg, "_params", {}) or {}
        meta_default = params.get("_meta", {}).get("abatement", {}).get("default", {})
        country_cfg = params.get("countries", {}).get(code, {})
        abat_cfg = country_cfg.get("abatement", {})
        fallback_logic = params.get("fallback_logic", {})

        wb_meta = self._fetch_country_metadata(code)
        region = self._normalize_region(country_cfg.get("region") or wb_meta.get("region"))
        income_level = country_cfg.get("income_level") or wb_meta.get("income_level")
        grid_mix = country_cfg.get("grid_mix_dominant", "Mixed")

        region_defaults = fallback_logic.get("region_defaults", {}).get(region, {})
        income_defaults = fallback_logic.get("income_level_defaults", {}).get(income_level, {})
        grid_defaults = fallback_logic.get("grid_mix_defaults", {}).get(grid_mix, {})
        global_defaults = fallback_logic.get("global_average", {})

        def _get_val(param_name, sources, ultimate_fallback):
            for dict_ref, source_name in sources:
                if param_name in dict_ref:
                    return float(dict_ref[param_name])
            return float(ultimate_fallback)

        carbon_price = _get_val(
            "carbon_price_usd_tco2e",
            [(abat_cfg, "country_cfg"), (region_defaults, "region"), (global_defaults, "global"), (meta_default, "meta")],
            80.0
        )

        penetration = min(max(_get_val(
            "penetration_factor",
            [(abat_cfg, "country_cfg"), (grid_defaults, "grid"), (region_defaults, "region"), 
             (income_defaults, "income"), (global_defaults, "global"), (meta_default, "meta")],
            0.80
        ), 0.0), 1.0)

        thermal_params: Dict[str, Dict[str, float]] = {}
        for fuel in meta_default.get("thermal_types", ["coal", "gas", "oil"]):
            fb = GLOBAL_THERMAL_FALLBACK.get(fuel, {"ef": 500.0, "cf": 0.45, "fuel_mc": 50.0})
            ef_val = float(meta_default.get("thermal_emission_factors_tco2e_gwh", {}).get(fuel, fb["ef"]))
            cf_val = float(abat_cfg.get("thermal_cf", {}).get(fuel, region_defaults.get("thermal_cf", {}).get(fuel, meta_default.get("thermal_cf", {}).get(fuel, fb["cf"]))))
            mc_val = float(abat_cfg.get("thermal_srmc", {}).get(fuel, region_defaults.get("thermal_srmc", {}).get(fuel, meta_default.get("thermal_marginal_cost", {}).get(fuel, fb["fuel_mc"]))))
            thermal_params[fuel] = {"ef": ef_val, "cf": cf_val, "fuel_mc": mc_val}

        cap_override = {k: float(v) for k, v in abat_cfg.get("thermal_capacity_mw", {}).items() if k in thermal_params}

        renewable_cf, renew_lifecycle_ef = {}, {}
        for tech in TECH_ORDER:
            tech_cfg = country_cfg.get(tech, {})
            renewable_cf[tech] = float(tech_cfg.get("capacity_factor", region_defaults.get(f"renew_capacity_factor_{tech}", GLOBAL_RENEWABLE_CF_FALLBACK.get(tech, 0.25))))
            renew_lifecycle_ef[tech] = float(abat_cfg.get("renewable_lifecycle_ef", {}).get(tech, region_defaults.get("renewable_lifecycle_ef", {}).get(tech, meta_default.get("renewable_lifecycle_gco2_kwh", {}).get(tech, GLOBAL_RENEW_LIFECYCLE_FALLBACK.get(tech, 50.0)))))

        logger.debug(f"Resolved abatement config params for {code}. CFs: {renewable_cf}, Penetration: {penetration}")
        return {
            "carbon_price": carbon_price, "cap_override": cap_override,
            "penetration": penetration, "thermal_params": thermal_params,
            "renewable_cf": renewable_cf, "renew_lifecycle_ef": renew_lifecycle_ef,
            "region": region, "income_level": income_level, "grid_mix_dominant": grid_mix,
        }
        
    def _normalize_region(self, region: Optional[str]) -> Optional[str]:
        if not region: return None
        r = str(region).strip().lower()
        if "europe" in r: return "Europe"
        if "north america" in r: return "North America"
        if "latin america" in r or "south america" in r: return "South America"
        if "africa" in r: return "Africa"
        if "middle east" in r: return "Middle East"
        if "asia" in r or "pacific" in r or "oceania" in r: return "Asia Pacific"
        return region

    def _fetch_country_metadata(self, code: str) -> Dict[str, Optional[str]]:
        url = f"https://api.worldbank.org/v2/country/{code}?format=json"
        try:
            resp = self._session.get(url, timeout=self._http_timeout)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, list) and len(payload) > 1 and payload[1]:
                row = payload[1][0]
                return {"region": row.get("region", {}).get("value"), "income_level": row.get("incomeLevel", {}).get("value")}
        except Exception as e:
            logger.debug(f"[{code}] World Bank country metadata fetch failed: {e}")
        return {"region": None, "income_level": None}

    def _fetch_world_bank_total_co2_mt(self, code: str) -> Tuple[Optional[float], Optional[int]]:
        url = f"https://api.worldbank.org/v2/country/{code}/indicator/EN.ATM.CO2E.KT?format=json&mrv=5"
        try:
            resp = self._session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=self._http_timeout)
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, list) and len(payload) > 1 and payload[1]:
                for row in payload[1]:
                    if row.get("value") is not None:
                        return float(row["value"]) / 1000.0, int(row.get("date", 0))
        except Exception as e:
            logger.debug(f"[{code}] World Bank CO2 fetch failed: {e}")
        return None, None

    def _fetch_owid_total_co2_mt(self, code: str) -> Tuple[Optional[float], Optional[int]]:
        from io import StringIO
        if not hasattr(self, "_owid_df_cache"):
            self._owid_df_cache = None

        if self._owid_df_cache is None:
            url = "https://raw.githubusercontent.com/owid/co2-data/master/owid-co2-data.csv"
            try:
                resp = self._session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
                resp.raise_for_status()
                self._owid_df_cache = pd.read_csv(
                    StringIO(resp.text), usecols=["iso_code", "year", "co2"],
                    dtype={"iso_code": str, "year": int, "co2": float},
                )
            except Exception as e:
                logger.debug(f"[{code}] OWID dataset download failed: {e}")
                return None, None

        try:
            country_df = self._owid_df_cache[self._owid_df_cache["iso_code"] == code.upper()].dropna(subset=["co2"])
            if not country_df.empty:
                latest = country_df.sort_values("year", ascending=False).iloc[0]
                return float(latest["co2"]), int(latest["year"])
        except Exception as e:
            logger.debug(f"[{code}] OWID dataframe cache lookup failed: {e}")
        return None, None

    def _build_net_zero_fallback(self, code: str) -> Dict[str, Any]:
        """Provides baseline structured data for NDC & net-zero targets."""
        db_raw = self._load_net_zero_db()
        db = {k: v for k, v in db_raw.get(code, {}).items() if v is not None}
        nz_cfg = getattr(self.cfg, "_params", {}).get("countries", {}).get(code, {}).get("net_zero_target", {})

        for src_key, dst_key in [("base_year", "base_year"), ("base_co2_mt", "base_co2_mt"),
                                 ("ndc_2030_reduction_pct", "ndc_2030_pct"),
                                 ("ndc_2030_intensity_reduction_pct", "ndc_2030_intensity_pct"),
                                 ("net_zero_year", "net_zero_year"), ("source", "source")]:
            if nz_cfg.get(src_key) is not None:
                db[dst_key] = nz_cfg[src_key]

        db.setdefault("country_code", code)
        db.setdefault("total_co2_mt_2022", 0.0)
        db.setdefault("net_zero_year", 2050)
        db.setdefault("source", "net_zero_db.json / fallback")
        return db

    def _load_net_zero_db(self) -> Dict[str, Any]:
        if hasattr(self, "_nz_db_cache"): return self._nz_db_cache
        
        db_path = Path.cwd() / "configs" / "net_zero_db.json"
        if not db_path.exists():
            found = list(Path.cwd().rglob("net_zero_db.json"))
            if found: db_path = found[0]

        if db_path.exists():
            try:
                import json
                with open(db_path, "r", encoding="utf-8") as f:
                    self._nz_db_cache = json.load(f).get("countries", {})
                logger.info(f"Net Zero DB carregado com sucesso — {db_path.name}")
                return self._nz_db_cache
            except Exception as e:
                logger.error(f"Failed to load local net_zero_db.json: {e}")
        
        self._nz_db_cache = {}
        return self._nz_db_cache

    def _fetch_net_zero_data(self, code: str) -> Dict[str, Any]:
        db = self._build_net_zero_fallback(code)
        
        wb_total, wb_year = self._fetch_world_bank_total_co2_mt(code)
        if wb_total is not None:
            db["total_co2_mt_2022"] = wb_total
            db["source"] = f"{db.get('source', '')}; World Bank CO2 ({wb_year})".strip("; ")
            logger.info(f"[{code}] Net Zero: Emissões atuais carregadas via World Bank (API) → {wb_total:.1f} Mt (ano {wb_year})")
        else:
            owid_total, owid_year = self._fetch_owid_total_co2_mt(code)
            if owid_total is not None:
                db["total_co2_mt_2022"] = owid_total
                db["source"] = f"{db.get('source', '')}; OWID/GCP CO2 ({owid_year})".strip("; ")
                logger.info(f"[{code}] Net Zero/World Bank sem dados. Emissões base carregadas via OWID/GCP → {owid_total:.1f} Mt (ano {owid_year})")

        return db

    def _load_renewable_data(
        self, potential_dir: Path, lcoe_dir: Path, code: str, renewable_cf: Dict[str, float],
    ) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, pd.DataFrame]]:
        renew_gwh, renew_lcoe, zonal_dfs = {}, {}, {}

        for tech in TECH_ORDER:
            p = potential_dir / "data" / f"{code}_{tech}_balanced_zonal.csv"
            if p.exists():
                try:
                    df = pd.read_csv(p)
                    zonal_dfs[tech] = df
                    if "generation_twh" in df.columns:     renew_gwh[tech] = float(df["generation_twh"].sum() * 1000.0)
                    elif "generation_gwh" in df.columns:   renew_gwh[tech] = float(df["generation_gwh"].sum())
                    elif "capacity_mw_sum" in df.columns:  renew_gwh[tech] = float(df["capacity_mw_sum"].sum() * renewable_cf.get(tech, 0.25) * 8760.0 / 1000.0)
                    else: renew_gwh[tech] = 0.0
                except Exception as e:
                    logger.debug(f"[{code}-{tech}] Error processing Phase 4 potential data: {e}")
                    renew_gwh[tech] = 0.0
            else:
                renew_gwh[tech] = 0.0

            lcoe_path = lcoe_dir / "data" / f"{code}_{tech}_lcoe_zonal.csv"
            avg_lcoe = 55.0  # Safe strict default fallback
            if lcoe_path.exists():
                try:
                    df_lcoe = pd.read_csv(lcoe_path)
                    lcoe_col = next((c for c in ["lcoe_mean", "lcoe_usd_mwh", "mean_lcoe", "lcoe"] if c in df_lcoe.columns), None)
                    weight_col = next((c for c in ["pixel_count", "count", "n_pixels", "area_km2"] if c in df_lcoe.columns), None)
                    
                    if lcoe_col:
                        valid = df_lcoe[df_lcoe[lcoe_col] > 0]
                        if not valid.empty:
                            avg_lcoe = float((valid[lcoe_col] * valid[weight_col]).sum() / valid[weight_col].sum() if weight_col else valid[lcoe_col].mean())
                except Exception as e:
                    logger.warning(f"[{code}-{tech}] Phase 5 LCOE parsing failure, reverting to fallback: {e}")
            
            renew_lcoe[tech] = avg_lcoe
        
        return renew_gwh, renew_lcoe, zonal_dfs

    # ─── Report formatting ──────────────────────────────────────────────────

    def _format_report(
        self, result, ci_result, footprint, nz,
        fleet_df, renew_gwh, renew_lcoe, country_name, code,
        renewable_cf, params, ts_report
    ) -> str:
        
        total_r = max(sum(renew_gwh.values()), 1)
        
        # Generator for fleet string blocks
        fleet_rows = "\n".join(
            f"  {r['fuel'].capitalize():<6} {r['capacity_mw']:>8,.0f} {r['cf']:>5.0%} "
            f"{r['gen_gwh']:>12,.0f} {r['ef']:>13,.0f} {r['fuel_mc']:>11.1f} {r['co2_mt']:>14.3f}"
            for _, r in fleet_df.iterrows()
        )
        
        renew_rows = "\n".join(
            f"  {TECH_LABELS.get(t['tech'], t['tech']):<14} {t['generation_gwh']:>12,.0f} "
            f"{t['generation_gwh'] / (renewable_cf.get(t['tech'], 0.25) * 8760):>6.1f} "
            f"{t['generation_gwh'] / total_r * 100:>8.1f} {t['lcoe_usd_mwh']:>12.1f} {t['mac_usd_tco2e']:>12.1f}"
            f"{' ✓ vs LRMC' if t['competitive_lrmc'] else (' ✓ vs SRMC' if t['competitive'] else '')}"
            for t in result["by_tech"]
        )

        subst_rows = "\n".join(
            f"    {r['fuel'].capitalize():<6} : {r['subst_gwh']:>8,.0f} GWh substituted  | {r['co2_avoided']:.3f} MtCO₂e avoided"
            for _, r in fleet_df.iterrows()
        ) if not fleet_df.empty and "subst_gwh" in fleet_df.columns else "    N/A"

        financing_status = "    → SELF-FINANCING (renewables already cheaper than thermal SRMC)" \
                           if result["mac_global"] <= 0 else \
                           f"    → EU ETS proxy at {result['carbon_price']:.0f} USD/tCO₂ {'COVERS' if result['carbon_price']>=result['mac_global'] else 'below'} breakeven"

        ndc_block_text = f"""
  NDC type                               : absolute reduction target
  NDC base year / base emissions         : {nz['base_year']} / {float(nz.get('base_mt') or 0):.1f} MtCO₂e/yr
  NDC 2030 commitment                    : −{float(nz.get('ndc_pct') or 0):.0f}% vs {nz['base_year']} baseline
  NDC 2030 target (absolute cap)         : <= {nz.get('target_2030_mt', 0):.1f} MtCO₂e/yr (all sectors)
  Current total vs target gap            : {nz.get('current_gap_mt', 0):.1f} MtCO₂e/yr to close by 2030
  Electricity transition contribution    : {nz['net_avoided_mt']:.2f} MtCO₂e/yr net avoided
    → covers {nz.get('coverage_pct', 0):.1f}% of the 2030 gap
  Residual gap after this transition     : {nz.get('residual_gap_mt', 0):.1f} MtCO₂e/yr (to be closed by other sectors)
  Source: {nz.get('source', '')}
""" if nz.get("ndc_type") == "absolute" else f"""
  NDC 2030 commitment type               : intensity-based / unavailable absolute target
  Reported intensity reduction target    : {float(nz.get('ndc_intensity_pct') or 0):.0f}%
  Net avoided by this transition         : {nz['net_avoided_mt']:.2f} MtCO₂e/yr
  Source: {nz.get('source', '')}
"""

        report_txt = f"""\
========================================================================
GHG ABATEMENT SYNTHESIS REPORT — Phase 7
{country_name} ({code})
Generated: {ts_report}
========================================================================

{'='*78}
EXECUTIVE SUMMARY
{'='*78}
MAC (Marginal Abatement Cost)      : {result['mac_global']:.1f} USD/tCO₂e
Breakeven Carbon Price (BCP)       : {result['bcp_global']:.1f} USD/tCO₂e
CO₂ Avoided (annual)               : {result['co2_avoided_mt']:.2f} MtCO₂e/yr
Total Economic Value               : {result['total_value_b']:.2f} Billion USD/yr
Grid Substitution Volume           : {result['subst_gwh']/1000:.1f} TWh ({result['subst_pct']:.1f}%)
Carbon Intensity Reduction         : {ci_result['ci_before_g_kwh']:.0f} → {ci_result['ci_after_g_kwh']:.0f} gCO₂/kWh
{'='*78}

SECTION 1 — THERMAL FLEET (BEFORE TRANSITION)
------------------------------------------------------------
  Fuel     Cap MW    CF   Gen GWh/yr   EF tCO₂/GWh  SRMC $/MWh   CO₂ MtCO₂/yr
  ------------------------------------------------------------
{fleet_rows}
  TOTAL  {fleet_df['capacity_mw'].sum():>8,.0f}       {fleet_df['gen_gwh'].sum():>12,.0f}                               {fleet_df['co2_mt'].sum():>14.3f}

SECTION 2 — RENEWABLE POTENTIAL (Balanced scenario, Phase 4+5)
------------------------------------------------------------
  Tech           Gen GWh/yr     GW  Share %   LCOE $/MWh   MAC $/tCO₂
  ------------------------------------------------------------
{renew_rows}

SECTION 3 — MAC / SUBSTITUTION RESULTS
------------------------------------------------------------
  Carbon proxy applied                   : {result['carbon_price']:.0f} USD/tCO₂e
  SRMC thermal (gen-weighted)            : {result['srmc_avg']:.1f} USD/MWh
  LRMC thermal (with carbon)             : {result['lrmc_avg']:.1f} USD/MWh
  Avg renewable LCOE                     : {result['lcoe_avg_renew']:.1f} USD/MWh
  ► MAC (global, gen-weighted)           : {result['mac_global']:.2f} USD/tCO₂e
  ► BCP (breakeven carbon price)         : {result['bcp_global']:.2f} USD/tCO₂e
{financing_status}
  Substitution (limit applied)           : {result['subst_gwh']:,.0f} GWh/yr ({result['subst_pct']:.1f}% of thermal)
{subst_rows}
  CO₂ avoided                            : {result['co2_avoided_mt']:.3f} MtCO₂e/yr
  TOTAL economic value created           : {result['total_value_b']:.3f} Billion USD/yr

SECTION 4 — CARBON INTENSITY (gCO₂eq/kWh)
------------------------------------------------------------
  Before transition (thermal mix)        : {ci_result['ci_before_g_kwh']:.1f} gCO₂/kWh
  After transition (post-subst mix)      : {ci_result['ci_after_g_kwh']:.1f} gCO₂/kWh
  Reduction                              : {ci_result['ci_reduction_pct']:.1f}%
  Renewables avg lifecycle               : {ci_result['ci_renew_avg_g_kwh']:.1f} gCO₂/kWh

SECTION 5 — CARBON FOOTPRINT BY SCOPE (GHG Protocol)
------------------------------------------------------------
  Scope 1 (direct thermal, after subst.) : {footprint['scope1_after_mt']:.3f} MtCO₂e/yr
  Scope 2 (indirect — new renew/grid)    : {footprint['scope2_after_mt']:.3f} MtCO₂e/yr
  Scope 3 (renew lifecycle: upstreams)   : {footprint['scope3_renew_mt']:.3f} MtCO₂e/yr
  Total Scope 1+2+3 (post-transition)    : {footprint['total_after_mt']:.3f} MtCO₂e/yr

SECTION 6 — NET ZERO ANALYSIS (Referencing WHOLE national economy)
------------------------------------------------------------
  Country net-zero target year           : {nz['net_zero_year']}
  Current total CO₂ (all sectors, API)   : {nz['current_total_mt']:.1f} MtCO₂e/yr
{ndc_block_text}
SECTION 7 — EMISSION BALANCE DECOMPOSITION
------------------------------------------------------------
  Current national footprint (all sectors): +{nz['current_total_mt']:.2f} MtCO₂e/yr
    of which modelled electricity sector  :  {result['total_thermal_co2']:.2f} MtCO₂e/yr
  − CO₂ avoided (thermal displacement)    : −{result['co2_avoided_mt']:.3f} MtCO₂e/yr
  + Lifecycle CO₂ (new renewables built)  : +{nz['renew_lifecycle_mt']:.3f} MtCO₂e/yr
  + Residual thermal CO₂ kept for grid    : +{ci_result['residual_thermal_co2_mt']:.3f} MtCO₂e/yr
  = Post-transition national proxy        :  {nz['total_after_mt']:.2f} MtCO₂e/yr
========================================================================
"""
        return report_txt