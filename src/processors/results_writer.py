"""
src/processors/results_writer.py
=================================
Phase 6 — Final Synthesis.

Produces:
  - Technology suitability dominance map (GeoTIFF + PNG)
  - Technology LCOE dominance map (GeoTIFF + PNG)
  - Executive dashboard (PNG)
  - Text synthesis report

Parameter architecture:
  - parameters.json → única fonte de CF, LUF, density, threshold
  - settings.yaml   → apenas infra, paths, offsets de cenário
  - Suitability TIFs lidos com o mesmo padrão de Phase 5:
      {CODE}_{tech}_suitability_owa_balanced.tif

Refactoring notes (Phase 6 → utils):
  - Data recovery    → src.utils.data_recovery
  - Report building  → src.utils.reporting
  - Dashboard panels → src.visualization.dashboard_panels
  This module retains only:
    · Raster I/O (load, validate, dominance compute, GeoTIFF export)
    · Map rendering (_draw_dominance_on_ax, _plot_dominance_map)
    · LCOE stats enrichment (_enrich_lcoe_stats)
    · Supply curve recovery (_recover_supply_curve — reads Phase 5's
      persisted Parquet directly, see BLOCKER-001)
    · Orchestration (run)

Pixel area calculation (FIX dup_geo_area):
  - Phase 4 exports suitable-pixel TIFFs to outputs/{CODE}/potential/tifs/
  - Phase 6 reads those TIFFs and uses build_pixel_area_array() to compute
    areas exactly as Phase 4 did — no Helmert fallback.
  - All three area calculation functions now consolidated in geo_stats.py.
"""

from __future__ import annotations

import logging
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
from rasterio.transform import Affine

from src.core.constants import (
    TECH_META,
    TECH_ORDER,
)
from src.core.validators import (
    validate_raster_exists,
    validate_raster_crs,
    validate_raster_shape,
)
from src.io.artifact_manager import ArtifactManager
from src.utils.data_recovery import (
    recover_potential_from_disk,
    recover_lcoe_from_disk,
    recover_abatement_from_disk,
    recover_supply_curve_from_disk,
)
from src.utils.geo_stats import build_pixel_area_array  # ← NOVO
from src.utils.map_styling import GeoWorldStyler
from src.utils.params_helpers import get_scenario_data
from src.utils.reporting import (
    ReportSection,
    build_phase_report,
)
from src.utils.timing import timer as _timer
from src.visualization.dashboard_panels import DashboardPanels

logger = logging.getLogger("geoworld.processors.ResultsWriter")

# OWA scenario used by Phase 3 and Phase 5 — must stay in sync
_DEFAULT_OWA_SCENARIO: str = "balanced"


# ═══════════════════════════════════════════════════════════════════════════
# Main Class
# ═══════════════════════════════════════════════════════════════════════════

