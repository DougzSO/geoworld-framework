"""
results_writer.py — Phase 6
============================
Integrates outputs from Phases 3–5 into a final visual and
technical synthesis. Generates dominance maps and the Executive
Dashboard.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.gridspec as gridspec
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from matplotlib.ticker import FuncFormatter
from rasterio.transform import Affine

from src.core.constants import (
    KM_PER_DEG_LAT,
    NODATA_FLOAT,
    TECH_META,
    TECH_ORDER,
)
from src.utils.timing import timer as _timer
from src.utils.map_styling import GeoWorldStyler

try:
    from src.core.constants import LCOE_BENCHMARK_USD_MWH
except ImportError:
    from src.processors.lcoe_calculator import (
        LCOE_BENCHMARKS as LCOE_BENCHMARK_USD_MWH
    )

logger = logging.getLogger("geoworld.processors.ResultsWriter")


# ===========================================================================
# MAIN CLASS
# ===========================================================================

class ResultsWriter:
    """
    Phase 6 — Final Synthesis.

    Produces technology dominance maps, executive dashboard and GeoTIFFs
    integrating outputs from Phases 3 (Suitability), 4 (Potential),
    5 (LCOE), and optionally 7 (GHG Abatement).
    """

    def __init__(self, cfg, outputs_dir: Path):
        """
        Initialize ResultsWriter.

        Args:
            cfg: ConfigLoader instance
            outputs_dir: Base directory for all pipeline outputs
        """
        self.cfg = cfg
        self.outputs_dir = Path(outputs_dir)
        self.viz_cfg = cfg.system.get("visualization", {})
        self._admin_gdf: Optional[gpd.GeoDataFrame] = None

        pipeline_dpi = (
            cfg.system.get("pipeline", {}).get("map_dpi_export", 150)
        )
        self.styler = GeoWorldStyler(self.viz_cfg, global_dpi=pipeline_dpi)

    # -----------------------------------------------------------------------
    # DATA RECOVERY (when Phase 4/5 outputs are not in memory)
    # -----------------------------------------------------------------------

    def _normalize_potential(
        self,
        data: Any,
        country_code: str,
    ) -> Dict:
        """
        Reconstruct Potential data from disk when Phase 4 results
        are not in memory (skip_potential: true).

        Args:
            data: Either a result dict with 'techs' key, or a Path to
                  the potential output directory
            country_code: ISO-3166-alpha-3 code

        Returns:
            Normalized dict with structure: {techs: {tech: {scenarios: {...}}}}
        """
        if isinstance(data, dict) and "techs" in data:
            return data

        logger.info(
            "  [Recovery] Reconstructing Potential data from disk..."
        )
        res = {"techs": {}}

        base_dir = (
            Path(data)
            if isinstance(data, (str, Path))
            else self.outputs_dir / country_code / "potential"
        )
        data_dir = base_dir / "data"

        if not data_dir.exists():
            logger.warning(
                "  [Recovery] Data directory not found: %s", data_dir
            )
            data_dir = base_dir

        res_deg = 0.01
        if self.cfg:
            res_deg = float(
                self.cfg.system
                .get("pipeline", {})
                .get("resolution_deg", 0.01)
            )

        for tech in TECH_ORDER:
            scen_dict = {}

            tech_cfg = (
                self.cfg.system
                .get("potential", {})
                .get("technologies", {})
                .get(tech, {})
            ) if self.cfg else {}
            land_use_factor = float(
                tech_cfg.get("land_use_factor", 0.20)
            )
            power_density = float(
                tech_cfg.get("power_density_mw_km2", 30.0)
            )

            for sc in ["optimistic", "balanced", "conservative"]:
                csv_file = (
                    data_dir / f"{country_code}_{tech}_{sc}_zonal.csv"
                )
                if not csv_file.exists():
                    candidates = sorted(
                        data_dir.glob(f"*{tech}*{sc}*.csv")
                    )
                    if candidates:
                        csv_file = candidates[0]
                        logger.debug(
                            "  [Recovery] Using alternative CSV: %s",
                            csv_file.name,
                        )

                gw = twh = area = 0.0

                if csv_file.exists():
                    try:
                        df = pd.read_csv(csv_file)
                        cols = set(df.columns)

                        for col_name, divisor in [
                            ("capacity_mw_sum", 1000.0),
                            ("capacity_mw", 1000.0),
                            ("capacity_gw_sum", 1.0),
                            ("capacity_gw", 1.0),
                        ]:
                            if col_name in cols:
                                gw = float(df[col_name].sum()) / divisor
                                break

                        for col_name, divisor in [
                            ("generation_twh_sum", 1.0),
                            ("generation_twh", 1.0),
                            ("generation_gwh_sum", 1000.0),
                            ("generation_gwh", 1000.0),
                            ("annual_generation_gwh", 1000.0),
                        ]:
                            if col_name in cols:
                                twh = (
                                    float(df[col_name].sum()) / divisor
                                )
                                break

                        for col_name, divisor in [
                            ("suitable_area_km2", 1.0),
                            ("suitable_area_km2_sum", 1.0),
                            ("area_km2_sum", 1.0),
                            ("area_km2", 1.0),
                            ("apt_area_km2", 1.0),
                            ("apt_area_km2_sum", 1.0),
                            ("pixel_area_km2_sum", 1.0),
                            ("pixel_area_km2", 1.0),
                            ("area_ha_sum", 100.0),
                            ("area_ha", 100.0),
                        ]:
                            if col_name in cols:
                                area = (
                                    float(df[col_name].sum()) / divisor
                                )
                                break

                        if area == 0.0:
                            n_pixels = 0.0
                            for count_col in [
                                "capacity_mw_count",
                                "n_pixels",
                                "pixel_count",
                                "count",
                            ]:
                                if count_col in cols:
                                    n_pixels = float(
                                        df[count_col].sum()
                                    )
                                    break

                            if n_pixels > 0:
                                px_km2 = (res_deg * 111.0) ** 2 * 0.85
                                area = n_pixels * px_km2
                                logger.debug(
                                    "  [Recovery] %s/%s: area from "
                                    "pixel count: %.0f px x %.3f "
                                    "km²/px = %,.0f km²",
                                    tech, sc, n_pixels, px_km2, area,
                                )

                        if area == 0.0 and gw > 0 and power_density > 0:
                            area = (
                                gw * 1000.0
                                / (land_use_factor * power_density)
                            )
                            logger.debug(
                                "  [Recovery] %s/%s: area "
                                "back-calculated from capacity: "
                                "%,.0f km²",
                                tech, sc, area,
                            )

                        logger.debug(
                            "  [Recovery] %s/%s: GW=%.2f, "
                            "TWh=%.2f, km²=%,.0f",
                            tech, sc, gw, twh, area,
                        )

                    except Exception as exc:
                        logger.warning(
                            "  [Recovery] Failed to read %s: %s",
                            csv_file.name,
                            exc,
                        )
                else:
                    logger.debug(
                        "  [Recovery] CSV not found: %s_%s_%s_zonal.csv",
                        country_code, tech, sc,
                    )

                scen_dict[sc] = {
                    "capacity_gw": gw,
                    "generation_twh": twh,
                    "area_km2": area,
                }

            res["techs"][tech] = {"scenarios": scen_dict}

        return res

    def _normalize_lcoe(
        self,
        data: Any,
        country_code: str,
        gis_dir: Path,
    ) -> Dict:
        """
        Reconstruct LCOE data from disk when Phase 5 results
        are not in memory.

        Args:
            data: Either a result dict with 'techs' key, or a Path
            country_code: ISO-3166-alpha-3 code
            gis_dir: Directory containing LCOE GeoTIFF files

        Returns:
            Normalized dict with structure:
            {techs: {tech: {lcoe_map: array, stats: {...}}}}
        """
        if isinstance(data, dict) and "techs" in data:
            return data

        logger.info(
            "  [Recovery] Reconstructing LCOE data from disk..."
        )
        res = {"techs": {}}

        for tech in TECH_ORDER:
            name_patterns = [
                f"{country_code}_{tech}_lcoe_usd_mwh.tif",
                f"{country_code}_{tech}_lcoe.tif",
                f"{tech}_lcoe_usd_mwh.tif",
            ]

            tif_file = None
            for pattern in name_patterns:
                candidate = gis_dir / pattern
                if candidate.exists():
                    tif_file = candidate
                    break

            if tif_file is None:
                found = list(
                    (self.outputs_dir / country_code).rglob(
                        f"*{tech}*lcoe*.tif"
                    )
                )
                if found:
                    tif_file = found[0]
                    logger.debug(
                        "  [Recovery] LCOE TIF found via rglob: %s",
                        tif_file.name,
                    )

            arr, stats = None, {}

            if tif_file and tif_file.exists():
                try:
                    with rasterio.open(str(tif_file)) as src:
                        arr = src.read(1).astype(np.float32)
                        nd = src.nodata
                        if nd is not None:
                            arr[arr == float(nd)] = np.nan
                        arr[arr <= 0] = np.nan

                    valid = arr[np.isfinite(arr)]
                    if valid.size > 0:
                        stats = {
                            "mean": float(np.mean(valid)),
                            "p10": float(np.percentile(valid, 10)),
                            "p25": float(np.percentile(valid, 25)),
                            "median": float(np.median(valid)),
                            "p75": float(np.percentile(valid, 75)),
                            "p90": float(np.percentile(valid, 90)),
                        }
                        logger.debug(
                            "  [Recovery] LCOE %s: median=$%.1f/MWh, "
                            "n_valid=%d",
                            tech,
                            stats["median"],
                            valid.size,
                        )
                except Exception as exc:
                    logger.warning(
                        "  [Recovery] Failed to read LCOE TIF "
                        "for %s: %s",
                        tech,
                        exc,
                    )
            else:
                logger.warning(
                    "  [Recovery] LCOE TIF not found for %s", tech
                )

            res["techs"][tech] = {"lcoe_map": arr, "stats": stats}

        return res

    def _normalize_abatement(
        self,
        data: Any,
        country_code: str,
    ) -> Dict:
        """
        Normalize abatement data from GHGAbatementCalculator output
        or from disk (legacy fallback).

        Accepts the dict returned directly by GHGAbatementCalculator.run()
        (keys: subst_gwh, co2_avoided_mt, total_value_b, mac_global,
        carbon_price, capex_total_b, ndc_coverage_pct, ci_before,
        ci_after, available) or a Path to the abatement folder.

        Args:
            data: Result dict from GHGAbatementCalculator or path to
                  abatement output directory
            country_code: ISO-3166-alpha-3 code

        Returns:
            Normalized abatement dict with 'available' key
        """
        if isinstance(data, dict) and "available" in data:
            return data
        if isinstance(data, dict) and "co2_avoided_mt" in data:
            data["available"] = True
            return data

        if isinstance(data, (str, Path)):
            abat_dir = Path(data)
            summary_csv = (
                abat_dir
                / "data"
                / f"{country_code}_abatement_summary.csv"
            )
            if summary_csv.exists():
                try:
                    df = pd.read_csv(summary_csv)
                    if not df.empty:
                        row = df.iloc[0]
                        return {
                            "available": True,
                            "co2_avoided_mt": float(
                                row.get("co2_avoided_mt", 0)
                            ),
                            "subst_gwh": float(
                                row.get("subst_gwh", 0)
                            ),
                            "total_value_b": float(
                                row.get("total_value_b", 0)
                            ),
                            "mac_global": float(
                                row.get("mac_global", 0)
                            ),
                            "carbon_price": float(
                                row.get("carbon_price", 80)
                            ),
                            "capex_total_b": float(
                                row.get("capex_total_b", 0)
                            ),
                            "ndc_coverage_pct": float(
                                row.get("ndc_coverage_pct", 0)
                            ),
                            "ci_before": float(
                                row.get("ci_before", 0)
                            ),
                            "ci_after": float(
                                row.get("ci_after", 0)
                            ),
                        }
                except Exception as e:
                    logger.warning(
                        "  [Recovery] Failed to load abatement CSV: %s",
                        e,
                    )

        return {"available": False}

    # -----------------------------------------------------------------------
    # MAIN ENTRY POINT
    # -----------------------------------------------------------------------

    def run(
        self,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        suitability_dir: Path,
        potential_dir: Any,
        lcoe_dir: Any,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        abatement_dir: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Run Phase 6 synthesis for a country.

        Integrates suitability, potential, LCOE, and optionally abatement
        results into dominance maps, an executive dashboard, and GeoTIFFs.

        Args:
            country_code: ISO-3166-alpha-3 code
            country_name: Full country name
            mainland_gdf: Mainland geometry GeoDataFrame
            suitability_dir: Directory with Phase 3 suitability TIFs
            potential_dir: Phase 4 result dict or directory path
            lcoe_dir: Phase 5 result dict or directory path
            context_gdf: Neighbouring countries GeoDataFrame (optional)
            abatement_dir: Phase 7 result dict or directory path (optional)

        Returns:
            Results dictionary with timings and exported file list
        """
        started_at = datetime.now()
        timings: Dict[str, float] = {}

        out_base = self.outputs_dir / country_code / "results"
        out_maps = out_base / "maps"
        out_gis = out_base / "gis"
        out_reports = out_base / "reports"
        for d in [out_maps, out_gis, out_reports]:
            d.mkdir(parents=True, exist_ok=True)

        potential_results = self._normalize_potential(
            potential_dir, country_code
        )
        lcoe_results = self._normalize_lcoe(
            lcoe_dir,
            country_code,
            self.outputs_dir / country_code / "lcoe" / "gis",
        )
        abatement_results = self._normalize_abatement(
            abatement_dir, country_code
        )

        self._admin_gdf = self.styler.load_admin_boundaries(
            country_name,
            mainland_gdf,
            Path(self.cfg.raw_path),
        )
        if self._admin_gdf is not None:
            logger.info(
                "  Admin: %d districts loaded", len(self._admin_gdf)
            )

        suit_arrays: Dict[str, Optional[np.ndarray]] = {}
        transform_ref, crs_ref, H, W = None, "EPSG:4326", 0, 0

        for tech in TECH_ORDER:
            tif = (
                Path(suitability_dir)
                / f"{country_code}_{tech}_suitability.tif"
            )
            if tif.exists():
                with rasterio.open(str(tif)) as src:
                    arr = src.read(1).astype(np.float32)
                    arr[arr < 0] = np.nan
                    suit_arrays[tech] = arr
                    if transform_ref is None:
                        transform_ref = src.transform
                        crs_ref = str(src.crs)
                        H, W = src.height, src.width
                logger.info("  [%s] Suitability TIF loaded.", tech)
            else:
                suit_arrays[tech] = None
                logger.warning(
                    "  [%s] Suitability TIF not found: %s",
                    tech,
                    tif.name,
                )

        if transform_ref is None or H == 0:
            logger.error(
                "  No suitability TIFs found — aborting ResultsWriter."
            )
            return {}

        lcoe_arrays: Dict[str, Optional[np.ndarray]] = {
            tech: (
                lcoe_results.get("techs", {})
                .get(tech, {})
                .get("lcoe_map")
            )
            for tech in TECH_ORDER
        }
        results: Dict[str, Any] = {
            "country": country_code,
            "timestamp": started_at.isoformat(),
            "timings": timings,
        }

        minx = transform_ref.c
        maxy = transform_ref.f
        maxx = minx + transform_ref.a * W
        miny = maxy + transform_ref.e * H
        extent = [minx, maxx, miny, maxy]

        with _timer("dominance_suitability", timings):
            dom_suit, dom_suit_score, comp_suit = (
                self._build_suitability_dominance(
                    suit_arrays, H, W
                )
            )
            self._plot_dominance_map(
                dom_suit,
                dom_suit_score,
                comp_suit,
                transform_ref,
                crs_ref,
                "suitability",
                country_name,
                mainland_gdf,
                context_gdf,
                out_maps / f"{country_code}_dominance_suitability.png",
            )

        with _timer("dominance_lcoe", timings):
            dom_lcoe, dom_lcoe_val, comp_lcoe = (
                self._build_lcoe_dominance(lcoe_arrays, H, W)
            )
            self._plot_dominance_map(
                dom_lcoe,
                dom_lcoe_val,
                comp_lcoe,
                transform_ref,
                crs_ref,
                "lcoe",
                country_name,
                mainland_gdf,
                context_gdf,
                out_maps / f"{country_code}_dominance_lcoe.png",
            )

        with _timer("executive_dashboard", timings):
            self._plot_executive_dashboard(
                dom_suit,
                dom_suit_score,
                comp_suit,
                dom_lcoe,
                dom_lcoe_val,
                comp_lcoe,
                potential_results,
                lcoe_results,
                transform_ref,
                crs_ref,
                country_name,
                country_code,
                mainland_gdf,
                context_gdf,
                extent,
                minx,
                maxx,
                miny,
                maxy,
                out_maps / f"{country_code}_executive_dashboard.png",
                abatement_results=abatement_results,
            )

        with _timer("geotiff_export", timings):
            results["exported_tifs"] = self._export_geotiffs(
                country_code,
                country_name,
                suit_arrays,
                lcoe_arrays,
                dom_suit,
                dom_lcoe,
                transform_ref,
                crs_ref,
                H,
                W,
                out_gis,
            )

        results["elapsed_total"] = round(
            (datetime.now() - started_at).total_seconds(), 1
        )
        report = self._format_report(
            results,
            country_name,
            country_code,
            potential_results,
            lcoe_results,
            dom_suit,
            dom_lcoe,
        )
        logger.info("\n%s", report)

        rep_path = (
            out_reports
            / f"{country_code}_results_"
            f"{started_at.strftime('%Y%m%d_%H%M%S')}.txt"
        )
        rep_path.write_text(report, encoding="utf-8")

        return results

    # -----------------------------------------------------------------------
    # DOMINANCE LOGIC
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_suitability_dominance(
        suit_arrays: Dict[str, Optional[np.ndarray]],
        H: int,
        W: int,
        competition_delta: float = 0.10,
        min_score: float = 0.30,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Compute pixel-wise technology dominance from suitability scores.

        Args:
            suit_arrays: Dict mapping tech names to suitability arrays
            H: Raster height in pixels
            W: Raster width in pixels
            competition_delta: Score difference threshold below which
                               pixels are flagged as competitive zones
            min_score: Minimum score for a pixel to be assigned a
                       dominant technology

        Returns:
            Tuple of (dominance_array, max_score_array,
                      competition_mask)
        """
        stack = np.full((3, H, W), 0.0, dtype=np.float32)
        for i, tech in enumerate(TECH_ORDER):
            arr = suit_arrays.get(tech)
            if arr is not None:
                stack[i] = np.where(
                    (np.isfinite(arr)) & (arr > 0), arr, 0.0
                )

        dom_idx = np.argmax(stack, axis=0).astype(np.int8)
        max_score = np.max(stack, axis=0)
        second_score = np.sort(stack, axis=0)[::-1][1]

        no_tech = max_score < min_score
        dom_arr = (dom_idx + 1).astype(np.int8)
        dom_arr[no_tech] = 0

        dom_score = max_score.copy()
        dom_score[no_tech] = np.nan
        competition = (
            (max_score - second_score < competition_delta) & ~no_tech
        )
        return dom_arr, dom_score.astype(np.float32), competition

    @staticmethod
    def _build_lcoe_dominance(
        lcoe_arrays: Dict[str, Optional[np.ndarray]],
        H: int,
        W: int,
        competition_delta_usd: float = 10.0,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Compute pixel-wise technology dominance from LCOE values.

        Dominance is assigned to the technology with the lowest LCOE
        at each pixel.

        Args:
            lcoe_arrays: Dict mapping tech names to LCOE arrays ($/MWh)
            H: Raster height in pixels
            W: Raster width in pixels
            competition_delta_usd: LCOE difference in $/MWh below which
                                   pixels are flagged as competitive

        Returns:
            Tuple of (dominance_array, min_lcoe_array,
                      competition_mask)
        """
        stack = np.full((3, H, W), np.inf, dtype=np.float32)
        for i, tech in enumerate(TECH_ORDER):
            arr = lcoe_arrays.get(tech)
            if arr is not None:
                stack[i] = np.where(
                    (np.isfinite(arr)) & (arr > 0.0), arr, np.inf
                )

        dom_idx = np.argmin(stack, axis=0).astype(np.int8)
        min_lcoe = np.min(stack, axis=0)
        second_lcoe = np.sort(stack, axis=0)[1]

        no_tech = ~np.any(np.isfinite(stack), axis=0)
        dom_arr = (dom_idx + 1).astype(np.int8)
        dom_arr[no_tech] = 0

        dom_val = min_lcoe.copy()
        dom_val[no_tech] = np.nan
        dom_val[np.isinf(dom_val)] = np.nan

        with np.errstate(invalid="ignore"):
            diff = np.where(
                np.isfinite(second_lcoe) & np.isfinite(min_lcoe),
                second_lcoe - min_lcoe,
                np.inf,
            )
            competition = (diff < competition_delta_usd) & ~no_tech

        return dom_arr, dom_val.astype(np.float32), competition

    # -----------------------------------------------------------------------
    # STANDALONE DOMINANCE MAP
    # -----------------------------------------------------------------------

    def _plot_dominance_map(
        self,
        dom_arr: np.ndarray,
        dom_score: np.ndarray,
        competition: np.ndarray,
        transform: Affine,
        crs: str,
        mode: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        out_path: Path,
    ) -> None:
        """
        Render a standalone dominance map and save to disk.

        Args:
            dom_arr: Dominance class array (0=none, 1=solar, 2=wind,
                     3=biomass)
            dom_score: Dominant technology score array
            competition: Boolean mask of competitive zones
            transform: Rasterio affine transform
            crs: Coordinate reference system string
            mode: Either 'suitability' or 'lcoe'
            country_name: Full country name for title
            mainland_gdf: Mainland geometry GeoDataFrame
            context_gdf: Neighbouring countries GeoDataFrame
            out_path: Output file path
        """
        H, W = dom_arr.shape
        r_minx, r_maxy = transform.c, transform.f
        r_maxx = r_minx + transform.a * W
        r_miny = r_maxy + transform.e * H
        extent = [r_minx, r_maxx, r_miny, r_maxy]

        bounds = mainland_gdf.total_bounds
        v_minx, v_miny = bounds[0], bounds[1]
        v_maxx, v_maxy = bounds[2], bounds[3]

        rgba = self._build_rgba_q1(dom_arr, dom_score, mode, H, W)

        fig, ax = self.styler.create_figure(
            v_minx, v_maxx, v_miny, v_maxy, right_in_override=0.55
        )
        self.styler.draw_basemap(
            ax, crs, mainland_gdf, context_gdf, self._admin_gdf
        )

        ax.imshow(
            rgba,
            extent=extent,
            origin="upper",
            zorder=3,
            interpolation="nearest",
        )

        try:
            x = np.linspace(r_minx, r_maxx, W)
            y = np.linspace(r_maxy, r_miny, H)
            ax.contourf(
                x,
                y,
                competition.astype(np.float32),
                levels=[0.5, 1.5],
                hatches=["////"],
                colors=["none"],
                zorder=4,
                alpha=0.35,
            )
        except Exception:
            pass

        self.styler.add_decorations(
            ax, v_minx, v_maxx, v_miny, v_maxy
        )

        crs_name = crs if crs else "EPSG:4326"
        self.styler.add_standard_footer(
            fig, crs_metadata=f"CRS: {crs_name}"
        )

        if self._admin_gdf is not None:
            self.styler.draw_admin_labels(
                ax, self._admin_gdf, v_minx, v_maxx, v_miny, v_maxy
            )

        comp_label = (
            "Competition Zone (ΔTOPSIS < 0.10)"
            if mode == "suitability"
            else "Competition Zone (ΔLCOE < 10 $/MWh)"
        )

        TECH_COLORS = {
            "solar": "#E07B39",
            "wind": "#3A7DC9",
            "biomass": "#3A9E5F",
        }
        legend_elements = [
            mpatches.Patch(
                facecolor=TECH_COLORS["solar"],
                alpha=0.88,
                label="Solar PV",
                edgecolor="#555555",
                linewidth=0.7,
            ),
            mpatches.Patch(
                facecolor=TECH_COLORS["wind"],
                alpha=0.88,
                label="Wind Onshore",
                edgecolor="#555555",
                linewidth=0.7,
            ),
            mpatches.Patch(
                facecolor=TECH_COLORS["biomass"],
                alpha=0.88,
                label="Biomass / Bioenergy",
                edgecolor="#555555",
                linewidth=0.7,
            ),
            mpatches.Patch(
                facecolor="none",
                edgecolor="#888888",
                hatch="////",
                linewidth=0.5,
                label=comp_label,
            ),
            mpatches.Patch(
                facecolor=self.styler.ocean_color,
                edgecolor="#AAAAAA",
                linewidth=0.5,
                label="No Suitable Area",
            ),
        ]

        self.styler.add_standard_legend(
            ax,
            legend_elements,
            "lower_center",
            bbox_anchor=(0.5, -0.09),
            ncol=3,
        )

        t_main = (
            "Technology Suitability Dominance"
            if mode == "suitability"
            else "Technology LCOE Dominance"
        )
        self.styler.add_standard_title(
            fig, title_main=t_main, title_sub=country_name
        )

        self.styler.save(fig, out_path)

    # -----------------------------------------------------------------------
    # EXECUTIVE DASHBOARD
    # -----------------------------------------------------------------------

    def _plot_executive_dashboard(
        self,
        dom_suit: np.ndarray,
        dom_suit_score: np.ndarray,
        comp_suit: np.ndarray,
        dom_lcoe: np.ndarray,
        dom_lcoe_val: np.ndarray,
        comp_lcoe: np.ndarray,
        potential_results: Dict,
        lcoe_results: Dict,
        transform: Affine,
        crs: str,
        country_name: str,
        country_code: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        extent: List[float],
        minx: float,
        maxx: float,
        miny: float,
        maxy: float,
        out_path: Path,
        abatement_results: Optional[Dict] = None,
    ) -> None:
        """
        Render the multi-panel executive dashboard and save to disk.

        Panels:
            (a) Suitability dominance map
            (b) Technical potential bar chart
            (c) LCOE distribution plot
            (d) LCOE dominance map
            (e) Supply cost curves
            (f) Summary metrics table
            (g) GHG abatement summary (if available)

        Args:
            dom_suit: Suitability dominance array
            dom_suit_score: Suitability dominance score array
            comp_suit: Suitability competition mask
            dom_lcoe: LCOE dominance array
            dom_lcoe_val: LCOE dominance value array
            comp_lcoe: LCOE competition mask
            potential_results: Normalized potential results dict
            lcoe_results: Normalized LCOE results dict
            transform: Rasterio affine transform
            crs: Coordinate reference system string
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            mainland_gdf: Mainland geometry GeoDataFrame
            context_gdf: Neighbouring countries GeoDataFrame
            extent: Map extent [minx, maxx, miny, maxy]
            minx: Minimum longitude
            maxx: Maximum longitude
            miny: Minimum latitude
            maxy: Maximum latitude
            out_path: Output file path
            abatement_results: GHG abatement results dict (optional)
        """
        has_abatement = (
            abatement_results is not None
            and abatement_results.get("available", False)
        )

        fig_width = 22 if has_abatement else 18
        fig = plt.figure(
            figsize=(fig_width, 13), dpi=self.styler.dpi
        )
        fig.patch.set_facecolor(self.styler.fig_bg)

        title_extra = "  ·  GHG Abatement" if has_abatement else ""
        title_main = (
            f"GeoWorld Framework — Renewable Energy Assessment: "
            f"{country_name} ({country_code})"
        )
        title_sub = (
            f"AHP-TOPSIS Multi-Criteria Suitability  ·  "
            f"Technical Potential  ·  LCOE Economics{title_extra}"
        )

        fig.suptitle(
            f"{title_main}\n{title_sub}",
            fontsize=15,
            fontweight="bold",
            y=0.99,
        )

        if has_abatement:
            gs = gridspec.GridSpec(
                2, 4,
                figure=fig,
                width_ratios=[1.05, 1.0, 1.0, 0.8],
                height_ratios=[1.0, 0.90],
                hspace=0.38,
                wspace=0.32,
                left=0.05,
                right=0.97,
                top=0.92,
                bottom=0.04,
            )
            axes = [
                fig.add_subplot(gs[0, 0]),
                fig.add_subplot(gs[0, 1]),
                fig.add_subplot(gs[0, 2]),
                fig.add_subplot(gs[1, 0]),
                fig.add_subplot(gs[1, 1]),
                fig.add_subplot(gs[1, 2]),
                fig.add_subplot(gs[:, 3]),
            ]
            letters = "abcdefg"
        else:
            gs = gridspec.GridSpec(
                2, 3,
                figure=fig,
                width_ratios=[1.05, 1.0, 1.0],
                height_ratios=[1.0, 0.90],
                hspace=0.38,
                wspace=0.32,
                left=0.05,
                right=0.97,
                top=0.92,
                bottom=0.04,
            )
            axes = [
                fig.add_subplot(gs[i // 3, i % 3]) for i in range(6)
            ]
            letters = "abcdef"

        for ax, letter in zip(axes, letters):
            ax.text(
                0.02, 0.97,
                f"({letter})",
                transform=ax.transAxes,
                fontsize=10,
                fontweight="bold",
                va="top",
                ha="left",
                color="#111827",
                zorder=10,
            )

        self._draw_dominance_on_ax(
            dom_suit, dom_suit_score, comp_suit, "suitability",
            axes[0], transform, crs, mainland_gdf, context_gdf,
            extent, minx, maxx, miny, maxy,
        )
        axes[0].set_title(
            "Suitability Dominance\n"
            "(AHP-TOPSIS | Balanced Scenario)",
            fontsize=9.5,
            fontweight="bold",
            pad=4,
        )

        self._draw_potential_bars(axes[1], potential_results)
        axes[1].set_title(
            "Technical Potential by Scenario",
            fontsize=9.5,
            fontweight="bold",
        )

        self._draw_lcoe_distribution(axes[2], lcoe_results)
        axes[2].set_title(
            "LCOE Distribution ($/MWh)\nP10 — IQR — P90",
            fontsize=9.5,
            fontweight="bold",
        )

        self._draw_dominance_on_ax(
            dom_lcoe, dom_lcoe_val, comp_lcoe, "lcoe",
            axes[3], transform, crs, mainland_gdf, context_gdf,
            extent, minx, maxx, miny, maxy,
        )
        axes[3].set_title(
            "LCOE Dominance\n(Cheapest Technology per Pixel)",
            fontsize=9.5,
            fontweight="bold",
            pad=4,
        )

        self._draw_supply_curves_on_ax(axes[4], lcoe_results)
        axes[4].set_title(
            "Resource Cost Curves (Merit Order)",
            fontsize=9.5,
            fontweight="bold",
        )

        self._draw_summary_table(
            axes[5], potential_results, lcoe_results
        )
        axes[5].set_title(
            "Key Metrics — Balanced Scenario",
            fontsize=9.5,
            fontweight="bold",
        )

        if has_abatement:
            self._draw_abatement_summary(axes[6], abatement_results)
            axes[6].set_title(
                "GHG Abatement & Thermal Substitution",
                fontsize=9.5,
                fontweight="bold",
            )

        self.styler.add_standard_footer(fig)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(
            str(out_path),
            bbox_inches="tight",
            dpi=self.styler.dpi,
            facecolor=fig.get_facecolor(),
        )
        plt.close(fig)

    def _draw_abatement_summary(
        self,
        ax: plt.Axes,
        abat_res: Dict,
    ) -> None:
        """
        Render the GHG abatement KPI panel for the executive dashboard.

        Args:
            ax: Matplotlib axes to draw on
            abat_res: Abatement results dict from GHGAbatementCalculator
        """
        ax.axis("off")
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)

        required = ["subst_gwh", "co2_avoided_mt"]
        if not all(k in abat_res for k in required):
            ax.text(
                0.5, 0.5,
                "Abatement data incomplete",
                ha="center",
                va="center",
                fontsize=10,
                style="italic",
            )
            return

        subst_twh = abat_res.get("subst_gwh", 0) / 1000
        co2_mt = abat_res.get("co2_avoided_mt", 0)
        val_b = abat_res.get("total_value_b", 0)
        mac = abat_res.get(
            "mac_usd_tco2e", abat_res.get("mac_global", 0)
        )
        cp = abat_res.get("carbon_price", 80)
        capex_b = abat_res.get("capex_total_b", 0)
        ndc_cov = abat_res.get("ndc_coverage_pct", 0)
        ci_before = abat_res.get("ci_before", 0)
        ci_after = abat_res.get("ci_after", 0)
        net_av = abat_res.get("net_avoided_mt", co2_mt)

        ax.text(
            0.5, 0.97,
            "GHG Abatement — Electricity Sector",
            ha="center",
            va="top",
            fontsize=9,
            fontweight="bold",
            color="#111827",
        )
        ax.axhline(0.91, color="#D1D5DB", linewidth=0.8)

        kpis = [
            (f"{co2_mt:.2f}", "MtCO₂e/yr\nAvoided (gross)", "#991b1b", 0.15),
            (f"${val_b:.2f}B", "USD/yr\nEcon. Value", "#15803d", 0.50),
            (f"{subst_twh:.1f}", "TWh/yr\nSubstituted", "#1e40af", 0.85),
        ]
        for val_str, lbl, col, x in kpis:
            ax.text(
                x, 0.82, val_str,
                ha="center",
                fontsize=22,
                fontweight="bold",
                color=col,
            )
            ax.text(
                x, 0.70, lbl,
                ha="center",
                fontsize=8,
                style="italic",
                color="#374151",
            )

        ax.axhline(0.64, color="#D1D5DB", linewidth=0.8)

        mac_lbl = (
            "Self-financing"
            if mac <= 0
            else f"MAC ${mac:.1f}/tCO₂e"
        )
        details = [
            ("MAC", mac_lbl),
            ("Est. CAPEX", f"${capex_b:.2f} B USD"),
            ("Carbon price", f"${cp:.0f}/tCO₂e"),
            ("Net avoided", f"{net_av:.2f} MtCO₂e/yr"),
            (
                "Carbon intensity",
                f"{ci_before:.0f}->{ci_after:.0f} gCO₂/kWh",
            ),
            ("NDC 2030", f"{ndc_cov:.0f}% of gap"),
        ]
        y_start = 0.57
        for i, (lbl, val) in enumerate(details):
            row_y = y_start - i * 0.085
            ax.text(
                0.04, row_y,
                f"{lbl}:",
                ha="left",
                fontsize=8,
                color="#6B7280",
            )
            ax.text(
                0.96, row_y,
                val,
                ha="right",
                fontsize=8,
                fontweight="bold",
                color="#111827",
            )

        ax.axhline(0.05, color="#D1D5DB", linewidth=0.8)
        mac_color = (
            "#15803d" if mac <= 0
            else ("#d97706" if mac <= cp else "#991b1b")
        )
        mac_note = (
            "Self-financing" if mac <= 0
            else (
                "Carbon price covers MAC" if cp >= mac
                else f"Gap ${mac - cp:.1f}/tCO₂e to breakeven"
            )
        )
        ax.text(
            0.5, 0.025,
            mac_note,
            ha="center",
            fontsize=7.5,
            color=mac_color,
            bbox=dict(
                boxstyle="round,pad=0.3",
                facecolor="#F9FAFB",
                edgecolor="#D1D5DB",
                alpha=0.9,
            ),
        )

    # -----------------------------------------------------------------------
    # DASHBOARD HELPERS
    # -----------------------------------------------------------------------

    @staticmethod
    def _build_rgba_q1(
        dom_arr: np.ndarray,
        dom_score: np.ndarray,
        mode: str,
        H: int,
        W: int,
    ) -> np.ndarray:
        """
        Build RGBA array for dominance map rendering using the Q1 palette.

        Args:
            dom_arr: Dominance class array (0=none, 1=solar, 2=wind,
                     3=biomass)
            dom_score: Score or value array for alpha mapping
            mode: Either 'suitability' (higher=more opaque) or
                  'lcoe' (lower cost=more opaque)
            H: Array height
            W: Array width

        Returns:
            Float32 RGBA array of shape (H, W, 4) with values in [0, 1]
        """
        rgba = np.zeros((H, W, 4), dtype=np.float32)

        valid_scores = dom_score[np.isfinite(dom_score)]
        if valid_scores.size == 0:
            return rgba

        lo = float(np.percentile(valid_scores, 5))
        hi = float(np.percentile(valid_scores, 95))
        denom = max(hi - lo, 1e-6)

        Q1_RGB = {
            "solar": (0.878, 0.482, 0.224),
            "wind": (0.227, 0.490, 0.788),
            "biomass": (0.227, 0.620, 0.373),
        }

        for i, tech in enumerate(TECH_ORDER):
            mask = dom_arr == (i + 1)
            if not np.any(mask):
                continue
            r, g, b = Q1_RGB.get(tech, (0.5, 0.5, 0.5))
            if mode == "suitability":
                alpha_map = np.clip(
                    dom_score * 0.85 + 0.15, 0.45, 0.92
                ).astype(np.float32)
            else:
                alpha_map = np.where(
                    np.isfinite(dom_score),
                    np.clip(
                        1.0 - (dom_score - lo) / denom * 0.55,
                        0.45,
                        0.92,
                    ),
                    0.0,
                ).astype(np.float32)
            rgba[mask, 0] = r
            rgba[mask, 1] = g
            rgba[mask, 2] = b
            rgba[mask, 3] = alpha_map[mask]
        return rgba

    def _draw_dominance_on_ax(
        self,
        dom_arr: np.ndarray,
        dom_score: np.ndarray,
        competition: np.ndarray,
        mode: str,
        ax: plt.Axes,
        transform: Affine,
        crs: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        extent: List[float],
        minx: float,
        maxx: float,
        miny: float,
        maxy: float,
    ) -> None:
        """
        Render a dominance map onto an existing axes object.

        Args:
            dom_arr: Dominance class array
            dom_score: Score or value array
            competition: Competition zone boolean mask
            mode: Either 'suitability' or 'lcoe'
            ax: Target matplotlib axes
            transform: Rasterio affine transform
            crs: Coordinate reference system string
            mainland_gdf: Mainland geometry GeoDataFrame
            context_gdf: Neighbouring countries GeoDataFrame
            extent: Map extent [minx, maxx, miny, maxy]
            minx: Minimum longitude
            maxx: Maximum longitude
            miny: Minimum latitude
            maxy: Maximum latitude
        """
        H, W = dom_arr.shape
        rgba = self._build_rgba_q1(dom_arr, dom_score, mode, H, W)

        self.styler.draw_basemap(
            ax, crs, mainland_gdf, context_gdf, self._admin_gdf
        )
        ax.set_facecolor(self.styler.ocean_color)

        ax.imshow(
            rgba,
            extent=extent,
            origin="upper",
            zorder=3,
            interpolation="nearest",
        )

        try:
            ax.contourf(
                np.linspace(minx, maxx, W),
                np.linspace(maxy, miny, H),
                competition.astype(np.float32),
                levels=[0.5, 1.5],
                hatches=["///"],
                colors=["none"],
                zorder=4,
                alpha=0.40,
            )
        except Exception:
            pass

        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)
        ax.set_xlabel("Lon", fontsize=7)
        ax.set_ylabel("Lat", fontsize=7)
        ax.tick_params(labelsize=6)
        ax.grid(False)

        if self._admin_gdf is not None:
            self.styler.draw_admin_labels(
                ax, self._admin_gdf,
                minx, maxx, miny, maxy,
                max_labels=6,
            )

        handles = [
            mpatches.Patch(
                facecolor=TECH_META[t]["color"],
                alpha=0.85,
                label=TECH_META[t]["short"],
                edgecolor="#555",
                linewidth=0.4,
            )
            for t in TECH_ORDER
        ]
        ax.legend(
            handles=handles,
            fontsize=6.5,
            framealpha=0.92,
            edgecolor="#E5E7EB",
            loc="lower right",
            handlelength=1.2,
            borderpad=0.4,
        )

    def _draw_potential_bars(
        self,
        ax: plt.Axes,
        potential_results: Dict,
    ) -> None:
        """
        Render grouped bar chart of technical potential by scenario.

        Args:
            ax: Target matplotlib axes
            potential_results: Normalized potential results dict
        """
        scenarios = ["optimistic", "balanced", "conservative"]
        s_colors = ["#93C5FD", "#3B82F6", "#1E3A8A"]
        s_labels = ["Optimistic", "Balanced", "Conservative"]
        bw = 0.24
        x_pos = np.arange(len(TECH_ORDER))
        max_val = 0.0

        for j, (sc, col, lab) in enumerate(
            zip(scenarios, s_colors, s_labels)
        ):
            gw_vals = [
                self._get_scenario_data(
                    potential_results, tech, sc
                ).get("capacity_gw", 0.0)
                for tech in TECH_ORDER
            ]
            max_val = max(max_val, max(gw_vals) if gw_vals else 0)
            bars = ax.bar(
                x_pos + (j - 1) * bw,
                gw_vals,
                width=bw,
                color=col,
                label=lab,
                edgecolor="white",
                linewidth=0.5,
                alpha=0.92,
            )
            for bar, val in zip(bars, gw_vals):
                if val > 1:
                    ax.text(
                        bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + max_val * 0.012,
                        f"{val:.0f}",
                        ha="center",
                        va="bottom",
                        fontsize=5.5,
                        color="#374151",
                    )

        ax.set_xticks(x_pos)
        ax.set_xticklabels(
            [TECH_META[t]["short"] for t in TECH_ORDER], fontsize=8
        )
        ax.set_ylabel("Installable Capacity (GW)", fontsize=8)
        ax.legend(
            fontsize=7,
            framealpha=0.9,
            ncol=3,
            loc="upper right",
            edgecolor="#E5E7EB",
        )
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(labelsize=7)
        ax.yaxis.set_major_formatter(
            FuncFormatter(lambda v, _: f"{v:.0f}")
        )
        ax.set_xlim(-0.5, len(TECH_ORDER) - 0.5)

    def _draw_lcoe_distribution(
        self,
        ax: plt.Axes,
        lcoe_results: Dict,
    ) -> None:
        """
        Render horizontal LCOE distribution plot (P10-IQR-P90).

        Args:
            ax: Target matplotlib axes
            lcoe_results: Normalized LCOE results dict
        """
        y_positions = [2, 1, 0]
        for tech, yp in zip(TECH_ORDER, y_positions):
            stats = (
                lcoe_results.get("techs", {})
                .get(tech, {})
                .get("stats", {})
            )
            bench = LCOE_BENCHMARK_USD_MWH.get(tech, {})
            lp = TECH_META[tech]
            if not stats:
                continue

            ax.plot(
                [stats.get("p10", 0), stats.get("p90", 0)],
                [yp, yp],
                color=lp["color"],
                linewidth=2.0,
                solid_capstyle="round",
                zorder=4,
            )
            ax.barh(
                yp,
                stats.get("p75", 0) - stats.get("p25", 0),
                left=stats.get("p25", 0),
                height=0.38,
                color=lp["color"],
                alpha=0.35,
                edgecolor=lp["color"],
                linewidth=0.9,
            )
            ax.scatter(
                stats.get("median", 0),
                yp,
                color=lp["color"],
                s=60,
                zorder=5,
                edgecolors="white",
                linewidths=0.8,
            )
            if bench.get("median"):
                ax.axvline(
                    bench["median"],
                    color=lp["color"],
                    linewidth=0.9,
                    linestyle=":",
                    alpha=0.45,
                    zorder=3,
                )

        ax.set_yticks(y_positions)
        ax.set_yticklabels(
            [TECH_META[t]["short"] for t in reversed(TECH_ORDER)],
            fontsize=8,
        )
        ax.set_xlabel("LCOE ($/MWh)", fontsize=8)
        ax.axvline(
            75,
            color="#374151",
            linewidth=0.9,
            linestyle="--",
            alpha=0.55,
            label="Market ~75 $/MWh",
        )
        ax.legend(fontsize=7, framealpha=0.9, edgecolor="#E5E7EB")
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(labelsize=7)
        ax.set_ylim(-0.5, 2.5)

    def _draw_summary_table(
        self,
        ax: plt.Axes,
        potential_results: Dict,
        lcoe_results: Dict,
    ) -> None:
        """
        Render the key metrics summary table for the balanced scenario.

        Args:
            ax: Target matplotlib axes
            potential_results: Normalized potential results dict
            lcoe_results: Normalized LCOE results dict
        """
        ax.axis("off")
        headers = [
            "Technology",
            "Capacity\n(GW)",
            "Generation\n(TWh/yr)",
            "Mean LCOE\n($/MWh)",
            "P10 LCOE\n($/MWh)",
        ]
        rows = []
        t_gw = t_twh = 0.0

        for tech in TECH_ORDER:
            sc = self._get_scenario_data(
                potential_results, tech, "balanced"
            )
            stats = (
                lcoe_results.get("techs", {})
                .get(tech, {})
                .get("stats", {})
            )
            gw = sc.get("capacity_gw", 0.0)
            twh = sc.get("generation_twh", sc.get("gen_twh", 0.0))
            t_gw += gw
            t_twh += twh
            rows.append([
                TECH_META[tech]["label"],
                f"{gw:.1f}",
                f"{twh:.1f}",
                f"{stats.get('mean', 0):.1f}",
                f"{stats.get('p10', 0):.1f}",
            ])

        rows.append(["TOTAL", f"{t_gw:.1f}", f"{t_twh:.1f}", "—", "—"])
        tbl = ax.table(
            cellText=rows,
            colLabels=headers,
            cellLoc="center",
            loc="center",
            bbox=[0, 0, 1, 1],
        )
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(7.5)

        for j in range(len(headers)):
            tbl[0, j].set_facecolor("#1E3A8A")
            tbl[0, j].set_text_props(color="white", fontweight="bold")

        row_colors = ["#FEF9C3", "#DBEAFE", "#DCFCE7", "#F3F4F6"]
        for i, bg in enumerate(row_colors):
            for j in range(len(headers)):
                tbl[i + 1, j].set_facecolor(bg)
                tbl[i + 1, j].set_text_props(
                    fontweight="bold" if i == 3 else "normal"
                )

    def _draw_supply_curves_on_ax(
        self,
        ax: plt.Axes,
        lcoe_results: Dict,
    ) -> None:
        """
        Render supply cost curves (merit order) for all technologies.

        Args:
            ax: Target matplotlib axes
            lcoe_results: Normalized LCOE results dict with supply_curve
                          DataFrames per technology
        """
        for tech in TECH_ORDER:
            sc = (
                lcoe_results.get("techs", {})
                .get(tech, {})
                .get("supply_curve")
            )
            lp = TECH_META[tech]
            if sc is None or not isinstance(sc, pd.DataFrame) or sc.empty:
                continue

            col = next(
                (
                    c for c in ["lcoe_usd_mwh", "lcoe_eur_mwh"]
                    if c in sc.columns
                ),
                None,
            )
            if col is None:
                continue

            if len(sc) > 2000:
                sc = sc.iloc[
                    np.linspace(0, len(sc) - 1, 2000, dtype=int)
                ].reset_index(drop=True)

            ax.fill_between(
                sc["cum_capacity_gw"],
                sc[col],
                alpha=0.10,
                color=lp["color"],
            )
            ax.plot(
                sc["cum_capacity_gw"],
                sc[col],
                color=lp["color"],
                linewidth=1.8,
                label=lp["short"],
            )
            if LCOE_BENCHMARK_USD_MWH.get(tech, {}).get("median"):
                ax.axhline(
                    LCOE_BENCHMARK_USD_MWH[tech]["median"],
                    color=lp["color"],
                    linewidth=0.8,
                    linestyle="--",
                    alpha=0.40,
                )

        ax.axhline(
            75,
            color="#374151",
            linewidth=0.9,
            linestyle=":",
            alpha=0.55,
            label="~75 $/MWh",
        )
        ax.set_xlabel("Cumulative Capacity (GW)", fontsize=8)
        ax.set_ylabel("LCOE ($/MWh)", fontsize=8)
        ax.legend(
            fontsize=7,
            framealpha=0.9,
            edgecolor="#E5E7EB",
            loc="upper left",
        )
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.18, linestyle="--")
        ax.tick_params(labelsize=7)
        ax.set_xlim(left=0)

    # -----------------------------------------------------------------------
    # GEOTIFF EXPORT
    # -----------------------------------------------------------------------

    def _export_geotiffs(
        self,
        country_code: str,
        country_name: str,
        suit_arrays: Dict[str, Optional[np.ndarray]],
        lcoe_arrays: Dict[str, Optional[np.ndarray]],
        dom_suit: np.ndarray,
        dom_lcoe: np.ndarray,
        transform: Affine,
        crs: str,
        H: int,
        W: int,
        out_gis: Path,
    ) -> List[str]:
        """
        Export suitability, LCOE, and dominance arrays as GeoTIFFs.

        Args:
            country_code: ISO-3166-alpha-3 code
            country_name: Full country name
            suit_arrays: Dict of suitability arrays per technology
            lcoe_arrays: Dict of LCOE arrays per technology
            dom_suit: Suitability dominance array
            dom_lcoe: LCOE dominance array
            transform: Rasterio affine transform
            crs: Coordinate reference system string
            H: Raster height
            W: Raster width
            out_gis: Output directory for GIS files

        Returns:
            List of exported filenames
        """
        exported = []
        base_tags = {
            "COUNTRY": country_name,
            "COUNTRY_CODE": country_code,
            "CREATED": datetime.now().isoformat(),
            "SOURCE": "GeoWorld Framework — Phase 6",
            "CRS": crs,
        }

        def _w_float(arr, path, desc, unit):
            out = np.where(
                np.isfinite(arr), arr, NODATA_FLOAT
            ).astype(np.float32)
            with rasterio.open(
                str(path), "w",
                driver="GTiff",
                height=H,
                width=W,
                count=1,
                dtype="float32",
                crs=crs,
                transform=transform,
                compress="lzw",
                predictor=3,
                nodata=NODATA_FLOAT,
            ) as dst:
                dst.write(out, 1)
                dst.update_tags(
                    DESCRIPTION=desc, UNIT=unit, **base_tags
                )
            exported.append(path.name)
            logger.info("  GIS: %s", path.name)

        def _w_uint8(arr, path, desc, legend):
            with rasterio.open(
                str(path), "w",
                driver="GTiff",
                height=H,
                width=W,
                count=1,
                dtype="uint8",
                crs=crs,
                transform=transform,
                compress="lzw",
                predictor=1,
                nodata=0,
            ) as dst:
                dst.write(arr.astype(np.uint8), 1)
                dst.update_tags(
                    DESCRIPTION=desc, LEGEND=legend, **base_tags
                )
            exported.append(path.name)
            logger.info("  GIS: %s", path.name)

        for t in TECH_ORDER:
            if suit_arrays.get(t) is not None:
                _w_float(
                    suit_arrays[t],
                    out_gis / f"{country_code}_{t}_suitability.tif",
                    f"TOPSIS Suitability Score — {TECH_META[t]['label']}",
                    "dimensionless [0-1]",
                )
            if lcoe_arrays.get(t) is not None:
                _w_float(
                    lcoe_arrays[t],
                    out_gis / f"{country_code}_{t}_lcoe_usd_mwh.tif",
                    f"Levelized Cost of Energy — {TECH_META[t]['label']}",
                    "USD/MWh",
                )

        _w_uint8(
            dom_suit,
            out_gis / f"{country_code}_dominance_suitability.tif",
            "Technology Suitability Dominance "
            "(highest TOPSIS score)",
            "0=None, 1=Solar PV, 2=Wind Onshore, 3=Biomass/Bioenergy",
        )
        _w_uint8(
            dom_lcoe,
            out_gis / f"{country_code}_dominance_lcoe.tif",
            "Technology LCOE Dominance "
            "(cheapest technology per pixel)",
            "0=None, 1=Solar PV, 2=Wind Onshore, 3=Biomass/Bioenergy",
        )
        return exported

    # -----------------------------------------------------------------------
    # TEXT REPORT AND HELPERS
    # -----------------------------------------------------------------------

    def _format_report(
        self,
        results: Dict,
        country_name: str,
        code: str,
        potential_results: Dict,
        lcoe_results: Dict,
        dom_suit: np.ndarray,
        dom_lcoe: np.ndarray,
    ) -> str:
        """
        Format the Phase 6 synthesis report as plain text.

        Args:
            results: Results dict with timings and exported files
            country_name: Full country name
            code: Country ISO code
            potential_results: Normalized potential results dict
            lcoe_results: Normalized LCOE results dict
            dom_suit: Suitability dominance array
            dom_lcoe: LCOE dominance array

        Returns:
            Formatted report string
        """
        lines = []

        def sep(c="="):
            lines.append(c * 72)

        sep()
        lines.append(
            f"  RESULTS SYNTHESIS REPORT — Phase 6\n"
            f"  {country_name} ({code})\n"
            f"  {results['timestamp'][:19].replace('T', ' ')}"
        )
        sep()
        lines.append("")

        total_s = max(int((dom_suit > 0).sum()), 1)
        sep("-")
        lines.append("  SUITABILITY DOMINANCE — TOPSIS Score")
        sep("-")
        for i, t in enumerate(TECH_ORDER):
            px = int((dom_suit == i + 1).sum())
            lines.append(
                f"  {TECH_META[t]['label']:<26} "
                f"{px:>8,} px  ({px / total_s * 100:>5.1f}%)"
            )

        lines.append("")
        total_l = max(int((dom_lcoe > 0).sum()), 1)
        sep("-")
        lines.append("  LCOE DOMINANCE — Cheapest Technology per Pixel")
        sep("-")
        for i, t in enumerate(TECH_ORDER):
            px = int((dom_lcoe == i + 1).sum())
            lines.append(
                f"  {TECH_META[t]['label']:<26} "
                f"{px:>8,} px  ({px / total_l * 100:>5.1f}%)"
            )

        lines.append("")
        sep("-")
        lines.append("  BALANCED SCENARIO — INTEGRATED SUMMARY")
        sep("-")
        lines.append(
            f"  {'Technology':<22} {'GW':>8} {'TWh/yr':>9} "
            f"{'km²':>10} {'$/MWh':>9}"
        )
        lines.append("  " + "─" * 62)

        t_gw = t_twh = 0.0
        for t in TECH_ORDER:
            sc = self._get_scenario_data(
                potential_results, t, "balanced"
            )
            stats = (
                lcoe_results.get("techs", {})
                .get(t, {})
                .get("stats", {})
            )
            gw = sc.get("capacity_gw", 0.0)
            twh = sc.get(
                "generation_twh", sc.get("gen_twh", 0.0)
            )
            area = sc.get("area_km2", 0.0)
            lce = stats.get("mean", 0.0)
            t_gw += gw
            t_twh += twh
            lines.append(
                f"  {TECH_META[t]['label']:<22} "
                f"{gw:>8.1f} {twh:>9.1f} "
                f"{area:>10,.0f} {lce:>9.1f}"
            )

        lines.append("  " + "─" * 62)
        lines.append(
            f"  {'TOTAL':<22} {t_gw:>8.1f} {t_twh:>9.1f}\n"
        )

        sep("-")
        lines.append("  GeoTIFFs EXPORTED  ->  results/gis/")
        sep("-")
        for f in results.get("exported_tifs", []):
            lines.append(f"  [OK]  {f}")

        lines.append("")
        sep()
        lines.append("  TIMINGS")
        sep("-")
        for step, t in results.get("timings", {}).items():
            lines.append(f"    {step:<32}: {t:>6.1f}s")
        lines.append(
            f"    {'TOTAL':<32}: "
            f"{results.get('elapsed_total', 0):>6.1f}s"
        )
        sep()
        lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _get_scenario_data(
        potential_results: Dict,
        tech: str,
        scenario: str,
    ) -> Dict:
        """
        Extract scenario data from potential results with fallback lookup.

        Args:
            potential_results: Normalized potential results dict
            tech: Technology name (solar, wind, biomass)
            scenario: Scenario name (optimistic, balanced, conservative)

        Returns:
            Scenario data dictionary, empty dict if not found
        """
        sc = (
            potential_results.get("techs", {})
            .get(tech, {})
            .get("scenarios", {})
            .get(scenario, {})
        )
        if sc:
            return sc
        return (
            potential_results.get(tech, {})
            .get("scenarios", {})
            .get(scenario, {})
            or {}
        )