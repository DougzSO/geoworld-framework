"""
src/processors/transport_decarbonization_calculator.py
=======================================================
Phase 9 — Transport Decarbonisation & Clean Electrification Pathways.

Generates annual projections (2025–2050) for:
  - Fleet size and composition by vehicle category AND powertrain
    (ICE, HEV, PHEV, BEV, FCEV)
  - EV/PHEV electricity demand (TWh/yr) by scenario
  - Required renewable energy capacity additions (GW solar/wind/biomass)
  - GHG emission trajectory vs. combustion baseline (MtCO2eq/yr)
  - Levelised cost of transport electrification (USD/km, USD/tCO2 avoided)
  - Charging hub placement via spatial clustering on the suitability rasters
    produced by Phases 3–5.

Powertrain Model
----------------
  ICE  — Internal Combustion Engine (gasoline, diesel, flex, ethanol, biodiesel)
  HEV  — Full Hybrid (self-charging; reduces fuel consumption, no plug)
  PHEV — Plug-in Hybrid (partial electric range; contributes to grid demand)
  BEV  — Battery Electric Vehicle (fully electric; main grid-demand driver)
  FCEV — Fuel Cell Electric Vehicle (hydrogen; grid demand via electrolysis)

Outputs
-------
  outputs/<CODE>/transport/
    data/
      <CODE>_transport_annual_timeseries.csv
      <CODE>_transport_fleet_composition.csv
      <CODE>_transport_hub_locations.csv
      <CODE>_transport_summary_2050.json
    reports/
      <CODE>_transport_decarbonization_report.txt
    maps/
      <CODE>_transport_hubs_2050.png
      <CODE>_transport_renewable_need.png
      <CODE>_transport_emissions_trajectory.png
      <CODE>_transport_fleet_transition.png

References
----------
.. [IEA-EV2024]      IEA (2024). Global EV Outlook 2024.
.. [IPCC2022]         IPCC AR6 WG3 (2022). Chapter 10 — Transport.
.. [IRENA2024t]       IRENA (2024). Electrification with Renewables.
.. [BloombergNEF2024] BNEF Electric Vehicle Outlook 2024.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

try:
    import rasterio
    _HAS_RASTERIO = True
except ImportError:
    _HAS_RASTERIO = False

logger = logging.getLogger("geoworld.processors.TransportDecarbonizationCalculator")

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_POWERTRAIN_TYPES = ("ice", "hev", "phev", "bev", "fcev")

# Fraction of PHEV km driven electrically (utility factor)
_PHEV_ELECTRIC_FRACTION = 0.45

_POWERTRAIN_COLORS = {
    "ice":  "#C62828",
    "hev":  "#F9A825",
    "phev": "#FDD835",
    "bev":  "#1565C0",
    "fcev": "#00ACC1",
}

_SCENARIO_COLORS = {
    "reference":    "#1565C0",
    "accelerated":  "#1B5E20",
    "conservative": "#B71C1C",
}

_TECH_RE_COLORS = {
    "solar":   "#F9A825",
    "wind":    "#1565C0",
    "biomass": "#6A1B9A",
}

_LIGHT_VEHICLE_CATEGORIES = {"passenger_car"}

_HUB_SUITABILITY_THRESHOLD = 0.90

_PT_EF_KEY: Dict[str, str] = {
    "ice":  "ice_gasoline",
    "hev":  "hybrid_hev",
    "phev": "hybrid_phev",
    "bev":  "bev",
    "fcev": "fcev",
}

_PT_ENERGY_KEY: Dict[str, str] = {
    "ice":  "ice_gasoline",
    "hev":  "hybrid_hev",
    "phev": "hybrid_phev",
    "bev":  "bev",
    "fcev": "fcev",
}


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _safe_anchors(raw: Dict) -> Dict[int, float]:
    """Extract {year: value} from a dict, skipping non-numeric keys."""
    out = {}
    for k, v in raw.items():
        try:
            out[int(k)] = float(v)
        except (ValueError, TypeError):
            pass
    return out


def _interpolate_trajectory(anchors: Dict[int, float], years: List[int]) -> Dict[int, float]:
    """Linear interpolation between anchor year→value pairs."""
    sorted_anchors = sorted(anchors.items())
    result = {}
    for y in years:
        if y <= sorted_anchors[0][0]:
            result[y] = sorted_anchors[0][1]
        elif y >= sorted_anchors[-1][0]:
            result[y] = sorted_anchors[-1][1]
        else:
            for i in range(len(sorted_anchors) - 1):
                y0, v0 = sorted_anchors[i]
                y1, v1 = sorted_anchors[i + 1]
                if y0 <= y <= y1:
                    t = (y - y0) / (y1 - y0)
                    result[y] = v0 + t * (v1 - v0)
                    break
    return result


def _interpolate_powertrain_shares(
    scenario_anchors: Dict[str, Dict],
    years: List[int],
) -> Dict[int, Dict[str, float]]:
    """
    Interpolate per-powertrain share trajectories from anchor year dicts.

    Parameters
    ----------
    scenario_anchors : {str(year): {pt: share}}
    years            : target year list

    Returns
    -------
    {year: {pt: share}}  — shares normalised to sum=1.0 per year.
    """
    int_anchors: Dict[int, Dict[str, float]] = {}
    for k, v in scenario_anchors.items():
        try:
            yr = int(k)
        except (ValueError, TypeError):
            continue
        if isinstance(v, dict):
            int_anchors[yr] = {pt: float(v.get(pt, 0.0)) for pt in _POWERTRAIN_TYPES}

    if not int_anchors:
        return {y: {"ice": 1.0, "hev": 0.0, "phev": 0.0, "bev": 0.0, "fcev": 0.0}
                for y in years}

    sorted_years = sorted(int_anchors.keys())

    result: Dict[int, Dict[str, float]] = {}
    for y in years:
        if y <= sorted_years[0]:
            shares = dict(int_anchors[sorted_years[0]])
        elif y >= sorted_years[-1]:
            shares = dict(int_anchors[sorted_years[-1]])
        else:
            shares = {}
            for i in range(len(sorted_years) - 1):
                y0, y1 = sorted_years[i], sorted_years[i + 1]
                if y0 <= y <= y1:
                    t = (y - y0) / (y1 - y0)
                    for pt in _POWERTRAIN_TYPES:
                        v0 = int_anchors[y0].get(pt, 0.0)
                        v1 = int_anchors[y1].get(pt, 0.0)
                        shares[pt] = v0 + t * (v1 - v0)
                    break

        total = sum(shares.values())
        if total > 0:
            result[y] = {pt: shares[pt] / total for pt in _POWERTRAIN_TYPES}
        else:
            result[y] = {"ice": 1.0, "hev": 0.0, "phev": 0.0, "bev": 0.0, "fcev": 0.0}

    return result


# ═══════════════════════════════════════════════════════════════════════════
# TransportDecarbonizationCalculator
# ═══════════════════════════════════════════════════════════════════════════

class TransportDecarbonizationCalculator:
    """
    Phase 9: Annual transport fleet electrification modelling from 2025 to 2050.

    Powertrain model: ICE · HEV · PHEV · BEV · FCEV

    Parameters
    ----------
    cfg                  : GeoWorld ConfigLoader.
    outputs_dir          : Root output directory.
    transport_params_path: Explicit path to transport_parameters.json (optional).
    """

    def __init__(
    self,
    cfg: Any,
    outputs_dir: Path,
    transport_params_path: Optional[Path] = None,
    ):
        self.cfg = cfg
        self.outputs_dir = Path(outputs_dir)

        if transport_params_path is not None:
            tp_path = Path(transport_params_path)
        else:
            candidates = [
                Path(__file__).parent.parent.parent / "configs" / "transport_parameters.json",
                Path(__file__).parent.parent.parent / "transport_parameters.json",
                Path(__file__).parent.parent / "configs" / "transport_parameters.json",
                Path(__file__).parent.parent / "transport_parameters.json",
                Path("configs") / "transport_parameters.json",
                Path("transport_parameters.json"),
            ]
            tp_path = next((p for p in candidates if p.exists()), None)

        if tp_path is None or not tp_path.exists():
            raise FileNotFoundError(
                "transport_parameters.json not found. "
                "Pass transport_params_path= explicitly or place it in configs/."
            )

        with open(tp_path, "r", encoding="utf-8") as fh:
            self._tp: Dict[str, Any] = json.load(fh)

        logger.info("Loaded transport parameters from: %s", tp_path)

        # ── Initialize styler for map generation ──────────────────────────────
        from src.utils.map_styling import GeoWorldStyler
        viz_cfg = cfg.system.get("visualization", {})
        pipeline_dpi = cfg.system.get("pipeline", {}).get("map_dpi_export", 150)
        self.styler = GeoWorldStyler(viz_cfg, global_dpi=pipeline_dpi)
        self._admin_gdf: Optional[gpd.GeoDataFrame] = None

    # ─────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────

    def run(
        self,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        pot_results:    Optional[Dict[str, Any]] = None,
        lcoe_results:   Optional[Dict[str, Any]] = None,
        suitability_dir: Optional[Path] = None,
        context_gdf:    Optional[gpd.GeoDataFrame] = None,
    ) -> Dict[str, Any]:
        t_start = time.perf_counter()
        code    = country_code

        # ── Load admin boundaries for maps ─────────────────────────────────────
        try:
            self._admin_gdf = self.styler.load_admin_boundaries(
                country_name, mainland_gdf, Path(self.cfg.raw_path)
            )
        except Exception as exc:
            logger.warning("Could not load admin boundaries: %s", exc)
            self._admin_gdf = None

        out_base = self.outputs_dir / code / "transport"
        out_data = out_base / "data"
        out_rep  = out_base / "reports"
        out_maps = out_base / "maps"
        for d in (out_data, out_rep, out_maps):
            d.mkdir(parents=True, exist_ok=True)

        defaults   = self._tp["global_defaults"]
        country_tp = self._tp.get("countries", {}).get(code, {})

        out_base = self.outputs_dir / code / "transport"
        out_data = out_base / "data"
        out_rep  = out_base / "reports"
        out_maps = out_base / "maps"
        for d in (out_data, out_rep, out_maps):
            d.mkdir(parents=True, exist_ok=True)

        defaults   = self._tp["global_defaults"]
        country_tp = self._tp.get("countries", {}).get(code, {})

        start_year = defaults["projection_horizon"]["start_year"]
        end_year   = defaults["projection_horizon"]["end_year"]
        years      = list(range(start_year, end_year + 1))

        self._log_parameter_dashboard(code, country_name, country_tp, defaults, years)

        logger.info("  [1/7] Building fleet size trajectory ...")
        fleet_df = self._build_fleet_trajectory(code, country_tp, defaults, years)

        logger.info("  [2/7] Calculating EV electricity demand & RE requirements ...")
        ts_df = self._build_timeseries(code, country_tp, defaults, fleet_df, years, pot_results)

        logger.info("  [3/7] Computing GHG emission trajectories ...")
        ts_df = self._compute_emissions(ts_df, fleet_df, country_tp, defaults, years)

        logger.info("  [4/7] Computing transport electrification costs ...")
        ts_df = self._compute_costs(ts_df, country_tp, defaults, years, lcoe_results)

        logger.info("  [5/7] Locating charging hubs from suitability rasters ...")
        hubs_gdf = self._place_charging_hubs(
            code, country_tp, defaults, mainland_gdf, suitability_dir
        )

        logger.info("  [6/7] Building summary ...")
        summary = self._build_summary(
            code, country_name, ts_df, fleet_df, hubs_gdf, pot_results
        )

        logger.info("  [7/7] Writing outputs & maps ...")
        ts_path      = out_data / f"{code}_transport_annual_timeseries.csv"
        fleet_path   = out_data / f"{code}_transport_fleet_composition.csv"
        hubs_path    = out_data / f"{code}_transport_hub_locations.csv"
        summary_path = out_data / f"{code}_transport_summary_2050.json"
        report_path  = out_rep  / f"{code}_transport_decarbonization_report.txt"

        ts_df.to_csv(ts_path, index=False)
        fleet_df.to_csv(fleet_path, index=False)
        if hubs_gdf is not None and not hubs_gdf.empty:
            hubs_gdf.to_csv(hubs_path, index=False)
        with open(summary_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, default=str)
        report_path.write_text(
            self._format_report(
                code, country_name, ts_df, fleet_df, hubs_gdf, summary, years
            ),
            encoding="utf-8",
        )

        self._plot_emissions_trajectory(
            ts_df, country_name, code,
            out_maps / f"{code}_transport_emissions_trajectory.png"
        )
        self._plot_fleet_transition(
            fleet_df, country_name, code,
            out_maps / f"{code}_transport_fleet_transition.png"
        )
        self._plot_renewable_need(
            ts_df, country_name, code,
            out_maps / f"{code}_transport_renewable_need.png"
        )
        if hubs_gdf is not None and not hubs_gdf.empty:
            self._plot_hub_map(
                hubs_gdf, mainland_gdf, context_gdf, country_name,
                code, suitability_dir,
                out_maps / f"{code}_transport_hubs_2050.png",
            )

        elapsed = time.perf_counter() - t_start
        logger.info("Phase 9 (Transport Decarbonisation) completed in %.1fs", elapsed)
        logger.info("  Report : %s", report_path)

        return {
            "timeseries_df": ts_df,
            "fleet_df":      fleet_df,
            "hubs_gdf":      hubs_gdf,
            "summary":       summary,
            "report_path":   report_path,
        }

    # ─────────────────────────────────────────────────────────────────────
    # Step 1: Fleet trajectory — ICE · HEV · PHEV · BEV · FCEV
    # ─────────────────────────────────────────────────────────────────────

    def _build_fleet_trajectory(
        self,
        code: str,
        country_tp: Dict,
        defaults:   Dict,
        years:      List[int],
    ) -> pd.DataFrame:
        """
        Fleet stock-turnover model for light passenger vehicles.

        Base fleet priority (three-tier):
          1. ``fleet_size_light_vehicles_2024``  — country-specific LV count (preferred)
          2. ``fleet_size_2024`` × passenger_car.share_of_fleet  — fallback
        This ensures the reported fleet aligns with actual passenger car registrations,
        not a gross-fleet fraction.
        """
        vehicle_cats = defaults["vehicle_categories"]
        lifetimes    = defaults["vehicle_lifetime_years"]

        # ── Resolve base light-vehicle fleet ────────────────────────────────────
        lv_key = "fleet_size_light_vehicles_2024"
        if lv_key in country_tp:
            base_lv_fleet = int(country_tp[lv_key])
            _fleet_source = f"fleet_size_light_vehicles_2024 = {base_lv_fleet:,}"
        else:
            # Fallback: total fleet × passenger_car share
            total_fleet = int(country_tp.get("fleet_size_2024", 10_000_000))
            pc_share    = float(vehicle_cats["passenger_car"].get("share_of_fleet", 0.72))
            base_lv_fleet = round(total_fleet * pc_share)
            _fleet_source = (
                f"fleet_size_2024({total_fleet:,}) × "
                f"share_of_fleet({pc_share}) = {base_lv_fleet:,}"
            )
        logger.info("  Fleet base (light vehicles): %s", _fleet_source)

        growth_rate = float(country_tp.get(
            "annual_fleet_growth_rate",
            defaults["fleet_growth"]["annual_growth_rate_default"]
        ))

        pt_override = country_tp.get("powertrain_penetration_override", {})
        pt_scenarios: Dict[str, Dict[int, Dict[str, float]]] = {}
        for sc in ("reference", "accelerated", "conservative"):
            raw_sc = pt_override.get(sc) or defaults["powertrain_penetration_scenarios"].get(sc, {})
            pt_scenarios[sc] = _interpolate_powertrain_shares(raw_sc, years)

        # Only passenger_car is modelled (light-vehicle only mode)
        cat     = "passenger_car"
        cat_cfg = vehicle_cats[cat]
        lifetime = lifetimes.get(cat, 15)

        records = []
        for sc in ("reference", "accelerated", "conservative"):
            pt_shares_by_year = pt_scenarios[sc]
            init_shares       = pt_shares_by_year[years[0]]
            stocks            = {pt: base_lv_fleet * init_shares.get(pt, 0.0)
                                  for pt in _POWERTRAIN_TYPES}
            fleet_size        = float(base_lv_fleet)

            for y in years:
                new_sales  = fleet_size / lifetime
                fleet_size = fleet_size * (1 + growth_rate)
                pt_split   = pt_shares_by_year[y]

                for pt in _POWERTRAIN_TYPES:
                    new_pt     = new_sales * pt_split[pt]
                    stocks[pt] = stocks[pt] * (1 - 1 / lifetime) + new_pt

                total_stock = sum(stocks.values())
                bev_stock   = stocks["bev"]
                hev_stock   = stocks["hev"]
                phev_stock  = stocks["phev"]
                ice_stock   = stocks["ice"]
                fcev_stock  = stocks["fcev"]
                electrified = bev_stock + hev_stock + phev_stock + fcev_stock

                records.append({
                    "year":                  y,
                    "country_code":          code,
                    "vehicle_category":      cat,
                    "scenario":              sc,
                    "total_fleet":           round(fleet_size),
                    "stock_ice":             round(ice_stock),
                    "stock_hev":             round(hev_stock),
                    "stock_phev":            round(phev_stock),
                    "stock_bev":             round(bev_stock),
                    "stock_fcev":            round(fcev_stock),
                    "bev_stock":             round(bev_stock),
                    "non_ev_stock":          round(ice_stock + hev_stock),
                    "bev_share_pct":         round(100 * bev_stock / max(fleet_size, 1), 2),
                    "electrified_share_pct": round(100 * electrified / max(fleet_size, 1), 2),
                    "new_sales_bev_pct":     round(pt_split["bev"]  * 100, 2),
                    "new_sales_hev_pct":     round(pt_split["hev"]  * 100, 2),
                    "new_sales_phev_pct":    round(pt_split["phev"] * 100, 2),
                    "new_sales_ice_pct":     round(pt_split["ice"]  * 100, 2),
                    "new_sales_fcev_pct":    round(pt_split["fcev"] * 100, 2),
                })

        return pd.DataFrame(records)

    # ─────────────────────────────────────────────────────────────────────
    # Step 2: Timeseries — energy demand & RE requirement
    # ─────────────────────────────────────────────────────────────────────

    def _build_timeseries(
        self,
        code:        str,
        country_tp:  Dict,
        defaults:    Dict,
        fleet_df:    pd.DataFrame,
        years:       List[int],
        pot_results: Optional[Dict],
    ) -> pd.DataFrame:
        vehicle_cats = defaults["vehicle_categories"]
        re_avail     = self._extract_re_potential(pot_results)

        records = []
        for sc in ("reference", "accelerated", "conservative"):
            for y in years:
                total_ev_twh = 0.0
                total_fleet  = 0.0
                total_bev    = 0.0
                total_hev    = 0.0
                total_phev   = 0.0
                total_ice    = 0.0
                total_fcev   = 0.0

                for cat, cat_cfg in vehicle_cats.items():
                    sub = fleet_df[
                        (fleet_df.year == y) &
                        (fleet_df.vehicle_category == cat) &
                        (fleet_df.scenario == sc)
                    ]
                    if sub.empty:
                        continue

                    row      = sub.iloc[0]
                    daily_km = float(cat_cfg.get("avg_daily_km", 40))
                    epk      = cat_cfg["energy_per_km_kwh"]
                    bev_kwh  = float(epk.get("bev", 0.18))
                    phev_kwh = float(epk.get("hybrid_phev", bev_kwh * 0.5))

                    bev_stk  = float(row["stock_bev"])
                    phev_stk = float(row["stock_phev"])

                    bev_twh  = bev_stk  * daily_km * 365 * bev_kwh / 1e9
                    phev_twh = (phev_stk * daily_km * 365
                                * phev_kwh * _PHEV_ELECTRIC_FRACTION / 1e9)
                    total_ev_twh += bev_twh + phev_twh

                    total_fleet += float(row["total_fleet"])
                    total_bev   += bev_stk
                    total_hev   += float(row["stock_hev"])
                    total_phev  += phev_stk
                    total_ice   += float(row["stock_ice"])
                    total_fcev  += float(row["stock_fcev"])

                cf_solar, cf_wind     = 0.22, 0.35
                solar_share, wind_share = 0.65, 0.35

                gw_solar = (total_ev_twh * solar_share * 1000 / (cf_solar * 8760)
                            if total_ev_twh > 0 else 0.0)
                gw_wind  = (total_ev_twh * wind_share  * 1000 / (cf_wind  * 8760)
                            if total_ev_twh > 0 else 0.0)

                prev = next(
                    (r for r in reversed(records)
                     if r["year"] == y - 1 and r["scenario"] == sc),
                    None,
                )
                delta_solar = max(
                    0.0, gw_solar - (prev["re_cumulative_solar_gw"] if prev else 0.0)
                )
                delta_wind = max(
                    0.0, gw_wind - (prev["re_cumulative_wind_gw"] if prev else 0.0)
                )

                records.append({
                    "year":                      y,
                    "country_code":              code,
                    "scenario":                  sc,
                    "total_fleet_all_cats":      round(total_fleet),
                    "stock_bev_all":             round(total_bev),
                    "stock_hev_all":             round(total_hev),
                    "stock_phev_all":            round(total_phev),
                    "stock_ice_all":             round(total_ice),
                    "stock_fcev_all":            round(total_fcev),
                    "ev_electricity_demand_twh": round(total_ev_twh, 4),
                    "re_needed_total_gw":        round(gw_solar + gw_wind, 3),
                    "re_needed_solar_gw":        round(gw_solar, 3),
                    "re_needed_wind_gw":         round(gw_wind, 3),
                    "re_cumulative_solar_gw":    round(gw_solar, 3),
                    "re_cumulative_wind_gw":     round(gw_wind, 3),
                    "re_delta_solar_gw":         round(delta_solar, 4),
                    "re_delta_wind_gw":          round(delta_wind, 4),
                    "re_avail_solar_gw":         round(re_avail.get("solar",   0.0), 2),
                    "re_avail_wind_gw":          round(re_avail.get("wind",    0.0), 2),
                    "re_avail_biomass_gw":       round(re_avail.get("biomass", 0.0), 2),
                    "re_sufficiency_solar_pct":  round(
                        min(100, re_avail.get("solar", 0) /
                            max(gw_solar, 1e-9) * 100), 1
                    ),
                    "re_sufficiency_wind_pct":   round(
                        min(100, re_avail.get("wind", 0) /
                            max(gw_wind, 1e-9) * 100), 1
                    ),
                })

        return pd.DataFrame(records)

    # ─────────────────────────────────────────────────────────────────────
    # Step 3: GHG trajectories
    # ─────────────────────────────────────────────────────────────────────

    def _compute_emissions(
        self,
        ts_df:      pd.DataFrame,
        fleet_df:   pd.DataFrame,
        country_tp: Dict,
        defaults:   Dict,
        years:      List[int],
    ) -> pd.DataFrame:
        vehicle_cats = defaults["vehicle_categories"]
        fuel_mix     = country_tp.get("dominant_fuel_mix", {})

        grid_raw = country_tp.get(
            "grid_emission_intensity_gco2_kwh",
            defaults["grid_emission_intensity_defaults"]
        )
        grid_intensity = _interpolate_trajectory(_safe_anchors(grid_raw), years)

        def _weighted_ice_ef(cat: str) -> float:
            cat_mix = fuel_mix.get(cat, {"ice_gasoline": 1.0})
            ef_map  = vehicle_cats[cat]["emission_factor_gco2_km"]
            non_ev  = {k: v for k, v in cat_mix.items()
                       if k not in ("bev", "fcev", "hydrogen_fcev",
                                    "hybrid_hev", "hybrid_phev")}
            total_w = sum(non_ev.values())
            if total_w == 0:
                return ef_map.get("ice_diesel", 168.0)
            return sum(
                w * ef_map.get(fuel, ef_map.get("ice_gasoline", 192.0))
                for fuel, w in non_ev.items()
            ) / total_w

        def _ef(cat: str, key: str) -> float:
            return float(
                vehicle_cats[cat]["emission_factor_gco2_km"].get(key, 0.0)
            )

        def _epk(cat: str, key: str, fallback: float = 0.18) -> float:
            return float(
                vehicle_cats[cat]["energy_per_km_kwh"].get(key, fallback)
            )

        # Baseline: all vehicles remain ICE (counterfactual)
        baseline_by_year: Dict[int, float] = {}
        for y in years:
            ref_row = ts_df[(ts_df.year == y) & (ts_df.scenario == "reference")]
            if ref_row.empty:
                baseline_by_year[y] = 0.0
                continue
            total_fleet = float(ref_row["total_fleet_all_cats"].values[0])
            co2 = 0.0
            for cat, cat_cfg in vehicle_cats.items():
                cat_share = float(cat_cfg["share_of_fleet"])
                daily_km  = float(cat_cfg.get("avg_daily_km", 40))
                ice_ef    = _weighted_ice_ef(cat)
                co2 += total_fleet * cat_share * daily_km * 365 * ice_ef / 1e12
            baseline_by_year[y] = co2

        emission_rows = []
        for sc in ("reference", "accelerated", "conservative"):
            for y in years:
                gi       = grid_intensity.get(y, 436.0)
                co2_ice  = 0.0
                co2_hev  = 0.0
                co2_phev = 0.0
                co2_bev  = 0.0

                for cat, cat_cfg in vehicle_cats.items():
                    sub = fleet_df[
                        (fleet_df.year == y) &
                        (fleet_df.vehicle_category == cat) &
                        (fleet_df.scenario == sc)
                    ]
                    if sub.empty:
                        continue
                    row      = sub.iloc[0]
                    daily_km = float(cat_cfg.get("avg_daily_km", 40))
                    km_yr    = daily_km * 365

                    ice_stk  = float(row["stock_ice"])
                    hev_stk  = float(row["stock_hev"])
                    phev_stk = float(row["stock_phev"])
                    bev_stk  = float(row["stock_bev"])

                    ice_ef   = _weighted_ice_ef(cat)
                    hev_ef   = _ef(cat, "hybrid_hev")
                    phev_ef  = _ef(cat, "hybrid_phev")
                    bev_kwh  = _epk(cat, "bev")
                    phev_kwh = _epk(cat, "hybrid_phev", bev_kwh * 0.5)

                    co2_ice  += ice_stk  * km_yr * ice_ef  / 1e12
                    co2_hev  += hev_stk  * km_yr * hev_ef  / 1e12
                    co2_phev += phev_stk * km_yr * (
                        phev_ef * (1 - _PHEV_ELECTRIC_FRACTION) +
                        gi * phev_kwh * _PHEV_ELECTRIC_FRACTION
                    ) / 1e12
                    co2_bev  += bev_stk  * km_yr * gi * bev_kwh / 1e12

                total_actual = co2_ice + co2_hev + co2_phev + co2_bev
                baseline     = baseline_by_year.get(y, 0.0)
                avoided      = max(0.0, baseline - total_actual)

                emission_rows.append({
                    "year":                     y,
                    "scenario":                 sc,
                    "co2_total_mtco2eq":        round(total_actual, 4),
                    "co2_ice_mtco2eq":          round(co2_ice,  4),
                    "co2_hev_mtco2eq":          round(co2_hev,  4),
                    "co2_phev_mtco2eq":         round(co2_phev, 4),
                    "co2_bev_indirect_mtco2eq": round(co2_bev,  4),
                    "co2_baseline_mtco2eq":     round(baseline,  4),
                    "co2_avoided_mtco2eq":      round(avoided,   4),
                    "grid_intensity_gco2_kwh":  round(gi, 1),
                })

        em_df  = pd.DataFrame(emission_rows)
        return ts_df.merge(em_df, on=["year", "scenario"], how="left")

    # ─────────────────────────────────────────────────────────────────────
    # Step 4: Cost analysis
    # ─────────────────────────────────────────────────────────────────────

    def _compute_costs(
    self,
    ts_df:        pd.DataFrame,
    country_tp:   Dict,
    defaults:     Dict,
    years:        List[int],
    lcoe_results: Optional[Dict],
    ) -> pd.DataFrame:
        """
        Cost model com LCOE integrado.

        Quando lcoe_results está disponível (Phase 5):
        capex_solar = lcoe_solar_usd_mwh × 1000 MWh/GWh × capacity_factor_solar × 8760 h
                        convertido para USD/kW (custo de construção equivalente)
        Ou diretamente: custo anual de energia = EV_demand_TWh × LCOE_USD_MWh × 1000

        O LCOE substitui o capex fixo do JSON para o cálculo do custo de energia RE,
        produzindo um custo de abatimento mais realista.
        """
        costs_cfg   = defaults["costs"]

        # ── Capex de referência do JSON (fallback quando LCOE não disponível) ──────
        capex_solar_usd_kw = costs_cfg["renewable_build_cost_usd_kw"]["solar"]
        capex_wind_usd_kw  = costs_cfg["renewable_build_cost_usd_kw"]["wind"]

        # ── LCOEs da Phase 5 (USD/MWh) ───────────────────────────────────────────
        lcoe_solar_usd_mwh   = self._extract_lcoe(lcoe_results, "solar",   fallback=55.0)
        lcoe_wind_usd_mwh    = self._extract_lcoe(lcoe_results, "wind",    fallback=48.0)
        lcoe_biomass_usd_mwh = self._extract_lcoe(lcoe_results, "biomass", fallback=85.0)

        using_lcoe = lcoe_results is not None
        if using_lcoe:
            logger.info(
                "    Using Phase-5 LCOEs — Solar: %.1f USD/MWh | Wind: %.1f USD/MWh"
                " | Biomass: %.1f USD/MWh",
                lcoe_solar_usd_mwh, lcoe_wind_usd_mwh, lcoe_biomass_usd_mwh,
            )
        else:
            logger.info(
                "    LCOE results not available — using JSON capex defaults "
                "(solar: %.0f USD/kW, wind: %.0f USD/kW)",
                capex_solar_usd_kw, capex_wind_usd_kw,
            )

        # Participação solar/wind na geração EV (consistente com _build_timeseries)
        solar_share = 0.65
        wind_share  = 0.35

        # Custos de combustível e eletricidade
        fuel_usd_per_kwh = costs_cfg.get("fuel_cost_usd_lge", 1.45) / 8.9
        elec_usd_per_kwh = costs_cfg.get("electricity_cost_usd_kwh", 0.14)

        cost_rows = []
        for sc in ("reference", "accelerated", "conservative"):
            cumulative_capex   = 0.0
            cumulative_savings = 0.0

            for y in years:
                sub = ts_df[(ts_df.year == y) & (ts_df.scenario == sc)]
                if sub.empty:
                    continue

                delta_solar_gw = float(sub["re_delta_solar_gw"].values[0])
                delta_wind_gw  = float(sub["re_delta_wind_gw"].values[0])
                ev_twh         = float(sub["ev_electricity_demand_twh"].values[0])
                avoided_mt     = (
                    float(sub["co2_avoided_mtco2eq"].values[0])
                    if "co2_avoided_mtco2eq" in sub.columns else 0.0
                )

                if using_lcoe:
                    # ── Custo anual de energia RE para EV (USD bn) ─────────────────
                    # Custo = demanda EV (TWh) × participação da fonte × LCOE (USD/MWh)
                    #         convertido para USD bn
                    # Representa o custo real da energia gerada, não apenas o capex
                    energy_cost_solar = (
                        ev_twh * solar_share * 1e6 *    # TWh → MWh
                        lcoe_solar_usd_mwh / 1e9        # USD/MWh → bn USD
                    )
                    energy_cost_wind = (
                        ev_twh * wind_share * 1e6 *
                        lcoe_wind_usd_mwh / 1e9
                    )
                    # Capex incremental (construção nova capacidade) — mantido para
                    # rastrear investimento físico separado do custo de energia
                    capex_incremental = (
                        delta_solar_gw * 1e6 * capex_solar_usd_kw +
                        delta_wind_gw  * 1e6 * capex_wind_usd_kw
                    ) / 1e9

                    # Custo total anual = energia RE + capex incremental
                    # (LCOE já embute O&M, então energia_cost é o custo total de geração;
                    #  capex_incremental aqui é apenas o delta de expansão de rede)
                    capex_yr = energy_cost_solar + energy_cost_wind

                    logger.debug(
                        "    [%s] %d — LCOE energy cost: solar %.3f + wind %.3f = %.3f bn USD",
                        sc, y,
                        energy_cost_solar, energy_cost_wind, capex_yr,
                    )
                else:
                    # Fallback: capex por kW instalado × delta GW
                    capex_yr = (
                        delta_solar_gw * 1e6 * capex_solar_usd_kw +
                        delta_wind_gw  * 1e6 * capex_wind_usd_kw
                    ) / 1e9

                # Economias de combustível (inalteradas — independentes do LCOE)
                fuel_savings = (
                    ev_twh * 1e9 *
                    (fuel_usd_per_kwh - elec_usd_per_kwh)
                ) / 1e9

                net_annual   = capex_yr - fuel_savings
                avoided_tco2 = avoided_mt * 1e6
                abatement_cost = (
                    net_annual * 1e9 / avoided_tco2
                ) if avoided_tco2 > 0 else 0.0

                cumulative_capex   += capex_yr
                cumulative_savings += fuel_savings

                cost_rows.append({
                    "year":                        y,
                    "scenario":                    sc,
                    "annual_capex_bn_usd":         round(capex_yr, 3),
                    "fuel_savings_bn_usd":         round(fuel_savings, 3),
                    "net_annual_cost_bn_usd":      round(net_annual, 3),
                    "abatement_cost_usd_tco2":     round(abatement_cost, 2),
                    "cumulative_capex_bn_usd":     round(cumulative_capex, 2),
                    "cumulative_savings_bn_usd":   round(cumulative_savings, 2),
                    # Flags de rastreabilidade
                    "lcoe_solar_usd_mwh":          round(lcoe_solar_usd_mwh, 1) if using_lcoe else None,
                    "lcoe_wind_usd_mwh":           round(lcoe_wind_usd_mwh,  1) if using_lcoe else None,
                    "cost_method":                 "lcoe" if using_lcoe else "capex_fixed",
                })

        cost_df = pd.DataFrame(cost_rows)
        return ts_df.merge(cost_df, on=["year", "scenario"], how="left")
    # ─────────────────────────────────────────────────────────────────────
    # Step 5: Hub placement
    # ─────────────────────────────────────────────────────────────────────

    def _place_charging_hubs(
    self,
    code:            str,
    country_tp:      Dict,
    defaults:        Dict,
    mainland_gdf:    gpd.GeoDataFrame,
    suitability_dir: Optional[Path],
    lcoe_results:    Optional[Dict] = None,   # ← novo parâmetro
    ) -> Optional[gpd.GeoDataFrame]:
        """
        Hub placement com score composto:
        score_final = w_suit * suitability_score
                    + w_dist * demand_proximity_score
                    + w_lcoe * lcoe_score (se disponível)

        Filtros aplicados:
        1. Apenas pixels dentro do polígono terrestre do país
        2. score >= min_suitability (0.80 por defeito)
        3. Espaçamento mínimo entre hubs (hub_radius_km)
        4. Exclusão de células sem demanda EV proxy
            (distância ao centróide ponderado > max_remote_factor)
        5. Distribuição forçada por células de grade (demand grid)
            para evitar clustering excessivo em zonas remotas de alta aptidão
        """
        if not _HAS_RASTERIO:
            logger.warning("rasterio not available — hub placement skipped.")
            return None
        if suitability_dir is None:
            logger.warning("No suitability_dir provided — hub placement skipped.")
            return None

        from shapely.geometry import Point
        from shapely.prepared import prep

        suitability_dir = Path(suitability_dir)
        hub_radius_km   = float(country_tp.get(
            "hub_radius_km",
            defaults["charging_infrastructure"]["hub_radius_km"]
        ))
        min_suitability = float(country_tp.get(
            "hub_min_suitability", _HUB_SUITABILITY_THRESHOLD
        ))
        # Parâmetros do score composto (configuráveis no transport_parameters.json)
        hub_cfg = country_tp.get("hub_placement", {})
        w_suit  = float(hub_cfg.get("weight_suitability",       0.50))
        w_dist  = float(hub_cfg.get("weight_demand_proximity",  0.35))
        w_lcoe  = float(hub_cfg.get("weight_lcoe",              0.15))
        # Fração máxima da diagonal do país além da qual um pixel é "remoto demais"
        max_remote_frac = float(hub_cfg.get("max_remote_fraction", 0.60))
        # Número de células do demand grid (NxN)
        demand_grid_n   = int(hub_cfg.get("demand_grid_cells",  8))

        # ── Identificar TIFs disponíveis ─────────────────────────────────────────
        tif_solar   = sorted(suitability_dir.glob(f"{code}_solar_suitability*.tif"))
        tif_wind    = sorted(suitability_dir.glob(f"{code}_wind_suitability*.tif"))

        if not tif_solar and not tif_wind:
            fallback = sorted(suitability_dir.glob("*.tif"))
            if not fallback:
                logger.warning("No suitability TIFFs found — hub placement skipped.")
                return None
            tif_solar = fallback[:1]

        def _read_raster(path):
            with rasterio.open(path) as src:
                arr = src.read(1).astype(np.float32)
                tf  = src.transform
                nd  = src.nodata if src.nodata is not None else -9999.0
            arr[arr == nd] = np.nan
            return arr, tf

        # ── Preparar máscara terrestre e estruturas geográficas ──────────────────
        # União de todos os polígonos do país → geometria terrestre
        try:
            land_union   = mainland_gdf.union_all() if hasattr(mainland_gdf, "union_all") \
                        else mainland_gdf.unary_union
            land_prep    = prep(land_union)
            bounds       = land_union.bounds          # (minx, miny, maxx, maxy)
            centroid     = land_union.centroid
            # Diagonal do bounding box — referência de distância
            diag_deg     = np.sqrt(
                (bounds[2] - bounds[0]) ** 2 + (bounds[3] - bounds[1]) ** 2
            )
            max_remote_deg = diag_deg * max_remote_frac
        except Exception as exc:
            logger.warning("Could not build land mask: %s — skipping spatial filter", exc)
            land_prep    = None
            centroid     = None
            max_remote_deg = None
            bounds       = mainland_gdf.total_bounds   # (minx, miny, maxx, maxy)

        # ── Demand-grid: distribuição forçada por célula ──────────────────────────
        # Divide o bounding box em demand_grid_n × demand_grid_n células.
        # Cada célula tem uma cota máxima de hubs = max(1, total_target/n_cells)
        # Isso evita que todas as vagas sejam preenchidas por pixels de uma
        # única região remota de alta aptidão (ex.: Nordeste para solar no Brasil).
        minx_b, miny_b, maxx_b, maxy_b = (
            float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3])
        )
        cell_w = (maxx_b - minx_b) / demand_grid_n
        cell_h = (maxy_b - miny_b) / demand_grid_n

        def _cell_idx(lon: float, lat: float) -> tuple:
            ci = int(min((lon - minx_b) / cell_w, demand_grid_n - 1))
            ri = int(min((lat - miny_b) / cell_h, demand_grid_n - 1))
            return (ci, ri)

        # ── LCOE score por fonte: menor LCOE → score mais alto ───────────────────
        # Normalizado entre 0 e 1 (invertido: LCOE_max - LCOE_i / range)
        lcoe_by_source: Dict[str, float] = {}
        if lcoe_results is not None:
            raw_lcoes: Dict[str, float] = {}
            for src_key, lcoe_key in [
                ("Solar",   "solar"),
                ("Wind",    "wind"),
            ]:
                try:
                    raw_lcoes[src_key] = float(lcoe_results[lcoe_key]["lcoe_usd_mwh"])
                except (KeyError, TypeError):
                    pass
            if raw_lcoes:
                lcoe_vals  = list(raw_lcoes.values())
                lcoe_min   = min(lcoe_vals)
                lcoe_max   = max(lcoe_vals)
                lcoe_range = max(lcoe_max - lcoe_min, 1.0)
                for src_key, val in raw_lcoes.items():
                    # Score alto = LCOE baixo (melhor custo-benefício)
                    lcoe_by_source[src_key] = (lcoe_max - val) / lcoe_range
                logger.info(
                    "    LCOE scores (normalised): %s",
                    {k: f"{v:.3f}" for k, v in lcoe_by_source.items()}
                )

        # ── Coleta de candidatos por fonte ───────────────────────────────────────
        all_sources: List[tuple] = []
        if tif_solar:   all_sources.append(("Solar",   tif_solar[0]))
        if tif_wind:    all_sources.append(("Wind",    tif_wind[0]))

        logger.info(
            "    Hub placement from: %s (min suitability: %.2f)",
            ", ".join(p.name for _, p in all_sources),
            min_suitability,
        )
        logger.info(
            "    Score weights — suitability: %.2f | demand proximity: %.2f | LCOE: %.2f",
            w_suit, w_dist, w_lcoe,
        )

        # Candidatos agregados: (composite_score, lon, lat, source_label)
        all_candidates: List[tuple] = []

        for source_label, tif_path in all_sources:
            try:
                data, transform = _read_raster(tif_path)
            except Exception as exc:
                logger.error("Failed to read %s raster: %s", source_label, exc)
                continue

            valid = np.isfinite(data) & (data >= min_suitability)
            rows, cols = np.where(valid)
            if len(rows) == 0:
                logger.warning(
                    "    No pixels in %s raster with suitability >= %.2f",
                    source_label, min_suitability,
                )
                continue

            scores_raw = data[rows, cols]
            lons_raw   = transform.c + transform.a * (cols + 0.5)
            lats_raw   = transform.f + transform.e * (rows + 0.5)

            logger.info(
                "    %s: %d candidate pixels (score >= %.2f)",
                source_label, len(rows), min_suitability,
            )

            # LCOE score para esta fonte
            src_lcoe_score = lcoe_by_source.get(source_label, 0.5)  # neutro se não disponível

            for lon_p, lat_p, suit_p in zip(lons_raw, lats_raw, scores_raw):

                # ── Filtro 1: dentro do território terrestre ──────────────────────
                if land_prep is not None:
                    pt = Point(lon_p, lat_p)
                    if not land_prep.contains(pt):
                        continue

                # ── Filtro 2: distância ao centróide — rejeitar remotos demais ────
                # Proxy de demanda EV: regiões mais próximas dos centros populacionais
                # recebem score de proximidade mais alto.
                if centroid is not None and max_remote_deg is not None:
                    dist_to_centroid = np.sqrt(
                        (lon_p - centroid.x) ** 2 + (lat_p - centroid.y) ** 2
                    )
                    if dist_to_centroid > max_remote_deg:
                        continue   # rejeitar pixel muito remoto
                    # Normalizar proximidade: 1.0 = no centróide, 0.0 = no limite máximo
                    proximity_score = 1.0 - (dist_to_centroid / max_remote_deg)
                else:
                    proximity_score = 0.5

                # ── Score composto ────────────────────────────────────────────────
                if w_lcoe > 0 and lcoe_by_source:
                    composite = (
                        w_suit * float(suit_p) +
                        w_dist * proximity_score +
                        w_lcoe * src_lcoe_score
                    )
                else:
                    # sem LCOE: renormalizar pesos restantes
                    w_total = w_suit + w_dist
                    composite = (
                        (w_suit / w_total) * float(suit_p) +
                        (w_dist / w_total) * proximity_score
                    )

                all_candidates.append((composite, lon_p, lat_p, source_label))

        if not all_candidates:
            logger.warning("Hub placement: no candidates passed geographic filters.")
            return None

        # Ordenar por score composto decrescente
        all_candidates.sort(key=lambda x: -x[0])

        # ── Greedy placement com demand-grid quota ────────────────────────────────
        # Cota por célula: permite no máximo ceil(sqrt(N_total_candidates)/grid_n) hubs
        # por célula, evitando saturação de uma região.
        n_cands          = len(all_candidates)
        max_hubs_per_cell = max(1, int(np.ceil(np.sqrt(n_cands) / demand_grid_n)))
        cell_counts: Dict[tuple, int] = {}

        min_sep_deg = hub_radius_km / 111.0
        acc_lons:    List[float] = []
        acc_lats:    List[float] = []
        acc_scores:  List[float] = []
        acc_sources: List[str]   = []

        for composite, lon_p, lat_p, source_label in all_candidates:

            # ── Verificar cota de célula ──────────────────────────────────────────
            cell = _cell_idx(lon_p, lat_p)
            if cell_counts.get(cell, 0) >= max_hubs_per_cell:
                continue

            # ── Verificar espaçamento mínimo ─────────────────────────────────────
            if acc_lons:
                dists = np.sqrt(
                    (np.array(acc_lons) - lon_p) ** 2 +
                    (np.array(acc_lats) - lat_p) ** 2
                )
                if dists.min() < min_sep_deg:
                    continue

            acc_lons.append(lon_p)
            acc_lats.append(lat_p)
            acc_scores.append(composite)
            acc_sources.append(source_label)
            cell_counts[cell] = cell_counts.get(cell, 0) + 1

        if not acc_lons:
            logger.warning("Hub placement produced no hubs after all filters.")
            return None

        hub_df = pd.DataFrame({
            "longitude":          acc_lons,
            "latitude":           acc_lats,
            "suitability_score":  acc_scores,
            "primary_source":     acc_sources,
            "hub_id":             [f"{code}_HUB_{i+1:04d}" for i in range(len(acc_lons))],
            "hub_radius_km":      hub_radius_km,
            "n_dc_fast_chargers": defaults["charging_infrastructure"]["hub_min_chargers"],
            "peak_power_mw":      defaults["charging_infrastructure"]["hub_power_mw"],
            "capex_usd":          defaults["charging_infrastructure"]["hub_capex_usd"],
        })
        geometry = [Point(lo, la) for lo, la in zip(hub_df["longitude"], hub_df["latitude"])]
        logger.info(
            "    Placed %d charging hubs (spacing ≥ %.0f km, score ≥ %.2f) "
            "— Solar: %d, Wind: %d, Biomass: %d.",
            len(hub_df), hub_radius_km, min_suitability,
            (hub_df["primary_source"] == "Solar").sum(),
            (hub_df["primary_source"] == "Wind").sum(),
            (hub_df["primary_source"] == "Biomass").sum(),
        )
        return gpd.GeoDataFrame(hub_df, geometry=geometry, crs="EPSG:4326")

    # ─────────────────────────────────────────────────────────────────────
    # Step 6: Summary
    # ─────────────────────────────────────────────────────────────────────

    def _build_summary(
        self,
        code:         str,
        country_name: str,
        ts_df:        pd.DataFrame,
        fleet_df:     pd.DataFrame,
        hubs_gdf:     Optional[gpd.GeoDataFrame],
        pot_results:  Optional[Dict],
    ) -> Dict[str, Any]:
        re_avail = self._extract_re_potential(pot_results)
        infra    = self._tp["global_defaults"]["charging_infrastructure"]
        summary: Dict[str, Any] = {
            "country_code": code,
            "country_name": country_name,
            "generated_at": datetime.utcnow().isoformat(),
            "scenarios":    {},
            "charging_hubs": {
                "total": int(len(hubs_gdf)) if hubs_gdf is not None else 0,
                "total_peak_power_mw": (
                    float(len(hubs_gdf) * infra["hub_power_mw"])
                    if hubs_gdf is not None else 0.0
                ),
                "total_capex_bn_usd": (
                    float(len(hubs_gdf) * infra["hub_capex_usd"] / 1e9)
                    if hubs_gdf is not None else 0.0
                ),
            },
            "renewable_potential_available_gw": re_avail,
        }

        for sc in ("reference", "accelerated", "conservative"):
            sub = ts_df[ts_df.scenario == sc]
            if sub.empty:
                continue
            last = sub[sub.year == sub.year.max()].iloc[0]

            fl_sc   = fleet_df[fleet_df.scenario == sc]
            fl_2050 = fl_sc[fl_sc.year == fl_sc.year.max()]
            total_2050 = fl_2050["total_fleet"].sum()
            bev_2050   = fl_2050["stock_bev"].sum()
            hev_2050   = fl_2050["stock_hev"].sum()
            phev_2050  = fl_2050["stock_phev"].sum()
            ice_2050   = fl_2050["stock_ice"].sum()

            summary["scenarios"][sc] = {
                "ev_electricity_demand_2050_twh": round(
                    float(last.get("ev_electricity_demand_twh", 0)), 2
                ),
                "re_needed_total_gw_2050": round(
                    float(last.get("re_needed_total_gw", 0)), 2
                ),
                "co2_total_2050_mtco2eq": (
                    round(float(last.get("co2_total_mtco2eq", 0)), 3)
                    if "co2_total_mtco2eq" in last.index else 0.0
                ),
                "co2_avoided_cumulative_mtco2eq": (
                    round(float(sub["co2_avoided_mtco2eq"].sum()), 1)
                    if "co2_avoided_mtco2eq" in sub.columns else 0.0
                ),
                "cumulative_capex_2050_bn_usd": (
                    round(float(last.get("cumulative_capex_bn_usd", 0)), 1)
                    if "cumulative_capex_bn_usd" in last.index else 0.0
                ),
                "fleet_2050_total":    int(total_2050),
                "fleet_2050_bev_pct":  round(100 * bev_2050  / max(total_2050, 1), 1),
                "fleet_2050_hev_pct":  round(100 * hev_2050  / max(total_2050, 1), 1),
                "fleet_2050_phev_pct": round(100 * phev_2050 / max(total_2050, 1), 1),
                "fleet_2050_ice_pct":  round(100 * ice_2050  / max(total_2050, 1), 1),
                "re_sufficiency_solar_2050_pct": round(
                    float(last.get("re_sufficiency_solar_pct", 0)), 1
                ),
                "ev_demand_vs_re_potential": (
                    "COVERED"
                    if float(last.get("re_needed_total_gw", 999)) <=
                       re_avail.get("solar", 0) + re_avail.get("wind", 0)
                    else "PARTIAL"
                    if re_avail.get("solar", 0) + re_avail.get("wind", 0) >
                       float(last.get("re_needed_total_gw", 0)) * 0.5
                    else "INSUFFICIENT"
                ),
            }

        return summary

    # ─────────────────────────────────────────────────────────────────────
    # Logging dashboard
    # ─────────────────────────────────────────────────────────────────────

    def _log_parameter_dashboard(
        self,
        code:         str,
        country_name: str,
        country_tp:   Dict,
        defaults:     Dict,
        years:        List[int],
    ) -> None:
        sep  = "=" * 60
        dash = "-" * 52
        logger.info(sep)
        logger.info("  PHASE 9 — TRANSPORT DECARBONISATION")
        logger.info("  %s (%s)", country_name, code)
        logger.info(sep)
        logger.info(dash)
        logger.info("  PARAMETERS IN USE")
        logger.info(dash)

        fleet  = int(country_tp.get("fleet_size_2024", 0))
        growth = float(country_tp.get(
            "annual_fleet_growth_rate",
            defaults["fleet_growth"]["annual_growth_rate_default"]
        )) * 100

        logger.info("  Projection horizon : %d – %d", years[0], years[-1])
        logger.info("  Fleet size (2024)  : %s vehicles", f"{fleet:,}")
        logger.info("  Annual fleet growth: %.1f%%", growth)

        grid_anchors = _safe_anchors(country_tp.get(
            "grid_emission_intensity_gco2_kwh",
            defaults["grid_emission_intensity_defaults"]
        ))
        logger.info("  Grid intensity (gCO2/kWh):")
        for yr, val in sorted(grid_anchors.items()):
            logger.info("    %d : %.0f", yr, val)

        hub_r = float(country_tp.get(
            "hub_radius_km",
            defaults["charging_infrastructure"]["hub_radius_km"]
        ))
        logger.info("  Hub spacing radius : %.0f km", hub_r)
        logger.info(
            "  Biofuel blends     : %s / %s",
            country_tp.get("biodiesel_blend", "B7"),
            country_tp.get("ethanol_blend",   "E10"),
        )

        pt_override = country_tp.get("powertrain_penetration_override", {})
        src_label   = "country-specific" if pt_override else "global defaults"
        logger.info(
            "  Powertrain penetration (%s — reference, new sales):", src_label
        )
        sc_data = (
            pt_override.get("reference", {}) or
            defaults["powertrain_penetration_scenarios"].get("reference", {})
        )
        for milestone in (2025, 2035, 2050):
            yr_key = str(milestone)
            if yr_key in sc_data and isinstance(sc_data[yr_key], dict):
                pt = sc_data[yr_key]
                logger.info(
                    "    %d : ICE %.0f%% | HEV %.0f%% | PHEV %.0f%% "
                    "| BEV %.0f%% | FCEV %.0f%%",
                    milestone,
                    pt.get("ice",  0) * 100,
                    pt.get("hev",  0) * 100,
                    pt.get("phev", 0) * 100,
                    pt.get("bev",  0) * 100,
                    pt.get("fcev", 0) * 100,
                )
        logger.info(sep)

    # ─────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────

    def _extract_re_potential(self, pot_results: Optional[Dict]) -> Dict[str, float]:
        if pot_results is None:
            return {"solar": 0.0, "wind": 0.0, "biomass": 0.0}
        out = {}
        for tech in ("solar", "wind", "biomass"):
            try:
                out[tech] = float(
                    pot_results["techs"][tech]["scenarios"]["balanced"]["capacity_gw"]
                )
            except (KeyError, TypeError):
                out[tech] = 0.0
        return out

    def _extract_lcoe(
        self,
        lcoe_results: Optional[Dict],
        tech:         str,
        fallback:     float,
    ) -> float:
        if lcoe_results is None:
            return fallback
        try:
            return float(lcoe_results[tech]["lcoe_usd_mwh"])
        except (KeyError, TypeError):
            return fallback

    # ─────────────────────────────────────────────────────────────────────
    # Plots
    # ─────────────────────────────────────────────────────────────────────

    def _plot_emissions_trajectory(
        self,
        ts_df:        pd.DataFrame,
        country_name: str,
        code:         str,
        out_path:     Path,
    ) -> None:
        fig, axes = plt.subplots(1, 2, figsize=(14, 6), dpi=150)
        fig.patch.set_facecolor("#F5F5F5")
        fig.suptitle(
            f"Transport GHG Emission Trajectories — {country_name}\n"
            "ICE · HEV · PHEV · BEV · FCEV — 2025–2050",
            fontsize=12, fontweight="bold",
        )
        for sc, color in _SCENARIO_COLORS.items():
            sub = ts_df[ts_df.scenario == sc].sort_values("year")
            if sub.empty:
                continue
            if "co2_total_mtco2eq" in sub.columns:
                axes[0].plot(
                    sub["year"], sub["co2_total_mtco2eq"],
                    color=color, label=sc.capitalize(), linewidth=2,
                )
                axes[0].plot(
                    sub["year"], sub["co2_baseline_mtco2eq"],
                    color=color, linestyle="--", alpha=0.35, linewidth=1.2,
                )
            if "co2_avoided_mtco2eq" in sub.columns:
                axes[1].fill_between(
                    sub["year"], 0, sub["co2_avoided_mtco2eq"],
                    color=color, alpha=0.35, label=sc.capitalize(),
                )
                axes[1].plot(
                    sub["year"], sub["co2_avoided_mtco2eq"],
                    color=color, linewidth=2,
                )

        for ax, title, ylabel in [
            (axes[0], "Total Transport Emissions vs. Full-ICE Baseline",
             "MtCO2eq/yr"),
            (axes[1], "Annual GHG Abatement (vs. Full-ICE Counterfactual)",
             "MtCO2eq/yr avoided"),
        ]:
            ax.set_title(title, fontsize=10, fontweight="bold")
            ax.set_ylabel(ylabel, fontsize=9)
            ax.set_xlabel("Year", fontsize=9)
            ax.legend(fontsize=8)
            ax.yaxis.grid(True, alpha=0.3)
            ax.set_facecolor("#FAFAFA")
            ax.spines[["top", "right"]].set_visible(False)

        axes[0].text(
            0.97, 0.97, "Dashed = Full-ICE baseline",
            transform=axes[0].transAxes, ha="right", va="top",
            fontsize=7, color="#888888",
        )
        plt.tight_layout(rect=[0, 0, 1, 0.93])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(out_path), bbox_inches="tight", dpi=150)
        plt.close(fig)

    def _plot_fleet_transition(
    self,
    fleet_df:     pd.DataFrame,
    country_name: str,
    code:         str,
    out_path:     Path,
    ) -> None:
        """
        Left  : Stacked area — % share of each powertrain (reference scenario).
                Milestone year % labels with dark outline for readability.
        Right : Three small stacked-area panels, one per scenario.
        """
        import matplotlib.patheffects as pe

        sub = fleet_df[fleet_df.scenario == "reference"].copy()
        if sub.empty:
            return

        agg = sub.groupby("year")[
            ["total_fleet", "stock_ice", "stock_hev",
            "stock_phev", "stock_bev", "stock_fcev"]
        ].sum().reset_index()

        total = agg["total_fleet"].clip(lower=1)
        pct = {
            "ice":  100 * agg["stock_ice"]  / total,
            "hev":  100 * agg["stock_hev"]  / total,
            "phev": 100 * agg["stock_phev"] / total,
            "bev":  100 * agg["stock_bev"]  / total,
            "fcev": 100 * agg["stock_fcev"] / total,
        }

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6), dpi=150)
        fig.patch.set_facecolor("#F5F5F5")
        fig.suptitle(
            f"Light-Vehicle Fleet Powertrain Transition — {country_name}",
            fontsize=12, fontweight="bold",
        )

        pt_order  = ("ice", "hev", "phev", "bev", "fcev")
        pt_labels = {"ice": "ICE", "hev": "HEV", "phev": "PHEV",
                    "bev": "BEV", "fcev": "FCEV"}

        # ── LEFT: stacked area reference ─────────────────────────────────────
        ax1.stackplot(
            agg["year"],
            *[pct[pt] for pt in pt_order],
            labels=[pt_labels[pt] for pt in pt_order],
            colors=[_POWERTRAIN_COLORS[pt] for pt in pt_order],
            alpha=0.88,
        )
        ax1.set_ylim(0, 100)
        ax1.set_xlim(agg["year"].min(), agg["year"].max())
        ax1.set_title("Fleet Composition by Powertrain — Reference (%)",
                      fontsize=10, fontweight="bold")
        ax1.set_ylabel("Share of total fleet (%)", fontsize=9)
        ax1.set_xlabel("Year", fontsize=9)
        ax1.legend(fontsize=8, loc="upper right", framealpha=0.85)
        ax1.yaxis.grid(True, alpha=0.3)
        ax1.set_facecolor("#FAFAFA")
        ax1.spines[["top", "right"]].set_visible(False)

        # Milestone labels with dark outline so they read on any colour band
        outline_fx = [
            pe.withStroke(linewidth=2.5, foreground="black"),
        ]
        milestones = [y for y in (2025, 2030, 2035, 2040, 2045, 2050)
                    if y in agg["year"].values]
        for y in milestones:
            row = agg[agg["year"] == y].iloc[0]
            row_total = max(float(row["total_fleet"]), 1)
            cumulative = 0.0
            for pt in pt_order:
                share = 100 * float(row[f"stock_{pt}"]) / row_total
                mid   = cumulative + share / 2
                if share >= 5.0:   # only label if band is wide enough
                    # Clip mid so text never goes outside 0–100
                    text_y = max(3.0, min(97.0, mid))
                    ax1.text(
                        y, text_y, f"{share:.0f}%",
                        ha="center", va="center",
                        fontsize=6.5, color="white", fontweight="bold",
                        path_effects=outline_fx,
                        clip_on=True,
                    )
                cumulative += share

        # ── RIGHT: three scenario mini-panels ────────────────────────────────
        fig.delaxes(ax2)
        gs_right = fig.add_gridspec(
            3, 1,
            left=0.52, right=0.97,
            top=0.88,  bottom=0.10,
            hspace=0.55,
        )
        scenario_axes = [fig.add_subplot(gs_right[i]) for i in range(3)]
        scenarios     = ("reference", "accelerated", "conservative")
        _SC_TITLES = {
            "reference":    "Reference",
            "accelerated":  "Accelerated",
            "conservative": "Conservative",
        }

        for ax_s, sc in zip(scenario_axes, scenarios):
            sc_sub = fleet_df[fleet_df.scenario == sc].groupby("year")[
                ["total_fleet", "stock_ice", "stock_hev",
                "stock_phev", "stock_bev", "stock_fcev"]
            ].sum().reset_index()

            sc_total = sc_sub["total_fleet"].clip(lower=1)
            sc_pct = {
                pt: 100 * sc_sub[f"stock_{pt}"] / sc_total
                for pt in pt_order
            }

            ax_s.stackplot(
                sc_sub["year"],
                *[sc_pct[pt] for pt in pt_order],
                labels=[pt_labels[pt] for pt in pt_order],
                colors=[_POWERTRAIN_COLORS[pt] for pt in pt_order],
                alpha=0.88,
            )
            ax_s.set_ylim(0, 100)
            ax_s.set_xlim(sc_sub["year"].min(), sc_sub["year"].max())
            ax_s.set_title(
                _SC_TITLES[sc],
                fontsize=9, fontweight="bold",
                color=_SCENARIO_COLORS[sc],
            )
            ax_s.set_ylabel("Share (%)", fontsize=7)
            ax_s.yaxis.grid(True, alpha=0.25)
            ax_s.set_facecolor("#FAFAFA")
            ax_s.spines[["top", "right"]].set_visible(False)
            ax_s.tick_params(axis="both", labelsize=7)

            if sc == "conservative":
                ax_s.set_xlabel("Year", fontsize=8)
            else:
                ax_s.set_xticklabels([])

            # 2050 BEV annotation — bottom-right inside the BEV band
            last = sc_sub[sc_sub.year == sc_sub.year.max()]
            if not last.empty:
                last_total = max(float(last["total_fleet"].values[0]), 1)
                bev_pct_2050 = 100 * float(last["stock_bev"].values[0]) / last_total
                # Place inside BEV band: cumulative at bottom of BEV layer
                ice_p  = 100 * float(last["stock_ice"].values[0])  / last_total
                hev_p  = 100 * float(last["stock_hev"].values[0])  / last_total
                phev_p = 100 * float(last["stock_phev"].values[0]) / last_total
                bev_bottom = ice_p + hev_p + phev_p
                bev_mid    = bev_bottom + bev_pct_2050 / 2
                text_y = max(3.0, min(97.0, bev_mid))
                ax_s.text(
                    sc_sub["year"].max(), text_y,
                    f"BEV: {bev_pct_2050:.0f}%",
                    ha="right", va="center",
                    fontsize=6.5, color="white", fontweight="bold",
                    path_effects=outline_fx,
                    clip_on=True,
                )

        handles, labels_leg = scenario_axes[0].get_legend_handles_labels()
        scenario_axes[0].legend(
            handles, labels_leg,
            fontsize=6.5, loc="upper right",
            framealpha=0.85, ncol=5,
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(out_path), bbox_inches="tight", dpi=150)
        plt.close(fig)

    def _plot_renewable_need(
        self,
        ts_df:        pd.DataFrame,
        country_name: str,
        code:         str,
        out_path:     Path,
    ) -> None:
        """
        2×2 panel:
          [0,0] BEV+PHEV electricity demand (TWh/yr)
          [0,1] Required RE capacity — cumulative (GW)
          [1,0] Annual RE capex (USD bn/yr)
          [1,1] Abatement cost (USD/tCO2 avoided) vs year
        """
        fig, axes = plt.subplots(2, 2, figsize=(14, 10), dpi=150)
        fig.patch.set_facecolor("#F5F5F5")
        fig.suptitle(
            f"Renewable Energy Requirements for Transport Electrification"
            f" — {country_name}",
            fontsize=12, fontweight="bold",
        )

        re_avail = {
            "solar": (float(ts_df["re_avail_solar_gw"].iloc[0])
                      if "re_avail_solar_gw" in ts_df.columns else 0.0),
            "wind":  (float(ts_df["re_avail_wind_gw"].iloc[0])
                      if "re_avail_wind_gw" in ts_df.columns else 0.0),
        }

        for sc, color in _SCENARIO_COLORS.items():
            sub = ts_df[ts_df.scenario == sc].sort_values("year")
            if sub.empty:
                continue
            label = sc.capitalize()

            # [0,0] Electricity demand
            axes[0, 0].plot(sub["year"], sub["ev_electricity_demand_twh"],
                            color=color, label=label, linewidth=2)

            # [0,1] Cumulative RE capacity needed
            axes[0, 1].plot(sub["year"], sub["re_needed_total_gw"],
                            color=color, label=label, linewidth=2)

            # [1,0] Annual capex (year vs USD bn)
            if "annual_capex_bn_usd" in sub.columns:
                axes[1, 0].plot(sub["year"], sub["annual_capex_bn_usd"],
                                color=color, label=label, linewidth=2)

            # [1,1] Abatement cost vs YEAR (not vs itself — fix from v1)
            if "abatement_cost_usd_tco2" in sub.columns:
                abat = sub["abatement_cost_usd_tco2"]
                # Show positive abatement cost only (negative = fuel savings exceed capex)
                axes[1, 1].plot(sub["year"], abat,
                                color=color, label=label, linewidth=2)
                # Shade region where savings exceed capex (net-negative abatement)
                axes[1, 1].fill_between(
                    sub["year"], abat, 0,
                    where=(abat < 0),
                    color=color, alpha=0.15,
                    label=f"{label}: savings > capex",
                )

        # RE potential reference lines
        for tech, clr in [("solar", _TECH_RE_COLORS["solar"]), ("wind", _TECH_RE_COLORS["wind"])]:
            if re_avail[tech] > 0:
                axes[0, 1].axhline(
                    re_avail[tech], color=clr, linestyle=":", linewidth=1.5,
                    label=f"{tech.capitalize()} potential ({re_avail[tech]:.0f} GW)",
                )

        titles   = [
            "BEV+PHEV Electricity Demand",
            "Required RE Capacity (Cumulative)",
            "Annual RE Investment Required",
            "Abatement Cost (Net capex / CO₂ avoided)",
        ]
        ylabels  = [
            "TWh/yr",
            "GW installed",
            "USD Billion/yr",
            "USD/tCO₂ avoided",
        ]
        for ax, title, ylabel in zip(axes.flat, titles, ylabels):
            ax.set_title(title, fontsize=10, fontweight="bold")
            ax.set_ylabel(ylabel, fontsize=9)
            ax.set_xlabel("Year", fontsize=9)
            ax.legend(fontsize=7)
            ax.yaxis.grid(True, alpha=0.3)
            ax.set_facecolor("#FAFAFA")
            ax.spines[["top", "right"]].set_visible(False)

        # Zero line on abatement cost panel for clarity
        axes[1, 1].axhline(0, color="#888888", linewidth=0.8, linestyle="--")
        axes[1, 1].set_ylim(
            min(-5, axes[1, 1].get_ylim()[0]),
            max(5, axes[1, 1].get_ylim()[1]),
        )

        axes[0, 1].legend(fontsize=6)

        plt.tight_layout(rect=[0, 0, 1, 0.95])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(out_path), bbox_inches="tight", dpi=150)
        plt.close(fig)

    def _plot_hub_map(
    self,
    hubs_gdf: gpd.GeoDataFrame,
    mainland_gdf: gpd.GeoDataFrame,
    context_gdf: Optional[gpd.GeoDataFrame],
    country_name: str,
    code: str,
    suitability_dir: Optional[Path],
    out_path: Path,
    ) -> None:
        """
        Map of charging hub candidates.
        • Hub colour  → primary RE source (Solar = amber, Wind = steel-blue)
        • Hub size    → suitability score (larger = higher score)
        • Background  → solar suitability raster underlay
        """
        import matplotlib.patches as mpatches

        _HUB_SOURCE_COLORS = {
            "Solar":   "#C62828",   # deep red — clearly distinct from the amber/pink raster
            "Wind":    "#0D47A1",   # deep blue
            "Biomass": "#4A148C",   # deep purple
        }
        _HUB_SOURCE_EDGE = {
            "Solar":   "#FFCDD2",   # light pink edge → makes red dots pop
            "Wind":    "#BBDEFB",   # light blue edge
            "Biomass": "#E1BEE7",   # light purple edge
        }

        # ── Load suitability raster ────────────────────────────────────────────
        suit_arr  = None
        transform = None
        crs       = None
        extent    = None

        if _HAS_RASTERIO and suitability_dir is not None:
            tif_cands = sorted(Path(suitability_dir).glob(f"{code}_solar_suitability*.tif"))
            if not tif_cands:
                tif_cands = sorted(Path(suitability_dir).glob("*.tif"))
            if tif_cands:
                try:
                    with rasterio.open(tif_cands[0]) as src:
                        suit_arr = src.read(1).astype(np.float32)
                        transform = src.transform
                        crs = str(src.crs)
                        bounds = src.bounds
                        nd = src.nodata if src.nodata is not None else -9999.0
                    suit_arr[suit_arr == nd] = np.nan
                    H, W = suit_arr.shape
                    extent = [
                        transform.c,
                        transform.c + transform.a * W,
                        transform.f + transform.e * H,
                        transform.f,
                    ]
                except Exception as exc:
                    logger.debug("Could not load suitability raster: %s", exc)

        # ── Figure setup (GeoWorldStyler) ──────────────────────────────────────
        vb = mainland_gdf.total_bounds
        fig, ax = self.styler.create_figure(
            vb[0], vb[2], vb[1], vb[3], right_in_override=1.80
        )
        self.styler.draw_basemap(
            ax, crs or "EPSG:4326", mainland_gdf, context_gdf,
            self._admin_gdf, extent=extent
        )

        # ── Suitability underlay ───────────────────────────────────────────────
        suitability_im = None
        if suit_arr is not None and extent is not None:
            cmap = self.styler.make_cmap(
                "YlOrRd", bad="none", under="#AAAAAA",
                vmin_frac=0.08, vmax_frac=1.0
            )
            suitability_im = ax.imshow(
                np.where(np.isfinite(suit_arr) & (suit_arr > 0), suit_arr, np.nan),
                extent=extent, origin="upper", cmap=cmap,
                vmin=0, vmax=1.0, alpha=0.50, zorder=2,
                interpolation="bilinear",
            )

        # ── Hub scatter — coloured by primary_source ───────────────────────────
        minx, miny, maxx, maxy = vb
        hubs_in = hubs_gdf[
            (hubs_gdf.geometry.x >= minx) &
            (hubs_gdf.geometry.x <= maxx) &
            (hubs_gdf.geometry.y >= miny) &
            (hubs_gdf.geometry.y <= maxy)
        ].copy()

        if not hubs_in.empty:
            # Use primary_source column if available; default Solar
            if "primary_source" not in hubs_in.columns:
                hubs_in["primary_source"] = "Solar"

            for src_label, grp in hubs_in.groupby("primary_source"):
                clr  = _HUB_SOURCE_COLORS.get(src_label, "#888888")
                eclr = _HUB_SOURCE_EDGE.get(src_label, "#444444")
                sizes = np.clip(grp["suitability_score"].values * 80, 12, 85)
                ax.scatter(
                    grp.geometry.x, grp.geometry.y,
                    s=sizes, c=clr, alpha=0.88,
                    edgecolors=eclr, linewidth=0.7,
                    label=f"{src_label} hubs (n={len(grp):,})",
                    zorder=5,
                )

        # ── Decorations & colorbar ─────────────────────────────────────────────
        self.styler.add_decorations(ax, vb[0], vb[2], vb[1], vb[3])
        if suitability_im is not None:
            self.styler.add_colorbar(
                fig, suitability_im, "Suitability Score (0–1)", extend="neither"
            )
        if self._admin_gdf is not None:
            self.styler.draw_admin_labels(
                ax, self._admin_gdf, vb[0], vb[2], vb[1], vb[3]
            )

        # ── Legend — one entry per source, placed INSIDE the map ──────────────
        if not hubs_in.empty:
            hub_radius = float(hubs_gdf["hub_radius_km"].iloc[0])
            legend_handles = []
            for src_label in sorted(hubs_in["primary_source"].unique()):
                n_src = (hubs_in["primary_source"] == src_label).sum()
                clr   = _HUB_SOURCE_COLORS.get(src_label, "#888888")
                eclr  = _HUB_SOURCE_EDGE.get(src_label, "#FFFFFF")
                legend_handles.append(
                    mpatches.Patch(
                        facecolor=clr, edgecolor=eclr, linewidth=0.8,
                        label=f"{src_label} (n={n_src:,}, ≥{hub_radius:.0f} km)",
                    )
                )
            ax.legend(
                handles=legend_handles,
                loc="lower center",
                bbox_to_anchor=(0.5, 0.01),
                ncol=min(3, len(legend_handles)),
                fontsize=8,
                framealpha=0.90,
                frameon=True,
                facecolor="white",
                edgecolor="#CCCCCC",
                borderpad=0.6,
                handlelength=1.2,
            )

        # ── Source breakdown counts ────────────────────────────────────────────
        src_counts = {}
        if not hubs_in.empty and "primary_source" in hubs_in.columns:
            src_counts = hubs_in["primary_source"].value_counts().to_dict()
        src_str = " | ".join(
            f"{k}: {v:,}" for k, v in sorted(src_counts.items())
        ) if src_counts else ""

        self.styler.add_standard_title(
            fig,
            title_main="EV Charging Hub Network — 2050 Horizon",
            title_sub=(
                f"{country_name}  |  Light Vehicles Only  |  "
                f"Coloured by RE Source  |  Sized by Suitability Score"
            ),
        )
        hub_stats = (
            f"Total hubs: {len(hubs_in):,}  |  {src_str}  |  "
            f"Mean suitability: {hubs_in['suitability_score'].mean():.3f}  |  "
            f"Hub spacing: ≥{hubs_gdf['hub_radius_km'].iloc[0]:.0f} km  |  "
            f"Total capex: USD {len(hubs_in) * float(hubs_gdf['capex_usd'].iloc[0]) / 1e9:.2f}B"
        )
        self.styler.add_standard_footer(
            fig,
            stats_text=hub_stats,
            crs_metadata=f"CRS: {crs or 'EPSG:4326'}",
        )
        self.styler.save(fig, out_path)
    # ─────────────────────────────────────────────────────────────────────
    # Text report
    # ─────────────────────────────────────────────────────────────────────

    def _format_report(
        self,
        code:         str,
        country_name: str,
        ts_df:        pd.DataFrame,
        fleet_df:     pd.DataFrame,
        hubs_gdf:     Optional[gpd.GeoDataFrame],
        summary:      Dict[str, Any],
        years:        List[int],
    ) -> str:
        sep   = "=" * 72
        dash  = "-" * 72
        sdash = "-" * 52
        now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        country_tp = self._tp.get("countries", {}).get(code, {})
        defaults   = self._tp["global_defaults"]

        lines = [
            sep,
            "  TRANSPORT DECARBONISATION REPORT",
            "  Energy Transition Pathways — Clean Electrification & Decarbonisation",
            f"  {country_name} ({code})",
            f"  Generated: {now}",
            "  Powertrain model: ICE · HEV · PHEV · BEV · FCEV",
            sep, "",
            "  EXECUTIVE SUMMARY",
            sdash,
        ]

        re_avail = summary.get("renewable_potential_available_gw", {})
        for sc, sc_data in summary.get("scenarios", {}).items():
            lines.append(
                f"  [{sc.upper():<12}] "
                f"2050 EV demand: "
                f"{sc_data.get('ev_electricity_demand_2050_twh', 0):.1f} TWh | "
                f"RE needed: {sc_data.get('re_needed_total_gw_2050', 0):.1f} GW | "
                f"GHG avoided (cumul.): "
                f"{sc_data.get('co2_avoided_cumulative_mtco2eq', 0):.0f} MtCO2 | "
                f"Capex: USD "
                f"{sc_data.get('cumulative_capex_2050_bn_usd', 0):.1f} bn"
            )
            lines.append(
                f"  {'':14}  Fleet 2050: "
                f"ICE {sc_data.get('fleet_2050_ice_pct',  0):.1f}% | "
                f"HEV {sc_data.get('fleet_2050_hev_pct',  0):.1f}% | "
                f"PHEV {sc_data.get('fleet_2050_phev_pct', 0):.1f}% | "
                f"BEV {sc_data.get('fleet_2050_bev_pct',  0):.1f}%"
            )

        lines += [
            "",
            "  RENEWABLE ENERGY AVAILABILITY (Phase 4)",
            sdash,
            f"  Solar  : {re_avail.get('solar',   0):.1f} GW",
            f"  Wind   : {re_avail.get('wind',    0):.1f} GW",
            f"  Biomass: {re_avail.get('biomass', 0):.1f} GW",
            f"  TOTAL  : {sum(re_avail.values()):.1f} GW",
            "",
        ]

        # ── Parameters ──────────────────────────────────────────────────
        fleet_size = int(country_tp.get("fleet_size_2024", 0))
        growth     = float(country_tp.get(
            "annual_fleet_growth_rate",
            defaults["fleet_growth"]["annual_growth_rate_default"]
        )) * 100
        hub_r = float(country_tp.get(
            "hub_radius_km",
            defaults["charging_infrastructure"]["hub_radius_km"]
        ))
        lines += [
            sep,
            "  PARAMETERS USED",
            dash,
            f"  Fleet size (2024)  : {fleet_size:,} vehicles",
            f"  Annual fleet growth: {growth:.1f}%/yr",
            f"  Projection         : {years[0]} – {years[-1]}",
            f"  Hub spacing        : {hub_r:.0f} km",
            f"  Biodiesel blend    : {country_tp.get('biodiesel_blend', 'B7')}",
            f"  Ethanol blend      : {country_tp.get('ethanol_blend',   'E10')}",
            f"  PHEV electric frac : "
            f"{_PHEV_ELECTRIC_FRACTION * 100:.0f}% of km driven electrically",
            "",
            "  Grid emission intensity (gCO2/kWh):",
        ]
        grid_anchors = _safe_anchors(country_tp.get(
            "grid_emission_intensity_gco2_kwh",
            defaults["grid_emission_intensity_defaults"]
        ))
        for yr, val in sorted(grid_anchors.items()):
            lines.append(f"    {yr}: {val:.0f}")

        # Powertrain penetration table (reference scenario)
        pt_override = country_tp.get("powertrain_penetration_override", {})
        sc_src      = (
            pt_override.get("reference", {}) or
            defaults["powertrain_penetration_scenarios"].get("reference", {})
        )
        lines += [
            "",
            "  Powertrain penetration — new sales (reference scenario):",
            f"  {'Year':>4}  {'ICE':>6}  {'HEV':>6}  {'PHEV':>6}"
            f"  {'BEV':>6}  {'FCEV':>6}",
            "  " + "-" * 40,
        ]
        for milestone in (2025, 2030, 2035, 2040, 2045, 2050):
            yr_key = str(milestone)
            if yr_key in sc_src and isinstance(sc_src[yr_key], dict):
                pt = sc_src[yr_key]
                lines.append(
                    f"  {milestone:>4}  "
                    f"{pt.get('ice',  0) * 100:>5.1f}%"
                    f"  {pt.get('hev',  0) * 100:>5.1f}%"
                    f"  {pt.get('phev', 0) * 100:>5.1f}%"
                    f"  {pt.get('bev',  0) * 100:>5.1f}%"
                    f"  {pt.get('fcev', 0) * 100:>5.1f}%"
                )

        # ── Annual timeseries snapshot ───────────────────────────────────
        lines += [
            "",
            sep,
            "  ANNUAL TIMESERIES — REFERENCE SCENARIO",
            dash,
            f"  {'Year':>4}  {'Fleet(M)':>8}  {'EV TWh':>8}  {'RE GW':>7}"
            f"  {'CO2 Mt':>8}  {'Avoid Mt':>9}  {'Capex bn$':>10}",
            "  " + "-" * 62,
        ]
        sc_df = ts_df[ts_df.scenario == "reference"].sort_values("year")
        for y in [yr for yr in (2025, 2030, 2035, 2040, 2045, 2050)
                  if yr in sc_df["year"].values]:
            row = sc_df[sc_df.year == y].iloc[0]
            lines.append(
                f"  {y:>4}  "
                f"{row.get('total_fleet_all_cats', 0) / 1e6:>8.2f}  "
                f"{row.get('ev_electricity_demand_twh', 0):>8.2f}  "
                f"{row.get('re_needed_total_gw', 0):>7.2f}  "
                f"{row.get('co2_total_mtco2eq', 0):>8.3f}  "
                f"{row.get('co2_avoided_mtco2eq', 0):>9.3f}  "
                f"{row.get('annual_capex_bn_usd', 0):>10.3f}"
            )

        # ── Scenario comparison 2050 ─────────────────────────────────────
        lines += [
            "",
            sep,
            "  SCENARIO COMPARISON — 2050",
            dash,
            f"  {'Scenario':<14}  {'EV TWh':>7}  {'RE GW':>6}  "
            f"{'CO2 Mt':>7}  {'Avoided':>8}  {'Capex bn$':>10}  {'Cover':>12}",
            "  " + "-" * 70,
        ]
        for sc in ("reference", "accelerated", "conservative"):
            sd = summary.get("scenarios", {}).get(sc, {})
            lines.append(
                f"  {sc.capitalize():<14}  "
                f"{sd.get('ev_electricity_demand_2050_twh', 0):>7.1f}  "
                f"{sd.get('re_needed_total_gw_2050', 0):>6.1f}  "
                f"{sd.get('co2_total_2050_mtco2eq', 0):>7.3f}  "
                f"{sd.get('co2_avoided_cumulative_mtco2eq', 0):>8.0f}  "
                f"{sd.get('cumulative_capex_2050_bn_usd', 0):>10.1f}  "
                f"{sd.get('ev_demand_vs_re_potential', 'N/A'):>12}"
            )

        # ── Fleet composition 2050 ───────────────────────────────────────
        lines += [
            "",
            sep,
            "  FLEET COMPOSITION — 2050",
            dash,
            f"  {'Scenario':<14}  {'Total(M)':>8}  {'ICE%':>6}  "
            f"{'HEV%':>6}  {'PHEV%':>6}  {'BEV%':>6}",
            "  " + "-" * 56,
        ]
        for sc in ("reference", "accelerated", "conservative"):
            sd = summary.get("scenarios", {}).get(sc, {})
            total_m = sd.get("fleet_2050_total", 0) / 1e6
            lines.append(
                f"  {sc.capitalize():<14}  "
                f"{total_m:>8.2f}  "
                f"{sd.get('fleet_2050_ice_pct',  0):>6.1f}  "
                f"{sd.get('fleet_2050_hev_pct',  0):>6.1f}  "
                f"{sd.get('fleet_2050_phev_pct', 0):>6.1f}  "
                f"{sd.get('fleet_2050_bev_pct',  0):>6.1f}"
            )

        # ── Vehicle count by powertrain — absolute numbers (millions) ─────────
        lines += [
            "",
            sep,
            "  FLEET BY POWERTRAIN — ABSOLUTE STOCK (Millions of vehicles)",
            dash,
            f"  Reference scenario — stock at milestone years",
            f"  {'Year':>4}  {'Total':>8}  {'ICE':>8}  {'HEV':>8}  "
            f"{'PHEV':>8}  {'BEV':>8}  {'FCEV':>7}",
            "  " + "-" * 62,
        ]
        ref_fleet = fleet_df[fleet_df.scenario == "reference"]
        for y in [yr for yr in (2025, 2030, 2035, 2040, 2045, 2050)
                  if yr in ref_fleet["year"].values]:
            yr_df = ref_fleet[ref_fleet.year == y]
            total  = yr_df["total_fleet"].sum() / 1e6
            ice    = yr_df["stock_ice"].sum()   / 1e6
            hev    = yr_df["stock_hev"].sum()   / 1e6
            phev   = yr_df["stock_phev"].sum()  / 1e6
            bev    = yr_df["stock_bev"].sum()   / 1e6
            fcev   = yr_df["stock_fcev"].sum()  / 1e6
            lines.append(
                f"  {y:>4}  {total:>8.2f}  {ice:>8.2f}  {hev:>8.2f}  "
                f"{phev:>8.2f}  {bev:>8.2f}  {fcev:>7.3f}"
            )

        lines += [
            "",
            f"  Accelerated scenario — stock at milestone years",
            f"  {'Year':>4}  {'Total':>8}  {'ICE':>8}  {'HEV':>8}  "
            f"{'PHEV':>8}  {'BEV':>8}  {'FCEV':>7}",
            "  " + "-" * 62,
        ]
        acc_fleet = fleet_df[fleet_df.scenario == "accelerated"]
        for y in [yr for yr in (2025, 2030, 2035, 2040, 2045, 2050)
                  if yr in acc_fleet["year"].values]:
            yr_df = acc_fleet[acc_fleet.year == y]
            total  = yr_df["total_fleet"].sum() / 1e6
            ice    = yr_df["stock_ice"].sum()   / 1e6
            hev    = yr_df["stock_hev"].sum()   / 1e6
            phev   = yr_df["stock_phev"].sum()  / 1e6
            bev    = yr_df["stock_bev"].sum()   / 1e6
            fcev   = yr_df["stock_fcev"].sum()  / 1e6
            lines.append(
                f"  {y:>4}  {total:>8.2f}  {ice:>8.2f}  {hev:>8.2f}  "
                f"{phev:>8.2f}  {bev:>8.2f}  {fcev:>7.3f}"
            )

        # ── Charging infrastructure ──────────────────────────────────────
        n_hubs           = summary.get("charging_hubs", {}).get("total", 0)
        total_capex_hubs = summary.get("charging_hubs", {}).get(
            "total_capex_bn_usd", 0.0
        )
        infra = defaults["charging_infrastructure"]
        lines += [
            "",
            sep,
            "  CHARGING INFRASTRUCTURE",
            dash,
            f"  Candidate hub locations    : {n_hubs:,}",
            f"  Hub spacing (min)          : {hub_r:.0f} km",
            f"  Chargers per hub           : {infra['hub_min_chargers']}",
            f"  Hub peak power             : {infra['hub_power_mw']:.1f} MW",
            f"  Hub unit capex             : USD {infra['hub_capex_usd']:,.0f}",
            f"  Total hub infrastructure   : USD {total_capex_hubs:.2f} bn",
            f"  Total peak power (fleet)   : "
            f"{summary.get('charging_hubs', {}).get('total_peak_power_mw', 0):.0f} MW",
        ]

        # ── Methodology ──────────────────────────────────────────────────
        lines += [
            "",
            sep,
            "  METHODOLOGY NOTES",
            dash,
            "  Fleet model    : Stock-turnover per powertrain × vehicle category.",
            "  Powertrain mix : Linear interpolation between anchor year shares.",
            "  Grid demand    : BEV full + PHEV partial (utility factor "
            f"{_PHEV_ELECTRIC_FRACTION*100:.0f}%).",
            "  RE requirement : EV demand × 65% solar / 35% wind (adj. CFs).",
            "  GHG — ICE/HEV  : Tailpipe; weighted by dominant fuel mix.",
            "  GHG — PHEV     : Blend of tailpipe (combustion) + grid indirect.",
            "  GHG — BEV      : Grid indirect only (tailpipe zero).",
            "  GHG — FCEV     : Zero (green hydrogen assumed).",
            "  Grid intensity : Country trajectory, linearly interpolated.",
            "  Hub placement  : Greedy max-score, minimum spacing constraint.",
            "  Cost model     : Delta-capex/yr; fuel-savings offset.",
            "",
            "  Key references:",
            "    [IEA-EV2024]      IEA Global EV Outlook 2024",
            "    [IPCC2022]        IPCC AR6 WG3 Chapter 10 — Transport",
            "    [IRENA2024t]      IRENA Electrification with Renewables 2024",
            "    [BloombergNEF2024] BNEF Electric Vehicle Outlook 2024",
            "",
            sep,
        ]

        return "\n".join(lines)