class ResultsWriter:
    """
    Phase 6 — Final Synthesis.
    Produces technology dominance maps, executive dashboard and GeoTIFFs.
    """

    def __init__(self, cfg: Any, outputs_dir: Path):
        self.cfg         = cfg
        self.outputs_dir = Path(outputs_dir)
        self.viz_cfg     = cfg.system.get("visualization", {})
        self._admin_gdf: Optional[gpd.GeoDataFrame] = None

        pipeline_dpi = (
            cfg.system.get("pipeline", {}).get("map_dpi_export", 150)
        )
        self.styler = GeoWorldStyler(self.viz_cfg, global_dpi=pipeline_dpi)
        self.panels = DashboardPanels(self.styler)

    # ─────────────────────────────────────────────────────────────────────
    # TIF discovery — suitability (mirrors Phase 5 logic exactly)
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_suitability_tif(
        suitability_dir: Path,
        tech: str,
        country_code: str,
        scenario: str = _DEFAULT_OWA_SCENARIO,
    ) -> Optional[Path]:
        """
        Locate the OWA suitability TIF produced by Phase 3.

        Naming convention (Phase 3):
          {CODE}_{tech}_suitability_owa_{scenario}.tif
          e.g. PRT_solar_suitability_owa_balanced.tif

        Falls back to legacy flat name for backward compat.
        """
        candidates: List[Path] = [
            suitability_dir / f"{country_code}_{tech}_suitability_owa_{scenario}.tif",
            suitability_dir / f"{country_code}_{tech}_suitability.tif",
            suitability_dir / f"{country_code.lower()}_{tech}_suitability_owa_{scenario}.tif",
            suitability_dir / f"{country_code.lower()}_{tech}_suitability.tif",
        ]
        for path in candidates:
            if path.exists():
                logger.debug("  [%s] suitability TIF: %s", tech, path.name)
                return path

        matches = sorted(
            suitability_dir.glob(f"*{tech}*suitability*{scenario}*.tif")
        )
        if not matches:
            matches = sorted(
                suitability_dir.glob(f"*{tech}*suitability*.tif")
            )
        if matches:
            logger.debug(
                "  [%s] suitability TIF (glob): %s", tech, matches[0].name
            )
            return matches[0]

        return None

    # ─────────────────────────────────────────────────────────────────────
    # TIF discovery — LCOE
    # ─────────────────────────────────────────────────────────────────────

    def _find_lcoe_tif(
        self, tech: str, country_code: str
    ) -> Optional[Path]:
        """
        Locate the LCOE TIF produced by Phase 5.

        Phase 5 writes to:
          outputs/{CODE}/lcoe/tif/{CODE}_{tech}_lcoe_usd_mwh.tif
        """
        lcoe_tif_dir = self.outputs_dir / country_code / "lcoe" / "tif"
        candidates: List[Path] = [
            lcoe_tif_dir / f"{country_code}_{tech}_lcoe_usd_mwh.tif",
            lcoe_tif_dir / f"{country_code}_{tech}_lcoe.tif",
            lcoe_tif_dir / f"{tech}_lcoe_usd_mwh.tif",
        ]
        for path in candidates:
            if path.exists():
                logger.debug("  [%s] LCOE TIF: %s", tech, path.name)
                return path

        matches = sorted(
            (self.outputs_dir / country_code / "lcoe").rglob(
                f"*{tech}*lcoe*.tif"
            )
        )
        if matches:
            logger.debug(
                "  [%s] LCOE TIF (glob): %s", tech, matches[0].name
            )
            return matches[0]

        return None

    # ─────────────────────────────────────────────────────────────────────
    # NEW: TIF discovery — suitable-pixel masks (Phase 4 export)
    # ─────────────────────────────────────────────────────────────────────

    def _find_suitable_tif(
        self, tech: str, country_code: str, scenario: str = "balanced"
    ) -> Optional[Path]:
        """
        Locate the suitable-pixel mask TIF exported by Phase 4.

        Phase 4 writes to:
          outputs/{CODE}/potential/tifs/{CODE}_{tech}_suitable_{scenario}.tif

        Returns None if not found (Phase 4 pre-refactor did not export these).
        """
        tif_dir = self.outputs_dir / country_code / "potential" / "tifs"
        path = tif_dir / f"{country_code}_{tech}_suitable_{scenario}.tif"
        if path.exists():
            logger.debug("  [%s] suitable TIF: %s", tech, path.name)
            return path
        return None

    # ─────────────────────────────────────────────────────────────────────
    # Data recovery — delegates to utils, handles format variants
    # ─────────────────────────────────────────────────────────────────────

    def _normalize_potential(
        self, data: Any, country_code: str
    ) -> Dict:
        """
        Accept Potential results as:
          1. Dict with "techs" key (from PotentialCalculator.run() directly)
          2. str/Path to potential output directory → recover from disk
        """
        if isinstance(data, dict) and "techs" in data:
            return data

        logger.info("  [Recovery] Reconstructing Potential data from disk...")

        base_dir = (
            Path(data)
            if isinstance(data, (str, Path))
            else self.outputs_dir / country_code / "potential"
        )

        resolution_deg = float(
            self.cfg.system
            .get("pipeline", {})
            .get("resolution_deg", 0.01)
        )

        return recover_potential_from_disk(
            base_dir,
            country_code,
            resolution_deg=resolution_deg,
        )

    def _normalize_lcoe(
        self, data: Any, country_code: str
    ) -> Dict:
        """
        Accepts the dict returned by LCOECalculator.run() directly,
        or reconstructs stats from Phase 5 output files via data_recovery.

        Looks for TIFs in:   outputs/{CODE}/lcoe/tif/
        Looks for CSVs in:   outputs/{CODE}/lcoe/data/

        Does NOT write to or read from results/tif/.
        """
        if isinstance(data, dict) and "techs" in data:
            return data

        logger.info("  [Recovery] Reconstructing LCOE data from disk...")
        lcoe_base = self.outputs_dir / country_code / "lcoe"
        lcoe_stats, _ = recover_lcoe_from_disk(lcoe_base, country_code)
        return lcoe_stats

    def _normalize_abatement(
        self, data: Any, country_code: str
    ) -> Dict:
        """
        Accepts multiple input shapes:
          1. Dict with "available" key  → pass-through
          2. Dict with "co2_avoided_mt" key → add "available": True
          3. str/Path to abatement output dir → recover from CSV
          4. None → {"available": False}
        """
        if isinstance(data, dict):
            if "available" in data:
                return data
            if "co2_avoided_mt" in data:
                return {"available": True, **data}

        if isinstance(data, (str, Path)):
            return recover_abatement_from_disk(Path(data), country_code)

        return {"available": False}

    # ─────────────────────────────────────────────────────────────────────
    # LCOE stats enrichment (not in data_recovery — Phase 6-specific)
    # ─────────────────────────────────────────────────────────────────────

    def _enrich_lcoe_stats(
        self, lcoe_results: Dict, country_code: str
    ) -> Dict:
        """
        Ensure stats dict has p25 / median / p75 keys.

        When LCOE results come directly from LCOECalculator.run() the
        stats dict only has p10, p90, mean (the calculator does not
        compute quartiles).  We fill in the missing keys by reading the
        LCOE TIF produced by Phase 5, or by estimating from p10/p90.
        """
        for tech in TECH_ORDER:
            stats = (
                lcoe_results.get("techs", {})
                .get(tech, {})
                .get("stats", {})
            )
            if not stats:
                continue

            if all(k in stats for k in ("p25", "median", "p75")):
                continue

            # ── Try to derive from TIF ─────────────────────────────────
            tif_path = self._find_lcoe_tif(tech, country_code)
            if tif_path and tif_path.exists():
                try:
                    with rasterio.open(str(tif_path)) as src:
                        arr = src.read(1).astype(np.float32)
                        nd  = src.nodata
                        if nd is not None:
                            arr[arr == float(nd)] = np.nan
                        arr[arr <= 0] = np.nan
                    valid_v = arr[np.isfinite(arr)]
                    if valid_v.size > 0:
                        stats["p25"]    = round(float(np.percentile(valid_v, 25)), 2)
                        stats["median"] = round(float(np.percentile(valid_v, 50)), 2)
                        stats["p75"]    = round(float(np.percentile(valid_v, 75)), 2)
                        logger.debug(
                            "  [%s] quartiles injected from TIF: "
                            "p25=%.1f median=%.1f p75=%.1f",
                            tech,
                            stats["p25"], stats["median"], stats["p75"],
                        )
                        continue
                except Exception as exc:
                    logger.debug(
                        "  [%s] quartile TIF read failed: %s", tech, exc
                    )

            # ── Fallback: linear interpolation from p10/p90 ────────────
            p10 = stats.get("p10", stats.get("mean", 0.0))
            p90 = stats.get("p90", stats.get("mean", 0.0))
            stats["p25"]    = round(p10 + (p90 - p10) * 0.25, 2)
            stats["median"] = round(p10 + (p90 - p10) * 0.50, 2)
            stats["p75"]    = round(p10 + (p90 - p10) * 0.75, 2)
            logger.debug(
                "  [%s] quartiles estimated from p10/p90 interpolation.", tech
            )

        return lcoe_results

    # ─────────────────────────────────────────────────────────────────────
    # Supply curve recovery — Parquet/CSV, written by Phase 5 (BLOCKER-001)
    # ─────────────────────────────────────────────────────────────────────

    def _recover_supply_curve(
        self, tech: str, country_code: str
    ) -> Optional[pd.DataFrame]:
        """
        Recover the real supply curve persisted by Phase 5 (Parquet,
        CSV fallback — see recover_supply_curve_from_disk).

        Returns None if Phase 5 has not run for this technology.
        """
        lcoe_base = self.outputs_dir / country_code / "lcoe"
        return recover_supply_curve_from_disk(lcoe_base, country_code, tech)

    # ─────────────────────────────────────────────────────────────────────
    # ✅ NOVO: Área integrada (usa TIF do Phase 4 + build_pixel_area_array)
    # ─────────────────────────────────────────────────────────────────────

    def _compute_integrated_area(
    self,
    tech: str,
    country_code: str,
    scenario: str,
    transform: Affine,
    ) -> float:
        """
        Compute total suitable area (km²) for a given tech/scenario using:
        1. Suitable-pixel TIF from Phase 4 (apt_mask)
        2. build_pixel_area_array() from geo_stats.py (same as Phase 4)

        Returns 0.0 if the TIF is not found (Phase 4 pre-refactor).

        This replaces the old _mean_pixel_area_from_tif() + Helmert fallback.
        """
        tif_path = self._find_suitable_tif(tech, country_code, scenario)
        if tif_path is None:
            logger.debug(
                "  [%s/%s] suitable TIF not found — area=0.0",
                tech, scenario
            )
            return 0.0

        try:
            with rasterio.open(str(tif_path)) as src:
                arr = src.read(1)  # uint8: 255=suitable, 0=NoData
                H, W = src.height, src.width
                
                # ✅ FIX: pixels suitable são aqueles com valor 255
                apt_mask = (arr == 255)
                
        except Exception as exc:
            logger.warning(
                "  [%s/%s] Cannot read suitable TIF: %s — area=0.0",
                tech, scenario, exc
            )
            return 0.0

        # ✅ FIX (dup_geo_area): usar SEMPRE build_pixel_area_array() canônico
        area_arr = build_pixel_area_array(transform, H, W)
        total_area_km2 = float(area_arr[apt_mask].sum())

        logger.debug(
            "  [%s/%s] area=%.1f km² (from TIF + geodesic)",
            tech, scenario, total_area_km2
        )
        return total_area_km2

    # ─────────────────────────────────────────────────────────────────────
    # Execution root
    # ─────────────────────────────────────────────────────────────────────

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
        """Execute Phase 6: final synthesis of Phases 3–5."""

        started_at = datetime.now()
        timings: Dict[str, float] = {}

        # ── 1. Discover suitability TIFs ─────────────────────────────────
        suitability_dir = Path(suitability_dir)
        suit_paths, ref_suit = self._discover_suitability_tifs(
            suitability_dir, country_code
        )

        # ── 2. Reference raster metadata ─────────────────────────────────
        with rasterio.open(str(ref_suit)) as src:
            expected_crs   = str(src.crs)
            expected_shape = (src.height, src.width)
            transform_ref  = src.transform  # ← salvar para _compute_integrated_area

        # ── 3. Validate and log suitability TIFs ─────────────────────────
        self._validate_and_log_suitability(
            suit_paths, expected_crs, expected_shape
        )

        # ── 4. Output directories ─────────────────────────────────────────
        out_base    = self.outputs_dir / country_code / "results"
        out_fig     = out_base / "figures"
        out_tif_dir = out_base / "tif"
        out_reports = out_base / "reports"
        for d in (out_fig, out_tif_dir, out_reports):
            d.mkdir(parents=True, exist_ok=True)

        # ── 5. Recover / normalise upstream results ───────────────────────
        potential_results = self._normalize_potential(potential_dir, country_code)
        lcoe_results      = self._normalize_lcoe(lcoe_dir, country_code)
        abatement_results = self._normalize_abatement(abatement_dir, country_code)

        lcoe_results = self._enrich_lcoe_stats(lcoe_results, country_code)
        self._recover_missing_supply_curves(lcoe_results, country_code)

        # ── 6. Admin boundaries ───────────────────────────────────────────
        self._admin_gdf = self.styler.load_admin_boundaries(
            country_name, mainland_gdf, Path(self.cfg.raw_path)
        )
        if self._admin_gdf is not None:
            logger.info(
                "  Admin boundaries: %d regions loaded", len(self._admin_gdf)
            )

        # ── 7. Load suitability arrays ─────────────────────────────────────
        suit_arrays, _transform_ref, crs_ref, H, W = (
            self._load_suitability_arrays(suit_paths)
        )
        if _transform_ref is None or H == 0:
            logger.error("No suitability TIFs loaded — aborting ResultsWriter.")
            return {}

        # Usar transform_ref do passo 2 (lido do primeiro TIF, inclui georef exata)
        # para garantir consistência com Phase 4
        if transform_ref is None:
            transform_ref = _transform_ref

        # ── 8. Load LCOE arrays ────────────────────────────────────────────
        lcoe_arrays = self._load_lcoe_arrays(country_code, H, W)

        # ── 9. Derived geometry ───────────────────────────────────────────
        minx = transform_ref.c
        maxy = transform_ref.f
        maxx = minx + transform_ref.a * W
        miny = maxy + transform_ref.e * H
        extent = [minx, maxx, miny, maxy]

        results: Dict[str, Any] = {
            "country":   country_code,
            "timestamp": started_at.isoformat(),
            "timings":   timings,
        }

        # ── 10. Dominance maps ────────────────────────────────────────────
        with _timer("dominance_suitability", timings):
            dom_suit, dom_suit_score, comp_suit = (
                self._build_suitability_dominance(suit_arrays, H, W)
            )
            self._plot_dominance_map(
                dom_suit, dom_suit_score, comp_suit,
                transform_ref, crs_ref,
                "suitability", country_name,
                mainland_gdf, context_gdf,
                out_fig / f"{country_code}_dominance_suitability.png",
            )

        with _timer("dominance_lcoe", timings):
            dom_lcoe, dom_lcoe_val, comp_lcoe = (
                self._build_lcoe_dominance(lcoe_arrays, H, W)
            )
            self._plot_dominance_map(
                dom_lcoe, dom_lcoe_val, comp_lcoe,
                transform_ref, crs_ref,
                "lcoe", country_name,
                mainland_gdf, context_gdf,
                out_fig / f"{country_code}_dominance_lcoe.png",
            )

        with _timer("executive_dashboard", timings):
            self._plot_executive_dashboard(
                dom_suit, dom_suit_score, comp_suit,
                dom_lcoe, dom_lcoe_val, comp_lcoe,
                potential_results, lcoe_results,
                transform_ref, crs_ref,
                country_name, country_code,
                mainland_gdf, context_gdf,
                extent, minx, maxx, miny, maxy,
                out_fig / f"{country_code}_executive_dashboard.png",
                abatement_results=abatement_results,
            )

        # ── 11. GeoTIFF export ────────────────────────────────────────────
        with _timer("geotiff_export", timings):
            results["exported_tifs"] = self._export_dominance_geotiffs(
                country_code, country_name,
                dom_suit, dom_lcoe,
                transform_ref, crs_ref, H, W,
                out_tif_dir,
            )

        results["elapsed_total"] = round(
            (datetime.now() - started_at).total_seconds(), 1
        )

        # ── 12. Text report (COM ÁREA INTEGRADA) ──────────────────────────
        report = self._format_report(
            results, country_name, country_code,
            potential_results, lcoe_results,
            dom_suit, dom_lcoe,
            transform_ref,  # ← NOVO: passar transform para cálculo de área
        )
        logger.info("\n%s", report)
        (
            out_reports
            / f"{country_code}_results_"
            f"{started_at.strftime('%Y%m%d_%H%M%S')}.txt"
        ).write_text(report, encoding="utf-8")

        # ── 13. Completeness checks ────────────────────────────────────────
        self._check_output_completeness(
            out_fig, country_code, results
        )

        # ── 14. Artifact persistence ──────────────────────────────────────
        self._persist_artifacts(
            country_code, suitability_dir, potential_dir, lcoe_dir,
            abatement_results, dom_suit, dom_lcoe,
            out_fig, out_tif_dir, out_reports, results,
        )

        return results

    # ─────────────────────────────────────────────────────────────────────
    # Run sub-steps (extracted from run() for readability)
    # ─────────────────────────────────────────────────────────────────────

    def _discover_suitability_tifs(
        self, suitability_dir: Path, country_code: str
    ) -> Tuple[Dict[str, Path], Path]:
        """Discover and validate presence of suitability TIFs."""
        suit_paths: Dict[str, Path] = {}
        ref_suit: Optional[Path] = None

        for tech in TECH_ORDER:
            sp = self._find_suitability_tif(
                suitability_dir, tech, country_code, _DEFAULT_OWA_SCENARIO
            )
            if sp is not None:
                suit_paths[tech] = sp
                if ref_suit is None:
                    ref_suit = sp
            else:
                logger.warning(
                    "  Suitability TIF not found for %s in %s",
                    tech, suitability_dir,
                )

        if not suit_paths:
            raise RuntimeError(
                "No suitability TIFs found. Run Phase 3 first.\n"
                f"  searched in: {suitability_dir}\n"
                f"  expected pattern: "
                f"{country_code}_<tech>_suitability_owa_{_DEFAULT_OWA_SCENARIO}.tif"
            )

        return suit_paths, ref_suit

    def _validate_and_log_suitability(
        self,
        suit_paths: Dict[str, Path],
        expected_crs: str,
        expected_shape: Tuple[int, int],
    ) -> None:
        """CRS/shape validation + per-tech statistics logging."""
        for tech, path in suit_paths.items():
            validate_raster_exists(path, f"suitability_{tech}")
            validate_raster_crs(path, expected_crs)
            validate_raster_shape(path, expected_shape)

            with rasterio.open(path) as src:
                arr = src.read(1).astype(np.float32)
                if src.nodata is not None:
                    arr = np.where(arr == src.nodata, np.nan, arr)
            finite = arr[np.isfinite(arr)]
            if finite.size:
                logger.info(
                    "  Input %-12s: min=%.3f  max=%.3f  mean=%.3f  "
                    "finite=%.1f%%",
                    tech,
                    float(np.min(finite)), float(np.max(finite)),
                    float(np.mean(finite)),
                    100.0 * finite.size / arr.size,
                )
            else:
                logger.warning("  Input %-12s: no finite values.", tech)

    def _load_suitability_arrays(
        self, suit_paths: Dict[str, Path]
    ) -> Tuple[Dict[str, Optional[np.ndarray]], Optional[Affine], str, int, int]:
        """Load all suitability rasters; return arrays + shared metadata."""
        suit_arrays: Dict[str, Optional[np.ndarray]] = {}
        transform_ref: Optional[Affine] = None
        crs_ref = "EPSG:4326"
        H, W = 0, 0

        for tech in TECH_ORDER:
            sp = suit_paths.get(tech)
            if sp is not None:
                with rasterio.open(str(sp)) as src:
                    arr = src.read(1).astype(np.float32)
                    arr[arr < 0] = np.nan
                    suit_arrays[tech] = arr
                    if transform_ref is None:
                        transform_ref = src.transform
                        crs_ref       = str(src.crs)
                        H, W          = src.height, src.width
            else:
                suit_arrays[tech] = None

        return suit_arrays, transform_ref, crs_ref, H, W

    def _load_lcoe_arrays(
        self, country_code: str, H: int, W: int
    ) -> Dict[str, Optional[np.ndarray]]:
        """Load LCOE rasters from Phase 5 TIFs; skip shape mismatches."""
        lcoe_arrays: Dict[str, Optional[np.ndarray]] = {}

        for tech in TECH_ORDER:
            tif_path = self._find_lcoe_tif(tech, country_code)
            if tif_path and tif_path.exists():
                try:
                    with rasterio.open(str(tif_path)) as src:
                        arr = src.read(1).astype(np.float32)
                        nd  = src.nodata
                        if nd is not None:
                            arr[arr == float(nd)] = np.nan
                        arr[arr <= 0] = np.nan

                    if arr.shape != (H, W):
                        logger.warning(
                            "  [%s] LCOE TIF shape %s ≠ suit shape %s — skipping.",
                            tech, arr.shape, (H, W),
                        )
                        lcoe_arrays[tech] = None
                    else:
                        lcoe_arrays[tech] = arr

                except Exception as exc:
                    logger.warning(
                        "  [%s] Cannot load LCOE TIF: %s", tech, exc
                    )
                    lcoe_arrays[tech] = None
            else:
                lcoe_arrays[tech] = None
                logger.warning(
                    "  [%s] LCOE TIF not found — "
                    "dominance map will skip this technology.", tech
                )

        return lcoe_arrays

    def _recover_missing_supply_curves(
        self, lcoe_results: Dict, country_code: str
    ) -> None:
        """In-place: fill missing supply_curve entries in lcoe_results."""
        for tech in TECH_ORDER:
            td = lcoe_results["techs"].get(tech, {})
            if td.get("supply_curve") is None:
                sc = self._recover_supply_curve(tech, country_code)
                if sc is not None:
                    td["supply_curve"] = sc
                    logger.info(
                        "  [%s] supply curve recovered (%d points).",
                        tech, len(sc),
                    )

    def _check_output_completeness(
        self,
        out_fig: Path,
        country_code: str,
        results: Dict,
    ) -> None:
        for name in [
            "dominance_suitability.png",
            "dominance_lcoe.png",
            "executive_dashboard.png",
        ]:
            p = out_fig / f"{country_code}_{name}"
            if p.exists():
                logger.info("  [OK] %s", name)
            else:
                logger.warning("  [MISSING] %s", name)

        if results.get("exported_tifs"):
            logger.info("  All expected GeoTIFFs exported.")
        else:
            logger.warning("  No GeoTIFFs exported.")

    def _persist_artifacts(
        self,
        country_code: str,
        suitability_dir: Path,
        potential_dir: Any,
        lcoe_dir: Any,
        abatement_results: Dict,
        dom_suit: np.ndarray,
        dom_lcoe: np.ndarray,
        out_fig: Path,
        out_tif_dir: Path,
        out_reports: Path,
        results: Dict,
    ) -> None:
        artifact_mgr = ArtifactManager(self.outputs_dir, country_code)
        phase_dir    = artifact_mgr.phase_dir("results")

        serializable: Dict[str, Any] = {
            "country":       results["country"],
            "timestamp":     results["timestamp"],
            "elapsed_total": results["elapsed_total"],
            "exported_tifs": results.get("exported_tifs", []),
            "dominance_suitability_counts": {
                TECH_META[tech]["label"]: int((dom_suit == i + 1).sum())
                for i, tech in enumerate(TECH_ORDER)
            },
            "dominance_lcoe_counts": {
                TECH_META[tech]["label"]: int((dom_lcoe == i + 1).sum())
                for i, tech in enumerate(TECH_ORDER)
            },
        }
        artifact_mgr.save_result(phase_dir, serializable, serializer="pickle")

        files: Dict[str, str] = {}
        for d in (out_fig, out_tif_dir, out_reports):
            for p in d.rglob("*"):
                if p.is_file():
                    files[str(p.relative_to(phase_dir))] = str(p)

        artifact_mgr.save_manifest(
            phase_dir, "results",
            files=files,
            parameters={
                "country":             country_code,
                "suitability_dir":     str(suitability_dir),
                "potential_dir":       str(potential_dir),
                "lcoe_dir":            str(lcoe_dir),
                "abatement_available": abatement_results.get("available", False),
            },
        )

    # ─────────────────────────────────────────────────────────────────────
    # Dominance computation
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_suitability_dominance(
        suit_arrays: Dict[str, Optional[np.ndarray]],
        H: int, W: int,
        competition_delta: float = 0.10,
        min_score: float = 0.30,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        stack = np.full((3, H, W), 0.0, dtype=np.float32)
        for i, tech in enumerate(TECH_ORDER):
            arr = suit_arrays.get(tech)
            if arr is not None:
                stack[i] = np.where(np.isfinite(arr) & (arr > 0), arr, 0.0)

        dom_idx   = np.argmax(stack, axis=0).astype(np.int8)
        max_score = np.max(stack, axis=0)
        second_s  = np.sort(stack, axis=0)[-2]

        no_tech   = max_score < min_score
        dom_arr   = (dom_idx + 1).astype(np.int8)
        dom_arr[no_tech] = 0

        dom_score = max_score.copy().astype(np.float32)
        dom_score[no_tech] = np.nan

        competition = (max_score - second_s < competition_delta) & ~no_tech
        return dom_arr, dom_score, competition

    @staticmethod
    def _build_lcoe_dominance(
        lcoe_arrays: Dict[str, Optional[np.ndarray]],
        H: int, W: int,
        competition_delta_usd: float = 10.0,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        stack = np.full((3, H, W), np.inf, dtype=np.float32)
        for i, tech in enumerate(TECH_ORDER):
            arr = lcoe_arrays.get(tech)
            if arr is not None:
                stack[i] = np.where(
                    np.isfinite(arr) & (arr > 0), arr, np.inf
                )

        dom_idx  = np.argmin(stack, axis=0).astype(np.int8)
        min_lcoe = np.min(stack, axis=0)
        second_l = np.sort(stack, axis=0)[1]

        no_tech = ~np.any(np.isfinite(stack), axis=0)
        dom_arr = (dom_idx + 1).astype(np.int8)
        dom_arr[no_tech] = 0

        dom_val = min_lcoe.copy().astype(np.float32)
        dom_val[no_tech]           = np.nan
        dom_val[np.isinf(dom_val)] = np.nan

        with np.errstate(invalid="ignore"):
            diff = np.where(
                np.isfinite(second_l) & np.isfinite(min_lcoe),
                second_l - min_lcoe,
                np.inf,
            )
            competition = (diff < competition_delta_usd) & ~no_tech

        return dom_arr, dom_val, competition

    # ─────────────────────────────────────────────────────────────────────
    # RGBA overlay builder
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_rgba(
        dom_arr: np.ndarray,
        dom_score: np.ndarray,
        mode: str,
        H: int, W: int,
    ) -> np.ndarray:
        """RGBA array for dominance overlay (fully transparent background)."""
        rgba = np.zeros((H, W, 4), dtype=np.float32)

        valid_scores = dom_score[np.isfinite(dom_score)]
        if valid_scores.size == 0:
            return rgba

        lo    = float(np.percentile(valid_scores, 5))
        hi    = float(np.percentile(valid_scores, 95))
        denom = max(hi - lo, 1e-6)

        Q1_RGB = {
            "solar":   (0.878, 0.482, 0.224),
            "wind":    (0.227, 0.490, 0.788),
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
                        0.45, 0.92,
                    ),
                    0.0,
                ).astype(np.float32)

            rgba[mask, 0] = r
            rgba[mask, 1] = g
            rgba[mask, 2] = b
            rgba[mask, 3] = alpha_map[mask]

        return rgba

    # ─────────────────────────────────────────────────────────────────────
    # Standalone dominance map
    # ─────────────────────────────────────────────────────────────────────

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
        H, W = dom_arr.shape
        minx = transform.c
        maxy = transform.f
        maxx = minx + transform.a * W
        miny = maxy + transform.e * H
        extent = [minx, maxx, miny, maxy]

        bounds = mainland_gdf.total_bounds
        v_minx, v_miny, v_maxx, v_maxy = (
            bounds[0], bounds[1], bounds[2], bounds[3]
        )

        rgba = self._build_rgba(dom_arr, dom_score, mode, H, W)

        fig, ax = self.styler.create_figure(
            v_minx, v_maxx, v_miny, v_maxy, right_in_override=0.55
        )
        self.styler.draw_basemap(
            ax, crs, mainland_gdf, context_gdf, self._admin_gdf
        )
        ax.imshow(
            rgba, extent=extent, origin="upper",
            zorder=3, interpolation="nearest",
        )

        try:
            x = np.linspace(minx, maxx, W)
            y = np.linspace(maxy, miny, H)
            ax.contourf(
                x, y, competition.astype(np.float32),
                levels=[0.5, 1.5],
                hatches=["////"], colors=["none"],
                zorder=4, alpha=0.35,
            )
        except Exception:
            pass

        self.styler.add_decorations(ax, v_minx, v_maxx, v_miny, v_maxy)
        self.styler.add_standard_footer(
            fig, crs_metadata=f"CRS: {crs or 'EPSG:4326'}"
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
        legend_elements = [
            mpatches.Patch(
                facecolor=TECH_META[t]["color"], alpha=0.88,
                label=TECH_META[t]["label"],
                edgecolor="#555555", linewidth=0.7,
            )
            for t in TECH_ORDER
        ] + [
            mpatches.Patch(
                facecolor="none", edgecolor="#888888",
                hatch="////", linewidth=0.5, label=comp_label,
            ),
            mpatches.Patch(
                facecolor=self.styler.ocean_color,
                edgecolor="#AAAAAA", linewidth=0.5,
                label="No Suitable Area",
            ),
        ]
        self.styler.add_standard_legend(
            ax, legend_elements, "lower_center",
            bbox_anchor=(0.5, -0.09), ncol=3,
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

    # ─────────────────────────────────────────────────────────────────────
    # Executive dashboard
    # ─────────────────────────────────────────────────────────────────────

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
        minx: float, maxx: float,
        miny: float, maxy: float,
        out_path: Path,
        abatement_results: Optional[Dict] = None,
    ) -> None:

        has_abat = (
            abatement_results is not None
            and abatement_results.get("available", False)
        )

        fig, axes, letters = self._build_dashboard_layout(has_abat, country_name, country_code)

        # (a) Suitability dominance map
        self._draw_dominance_on_ax(
            dom_suit, dom_suit_score, comp_suit, "suitability",
            axes[0], transform, crs,
            mainland_gdf, context_gdf,
            extent, minx, maxx, miny, maxy,
        )
        axes[0].set_title(
            "Suitability Dominance\n(AHP-TOPSIS | Balanced Scenario)",
            fontsize=9.5, fontweight="bold", pad=4,
        )

        # (b) Technical potential bars
        self.panels.draw_potential_bars(axes[1], potential_results)
        axes[1].set_title(
            "Technical Potential by Scenario",
            fontsize=9.5, fontweight="bold",
        )

        # (c) LCOE distribution box-whisker
        self.panels.draw_lcoe_distribution(axes[2], lcoe_results)
        axes[2].set_title(
            "LCOE Distribution ($/MWh)\nP10 ─ IQR ─ P90",
            fontsize=9.5, fontweight="bold",
        )

        # (d) LCOE dominance map
        self._draw_dominance_on_ax(
            dom_lcoe, dom_lcoe_val, comp_lcoe, "lcoe",
            axes[3], transform, crs,
            mainland_gdf, context_gdf,
            extent, minx, maxx, miny, maxy,
        )
        axes[3].set_title(
            "LCOE Dominance\n(Cheapest Technology per Pixel)",
            fontsize=9.5, fontweight="bold", pad=4,
        )

        # (e) Merit-order supply curves
        self.panels.draw_supply_curves(axes[4], lcoe_results)
        axes[4].set_title(
            "Resource Cost Curves (Merit Order)",
            fontsize=9.5, fontweight="bold",
        )

        # (f) Summary table
        self.panels.draw_summary_table(axes[5], potential_results, lcoe_results)
        axes[5].set_title(
            "Key Metrics — Balanced Scenario",
            fontsize=9.5, fontweight="bold",
        )

        # (g) GHG abatement (optional)
        if has_abat:
            self.panels.draw_abatement_summary(axes[6], abatement_results)
            axes[6].set_title(
                "GHG Abatement & Thermal Substitution",
                fontsize=9.5, fontweight="bold",
            )

        self.styler.add_standard_footer(fig)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(
            str(out_path), bbox_inches="tight",
            dpi=self.styler.dpi, facecolor=fig.get_facecolor(),
        )
        plt.close(fig)

    def _build_dashboard_layout(
        self,
        has_abat: bool,
        country_name: str,
        country_code: str,
    ) -> Tuple[plt.Figure, List[plt.Axes], str]:
        """Create figure + axes grid for the executive dashboard."""
        title_extra = "  ·  GHG Abatement" if has_abat else ""
        fig_width   = 22 if has_abat else 18

        fig = plt.figure(figsize=(fig_width, 13), dpi=self.styler.dpi)
        fig.patch.set_facecolor(self.styler.fig_bg)
        fig.suptitle(
            f"GeoWorld Framework — Renewable Energy Assessment: "
            f"{country_name} ({country_code})\n"
            f"AHP-TOPSIS Multi-Criteria Suitability  ·  Technical Potential"
            f"  ·  LCOE Economics{title_extra}",
            fontsize=15, fontweight="bold", y=0.99,
        )

        if has_abat:
            gs = gridspec.GridSpec(
                2, 4, figure=fig,
                width_ratios=[1.05, 1.0, 1.0, 0.8],
                height_ratios=[1.0, 0.90],
                hspace=0.38, wspace=0.32,
                left=0.05, right=0.97, top=0.92, bottom=0.04,
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
                2, 3, figure=fig,
                width_ratios=[1.05, 1.0, 1.0],
                height_ratios=[1.0, 0.90],
                hspace=0.38, wspace=0.32,
                left=0.05, right=0.97, top=0.92, bottom=0.04,
            )
            axes   = [fig.add_subplot(gs[i // 3, i % 3]) for i in range(6)]
            letters = "abcdef"

        for ax, letter in zip(axes, letters):
            ax.text(
                0.02, 0.97, f"({letter})",
                transform=ax.transAxes,
                fontsize=10, fontweight="bold",
                va="top", ha="left", color="#111827", zorder=10,
            )

        return fig, axes, letters

    # ─────────────────────────────────────────────────────────────────────
    # Dominance map on dashboard axis (raster + GDF — stays in this module)
    # ─────────────────────────────────────────────────────────────────────

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
        minx: float, maxx: float,
        miny: float, maxy: float,
    ) -> None:
        H, W = dom_arr.shape
        rgba = self._build_rgba(dom_arr, dom_score, mode, H, W)

        self.styler.draw_basemap(
            ax, crs, mainland_gdf, context_gdf, self._admin_gdf
        )
        ax.set_facecolor(self.styler.ocean_color)
        ax.imshow(
            rgba, extent=extent, origin="upper",
            zorder=3, interpolation="nearest",
        )

        try:
            ax.contourf(
                np.linspace(minx, maxx, W),
                np.linspace(maxy, miny, H),
                competition.astype(np.float32),
                levels=[0.5, 1.5],
                hatches=["///"], colors=["none"],
                zorder=4, alpha=0.40,
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
                ax, self._admin_gdf, minx, maxx, miny, maxy, max_labels=6
            )

        handles = [
            mpatches.Patch(
                facecolor=TECH_META[t]["color"], alpha=0.85,
                label=TECH_META[t]["short"],
                edgecolor="#555", linewidth=0.4,
            )
            for t in TECH_ORDER
        ]
        ax.legend(
            handles=handles, fontsize=6.5, framealpha=0.92,
            edgecolor="#E5E7EB", loc="lower right",
            handlelength=1.2, borderpad=0.4,
        )

    # ─────────────────────────────────────────────────────────────────────
    # GeoTIFF export — dominance maps only
    # ─────────────────────────────────────────────────────────────────────

    def _export_dominance_geotiffs(
        self,
        country_code: str,
        country_name: str,
        dom_suit: np.ndarray,
        dom_lcoe: np.ndarray,
        transform: Affine,
        crs: str,
        H: int, W: int,
        out_tif_dir: Path,
    ) -> List[str]:
        """
        Export only the two dominance GeoTIFFs.

        Suitability and LCOE TIFs already exist in their respective
        phase output directories and are NOT duplicated here.
        """
        exported: List[str] = []
        base_tags = {
            "COUNTRY":      country_name,
            "COUNTRY_CODE": country_code,
            "CREATED":      datetime.now().isoformat(),
            "SOURCE":       "GeoWorld Framework — Phase 6",
            "CRS":          crs,
        }

        def _write_uint8(arr: np.ndarray, path: Path, desc: str, legend: str):
            with rasterio.open(
                str(path), "w",
                driver="GTiff", height=H, width=W,
                count=1, dtype="uint8",
                crs=crs, transform=transform,
                compress="lzw", predictor=1, nodata=0,
            ) as dst:
                dst.write(arr.astype(np.uint8), 1)
                dst.update_tags(
                    DESCRIPTION=desc, LEGEND=legend, **base_tags
                )
            exported.append(path.name)
            logger.info("  GeoTIFF: %s", path.name)

        _write_uint8(
            dom_suit,
            out_tif_dir / f"{country_code}_dominance_suitability.tif",
            "Technology Suitability Dominance (highest TOPSIS score)",
            "0=None, 1=Solar PV, 2=Wind Onshore, 3=Biomass/Bioenergy",
        )
        _write_uint8(
            dom_lcoe,
            out_tif_dir / f"{country_code}_dominance_lcoe.tif",
            "Technology LCOE Dominance (cheapest technology per pixel)",
            "0=None, 1=Solar PV, 2=Wind Onshore, 3=Biomass/Bioenergy",
        )
        return exported

    # ─────────────────────────────────────────────────────────────────────
    # Text report — uses reporting.build_phase_report
    # ─────────────────────────────────────────────────────────────────────

    def _format_report(
        self,
        results: Dict,
        country_name: str,
        code: str,
        potential_results: Dict,
        lcoe_results: Dict,
        dom_suit: np.ndarray,
        dom_lcoe: np.ndarray,
        transform: Affine,  # ← NOVO
    ) -> str:
        total_s = max(int((dom_suit > 0).sum()), 1)
        total_l = max(int((dom_lcoe > 0).sum()), 1)

        # ── Section 1: suitability dominance ─────────────────────────────
        suit_rows = [
            (
                TECH_META[tech]["label"],
                f"{int((dom_suit == i + 1).sum()):>8,} px  "
                f"({int((dom_suit == i + 1).sum()) / total_s * 100:>5.1f}%)",
            )
            for i, tech in enumerate(TECH_ORDER)
        ]

        # ── Section 2: LCOE dominance ─────────────────────────────────────
        lcoe_dom_rows = [
            (
                TECH_META[tech]["label"],
                f"{int((dom_lcoe == i + 1).sum()):>8,} px  "
                f"({int((dom_lcoe == i + 1).sum()) / total_l * 100:>5.1f}%)",
            )
            for i, tech in enumerate(TECH_ORDER)
        ]

        # ── Section 3: integrated summary (COM ÁREA CORRIGIDA) ───────────
        summary_rows = []
        t_gw = t_twh = 0.0
        for tech in TECH_ORDER:
            sc    = get_scenario_data(potential_results, tech, "balanced")
            stats = (
                lcoe_results.get("techs", {})
                .get(tech, {})
                .get("stats", {})
            )
            gw   = sc.get("capacity_gw",    0.0)
            twh  = sc.get("generation_twh", sc.get("gen_twh", 0.0))

            # ✅ FIX (dup_geo_area): área calculada IGUAL à Fase 4
            area = self._compute_integrated_area(tech, code, "balanced", transform)

            lce  = stats.get("mean",        0.0)
            t_gw  += gw
            t_twh += twh
            summary_rows.append([
                TECH_META[tech]["label"],
                f"{gw:.1f}",
                f"{twh:.1f}",
                f"{area:,.0f}",  # ← agora bate com Fase 4
                f"{lce:.1f}",
            ])
        summary_rows.append(["TOTAL", f"{t_gw:.1f}", f"{t_twh:.1f}", "—", "—"])

        # ── Section 4: exported files ─────────────────────────────────────
        file_rows = [
            (f"✓  {f}", "")
            for f in results.get("exported_tifs", [])
        ]

        sections = [
            ReportSection(
                title="SUITABILITY DOMINANCE — TOPSIS Score",
                rows=suit_rows,
            ),
            ReportSection(
                title="LCOE DOMINANCE — Cheapest Technology per Pixel",
                rows=lcoe_dom_rows,
            ),
            _build_integrated_summary_section(summary_rows),
            ReportSection(
                title="GeoTIFFs EXPORTED",
                rows=file_rows,
            ),
        ]

        return build_phase_report(
            title="RESULTS SYNTHESIS REPORT — Phase 6",
            country_name=country_name,
            country_code=code,
            timestamp=results["timestamp"],
            sections=sections,
            timings=results.get("timings", {}),
            elapsed_total=results.get("elapsed_total", 0.0),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Module-level helpers (used internally; not part of the class API)
# ═══════════════════════════════════════════════════════════════════════════

def _build_integrated_summary_section(
    data_rows: List[List[str]],
) -> ReportSection:
    """
    Build the balanced scenario summary section with a fixed 5-column layout.

    Keeps column alignment independent of build_stats_table_section since
    this report needs a specific header format with units inline.
    """
    header = f"  {'Technology':<22} {'GW':>8} {'TWh/yr':>9} {'km²':>10} {'$/MWh':>9}"
    sep    = "  " + "─" * 62

    rows: List[Tuple[str, str]] = [(header, ""), (sep, "")]
    for row in data_rows[:-1]:  # tech rows
        label, gw, twh, area, lcoe = row
        rows.append(
            (f"  {label:<22} {gw:>8} {twh:>9} {area:>10} {lcoe:>9}", "")
        )
    rows.append((sep, ""))
    # total row
    total = data_rows[-1]
    rows.append(
        (f"  {'TOTAL':<22} {total[1]:>8} {total[2]:>9}", "")
    )

    return ReportSection(
        title="BALANCED SCENARIO — INTEGRATED SUMMARY",
        rows=rows,
    )