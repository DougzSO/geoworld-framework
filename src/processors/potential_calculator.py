"""
src/processors/potential_calculator.py
======================================
Phase 4 — Spatial-Technical Energy Potential.

Calculates installable capacity (GW) and annual generation (TWh/yr)
from spatial suitability outputs (Phase 3), applying geodetic area
corrections and technology-specific deployment constraints.

References
----------
.. [1] IRENA (2024). Renewable Power Generation Costs in 2023.
.. [2] Hoogwijk et al. (2004). Biomass potential assessment.
.. [3] Eurek et al. (2017). Wind potential. Nature Energy.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from PIL import Image as _PIL_Image
from rasterio.features import rasterize
from rasterio.transform import Affine
from shapely.geometry import mapping

from src.core.constants import (
    TECH_ORDER,
    build_tech_params,
)
from src.utils.map_styling import GeoWorldStyler
from src.utils.timing import timer as _timer

logger = logging.getLogger("geoworld.processors.PotentialCalculator")

SCENARIOS: List[str] = ["optimistic", "balanced", "conservative"]

SCENARIO_COLORS: Dict[str, str] = {
    "optimistic": "#1B5E20",
    "balanced": "#1565C0",
    "conservative": "#B71C1C",
}

SCENARIO_ALPHA: Dict[str, float] = {
    "optimistic": 0.85,
    "balanced": 1.00,
    "conservative": 0.85,
}

_TECH_REPORT_LABELS: Dict[str, str] = {
    "solar": "SOLAR PV",
    "wind": "WIND ONSHORE",
    "biomass": "BIOMASS / BIOENERGY",
}

# Comparison layout constants
_PANEL_GAP_PX: int = 8
_TITLE_FRAC: float = 0.06
_FOOTER_FRAC: float = 0.025
_TITLE_MIN_PX: int = 80
_FOOTER_MIN_PX: int = 30


# ═══════════════════════════════════════════════════════════════════════════
# WGS84 area calculations
# ═══════════════════════════════════════════════════════════════════════════

def pixel_area_km2(transform: Affine, lat_center: float) -> float:
    """
    Physical pixel area (km²) using WGS84 ellipsoidal approximation.

    Accounts for latitude-dependent convergence of meridians.

    Args:
        transform: Rasterio affine transform of the raster.
        lat_center: Latitude (degrees) at pixel center.

    Returns:
        Pixel area in square kilometers.
    """
    deg_lon = abs(transform.a)
    deg_lat = abs(transform.e)
    φ = math.radians(lat_center)

    lat_km = (
        111132.92
        - 559.82 * math.cos(2 * φ)
        + 1.175 * math.cos(4 * φ)
        - 0.0023 * math.cos(6 * φ)
    ) / 1000.0

    lon_km = (
        111412.84 * math.cos(φ)
        - 93.50 * math.cos(3 * φ)
        + 0.118 * math.cos(5 * φ)
    ) / 1000.0

    return (deg_lat * lat_km) * (deg_lon * lon_km)


def build_pixel_area_array(
    transform: Affine,
    height: int,
    width: int,
) -> np.ndarray:
    """
    Row-wise pixel area array (km²) via numpy broadcasting.

    Args:
        transform: Rasterio affine transform.
        height: Number of rows in the raster.
        width: Number of columns in the raster.

    Returns:
        2D array (height, width) with pixel areas in km².

    Notes:
        Memory: O(height) — one value per row, broadcast to (height, width).
    """
    lat_centers = transform.f + transform.e * (np.arange(height) + 0.5)
    φ = np.radians(lat_centers)

    lat_km = (
        111132.92
        - 559.82 * np.cos(2 * φ)
        + 1.175 * np.cos(4 * φ)
        - 0.0023 * np.cos(6 * φ)
    ) / 1000.0

    lon_km = (
        111412.84 * np.cos(φ)
        - 93.50 * np.cos(3 * φ)
        + 0.118 * np.cos(5 * φ)
    ) / 1000.0

    row_areas = (abs(transform.e) * lat_km * abs(transform.a) * lon_km)
    return np.broadcast_to(
        row_areas.reshape(-1, 1),
        (height, width),
    ).copy()


# ═══════════════════════════════════════════════════════════════════════════
# Zonal statistics
# ═══════════════════════════════════════════════════════════════════════════

def zonal_stats_raster(
    value_arr: np.ndarray,
    valid_mask: np.ndarray,
    admin_gdf: Optional[gpd.GeoDataFrame],
    transform: Affine,
    crs: str,
    value_name: str = "value",
) -> pd.DataFrame:
    """
    Aggregate raster values within administrative polygons.

    Args:
        value_arr: 2D array of values to aggregate.
        valid_mask: Boolean mask indicating valid pixels.
        admin_gdf: GeoDataFrame with administrative boundaries.
        transform: Rasterio affine transform.
        crs: Coordinate reference system string.
        value_name: Prefix for output column names.

    Returns:
        DataFrame with aggregated statistics per region, sorted by sum
        descending. Columns include {value_name}_sum, {value_name}_mean,
        {value_name}_count, {value_name}_p90.
    """
    if admin_gdf is None or admin_gdf.empty:
        return pd.DataFrame()

    try:
        gdf = admin_gdf.to_crs(crs).copy()
    except Exception:
        gdf = admin_gdf.copy()

    gdf = gdf.reset_index(drop=True)
    height, width = value_arr.shape

    shapes = (
        (mapping(geom), int(i)) for i, geom in enumerate(gdf.geometry)
    )
    admin_mask = rasterize(
        shapes,
        out_shape=(height, width),
        transform=transform,
        fill=-1,
        dtype=np.int32,
        all_touched=False,
    )

    records = []
    for i, row in gdf.iterrows():
        combined = (
            (admin_mask == i)
            & valid_mask
            & np.isfinite(value_arr)
        )
        n = int(combined.sum())
        if n == 0:
            continue
        vals = value_arr[combined]
        records.append({
            "admin_name": str(
                row.get("_admin_name", row.get("NAME_1", "")),
            ),
            f"{value_name}_sum": float(vals.sum()),
            f"{value_name}_mean": float(vals.mean()),
            f"{value_name}_count": n,
            f"{value_name}_p90": float(np.percentile(vals, 90)),
        })

    return (
        pd.DataFrame(records)
        .sort_values(f"{value_name}_sum", ascending=False)
        .reset_index(drop=True)
    )


# ═══════════════════════════════════════════════════════════════════════════
# PotentialCalculator
# ═══════════════════════════════════════════════════════════════════════════

class PotentialCalculator:
    """
    Phase 4: Suitability → Capacity (GW) + Generation (TWh/yr).

    Transforms normalized suitability scores into technical potential
    estimates by applying technology-specific thresholds, power density,
    capacity factors, and geodetic area corrections.
    """

    def __init__(self, cfg: Any, outputs_dir: Path):
        """
        Initialize the potential calculator.

        Args:
            cfg: Configuration object with system parameters.
            outputs_dir: Root directory for writing results.
        """
        self.cfg = cfg
        self.outputs_dir = Path(outputs_dir)
        self.viz_cfg = cfg.system.get("visualization", {})
        self._admin_gdf: Optional[gpd.GeoDataFrame] = None
        self.tech_params: Optional[Dict[str, Dict]] = None

        pipeline_dpi = (
            cfg.system
            .get("pipeline", {})
            .get("map_dpi_export", 150)
        )
        self.styler = GeoWorldStyler(self.viz_cfg, global_dpi=pipeline_dpi)

    def run(
        self,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        suitability_dir: Path,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        country_params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Execute Phase 4 potential calculation for all technologies.

        Args:
            country_code: ISO3 country code.
            country_name: Full country name.
            mainland_gdf: GeoDataFrame of country mainland boundaries.
            suitability_dir: Path to Phase 3 suitability rasters.
            context_gdf: Optional neighboring countries for context.
            country_params: Optional country-specific parameter overrides.

        Returns:
            Dictionary with results including capacity, generation, timings,
            and zonal statistics for each technology and scenario.
        """
        # Build tech params once (with country overrides)
        self.tech_params = build_tech_params(
            self.cfg.system,
            country_params,
        )
        self._log_tech_params()

        started_at = datetime.now()
        timings: Dict[str, float] = {}
        suitability_dir = Path(suitability_dir)

        out_base = self.outputs_dir / country_code / "potential"
        out_maps = out_base / "maps"
        out_data = out_base / "data"
        out_rep = out_base / "reports"
        for d in (out_maps, out_data, out_rep):
            d.mkdir(parents=True, exist_ok=True)

        self._admin_gdf = self.styler.load_admin_boundaries(
            country_name,
            mainland_gdf,
            Path(self.cfg.raw_path),
        )
        if self._admin_gdf is not None:
            logger.info(
                "  Admin boundaries: %d regions active",
                len(self._admin_gdf),
            )

        results: Dict[str, Any] = {
            "country": country_code,
            "timestamp": started_at.isoformat(),
            "techs": {},
            "timings": timings,
        }

        for tech in TECH_ORDER:
            tif_name = f"{country_code}_{tech}_suitability.tif"
            tif = suitability_dir / tif_name
            if not tif.exists():
                logger.warning(
                    "  [%s] TIF not found: %s",
                    tech,
                    tif.name,
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
                    tif,
                    country_code,
                    country_name,
                    mainland_gdf,
                    context_gdf,
                    out_maps,
                    out_data,
                )

        with _timer("comparison_map", timings):
            comp_out = out_maps / f"{country_code}_potential_comparison.png"
            self._plot_comparison(
                results,
                country_name,
                country_code,
                mainland_gdf,
                context_gdf,
                comp_out,
            )

        with _timer("scenario_chart", timings):
            scen_out = out_maps / f"{country_code}_potential_scenarios.png"
            self._plot_scenario_chart(
                results,
                country_name,
                country_code,
                scen_out,
            )

        results["elapsed_total"] = round(
            (datetime.now() - started_at).total_seconds(),
            1,
        )

        report = self._format_report(results, country_name, country_code)
        logger.info("\n%s", report)

        rep_path = (
            out_rep
            / f"{country_code}_potential_"
            f"{started_at.strftime('%Y%m%d_%H%M%S')}.txt"
        )
        rep_path.write_text(report, encoding="utf-8")

        return results

    def _log_tech_params(self) -> None:
        """Log final (post-merge) technology parameters once."""
        p = self.tech_params
        logger.info(
            "  [params] solar=%.1f MW/km² lu=%.2f thr=%.2f | "
            "wind=%.1f MW/km² thr=%.2f | "
            "biomass=%.1f MW/km² thr=%.2f",
            p["solar"]["power_density_mw_km2"],
            p["solar"]["land_use_factor"],
            p["solar"]["thresholds"]["balanced"],
            p["wind"]["power_density_mw_km2"],
            p["wind"]["thresholds"]["balanced"],
            p["biomass"]["power_density_mw_km2"],
            p["biomass"]["thresholds"]["balanced"],
        )

    # ── Core calculation ─────────────────────────────────────────────────

    def _run_technology(
        self,
        tech: str,
        tif_path: Path,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        out_maps: Path,
        out_data: Path,
    ) -> Dict[str, Any]:
        """
        Calculate potential for a single technology across all scenarios.

        Args:
            tech: Technology identifier (solar/wind/biomass).
            tif_path: Path to suitability raster TIF.
            country_code: ISO3 country code.
            country_name: Full country name.
            mainland_gdf: GeoDataFrame of country boundaries.
            context_gdf: Optional neighboring countries.
            out_maps: Output directory for maps.
            out_data: Output directory for CSV data.

        Returns:
            Dictionary with technology results including scenarios,
            parameters, and zonal statistics.
        """
        params = self.tech_params[tech]

        with rasterio.open(str(tif_path)) as src:
            suit_arr = src.read(1).astype(np.float32)
            transform = src.transform
            crs = str(src.crs)
            height, width = src.height, src.width

        area_arr = build_pixel_area_array(transform, height, width)
        lat_center = transform.f + transform.e * height / 2

        logger.info(
            "  [%s] Grid: %dx%d px | lat_center=%.2f° | px_area=%.3f km²",
            tech,
            height,
            width,
            lat_center,
            pixel_area_km2(transform, lat_center),
        )

        scenario_results: Dict[str, Dict] = {}

        for scenario in SCENARIOS:
            threshold = params["thresholds"].get(scenario, 0.60)
            apt_mask = np.isfinite(suit_arr) & (suit_arr >= threshold)
            n_apt = int(apt_mask.sum())

            if n_apt == 0:
                logger.warning(
                    "  [%s/%s] No valid pixels at threshold %.2f.",
                    tech,
                    scenario,
                    threshold,
                )
                scenario_results[scenario] = {
                    "threshold": threshold,
                    "n_pixels": 0,
                    "area_km2": 0.0,
                    "capacity_mw": 0.0,
                    "capacity_gw": 0.0,
                    "generation_gwh": 0.0,
                    "generation_twh": 0.0,
                }
                continue

            area_apt = float(area_arr[apt_mask].sum())
            area_eff = area_apt * params["land_use_factor"]
            cap_mw = area_eff * params["power_density_mw_km2"]
            cap_gw = cap_mw / 1_000
            gen_gwh = (
                cap_mw
                * params["capacity_factor"]
                * params["hours_year"]
                / 1_000
            )
            gen_twh = gen_gwh / 1_000

            logger.info(
                "  [%s/%s] threshold=%.2f | area=%s km² | "
                "cap=%.2f GW | gen=%.2f TWh/yr",
                tech,
                scenario,
                threshold,
                "{:,.0f}".format(area_apt),
                cap_gw,
                gen_twh,
            )

            # Pixel-level capacity map
            cap_map = np.full((height, width), np.nan, dtype=np.float32)
            cap_map[apt_mask] = (
                area_arr[apt_mask]
                * params["land_use_factor"]
                * params["power_density_mw_km2"]
            )

            # Zonal statistics
            zonal_df = zonal_stats_raster(
                cap_map,
                apt_mask,
                self._admin_gdf,
                transform,
                crs,
                "capacity_mw",
            )
            if not zonal_df.empty:
                zonal_df["generation_gwh"] = (
                    zonal_df["capacity_mw_sum"]
                    * params["capacity_factor"]
                    * params["hours_year"]
                    / 1_000
                )
                csv_name = f"{country_code}_{tech}_{scenario}_zonal.csv"
                zonal_df.to_csv(
                    str(out_data / csv_name),
                    index=False,
                    encoding="utf-8",
                )

            scenario_results[scenario] = {
                "threshold": threshold,
                "n_pixels": n_apt,
                "area_km2": round(area_apt, 1),
                "area_eff_km2": round(area_eff, 1),
                "capacity_mw": round(cap_mw, 1),
                "capacity_gw": round(cap_gw, 3),
                "generation_gwh": round(gen_gwh, 1),
                "generation_twh": round(gen_twh, 3),
                "zonal_df": zonal_df,
                "cap_map": cap_map,
            }

        # Plot balanced scenario
        balanced = scenario_results.get("balanced", {})
        cap_map_bal = balanced.get("cap_map")

        if cap_map_bal is not None:
            valid = cap_map_bal[np.isfinite(cap_map_bal)]
            p90 = float(np.percentile(valid, 90)) if valid.size else 0.0

            map_out = out_maps / f"{country_code}_{tech}_potential_map.png"
            self._plot_potential_map(
                suit_arr,
                cap_map_bal,
                transform,
                crs,
                tech,
                params,
                country_name,
                mainland_gdf,
                context_gdf,
                self._admin_gdf,
                map_out,
                p90,
            )
            stats_out = (
                out_maps / f"{country_code}_{tech}_potential_stats.png"
            )
            self._plot_technology_stats(
                tech,
                params,
                scenario_results,
                stats_out,
            )

        # Free large arrays
        for sc in scenario_results:
            scenario_results[sc].pop("cap_map", None)

        return {
            "tech": tech,
            "label": params["label"],
            "params": params,
            "scenarios": scenario_results,
        }

    # ── Plots ────────────────────────────────────────────────────────────

    def _plot_potential_map(
        self,
        suit_arr: np.ndarray,
        cap_map: np.ndarray,
        transform: Affine,
        crs: str,
        tech: str,
        params: Dict,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        admin_gdf: Optional[gpd.GeoDataFrame],
        out_path: Path,
        p90_val: float,
    ) -> None:
        """
        Generate potential map for a single technology.

        Args:
            suit_arr: Suitability raster array.
            cap_map: Capacity raster array (MW per pixel).
            transform: Rasterio affine transform.
            crs: Coordinate reference system.
            tech: Technology identifier.
            params: Technology parameters dict.
            country_name: Full country name.
            mainland_gdf: Country boundaries.
            context_gdf: Optional neighboring countries.
            admin_gdf: Optional administrative boundaries.
            out_path: Output PNG file path.
            p90_val: 90th percentile capacity value for contour.
        """
        H, W = suit_arr.shape
        extent = [
            transform.c,
            transform.c + transform.a * W,
            transform.f + transform.e * H,
            transform.f,
        ]
        vb = mainland_gdf.total_bounds

        fig, ax = self.styler.create_figure(vb[0], vb[2], vb[1], vb[3])
        self.styler.draw_basemap(
            ax,
            crs,
            mainland_gdf,
            context_gdf,
            admin_gdf,
            extent=extent,
        )

        # Faint suitability underlay
        ax.imshow(
            np.where(
                np.isfinite(suit_arr) & (suit_arr > 0),
                suit_arr,
                np.nan,
            ),
            extent=extent,
            origin="upper",
            cmap="Greys",
            vmin=0,
            vmax=1.2,
            alpha=0.12,
            zorder=2,
            interpolation="bilinear",
        )

        # Capacity overlay
        cmap_cap = self.styler.make_cmap(
            params.get("cmap", "YlOrRd"),
            under="none",
            bad="none",
            vmin_frac=0.10,
            vmax_frac=1.0,
        )
        vmax = float(np.nanpercentile(cap_map, 98))
        im = ax.imshow(
            np.where(np.isfinite(cap_map), cap_map, np.nan),
            extent=extent,
            origin="upper",
            cmap=cmap_cap,
            vmin=0,
            vmax=vmax,
            zorder=3,
            interpolation="bilinear",
        )

        # P90 contour
        if p90_val > 0:
            ax.contour(
                np.where(
                    np.isfinite(cap_map) & (cap_map >= p90_val),
                    1.0,
                    np.nan,
                ),
                levels=[0.5],
                colors=[self.styler.CONTOUR_P90_COLOR],
                linewidths=[self.styler.CONTOUR_P90_LINEWIDTH],
                extent=extent,
                origin="upper",
                zorder=5,
            )

        self.styler.add_decorations(ax, vb[0], vb[2], vb[1], vb[3])
        self.styler.add_colorbar(
            fig,
            im,
            r"Technical Potential (MW px$^{-1}$)",
            extend="neither",
        )
        self.styler.add_standard_footer(
            fig,
            crs_metadata=f"CRS: {crs or 'EPSG:4326'}",
        )

        if admin_gdf is not None:
            self.styler.draw_admin_labels(
                ax,
                admin_gdf,
                vb[0],
                vb[2],
                vb[1],
                vb[3],
                max_labels=12,
            )

        thr = params["thresholds"]["balanced"]
        self.styler.add_standard_legend(
            ax,
            [
                mpatches.Patch(
                    facecolor=cmap_cap(0.65),
                    alpha=0.90,
                    label=f"Suitable (Score ≥ {thr:.2f})",
                ),
                mpatches.Patch(
                    facecolor="none",
                    edgecolor=self.styler.CONTOUR_P90_COLOR,
                    linewidth=1.5,
                    label=f"Top 10% (≥ {p90_val:.2f} MW/px)",
                ),
            ],
            "lower_center",
            bbox_anchor=(0.5, -0.060),
            ncol=2,
        )
        self.styler.add_standard_title(
            fig,
            title_main=f"{params['label']} Technical Potential",
            title_sub=country_name,
        )
        self.styler.save(fig, out_path)

    def _plot_technology_stats(
        self,
        tech: str,
        params: Dict,
        scenario_res: Dict,
        out_path: Path,
    ) -> None:
        """
        Generate dual-panel statistics figure for one technology.

        Args:
            tech: Technology identifier.
            params: Technology parameters dict.
            scenario_res: Dictionary of scenario results.
            out_path: Output PNG file path.
        """
        fig, (ax_dist, ax_scen) = plt.subplots(
            1,
            2,
            figsize=(12, 4.5),
            dpi=self.styler.dpi,
        )
        fig.patch.set_facecolor(self.styler.fig_bg)

        # Left: Top 5 districts
        zonal_df = scenario_res.get("balanced", {}).get("zonal_df")
        if zonal_df is not None and not zonal_df.empty:
            top5 = zonal_df.head(5).iloc[::-1]
            bars = ax_dist.barh(
                top5["admin_name"],
                top5["capacity_mw_sum"],
                color=params["color"],
                alpha=0.85,
                edgecolor="black",
                linewidth=0.5,
            )
            for bar in bars:
                w = bar.get_width()
                ax_dist.text(
                    w * 1.02,
                    bar.get_y() + bar.get_height() / 2,
                    f"{w:,.0f} MW",
                    ha="left",
                    va="center",
                    fontsize=9,
                )
            ax_dist.set_title(
                "Top 5 Districts (Balanced)",
                fontsize=11,
                fontweight="bold",
            )
            ax_dist.set_xlabel("Installable Capacity (MW)")
            ax_dist.spines[["top", "right"]].set_visible(False)
            ax_dist.set_xlim(0, top5["capacity_mw_sum"].max() * 1.25)

        # Right: Scenario comparison
        gw_vals = [
            scenario_res.get(s, {}).get("capacity_gw", 0)
            for s in SCENARIOS
        ]
        colours = ["#10B981", "#3B82F6", "#EF4444"]
        bars2 = ax_scen.bar(
            range(len(SCENARIOS)),
            gw_vals,
            color=colours,
            alpha=0.85,
            edgecolor="black",
            linewidth=0.5,
            width=0.5,
        )
        for i, bar in enumerate(bars2):
            h = bar.get_height()
            twh = scenario_res.get(SCENARIOS[i], {}).get(
                "generation_twh",
                0,
            )
            ax_scen.text(
                bar.get_x() + bar.get_width() / 2,
                h * 1.02,
                f"{h:.1f} GW\n({twh:.1f} TWh/yr)",
                ha="center",
                va="bottom",
                fontsize=9,
            )

        ax_scen.set_xticks(range(len(SCENARIOS)))
        ax_scen.set_xticklabels([s.capitalize() for s in SCENARIOS])
        ax_scen.set_title(
            "Potential by Scenario",
            fontsize=11,
            fontweight="bold",
        )
        ax_scen.set_ylabel("Total Capacity (GW)")
        ax_scen.spines[["top", "right"]].set_visible(False)
        y_max = max(gw_vals) * 1.25 if max(gw_vals) > 0 else 1
        ax_scen.set_ylim(0, y_max)

        fig.suptitle(
            f"{params['label']} — Technical Potential Summary",
            fontsize=13,
            fontweight="bold",
            y=0.98,
        )
        fig.subplots_adjust(
            top=0.88,
            bottom=0.12,
            left=0.08,
            right=0.97,
            wspace=0.35,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(
            str(out_path),
            bbox_inches="tight",
            dpi=self.styler.dpi,
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
        Stitch per-technology potential maps into single comparison panel.

        Args:
            results: Full results dictionary from run().
            country_name: Full country name.
            country_code: ISO3 country code.
            mainland_gdf: Country boundaries (not used in current impl).
            context_gdf: Neighboring countries (not used in current impl).
            out_path: Output PNG file path.
        """
        panels = []
        for tech in TECH_ORDER:
            png = (
                self.outputs_dir
                / country_code
                / "potential"
                / "maps"
                / f"{country_code}_{tech}_potential_map.png"
            )
            if png.exists():
                panels.append(_PIL_Image.open(png).convert("RGB"))

        if not panels:
            return

        # Normalise heights
        bg_rgb = panels[0].getpixel((5, 5))
        target_h = max(img.height for img in panels)
        normalised = []
        for img in panels:
            if img.height == target_h:
                normalised.append(img)
            else:
                padded = _PIL_Image.new(
                    "RGB",
                    (img.width, target_h),
                    bg_rgb,
                )
                y_off = (target_h - img.height) // 2
                padded.paste(img, (0, y_off))
                normalised.append(padded)

        gap = _PANEL_GAP_PX
        title_h = max(_TITLE_MIN_PX, int(target_h * _TITLE_FRAC))
        footer_h = max(_FOOTER_MIN_PX, int(target_h * _FOOTER_FRAC))
        total_w = (
            sum(p.width for p in normalised)
            + gap * (len(normalised) - 1)
        )
        total_h = title_h + target_h + footer_h

        canvas = _PIL_Image.new("RGB", (total_w, total_h), bg_rgb)
        x = 0
        for p in normalised:
            canvas.paste(p, (x, title_h))
            x += p.width + gap

        # Render title via matplotlib
        bg_hex = "#{:02x}{:02x}{:02x}".format(*bg_rgb)
        fig = plt.figure(
            figsize=(
                total_w / self.styler.dpi,
                total_h / self.styler.dpi,
            ),
            dpi=self.styler.dpi,
        )
        fig.patch.set_facecolor(bg_hex)
        ax_img = fig.add_axes([0, 0, 1, 1])
        ax_img.axis("off")
        ax_img.imshow(
            np.array(canvas),
            aspect="auto",
            interpolation="nearest",
        )

        y_title = 1.0 - (title_h * 0.4) / total_h
        font_main = max(12, int(total_w / self.styler.dpi))
        font_sub = max(10, int(total_w / self.styler.dpi * 0.85))

        fig.text(
            0.5,
            y_title + 0.015,
            f"Renewable Energy Technical Potential — {country_name}",
            ha="center",
            va="center",
            fontsize=font_main,
            fontweight="bold",
            color="#1A1A1A",
            transform=fig.transFigure,
        )
        fig.text(
            0.5,
            y_title - 0.015,
            "Balanced Scenario | AHP-TOPSIS Suitability + "
            "Technological Thresholds",
            ha="center",
            va="center",
            fontsize=font_sub,
            fontweight="bold",
            color="#1A1A1A",
            transform=fig.transFigure,
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(
            str(out_path),
            dpi=self.styler.dpi,
            bbox_inches="tight",
            pad_inches=0.1,
            facecolor=bg_hex,
        )
        plt.close(fig)

    def _plot_scenario_chart(
        self,
        results: Dict,
        country_name: str,
        country_code: str,
        out_path: Path,
    ) -> None:
        """
        Generate dual-panel chart comparing scenarios across technologies.

        Args:
            results: Full results dictionary from run().
            country_name: Full country name.
            country_code: ISO3 country code (not used in current impl).
            out_path: Output PNG file path.
        """
        fig, (ax1, ax2) = plt.subplots(
            1,
            2,
            figsize=(14, 6),
            dpi=self.styler.dpi,
        )
        fig.patch.set_facecolor(self.styler.fig_bg)
        fig.suptitle(
            f"Renewable Technical Potential — {country_name}\n"
            "Scenario Comparison",
            fontsize=12,
            fontweight="bold",
        )

        x_base = np.arange(len(TECH_ORDER))
        for sc_idx, scenario in enumerate(SCENARIOS):
            gw_vals = []
            twh_vals = []
            colours = []
            for tech in TECH_ORDER:
                sc_r = (
                    results["techs"]
                    .get(tech, {})
                    .get("scenarios", {})
                    .get(scenario, {})
                )
                gw_vals.append(sc_r.get("capacity_gw", 0.0))
                twh_vals.append(sc_r.get("generation_twh", 0.0))
                colours.append(self.tech_params[tech]["color"])

            offset = (sc_idx - 1) * 0.22

            for ax, vals in ((ax1, gw_vals), (ax2, twh_vals)):
                bars = ax.bar(
                    x_base + offset,
                    vals,
                    0.22,
                    color=colours,
                    alpha=SCENARIO_ALPHA[scenario],
                    label=scenario.capitalize(),
                    edgecolor=SCENARIO_COLORS[scenario],
                    linewidth=0.8,
                )
                for bar, val in zip(bars, vals):
                    if val > 0:
                        ax.text(
                            bar.get_x() + bar.get_width() / 2,
                            bar.get_height() * 1.02,
                            f"{val:.1f}",
                            ha="center",
                            fontsize=6.5,
                        )

        labels_tech = [self.tech_params[t]["label"] for t in TECH_ORDER]
        for ax, ylabel, title in (
            (ax1, "Installable Capacity (GW)", "Capacity"),
            (ax2, "Annual Generation (TWh/yr)", "Generation"),
        ):
            ax.set_xticks(x_base)
            ax.set_xticklabels(labels_tech, fontsize=9)
            ax.set_ylabel(ylabel, fontsize=9)
            ax.set_title(title, fontsize=10, fontweight="bold")
            ax.legend(fontsize=8, loc="upper right")
            ax.yaxis.grid(True, alpha=0.3)
            ax.set_facecolor("#FAFAFA")
            ax.spines[["top", "right"]].set_visible(False)

        fig.subplots_adjust(
            top=0.88,
            bottom=0.12,
            left=0.07,
            right=0.97,
            wspace=0.30,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(
            str(out_path),
            bbox_inches="tight",
            dpi=self.styler.dpi,
            facecolor=fig.get_facecolor(),
        )
        plt.close(fig)

    # ── Report ───────────────────────────────────────────────────────────

    def _format_report(
        self,
        results: Dict,
        country_name: str,
        code: str,
    ) -> str:
        """
        Generate human-readable text report of potential results.

        Args:
            results: Full results dictionary from run().
            country_name: Full country name.
            code: ISO3 country code.

        Returns:
            Multi-line formatted text report.
        """
        sep = "=" * 72
        dash = "-" * 72
        lines = [
            sep,
            "  POTENTIAL REPORT — Technical & Energy Potential",
            f"  {country_name} ({code})",
            f"  {results['timestamp'][:19].replace('T', ' ')}",
            sep,
        ]

        for tech, label in _TECH_REPORT_LABELS.items():
            r = results["techs"].get(tech)
            if not r:
                continue
            p = r.get("params", {})
            luf = p.get("land_use_factor", 0) * 100
            pd_val = p.get("power_density_mw_km2", 0)
            cf = p.get("capacity_factor", 0) * 100

            lines.extend([
                "",
                dash,
                f"  {label}",
                f"  Land Use: {luf:.1f}%  |  Density: {pd_val:.1f} MW/km²"
                f"  |  CF: {cf:.1f}%",
                dash,
                f"  {'Scenario':<14} {'Thr':>5} {'Area (km²)':>12} "
                f"{'GW':>8} {'TWh/yr':>10} {'Pixels':>8}",
                "  " + "-" * 60,
            ])

            for sc in SCENARIOS:
                sc_r = r.get("scenarios", {}).get(sc, {})
                if not sc_r or sc_r.get("n_pixels", 0) == 0:
                    continue
                lines.append(
                    f"  {sc.capitalize():<14} "
                    f"{sc_r['threshold']:>5.2f} "
                    f"{sc_r['area_km2']:>12,.0f} "
                    f"{sc_r['capacity_gw']:>8.2f} "
                    f"{sc_r['generation_twh']:>10.2f} "
                    f"{sc_r['n_pixels']:>8,}"
                )

            zon = (
                r.get("scenarios", {})
                .get("balanced", {})
                .get("zonal_df")
            )
            if zon is not None and not zon.empty:
                lines.extend([
                    "",
                    "    Top 5 Districts (Balanced Scenario):",
                    f"    {'District':<20} {'MW':>10} {'GWh/yr':>12} "
                    f"{'Pixels':>8}",
                    "    " + "-" * 54,
                ])
                for _, zrow in zon.head(5).iterrows():
                    lines.append(
                        f"    {str(zrow.get('admin_name', '')):<20} "
                        f"{zrow.get('capacity_mw_sum', 0):>10,.0f} "
                        f"{zrow.get('generation_gwh', 0):>12,.0f} "
                        f"{zrow.get('capacity_mw_count', 0):>8,}"
                    )

        # Summary
        lines.extend([
            "",
            sep,
            "  COMPARATIVE SUMMARY — BALANCED SCENARIO",
            dash,
            f"  {'Technology':<20} {'GW':>8} {'TWh/yr':>10} "
            f"{'Area km²':>12} {'CF%':>6}",
            "  " + "-" * 60,
        ])

        total_gw = total_twh = 0.0
        for tech in TECH_ORDER:
            tr = results["techs"].get(tech)
            if not tr:
                continue
            bal = tr.get("scenarios", {}).get("balanced", {})
            gw = bal.get("capacity_gw", 0)
            twh = bal.get("generation_twh", 0)
            area = bal.get("area_km2", 0)
            cf = tr.get("params", {}).get("capacity_factor", 0) * 100
            label = tr.get("params", {}).get("label", tech)
            total_gw += gw
            total_twh += twh
            lines.append(
                f"  {label:<20} {gw:>8.2f} {twh:>10.2f} "
                f"{area:>12,.0f} {cf:>6.1f}%"
            )

        lines.extend([
            "  " + "-" * 60,
            f"  {'TOTAL':<20} {total_gw:>8.2f} {total_twh:>10.2f}",
            "",
            sep,
            "  TIMINGS BY STAGE",
            dash,
        ])
        for step, t in results.get("timings", {}).items():
            lines.append(f"    {step:<26}: {t:>6.1f}s")
        lines.extend([
            f"    {'TOTAL':<26}: {results.get('elapsed_total', 0):.1f}s",
            sep,
            "",
        ])

        return "\n".join(lines)