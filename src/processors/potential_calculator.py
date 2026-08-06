"""
src/processors/potential_calculator.py
======================================
Phase 4 — Spatial-Technical Energy Potential.

Calculates installable capacity (GW) and annual generation (TWh/yr)
from spatial suitability outputs (Phase 3), applying geodetic area
corrections and technology-specific deployment constraints.

Input selection
---------------
Phase 3 produces both TOPSIS and OWA suitability GeoTIFFs:

  - TOPSIS (primary): ``{code}_{tech}_suitability.tif``
  - OWA per scenario: ``{code}_{tech}_suitability_owa_{scenario}.tif``

By default Phase 4 uses the **TOPSIS** output as the primary suitability
surface and applies per-scenario thresholds (from ``build_tech_params``,
which incorporates the ``settings.yaml`` scenario offsets) to define the
apt-pixel mask.

The OWA GeoTIFFs are available in the suitability directory and can be
selected by passing ``use_owa=True`` to ``_run_technology`` (reserved for
future scenario-specific runs; not yet wired in the orchestrator).

Scenario thresholds
-------------------
Phase 3 forwards ``scenario_thresholds`` (offsets relative to the
technology base threshold) in its return dict.  ``build_tech_params``
already applies these offsets when constructing
``params["thresholds"][scenario]``, so no double-application occurs.
The thresholds actually used are logged per technology/scenario.

GeoTIFF export
--------------
Phase 4 now exports **suitable-pixel masks** (boolean TIFFs) per
technology/scenario to ``outputs/{code}/potential/tifs/``.  These TIFFs
enable Phase 6 (Results Synthesis) to compute pixel areas directly from
disk, eliminating the Helmert fallback and ensuring consistency with
Phase 4 capacity calculations.

References
----------
.. [1] IRENA (2024). Renewable Power Generation Costs in 2023.
.. [2] Hoogwijk et al. (2004). Biomass potential assessment.
.. [3] Eurek et al. (2017). Wind potential. Nature Energy.
"""

from __future__ import annotations

import logging
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
from rasterio.transform import Affine

from src.core.constants import (
    FALLBACK_SUITABILITY_THRESHOLD,
    TECH_ORDER,
    build_tech_params,
)
from src.core.validators import (
    validate_raster_exists,
    validate_raster_crs,
    validate_raster_shape,
)
from src.utils.geo_stats import (
    build_pixel_area_array,
    pixel_area_km2,
    zonal_stats_raster,
)
from src.utils.map_styling import GeoWorldStyler
from src.core.schemas import PotentialResult
from src.utils.timing import timer as _timer

logger = logging.getLogger("geoworld.processors.PotentialCalculator")

SCENARIOS: List[str] = ["optimistic", "balanced", "conservative"]

SCENARIO_COLORS: Dict[str, str] = {
    "optimistic":   "#1B5E20",
    "balanced":     "#1565C0",
    "conservative": "#B71C1C",
}

SCENARIO_ALPHA: Dict[str, float] = {
    "optimistic":   0.85,
    "balanced":     1.00,
    "conservative": 0.85,
}

_TECH_REPORT_LABELS: Dict[str, str] = {
    "solar":   "SOLAR PV",
    "wind":    "WIND ONSHORE",
    "biomass": "BIOMASS / BIOENERGY",
}

_PANEL_GAP_PX: int   = 8
_TITLE_FRAC: float   = 0.06
_FOOTER_FRAC: float  = 0.025
_TITLE_MIN_PX: int   = 80
_FOOTER_MIN_PX: int  = 30


# ═══════════════════════════════════════════════════════════════════════════
# POTENTIAL CALCULATOR
# ═══════════════════════════════════════════════════════════════════════════


