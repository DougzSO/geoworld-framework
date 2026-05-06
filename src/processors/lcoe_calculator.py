"""
src/processors/lcoe_calculator.py
==================================
Phase 5: Spatial-Economic Levelised Cost of Energy (LCOE) Modelling.

Projects techno-economic parameters (CAPEX, OPEX, WACC) onto spatial
geographic matrices, modifying the baseline operational yield via
pixel-by-pixel environmental resource variability.

Mathematical Formulation
------------------------
1. Capital Recovery Factor (CRF):
   Annualises total upfront capital expenditure over asset lifetime.

   CRF = r * (1+r)^n / ((1+r)^n - 1)

2. Spatial Capacity Factor Adjustment:
   Modulates national CF via localised geographic resource variations.

   CF_local(x,y) = CF_base * (R(x,y) / R_mean_suitable)

3. Levelised Cost of Energy (LCOE):

   LCOE(x,y) = (CAPEX * CRF + OPEX_fixed) / (CF_local(x,y) * 8760) * 1000

References
----------
IRENA (2024). Renewable Power Generation Costs in 2023.
Camargo & Valente (2022). Spatial resolution implications on LCOE
    methodologies. Energies.
"""

from __future__ import annotations

import logging
import time
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, Optional

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import Affine

from src.core.constants import (
    CF_ABS_CEILING,
    DEFAULT_TECH_PARAMS,
    LCOE_BENCHMARK_USD_MWH,
    TECH_ORDER,
    build_tech_params,
)
from src.processors.potential_calculator import (
    build_pixel_area_array,
    zonal_stats_raster,
)
from src.utils.map_styling import GeoWorldStyler

logger = logging.getLogger("geoworld.processors.LCOECalculator")


# ===========================================================================
# Baseline LCOE Parameters (IRENA 2024 Tier 3 Fallbacks)
# ===========================================================================

DEFAULT_LCOE_PARAMS: Dict[str, Dict] = {
    "solar": {
        "capex_usd_kw": 760.0,
        "opex_usd_kw_yr": 13.0,
        "lifetime_years": 25,
        "discount_rate": 0.060,
        "color": "#F9A825",
        "label": "Solar PV",
        "resource_stem": "solar_resource",
    },
    "wind": {
        "capex_usd_kw": 1360.0,
        "opex_usd_kw_yr": 44.0,
        "lifetime_years": 25,
        "discount_rate": 0.060,
        "color": "#1565C0",
        "label": "Wind Onshore",
        "resource_stem": "wind_resource",
    },
    "biomass": {
        "capex_usd_kw": 2720.0,
        "opex_usd_kw_yr": 109.0,
        "lifetime_years": 20,
        "discount_rate": 0.070,
        "color": "#2E7D32",
        "label": "Biomass / Bioenergy",
        "resource_stem": "biomass_resource",
    },
}


# ===========================================================================
# Core Mathematical Functions
# ===========================================================================

@contextmanager
def _timer(
    label: str,
    timings: Dict[str, float],
) -> Generator[None, None, None]:
    """
    Context manager that measures execution time per technology phase.

    Args:
        label: Stage label for logging and storage
        timings: Dictionary to store elapsed time values
    """
    t0 = time.perf_counter()
    logger.info("  [%s] starting...", label)
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        timings[label] = round(elapsed, 2)
        logger.info("  [%s] completed in %.1fs", label, elapsed)


def capital_recovery_factor(rate: float, lifetime: int) -> float:
    """
    Compute the Capital Recovery Factor (CRF).

    Maps total upfront CAPEX to an equivalent annualised cost series.

    Args:
        rate: Annual discount rate (e.g., 0.06 for 6%)
        lifetime: Asset economic lifetime in years

    Returns:
        CRF value (dimensionless)
    """
    if rate <= 0.0:
        return 1.0 / lifetime
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
    Compute the Levelized Cost of Energy (LCOE) for a single pixel.

    Args:
        capex_usd_kw: Capital expenditure in USD per kW
        opex_usd_kw_yr: Annual fixed operating cost in USD per kW per year
        crf: Capital Recovery Factor (dimensionless)
        capacity_factor: Capacity factor in [0, 1]
        hours_year: Hours per year (default: 8760)

    Returns:
        LCOE in USD per MWh, or NaN if capacity factor is zero
    """
    if capacity_factor <= 0.0:
        return np.nan
    annual_cost_kw = (capex_usd_kw * crf) + opex_usd_kw_yr
    annual_gen_h = capacity_factor * hours_year
    return (annual_cost_kw / annual_gen_h) * 1000.0


# ===========================================================================
# Main Class
# ===========================================================================

class LCOECalculator:
    """
    Phase 5 spatial-economic LCOE solver.

    Computes pixel-wise LCOE distributions by combining techno-economic
    parameters (CAPEX, OPEX, discount rate) with spatially-varying
    capacity factors derived from resource rasters.

    Args:
        cfg: ConfigLoader instance
        outputs_dir: Base directory for all pipeline outputs
    """

    def __init__(self, cfg: Any, outputs_dir: Path):
        """
        Initialize LCOECalculator.

        Args:
            cfg: ConfigLoader instance
            outputs_dir: Base directory for all pipeline outputs
        """
        self.cfg = cfg
        self.outputs_dir = Path(outputs_dir)
        self.viz_cfg = cfg.system.get("visualization", {})
        self._admin_gdf: Optional[gpd.GeoDataFrame] = None

        self.tech_params = self._load_tech_params()
        self.pot_params = {k: dict(v) for k, v in DEFAULT_TECH_PARAMS.items()}

        pipeline_dpi = (
            cfg.system.get("pipeline", {}).get("map_dpi_export", 150)
        )
        self.styler = GeoWorldStyler(self.viz_cfg, global_dpi=pipeline_dpi)

    def _load_tech_params(
        self,
        country_code: Optional[str] = None,
    ) -> Dict[str, Dict]:
        """
        Merge global default LCOE parameters with country-specific overrides.

        Priority: country parameters > settings.yaml > DEFAULT_LCOE_PARAMS.

        Args:
            country_code: ISO-3166-alpha-3 code for country overrides
                          (uses global defaults if None)

        Returns:
            Dictionary of merged LCOE parameters per technology
        """
        params = {k: dict(v) for k, v in DEFAULT_LCOE_PARAMS.items()}

        lcoe_yaml = (
            self.cfg.system.get("lcoe", {}).get("technologies", {})
        )
        for tech in params:
            tc = lcoe_yaml.get(tech, {})
            for key in ("capex_usd_kw", "opex_usd_kw_yr", "discount_rate"):
                if key in tc:
                    params[tech][key] = float(tc[key])
            if "lifetime_years" in tc:
                params[tech]["lifetime_years"] = int(tc["lifetime_years"])

        if country_code:
            try:
                country_lcoe = self.cfg.get_lcoe_params(country_code)
                for tech in params:
                    tc = country_lcoe.get(tech, {})
                    for key in (
                        "capex_usd_kw",
                        "opex_usd_kw_yr",
                        "discount_rate",
                    ):
                        if key in tc:
                            params[tech][key] = float(tc[key])
                    if "lifetime_years" in tc:
                        params[tech]["lifetime_years"] = int(
                            tc["lifetime_years"]
                        )

                logger.info(
                    "  [LCOE Params] Country parameters loaded for %s.",
                    country_code,
                )
            except Exception as exc:
                logger.warning(
                    "  [LCOE Params] Using global defaults for %s: %s",
                    country_code,
                    exc,
                )

        return params

    def _load_pot_params_from_country(
        self,
        country_params: Optional[Dict],
    ) -> Dict[str, Dict]:
        """
        Extract physical technology parameters from country configuration.

        Retrieves land use factor (LUF) and capacity factor (CF) set
        during Phase 4 to ensure consistent physical assumptions.

        Args:
            country_params: Country parameter dict from ConfigLoader
                            (None uses global defaults)

        Returns:
            Dictionary of physical technology parameters per technology
        """
        params = build_tech_params(self.cfg.system, country_params)
        logger.info(
            "  [pot_params] Physical constants: Solar LUF=%.2f | "
            "Wind CF=%.3f",
            params["solar"]["land_use_factor"],
            params["wind"]["capacity_factor"],
        )
        return params

    def _find_resource_tif(
        self,
        criteria_dir: Path,
        tech: str,
        country_code: str,
    ) -> Optional[Path]:
        """
        Locate the resource raster for a given technology.

        Searches for files by expected name patterns, then falls back
        to a glob search if the expected name is not found.

        Args:
            criteria_dir: Directory containing criteria rasters
            tech: Technology name (solar, wind, biomass)
            country_code: ISO-3166-alpha-3 code

        Returns:
            Path to the resource raster if found, None otherwise
        """
        stem = DEFAULT_LCOE_PARAMS[tech].get(
            "resource_stem", f"{tech}_resource"
        )
        candidates = [
            criteria_dir / f"{country_code.upper()}_{stem}.tif",
            criteria_dir / f"{country_code.lower()}_{stem}.tif",
            criteria_dir / f"{stem}.tif",
        ]
        for path in candidates:
            if path.exists():
                return path
        matches = sorted(criteria_dir.glob(f"*{stem}*.tif"))
        return matches[0] if matches else None

    # -----------------------------------------------------------------------
    # Main Entry Point
    # -----------------------------------------------------------------------

    def run(
        self,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        suitability_dir: Path,
        criteria_dir: Path,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        country_params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Run Phase 5 LCOE computation for all technologies.

        For each technology, loads suitability and resource rasters,
        computes pixel-wise LCOE, builds supply curves, and generates
        maps and zonal statistics.

        Args:
            country_code: ISO-3166-alpha-3 code
            country_name: Full country name
            mainland_gdf: Mainland geometry GeoDataFrame
            suitability_dir: Directory with Phase 3 suitability TIFs
            criteria_dir: Directory with Phase 2b criteria TIFs
            context_gdf: Neighbouring countries GeoDataFrame (optional)
            country_params: Country parameter dict from ConfigLoader

        Returns:
            Results dictionary with per-technology stats, supply curves,
            and LCOE arrays
        """
        self.tech_params = self._load_tech_params(country_code)
        self.pot_params = self._load_pot_params_from_country(
            country_params
        )

        started_at = datetime.now()
        timings: Dict[str, float] = {}

        base = self.outputs_dir / country_code / "lcoe"
        out_maps = base / "maps"
        out_data = base / "data"
        out_rep = base / "reports"

        for d in [out_maps, out_data, out_rep]:
            d.mkdir(parents=True, exist_ok=True)

        self._admin_gdf = self.styler.load_admin_boundaries(
            country_name, mainland_gdf, Path(self.cfg.raw_path)
        )
        if self._admin_gdf is not None:
            logger.info(
                "  Admin boundaries: %d regions loaded",
                len(self._admin_gdf),
            )

        results: Dict[str, Any] = {
            "country": country_code,
            "timestamp": started_at.isoformat(),
            "techs": {},
            "timings": timings,
        }

        for tech in TECH_ORDER:
            suit_path = (
                Path(suitability_dir)
                / f"{country_code}_{tech}_suitability.tif"
            )
            res_path = self._find_resource_tif(
                Path(criteria_dir), tech, country_code
            )

            if not suit_path.exists():
                logger.warning(
                    "  [%s] Suitability raster not found: %s",
                    tech,
                    suit_path.name,
                )
                continue

            logger.info(
                "\n  %s\n  Technology: %s\n  %s",
                "─" * 50,
                self.tech_params[tech]["label"],
                "─" * 50,
            )

            with _timer(tech, timings):
                results["techs"][tech] = self._run_technology(
                    tech,
                    suit_path,
                    res_path,
                    country_code,
                    country_name,
                    mainland_gdf,
                    context_gdf,
                    out_maps,
                    out_data,
                )

        with _timer("comparison_map", timings):
            self._plot_comparison(
                results,
                country_name,
                country_code,
                mainland_gdf,
                context_gdf,
                out_maps / f"{country_code}_lcoe_comparison.png",
            )

        results["elapsed_total"] = round(
            (datetime.now() - started_at).total_seconds(), 1
        )
        report_text = self._format_report(
            results, country_name, country_code
        )

        logger.info("\n%s", report_text)
        (
            out_rep
            / f"{country_code}_lcoe_"
            f"{started_at.strftime('%Y%m%d_%H%M%S')}.txt"
        ).write_text(report_text, encoding="utf-8")

        return results

    # -----------------------------------------------------------------------
    # Per-Technology Spatial Computation
    # -----------------------------------------------------------------------

    def _run_technology(
        self,
        tech: str,
        suit_path: Path,
        res_path: Optional[Path],
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        out_maps: Path,
        out_data: Path,
    ) -> Dict[str, Any]:
        """
        Compute spatial LCOE for a single technology.

        Adjusts the baseline capacity factor pixel-by-pixel using the
        resource raster (or suitability as fallback), then computes LCOE
        via the CRF formula and builds supply curves and zonal statistics.

        Args:
            tech: Technology name (solar, wind, biomass)
            suit_path: Path to suitability raster
            res_path: Path to resource raster (optional)
            country_code: ISO-3166-alpha-3 code
            country_name: Full country name
            mainland_gdf: Mainland geometry GeoDataFrame
            context_gdf: Neighbouring countries GeoDataFrame
            out_maps: Output directory for map figures
            out_data: Output directory for CSV data

        Returns:
            Dictionary with tech, stats, supply_curve, lcoe_map,
            transform, and crs
        """
        lp = self.tech_params[tech]
        pp = self.pot_params[tech]

        with rasterio.open(str(suit_path)) as src:
            suit_arr = src.read(1).astype(np.float32)
            transform = src.transform
            crs = str(src.crs)
            H, W = src.height, src.width

        res_arr = None
        if res_path is not None:
            with rasterio.open(str(res_path)) as src:
                raw = src.read(1).astype(np.float32)
                res_arr = np.where(raw >= 0, raw, np.nan)

        crf = capital_recovery_factor(
            lp["discount_rate"], lp["lifetime_years"]
        )
        base_cf = pp["capacity_factor"]
        base_lcoe = compute_lcoe(
            lp["capex_usd_kw"], lp["opex_usd_kw_yr"], crf, base_cf
        )

        apt_mask = (
            np.isfinite(suit_arr)
            & (suit_arr >= pp["thresholds"]["balanced"])
        )

        source_arr = (
            res_arr
            if (res_arr is not None and res_arr.shape == (H, W))
            else suit_arr
        )
        src_apt = source_arr[apt_mask & np.isfinite(source_arr)]
        src_mean = (
            float(np.nanmean(src_apt)) if src_apt.size > 0 else 1.0
        )
        src_mean = src_mean if src_mean > 0 else 1.0

        if tech == "biomass":
            cf_local = np.where(
                apt_mask, base_cf, np.nan
            ).astype(np.float32)
        else:
            cf_floor = base_cf * 0.40
            cf_local = np.where(
                apt_mask & np.isfinite(source_arr),
                np.clip(
                    base_cf * (source_arr / src_mean),
                    cf_floor,
                    min(CF_ABS_CEILING, base_cf * 1.80),
                ),
                np.nan,
            ).astype(np.float32)

        annual_cost = lp["capex_usd_kw"] * crf + lp["opex_usd_kw_yr"]
        lcoe_map = np.where(
            np.isfinite(cf_local) & (cf_local > 0),
            (annual_cost / (cf_local * 8760.0)) * 1000.0,
            np.nan,
        ).astype(np.float32)

        lcoe_valid = lcoe_map[
            np.isfinite(lcoe_map) & (lcoe_map > 0)
        ]
        if lcoe_valid.size > 0:
            stats = {
                "base_lcoe": round(base_lcoe, 2),
                "mean": round(float(np.mean(lcoe_valid)), 2),
                "p10": round(float(np.percentile(lcoe_valid, 10)), 2),
                "p25": round(float(np.percentile(lcoe_valid, 25)), 2),
                "median": round(float(np.median(lcoe_valid)), 2),
                "p75": round(float(np.percentile(lcoe_valid, 75)), 2),
                "p90": round(float(np.percentile(lcoe_valid, 90)), 2),
                "n_pixels": int(lcoe_valid.size),
                "crf": round(crf, 4),
                "base_cf": base_cf,
            }
        else:
            stats = {}

        pixel_area = build_pixel_area_array(transform, H, W)
        cap_map = np.where(
            np.isfinite(lcoe_map),
            pixel_area * pp["land_use_factor"] * pp["power_density_mw_km2"],
            np.nan,
        )
        supply_curve = self._compute_supply_curve(lcoe_map, cap_map)

        zonal_df = zonal_stats_raster(
            lcoe_map,
            (np.isfinite(lcoe_map) & (lcoe_map > 0)),
            self._admin_gdf,
            transform,
            crs,
            "lcoe",
        )
        if not zonal_df.empty:
            zonal_df.sort_values("lcoe_mean").to_csv(
                str(
                    out_data
                    / f"{country_code}_{tech}_lcoe_zonal.csv"
                ),
                index=False,
                encoding="utf-8",
            )

        self._plot_lcoe_map(
            lcoe_map,
            transform,
            crs,
            tech,
            stats,
            country_name,
            mainland_gdf,
            context_gdf,
            out_maps / f"{country_code}_{tech}_lcoe_map.png",
        )
        self._plot_supply_curve(
            supply_curve,
            tech,
            stats,
            country_name,
            out_maps / f"{country_code}_{tech}_supply_curve.png",
        )

        return {
            "tech": tech,
            "label": lp["label"],
            "params": lp,
            "stats": stats,
            "supply_curve": supply_curve,
            "lcoe_map": lcoe_map,
            "transform": transform,
            "crs": crs,
        }

    # -----------------------------------------------------------------------
    # Supply Curve (Merit Order)
    # -----------------------------------------------------------------------

    def _compute_supply_curve(
        self,
        lcoe_map: np.ndarray,
        cap_map: np.ndarray,
    ) -> pd.DataFrame:
        """
        Build the economic merit order supply curve.

        Sorts pixels by ascending LCOE and accumulates capacity to
        produce the resource cost curve.

        Args:
            lcoe_map: Pixel-wise LCOE array (USD/MWh)
            cap_map: Pixel-wise installable capacity array (MW)

        Returns:
            DataFrame with columns: lcoe_usd_mwh, capacity_mw,
            cum_capacity_gw
        """
        valid = (
            np.isfinite(lcoe_map)
            & np.isfinite(cap_map)
            & (cap_map > 0)
        )
        if valid.sum() == 0:
            return pd.DataFrame(
                columns=["lcoe_usd_mwh", "capacity_mw", "cum_capacity_gw"]
            )

        lcoe_vals = lcoe_map[valid]
        cap_vals = cap_map[valid]
        order = np.argsort(lcoe_vals)

        return pd.DataFrame({
            "lcoe_usd_mwh": lcoe_vals[order].astype(np.float32),
            "capacity_mw": cap_vals[order].astype(np.float32),
            "cum_capacity_gw": (
                np.cumsum(cap_vals[order]) / 1000.0
            ).astype(np.float32),
        })

    # -----------------------------------------------------------------------
    # Visual Outputs
    # -----------------------------------------------------------------------

    def _plot_lcoe_map(
        self,
        lcoe_map: np.ndarray,
        transform: Affine,
        crs: str,
        tech: str,
        stats: Dict,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        out_path: Path,
    ) -> None:
        """
        Render the spatial LCOE map for a single technology.

        Args:
            lcoe_map: Pixel-wise LCOE array (USD/MWh)
            transform: Rasterio affine transform
            crs: Coordinate reference system string
            tech: Technology name
            stats: LCOE statistics dictionary
            country_name: Full country name
            mainland_gdf: Mainland geometry GeoDataFrame
            context_gdf: Neighbouring countries GeoDataFrame
            out_path: Output file path
        """
        H, W = lcoe_map.shape
        extent = [
            transform.c,
            transform.c + transform.a * W,
            transform.f + transform.e * H,
            transform.f,
        ]
        v_bounds = mainland_gdf.total_bounds

        fig, ax = self.styler.create_figure(
            v_bounds[0], v_bounds[2],
            v_bounds[1], v_bounds[3],
            right_in_override=1.55,
        )
        self.styler.draw_basemap(
            ax, crs, mainland_gdf, context_gdf,
            self._admin_gdf, extent=extent,
        )

        vmin = max(stats.get("p10", 20), 10)
        vmax = min(stats.get("p90", 200), 300)
        im = ax.imshow(
            np.where(np.isfinite(lcoe_map), lcoe_map, np.nan),
            extent=extent,
            origin="upper",
            cmap=self.styler.make_cmap(
                "RdYlGn",
                reverse=True,
                under="none",
                bad="none",
            ),
            vmin=vmin,
            vmax=vmax,
            zorder=3,
            interpolation="bilinear",
        )

        self.styler.add_decorations(
            ax, v_bounds[0], v_bounds[2], v_bounds[1], v_bounds[3]
        )
        self.styler.add_colorbar(
            fig, im, r"LCOE (USD MWh$^{-1}$)", extend="neither"
        )
        if self._admin_gdf is not None:
            self.styler.draw_admin_labels(
                ax, self._admin_gdf,
                v_bounds[0], v_bounds[2],
                v_bounds[1], v_bounds[3],
            )

        lp = self.tech_params[tech]
        self.styler.add_standard_title(
            fig,
            title_main=f"{lp['label']} — Levelized Cost of Energy",
            title_sub=country_name,
        )

        self.styler.add_standard_footer(
            fig,
            params_text=(
                f"CAPEX: ${lp['capex_usd_kw']:.0f}/kW  |  "
                f"OPEX: ${lp['opex_usd_kw_yr']:.0f}/kW/yr  |  "
                f"r: {lp['discount_rate'] * 100:.0f}%"
            ),
            stats_text=(
                f"Mean: {stats.get('mean', 0):.1f}  |  "
                f"P10: {stats.get('p10', 0):.1f}  |  "
                f"P90: {stats.get('p90', 0):.1f}  USD/MWh"
            ),
            crs_metadata=f"CRS: {crs or 'EPSG:4326'}",
        )
        self.styler.save(fig, out_path)

    def _plot_supply_curve(
        self,
        supply_curve: pd.DataFrame,
        tech: str,
        stats: Dict,
        country_name: str,
        out_path: Path,
    ) -> None:
        """
        Render the merit order supply cost curve for a single technology.

        Args:
            supply_curve: DataFrame with lcoe_usd_mwh, capacity_mw,
                          cum_capacity_gw columns
            tech: Technology name
            stats: LCOE statistics dictionary
            country_name: Full country name
            out_path: Output file path
        """
        if supply_curve.empty:
            return

        lp = self.tech_params[tech]
        bench = LCOE_BENCHMARK_USD_MWH.get(tech, {})

        fig, ax = plt.subplots(figsize=(10, 6), dpi=self.styler.dpi)
        fig.patch.set_facecolor(self.styler.fig_bg)
        ax.set_facecolor("#FAFAFA")

        sc = supply_curve.iloc[
            np.linspace(
                0,
                len(supply_curve) - 1,
                min(5000, len(supply_curve)),
                dtype=int,
            )
        ].reset_index(drop=True)

        ax.fill_between(
            sc["cum_capacity_gw"],
            sc["lcoe_usd_mwh"],
            alpha=0.18,
            color=lp["color"],
            zorder=2,
        )
        ax.plot(
            sc["cum_capacity_gw"],
            sc["lcoe_usd_mwh"],
            color=lp["color"],
            linewidth=2.2,
            zorder=3,
            label=f"{lp['label']} — Cost Curve",
        )

        if bench:
            ax.axhspan(
                bench["p25"],
                bench["p75"],
                alpha=0.08,
                color="#888888",
                zorder=1,
                label=(
                    f"IRENA 2024 IQR: "
                    f"{bench['p25']:.0f}–{bench['p75']:.0f}"
                ),
            )
            ax.axhline(
                bench["median"],
                color="#555555",
                linewidth=1.4,
                linestyle="--",
                zorder=4,
                label=f"IRENA Median: {bench['median']:.0f}",
            )

        if "base_lcoe" in stats:
            ax.axhline(
                stats["base_lcoe"],
                color=lp["color"],
                linewidth=1.2,
                linestyle=":",
                zorder=4,
                alpha=0.75,
                label=f"Nat. Base LCOE: {stats['base_lcoe']:.1f}",
            )

        ax.set_xlabel(
            "Cumulative Installable Capacity (GW)", fontsize=11
        )
        ax.set_ylabel(r"LCOE (USD MWh$^{-1}$)", fontsize=11)
        ax.set_title(
            f"{lp['label']} — Merit Order Curve\n"
            f"{country_name}  |  Balanced Scenario",
            fontsize=12,
            fontweight="bold",
            pad=12,
        )
        ax.legend(fontsize=9, loc="upper left")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.3, linestyle="--")
        ax.set_xlim(left=0)

        fig.tight_layout()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(
            str(out_path),
            bbox_inches="tight",
            dpi=self.styler.dpi,
            facecolor=fig.get_facecolor(),
        )
        plt.close(fig)

    def _plot_comparison(
        self,
        results: Dict,
        country_name: str,
        country_code: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        out_path: Path,
    ) -> None:
        """
        Render a side-by-side LCOE comparison map for all technologies.

        Args:
            results: Full results dictionary from run()
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            mainland_gdf: Mainland geometry GeoDataFrame
            context_gdf: Neighbouring countries GeoDataFrame
            out_path: Output file path
        """
        plot_data_list = []
        for tech in TECH_ORDER:
            r = results["techs"].get(tech, {})
            if r and r.get("lcoe_map") is not None:
                plot_data_list.append({
                    "lcoe_map": r["lcoe_map"],
                    "transform": r["transform"],
                    "crs": r.get("crs", "EPSG:4326"),
                    "tech": tech,
                    "stats": r.get("stats", {}),
                    "country_name": country_name,
                    "mainland_gdf": mainland_gdf,
                    "context_gdf": context_gdf,
                    "out_path": Path(tempfile.gettempdir()) / f"{tech}_lcoe_tmp.png",
                })

        if plot_data_list:
            self.styler.create_comparison_via_pil(
                individual_plot_func=self._plot_lcoe_map,
                plot_data_list=plot_data_list,
                country_name=country_name,
                title_line1=(
                    f"Levelized Cost of Energy (LCOE) — {country_name}"
                ),
                title_line2=(
                    "Resource-Adjusted Capacity Factor  |  "
                    "Balanced Scenario"
                ),
                out_path=out_path,
                gap_px=20,
            )

    def _format_report(
        self,
        results: Dict,
        country_name: str,
        code: str,
    ) -> str:
        """
        Format the Phase 5 LCOE report as plain text.

        Args:
            results: Results dictionary from run()
            country_name: Full country name
            code: Country ISO code

        Returns:
            Formatted report string
        """
        lines = [
            "=" * 72,
            (
                f"  LCOE REPORT — Levelized Cost of Energy (Phase 5)\n"
                f"  {country_name} ({code})\n"
                f"  {results['timestamp'][:19].replace('T', ' ')}"
            ),
            "=" * 72,
        ]

        for tech in TECH_ORDER:
            if not results["techs"].get(tech):
                continue
            lp = self.tech_params[tech]
            stats = results["techs"][tech].get("stats", {})

            lines.extend([
                "",
                "-" * 72,
                f"  {lp['label'].upper()}",
                "-" * 72,
                (
                    f"  CAPEX: {lp['capex_usd_kw']:.0f} USD/kW  |  "
                    f"OPEX: {lp['opex_usd_kw_yr']:.0f} USD/kW/yr  |  "
                    f"r: {lp['discount_rate'] * 100:.0f}%  |  "
                    f"n: {lp['lifetime_years']} yr"
                ),
                (
                    f"  CRF: {stats.get('crf', 0):.4f}  |  "
                    f"Base CF: {stats.get('base_cf', 0):.3f}"
                ),
                "",
                f"  {'Metric':<36} {'Value':>10} {'Unit':>8}",
                "  " + "-" * 56,
            ])

            metric_labels = [
                ("base_lcoe", "Base LCOE (National mean CF)"),
                ("mean", "Spatial mean LCOE"),
                ("p10", "P10 — Cheapest 10%"),
                ("p90", "P90 — Most expensive 10%"),
            ]
            for key, label in metric_labels:
                lines.append(
                    f"  {label:<36} "
                    f"{stats.get(key, 0):>10.1f} "
                    f"{'USD/MWh':>8}"
                )

            sc = results["techs"][tech].get(
                "supply_curve", pd.DataFrame()
            )
            if not sc.empty:
                lines.extend([
                    "",
                    "  Available capacity by cost threshold:",
                    f"  {'Threshold (USD/MWh)':<24} {'Available GW':>14}",
                    "  " + "-" * 40,
                ])
                tot = float(sc["cum_capacity_gw"].max())
                for thr in [40, 50, 60, 70, 80, 100, 120]:
                    sub = sc[sc["lcoe_usd_mwh"] <= thr]
                    gw = (
                        float(sub["cum_capacity_gw"].max())
                        if not sub.empty
                        else 0.0
                    )
                    pct = (gw / tot * 100) if tot > 0 else 0.0
                    lines.append(
                        f"  <= {thr:>3} USD/MWh         "
                        f"{gw:>10.2f} GW  ({pct:.1f}%)"
                    )

        lines.extend([
            "",
            "=" * 72,
            "  TIMINGS BY STAGE",
            "-" * 72,
        ])
        for step, t in results.get("timings", {}).items():
            lines.append(f"    {step:<26}: {t:>6.1f}s")
        lines.extend(["=" * 72, ""])

        return "\n".join(lines)