class PotentialCalculator:
    """Phase 4: Suitability → Installable Capacity (GW) + Generation (TWh/yr).

    For each technology and scenario:
      1. Loads the TOPSIS suitability GeoTIFF from Phase 3.
      2. Applies the per-scenario threshold (from ``build_tech_params``).
      3. Computes geodetically corrected pixel areas.
      4. Applies ``land_use_factor`` and ``power_density_mw_km2``.
      5. Computes annual generation via ``capacity_factor × hours_year``.
      6. Runs zonal statistics against admin-level boundaries.
      7. **Exports suitable-pixel masks** (boolean TIFFs) to ``tifs/``.

    OWA suitability maps (``{code}_{tech}_suitability_owa_{scenario}.tif``)
    are produced by Phase 3 and stored alongside the TOPSIS output.  This
    module currently uses TOPSIS as the primary surface; OWA-based scenario
    analysis is reserved for future extension.
    """

    def __init__(self, cfg: Any, outputs_dir: Path) -> None:
        self.cfg         = cfg
        self.outputs_dir = Path(outputs_dir)
        self.viz_cfg     = cfg.system.get("visualization", {})
        self._admin_gdf: Optional[gpd.GeoDataFrame] = None
        self.tech_params: Optional[Dict[str, Dict]] = None
        pipeline_dpi     = cfg.system.get("pipeline", {}).get("map_dpi_export", 150)
        self.styler      = GeoWorldStyler(self.viz_cfg, global_dpi=pipeline_dpi)

    # ------------------------------------------------------------------
    # PUBLIC ENTRY POINT
    # ------------------------------------------------------------------

    def run(
        self,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        suitability_dir: Path,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        country_params=None,
    ) -> Dict[str, Any]:
        """Execute Phase 4: calculate technical potential from suitability maps.

        Accepts *country_params* as either a Pydantic ``CountryParams`` model
        or a plain ``dict`` — both are handled transparently by
        ``build_tech_params()``, which prefers ``CountryParams.to_flat_dict()``
        when available.

        Args:
            country_code:    ISO-3166 alpha-3 code.
            country_name:    Full country name for labelling.
            mainland_gdf:    Country boundary GeoDataFrame.
            suitability_dir: Directory containing Phase 3 GeoTIFF outputs.
            context_gdf:     Neighbouring-countries GeoDataFrame (optional).
            country_params:  Per-country parameters (``CountryParams`` or dict).

        Returns:
            Validated ``PotentialResult.model_dump()`` — a serialisable dict
            with per-technology capacity and generation estimates.
        """
        self.tech_params = build_tech_params(self.cfg.system, country_params)
        self._log_tech_params()

        started_at   = datetime.now()
        timings: Dict[str, float] = {}
        suitability_dir = Path(suitability_dir)

        # ── 1. Locate suitability TIFs ─────────────────────────────────
        tif_paths: Dict[str, Path] = {}
        ref_tif:   Optional[Path]  = None

        for tech in TECH_ORDER:
            tif_path = suitability_dir / f"{country_code}_{tech}_suitability.tif"
            if not tif_path.exists():
                logger.warning(
                    "  Suitability TIF missing for %s: %s", tech, tif_path
                )
                continue
            tif_paths[tech] = tif_path
            if ref_tif is None:
                ref_tif = tif_path

        if not tif_paths:
            raise RuntimeError(
                "No suitability TIFs found in %s. Run Phase 3 first."
                % suitability_dir
            )

        # Load reference spatial metadata
        with rasterio.open(str(ref_tif)) as src:
            expected_crs   = str(src.crs)
            expected_shape = (src.height, src.width)
            ref_transform  = src.transform  # ← salvar para exportar TIFs

        # Validate and log per-technology input statistics
        for tech, path in tif_paths.items():
            validate_raster_exists(path, f"suitability_{tech}")
            validate_raster_crs(path, expected_crs)
            validate_raster_shape(path, expected_shape)
            with rasterio.open(str(path)) as src:
                arr = src.read(1).astype(np.float32)
                if src.nodata is not None:
                    arr = np.where(arr == src.nodata, np.nan, arr)
                finite = arr[np.isfinite(arr)]
                if finite.size:
                    logger.info(
                        "  Input %-12s: min=%.3f  max=%.3f  "
                        "mean=%.3f  finite=%.1f%%",
                        tech,
                        float(np.min(finite)),
                        float(np.max(finite)),
                        float(np.mean(finite)),
                        100.0 * finite.size / arr.size,
                    )
                else:
                    logger.warning("  Input %-12s: no finite values.", tech)

        # ── 2. Output directories ──────────────────────────────────────
        out_base = self.outputs_dir / country_code / "potential"
        out_fig  = out_base / "figures"
        out_data = out_base / "data"
        out_rep  = out_base / "reports"
        out_tif  = out_base / "tifs"  # ← NOVO: exportar TIFs aqui
        for d in (out_fig, out_data, out_rep, out_tif):
            d.mkdir(parents=True, exist_ok=True)

        self._admin_gdf = self.styler.load_admin_boundaries(
            country_name, mainland_gdf, Path(self.cfg.raw_path),
        )
        if self._admin_gdf is not None:
            logger.info(
                "  Admin boundaries: %d regions", len(self._admin_gdf)
            )

        results: Dict[str, Any] = {
            "country":   country_code,
            "timestamp": started_at.isoformat(),
            "techs":     {},
            "timings":   timings,
        }

        # ── 3. Per-technology calculation ──────────────────────────────
        for tech in TECH_ORDER:
            if tech not in tif_paths:
                logger.warning(
                    "  [%s] Skipping: suitability TIF not found.", tech
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
                    tech=tech,
                    tif_path=tif_paths[tech],
                    country_code=country_code,
                    country_name=country_name,
                    mainland_gdf=mainland_gdf,
                    context_gdf=context_gdf,
                    out_fig=out_fig,
                    out_data=out_data,
                    out_tif=out_tif,           # ← passar diretório TIF
                    ref_transform=ref_transform,  # ← metadados espaciais
                    ref_crs=expected_crs,
                )

        # ── 4. Comparison and scenario charts ──────────────────────────
        with _timer("comparison_map", timings):
            self._plot_comparison(
                results, country_name, country_code,
                mainland_gdf, context_gdf,
                out_fig / f"{country_code}_potential_comparison.png",
            )

        with _timer("scenario_chart", timings):
            self._plot_scenario_chart(
                results, country_name, country_code,
                out_fig / f"{country_code}_potential_scenarios.png",
            )

        results["elapsed_total"] = round(
            (datetime.now() - started_at).total_seconds(), 1
        )

        # ── 5. Report ──────────────────────────────────────────────────
        report = self._format_report(results, country_name, country_code)
        logger.info("\n%s", report)

        rep_path = (
            out_rep
            / f"{country_code}_potential_"
            f"{started_at.strftime('%Y%m%d_%H%M%S')}.txt"
        )
        rep_path.write_text(report, encoding="utf-8")

        # ── 6. Completeness check ──────────────────────────────────────
        generated = [t for t in TECH_ORDER if t in results["techs"]]
        missing   = [t for t in TECH_ORDER if t not in generated]
        if missing:
            logger.warning(
                "  Missing potential results for: %s", ", ".join(missing)
            )
        else:
            logger.info("  All expected potential results generated.")

        # ── 7. Strip non-serialisable objects, validate, return ────────
        clean_results: Dict[str, Any] = {
            "country_code":  results["country"],
            "timestamp":     results["timestamp"],
            "elapsed_total": results["elapsed_total"],
            "timings":       results["timings"],
            "techs":         {},
        }

        _DROP_KEYS = frozenset({"cap_map", "zonal_df"})

        for tech, data in results["techs"].items():
            tech_serial = {
                "tech":      tech,
                "label":     data.get("label"),
                "params":    data.get("params"),
                "scenarios": {},
            }
            for sc, sc_data in data.get("scenarios", {}).items():
                tech_serial["scenarios"][sc] = {
                    k: v for k, v in sc_data.items()
                    if k not in _DROP_KEYS
                }
            clean_results["techs"][tech] = tech_serial

        validated = PotentialResult(**clean_results)
        return validated.model_dump()

    # ------------------------------------------------------------------
    # PRIVATE: PARAMETER LOGGING
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # PRIVATE: SINGLE-TECHNOLOGY PIPELINE
    # ------------------------------------------------------------------

    def _run_technology(
        self,
        tech: str,
        tif_path: Path,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        out_fig: Path,
        out_data: Path,
        out_tif: Path,  # ← NOVO
        ref_transform: Affine,  # ← NOVO
        ref_crs: str,  # ← NOVO
    ) -> Dict[str, Any]:
        """Calculate potential for a single technology across all scenarios.

        **NEW**: Exports suitable-pixel masks (boolean TIFFs) to ``out_tif/``
        for each scenario, enabling Phase 6 to compute pixel areas directly
        from disk without Helmert fallback.

        Returns a dict with keys ``"tech"``, ``"label"``, ``"params"``,
        ``"scenarios"``.  Each scenario entry includes ``cap_map``
        (``np.ndarray``) and ``zonal_df`` (``pd.DataFrame``) which are
        consumed by ``_format_report`` and ``_plot_*`` methods before being
        stripped in ``run()`` for serialisation.

        Scenario thresholds
        ~~~~~~~~~~~~~~~~~~~
        ``params["thresholds"][scenario]`` already incorporates the
        ``settings.yaml`` scenario offsets (applied by ``build_tech_params``).
        No additional offset is applied here.
        """
        params = self.tech_params[tech]

        with rasterio.open(str(tif_path)) as src:
            suit_arr  = src.read(1).astype(np.float32)
            transform = src.transform
            crs       = str(src.crs)
            height, width = src.height, src.width
            if src.nodata is not None:
                suit_arr = np.where(suit_arr == src.nodata, np.nan, suit_arr)

        # ✅ FIX (dup_geo_area): usar SEMPRE build_pixel_area_array() canônico
        area_arr   = build_pixel_area_array(transform, height, width)
        lat_center = transform.f + transform.e * height / 2.0

        logger.info(
            "  [%s] Grid: %d×%d px | lat_center=%.2f° | "
            "px_area≈%.4f km²",
            tech, height, width, lat_center,
            pixel_area_km2(transform, lat_center),
        )

        scenario_results: Dict[str, Dict] = {}

        for scenario in SCENARIOS:
            threshold = params["thresholds"].get(scenario, FALLBACK_SUITABILITY_THRESHOLD)
            apt_mask  = np.isfinite(suit_arr) & (suit_arr >= threshold)
            n_apt     = int(apt_mask.sum())

            if n_apt == 0:
                logger.warning(
                    "  [%s/%s] No valid pixels at threshold %.2f.",
                    tech, scenario, threshold,
                )
                scenario_results[scenario] = {
                    "threshold":       threshold,
                    "n_pixels":        0,
                    "area_km2":        0.0,
                    "area_eff_km2":    0.0,
                    "capacity_mw":     0.0,
                    "capacity_gw":     0.0,
                    "generation_gwh":  0.0,
                    "generation_twh":  0.0,
                    "cap_map":         None,
                    "zonal_df":        pd.DataFrame(),
                }
                continue

            area_apt  = float(area_arr[apt_mask].sum())
            area_eff  = area_apt * params["land_use_factor"]
            cap_mw    = area_eff * params["power_density_mw_km2"]
            cap_gw    = cap_mw / 1_000.0
            gen_gwh   = (
                cap_mw * params["capacity_factor"] * params["hours_year"]
                / 1_000.0
            )
            gen_twh   = gen_gwh / 1_000.0

            logger.info(
                "  [%s/%s] thr=%.2f | area=%s km² | "
                "eff=%s km² | cap=%.2f GW | gen=%.2f TWh/yr",
                tech, scenario, threshold,
                f"{area_apt:,.0f}", f"{area_eff:,.0f}",
                cap_gw, gen_twh,
            )

            # Pixel-level capacity map (MW per pixel)
            cap_map = np.full((height, width), np.nan, dtype=np.float32)
            cap_map[apt_mask] = (
                area_arr[apt_mask]
                * params["land_use_factor"]
                * params["power_density_mw_km2"]
            )

            # ✅ CORRIGIDO: Exportar máscara suitable com valores corretos
            tif_name = f"{country_code}_{tech}_suitable_{scenario}.tif"
            tif_out  = out_tif / tif_name

            # Criar array uint8: 255 = suitable, 0 = not suitable
            suitable_uint8 = np.where(apt_mask, np.uint8(255), np.uint8(0))

            with rasterio.open(
                tif_out, 'w',
                driver='GTiff',
                height=height,
                width=width,
                count=1,
                dtype=np.uint8,
                crs=ref_crs,
                transform=ref_transform,
                compress='lzw',
                nodata=0,  # ← 0 = NoData (pixels não-suitable)
            ) as dst:
                dst.write(suitable_uint8, 1)
                dst.update_tags(
                    DESCRIPTION=f"Suitable pixels for {tech} ({scenario} scenario)",
                    THRESHOLD=str(threshold),
                )

            # Zonal statistics
            zonal_df = zonal_stats_raster(
                cap_map, apt_mask,
                self._admin_gdf, transform, crs, "capacity_mw",
            )
            if not zonal_df.empty:
                zonal_df["generation_gwh"] = (
                    zonal_df["capacity_mw_sum"]
                    * params["capacity_factor"]
                    * params["hours_year"]
                    / 1_000.0
                )
                # Real geodesic area per region (BLOCKER-003), merged in so
                # downstream recovery can sum area_km2_sum instead of
                # re-deriving area from the suitable-pixel TIF.
                area_zonal_df = zonal_stats_raster(
                    area_arr, apt_mask,
                    self._admin_gdf, transform, crs, "area_km2",
                )
                if not area_zonal_df.empty:
                    zonal_df = zonal_df.merge(
                        area_zonal_df[["admin_name", "area_km2_sum"]],
                        on="admin_name", how="left",
                    )
                csv_name = f"{country_code}_{tech}_{scenario}_zonal.csv"
                zonal_df.to_csv(
                    str(out_data / csv_name), index=False, encoding="utf-8"
                )

            scenario_results[scenario] = {
                "threshold":       threshold,
                "n_pixels":        n_apt,
                "area_km2":        round(area_apt, 1),
                "area_eff_km2":    round(area_eff, 1),
                "capacity_mw":     round(cap_mw, 1),
                "capacity_gw":     round(cap_gw, 3),
                "generation_gwh":  round(gen_gwh, 1),
                "generation_twh":  round(gen_twh, 3),
                "cap_map":         cap_map,    # stripped in run() after report
                "zonal_df":        zonal_df,   # stripped in run() after report
            }

        # ── Plot balanced scenario ─────────────────────────────────────
        balanced    = scenario_results.get("balanced", {})
        cap_map_bal = balanced.get("cap_map")

        if cap_map_bal is not None:
            valid = cap_map_bal[np.isfinite(cap_map_bal)]
            p90   = float(np.percentile(valid, 90)) if valid.size else 0.0

            self._plot_potential_map(
                suit_arr, cap_map_bal, transform, crs, tech, params,
                country_name, mainland_gdf, context_gdf, self._admin_gdf,
                out_fig / f"{country_code}_{tech}_potential_map.png", p90,
            )
            self._plot_technology_stats(
                tech, params, scenario_results,
                out_fig / f"{country_code}_{tech}_potential_stats.png",
            )

        return {
            "tech":      tech,
            "label":     params["label"],
            "params":    params,
            "scenarios": scenario_results,
        }

    # ------------------------------------------------------------------
    # PRIVATE: VISUALISATION
    # ------------------------------------------------------------------

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
            ax, crs, mainland_gdf, context_gdf, admin_gdf, extent=extent,
        )

        # Faint suitability underlay
        ax.imshow(
            np.where(np.isfinite(suit_arr) & (suit_arr > 0), suit_arr, np.nan),
            extent=extent, origin="upper", cmap="Greys",
            vmin=0, vmax=1.2, alpha=0.12, zorder=2,
            interpolation="bilinear",
        )

        # Capacity overlay
        cmap_cap = self.styler.make_cmap(
            params.get("cmap", "YlOrRd"),
            under="none", bad="none",
            vmin_frac=0.10, vmax_frac=1.0,
        )
        vmax = float(np.nanpercentile(cap_map, 98))
        im   = ax.imshow(
            np.where(np.isfinite(cap_map), cap_map, np.nan),
            extent=extent, origin="upper", cmap=cmap_cap,
            vmin=0, vmax=vmax, zorder=3, interpolation="bilinear",
        )

        # P90 contour
        if p90_val > 0:
            ax.contour(
                np.where(
                    np.isfinite(cap_map) & (cap_map >= p90_val), 1.0, np.nan,
                ),
                levels=[0.5],
                colors=[self.styler.CONTOUR_P90_COLOR],
                linewidths=[self.styler.CONTOUR_P90_LINEWIDTH],
                extent=extent, origin="upper", zorder=5,
            )

        self.styler.add_decorations(ax, vb[0], vb[2], vb[1], vb[3])
        self.styler.add_colorbar(
            fig, im, r"Technical Potential (MW px$^{-1}$)", extend="neither",
        )
        self.styler.add_standard_footer(
            fig, crs_metadata=f"CRS: {crs or 'EPSG:4326'}",
        )

        if admin_gdf is not None:
            self.styler.draw_admin_labels(
                ax, admin_gdf, vb[0], vb[2], vb[1], vb[3], max_labels=12,
            )

        thr = params["thresholds"]["balanced"]
        self.styler.add_standard_legend(
            ax,
            [
                mpatches.Patch(
                    facecolor=cmap_cap(0.65), alpha=0.90,
                    label=f"Suitable (Score ≥ {thr:.2f})",
                ),
                mpatches.Patch(
                    facecolor="none",
                    edgecolor=self.styler.CONTOUR_P90_COLOR,
                    linewidth=1.5,
                    label=f"Top 10% (≥ {p90_val:.2f} MW/px)",
                ),
            ],
            "lower_center", bbox_anchor=(0.5, -0.060), ncol=2,
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
        fig, (ax_dist, ax_scen) = plt.subplots(
            1, 2, figsize=(12, 4.5), dpi=self.styler.dpi,
        )
        fig.patch.set_facecolor(self.styler.fig_bg)

        # Left panel: top-5 districts (balanced scenario)
        zonal_df = scenario_res.get("balanced", {}).get("zonal_df")
        if zonal_df is not None and not zonal_df.empty:
            top5 = zonal_df.head(5).iloc[::-1]
            bars = ax_dist.barh(
                top5["admin_name"], top5["capacity_mw_sum"],
                color=params["color"], alpha=0.85,
                edgecolor="black", linewidth=0.5,
            )
            for bar in bars:
                w = bar.get_width()
                ax_dist.text(
                    w * 1.02, bar.get_y() + bar.get_height() / 2,
                    f"{w:,.0f} MW", ha="left", va="center", fontsize=9,
                )
            ax_dist.set_title(
                "Top 5 Districts (Balanced)", fontsize=11, fontweight="bold",
            )
            ax_dist.set_xlabel("Installable Capacity (MW)")
            ax_dist.spines[["top", "right"]].set_visible(False)
            ax_dist.set_xlim(
                0, top5["capacity_mw_sum"].max() * 1.25
            )

        # Right panel: scenario comparison
        gw_vals  = [scenario_res.get(s, {}).get("capacity_gw", 0) for s in SCENARIOS]
        colours  = ["#10B981", "#3B82F6", "#EF4444"]
        bars2    = ax_scen.bar(
            range(len(SCENARIOS)), gw_vals,
            color=colours, alpha=0.85,
            edgecolor="black", linewidth=0.5, width=0.5,
        )
        for i, bar in enumerate(bars2):
            h   = bar.get_height()
            twh = scenario_res.get(SCENARIOS[i], {}).get("generation_twh", 0)
            ax_scen.text(
                bar.get_x() + bar.get_width() / 2,
                h * 1.02,
                f"{h:.1f} GW\n({twh:.1f} TWh/yr)",
                ha="center", va="bottom", fontsize=9,
            )

        ax_scen.set_xticks(range(len(SCENARIOS)))
        ax_scen.set_xticklabels([s.capitalize() for s in SCENARIOS])
        ax_scen.set_title(
            "Potential by Scenario", fontsize=11, fontweight="bold",
        )
        ax_scen.set_ylabel("Total Capacity (GW)")
        ax_scen.spines[["top", "right"]].set_visible(False)
        y_max = max(gw_vals) * 1.25 if max(gw_vals) > 0 else 1
        ax_scen.set_ylim(0, y_max)

        fig.suptitle(
            f"{params['label']} — Technical Potential Summary",
            fontsize=13, fontweight="bold", y=0.98,
        )
        fig.subplots_adjust(
            top=0.88, bottom=0.12, left=0.08, right=0.97, wspace=0.35,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(out_path), bbox_inches="tight", dpi=self.styler.dpi)
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
        """Stitch per-technology potential maps into a single comparison panel."""
        panels = []
        for tech in TECH_ORDER:
            png = (
                self.outputs_dir / country_code / "potential" / "figures"
                / f"{country_code}_{tech}_potential_map.png"
            )
            if png.exists():
                panels.append(_PIL_Image.open(png).convert("RGB"))

        if not panels:
            logger.warning("  No potential map PNGs found for comparison.")
            return

        bg_rgb    = panels[0].getpixel((5, 5))
        target_h  = max(img.height for img in panels)
        normalised = []
        for img in panels:
            if img.height == target_h:
                normalised.append(img)
            else:
                padded = _PIL_Image.new("RGB", (img.width, target_h), bg_rgb)
                padded.paste(img, (0, (target_h - img.height) // 2))
                normalised.append(padded)

        gap      = _PANEL_GAP_PX
        title_h  = max(_TITLE_MIN_PX, int(target_h * _TITLE_FRAC))
        footer_h = max(_FOOTER_MIN_PX, int(target_h * _FOOTER_FRAC))
        total_w  = sum(p.width for p in normalised) + gap * (len(normalised) - 1)
        total_h  = title_h + target_h + footer_h

        canvas = _PIL_Image.new("RGB", (total_w, total_h), bg_rgb)
        x = 0
        for p in normalised:
            canvas.paste(p, (x, title_h))
            x += p.width + gap

        bg_hex  = "#{:02x}{:02x}{:02x}".format(*bg_rgb)
        fig     = plt.figure(
            figsize=(total_w / self.styler.dpi, total_h / self.styler.dpi),
            dpi=self.styler.dpi,
        )
        fig.patch.set_facecolor(bg_hex)
        ax_img = fig.add_axes([0, 0, 1, 1])
        ax_img.axis("off")
        ax_img.imshow(np.array(canvas), aspect="auto", interpolation="nearest")

        y_title   = 1.0 - (title_h * 0.4) / total_h
        font_main = max(12, int(total_w / self.styler.dpi))
        fig.text(
            0.5, y_title + 0.015,
            f"Renewable Energy Technical Potential — {country_name}",
            ha="center", va="center",
            fontsize=font_main, fontweight="bold", color="#1A1A1A",
            transform=fig.transFigure,
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(
            str(out_path), dpi=self.styler.dpi,
            bbox_inches="tight", pad_inches=0.1, facecolor=bg_hex,
        )
        plt.close(fig)

    def _plot_scenario_chart(
        self,
        results: Dict,
        country_name: str,
        country_code: str,
        out_path: Path,
    ) -> None:
        fig, (ax1, ax2) = plt.subplots(
            1, 2, figsize=(14, 6), dpi=self.styler.dpi,
        )
        fig.patch.set_facecolor(self.styler.fig_bg)
        fig.suptitle(
            f"Renewable Technical Potential — {country_name}\n"
            "Scenario Comparison",
            fontsize=12, fontweight="bold",
        )

        x_base = np.arange(len(TECH_ORDER))
        for sc_idx, scenario in enumerate(SCENARIOS):
            gw_vals  = []
            twh_vals = []
            colours  = []
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
                    x_base + offset, vals, 0.22,
                    color=colours, alpha=SCENARIO_ALPHA[scenario],
                    label=scenario.capitalize(),
                    edgecolor=SCENARIO_COLORS[scenario], linewidth=0.8,
                )
                for bar, val in zip(bars, vals):
                    if val > 0:
                        ax.text(
                            bar.get_x() + bar.get_width() / 2,
                            bar.get_height() * 1.02,
                            f"{val:.1f}", ha="center", fontsize=6.5,
                        )

        labels_tech = [self.tech_params[t]["label"] for t in TECH_ORDER]
        for ax, ylabel, title in (
            (ax1, "Installable Capacity (GW)",  "Capacity"),
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
            top=0.88, bottom=0.12, left=0.07, right=0.97, wspace=0.30,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(
            str(out_path), bbox_inches="tight", dpi=self.styler.dpi,
            facecolor=fig.get_facecolor(),
        )
        plt.close(fig)

    # ------------------------------------------------------------------
    # PRIVATE: REPORT
    # ------------------------------------------------------------------

    def _format_report(
        self, results: Dict, country_name: str, code: str,
    ) -> str:
        """Generate Phase 4 text report.

        Must be called BEFORE ``run()`` strips ``cap_map`` and ``zonal_df``
        from ``results`` for serialisation.  This ordering is enforced in
        ``run()`` — the report is written first, then ``clean_results`` is
        constructed.
        """
        sep  = "=" * 72
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
            p    = r.get("params", {})
            luf  = p.get("land_use_factor", 0) * 100
            pd_v = p.get("power_density_mw_km2", 0)
            cf   = p.get("capacity_factor", 0) * 100

            lines.extend([
                "", dash,
                f"  {label}",
                f"  Land Use: {luf:.1f}%  |  "
                f"Density: {pd_v:.1f} MW/km²  |  CF: {cf:.1f}%",
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

            zon = r.get("scenarios", {}).get("balanced", {}).get("zonal_df")
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

        lines.extend([
            "", sep,
            "  COMPARATIVE SUMMARY — BALANCED SCENARIO",
            dash,
            f"  {'Technology':<20} {'GW':>8} {'TWh/yr':>10} "
            f"{'Area km²':>12} {'CF%':>6}",
            "  " + "-" * 60,
        ])

        total_gw = total_twh = 0.0
        for tech in TECH_ORDER:
            tr  = results["techs"].get(tech)
            if not tr:
                continue
            bal  = tr.get("scenarios", {}).get("balanced", {})
            gw   = bal.get("capacity_gw", 0)
            twh  = bal.get("generation_twh", 0)
            area = bal.get("area_km2", 0)
            cf   = tr.get("params", {}).get("capacity_factor", 0) * 100
            lbl  = tr.get("params", {}).get("label", tech)
            total_gw  += gw
            total_twh += twh
            lines.append(
                f"  {lbl:<20} {gw:>8.2f} {twh:>10.2f} "
                f"{area:>12,.0f} {cf:>6.1f}%"
            )

        lines.extend([
            "  " + "-" * 60,
            f"  {'TOTAL':<20} {total_gw:>8.2f} {total_twh:>10.2f}",
            "", sep,
            "  TIMINGS",
            dash,
        ])
        for step, t in results.get("timings", {}).items():
            lines.append(f"    {step:<26}: {t:>6.1f}s")
        lines.extend([
            f"    {'TOTAL':<26}: {results.get('elapsed_total', 0):.1f}s",
            sep, "",
        ])

        return "\n".join(lines)