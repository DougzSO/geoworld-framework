"""
src/processors/suitability_builder.py
=====================================
Phase 3: Multi-Criteria Spatial Decision Analysis for Renewable Energy Siting.

Generates final suitability maps (0–1) for Solar PV, Wind Onshore, and Biomass
using spatially explicit multi-criteria evaluation with AHP-derived weights
(Saaty scale).

Scientific Framework
--------------------
This module implements a two-stage MCDA pipeline:

**Stage 1: Analytic Hierarchy Process (AHP)**
Derives criterion weights from a pairwise comparison matrix using the geometric
mean approximation (Saaty, 1980).  The Saaty scale maps ordinal criterion
priority to preference ratios; the consistency ratio (CR) must be ≤ 0.10.

**Stage 2: Aggregation — TOPSIS + OWA (dual output)**
Two complementary spatial aggregation operators are applied:

  *TOPSIS* (Hwang & Yoon, 1981): ranks each pixel by its Euclidean distance to
  the Positive Ideal Solution (PIS) and Negative Ideal Solution (NIS) in the
  AHP-weighted criterion space.  Produces a single score in [0, 1] that
  reflects overall closeness to the ideal.

  *OWA* (Yager, 1988): Ordered Weighted Averaging.  For each pixel the criteria
  values are sorted in descending order and the OWA weights — read from
  ``parameters.json`` (optimistic / balanced / conservative vectors) — are
  applied to that *ordered* sequence rather than to named criteria.  The result
  quantifies the degree of optimism in the aggregation.

Both TOPSIS and OWA outputs are written as separate GeoTIFFs so that Phase 4
can select the appropriate aggregation per scenario.

Suitability Threshold Semantics
--------------------------------
Phase 3 produces **continuous** suitability maps in [0, 1].  It does not apply
any threshold internally.  The per-scenario thresholds from ``settings.yaml``
are forwarded in the return dict (key ``"scenario_thresholds"``) so that
Phase 4 (PotentialCalculator) can apply them when computing installable area.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")

import numpy as np
import rasterio
from rasterio.transform import Affine

from src.core.constants import (
    AHP_CR_THRESHOLD,
    HIGH_SUITABILITY_THRESHOLD,
    NODATA_FLOAT,
    TECH_LABELS,
    TECH_META,
    TECH_ORDER,
)
from src.core.validators import validate_raster_crs, validate_raster_shape
from src.io.artifact_manager import ArtifactManager
from src.utils.ahp import compute_ahp_weights, log_ahp_result
from src.utils.exclusion import TechnologyConfig, apply_hard_exclusions
from src.utils.map_styling import GeoWorldStyler
from src.utils.owa import OWA_DEFAULTS, owa_spatial, prepare_owa_weights
from src.utils.params_helpers import extract_params_dict
from src.utils.raster_io import (
    load_all_criteria,
    load_aux_raster,
    load_reference_meta,
)
from src.utils.reporting import ReportSection, build_phase_report
from src.utils.timing import timer as _timer
from src.utils.topsis import TOPSIS_EXCLUSION_SCORE, topsis_spatial
from src.utils.utils import safe_raster_write

logger = logging.getLogger("geoworld.processors.SuitabilityBuilder")

# ═══════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════

# Per-technology slope offsets added on top of country slope_threshold_deg.
# Fallback defaults only -- get_technology_configs() reads the real values
# from CountryParams.slope_offset_{tech}_deg (parameters.json's
# exclusion_thresholds.slope_offset_deg, Bloco 1) when available, and falls
# back to these literals only if country_params lacks the fields.
_SLOPE_OFFSET_DEG: Dict[str, float] = {
    "solar": 5.0,
    "wind": 10.0,
    "biomass": 20.0,
}

# OWA scenario names that are recognised.
_OWA_SCENARIOS: List[str] = ["optimistic", "balanced", "conservative"]


# ═══════════════════════════════════════════════════════════════════════════
# TECHNOLOGY CONFIGURATION FACTORY
# ═══════════════════════════════════════════════════════════════════════════


def get_technology_configs(
    country_params=None,
) -> Dict[str, TechnologyConfig]:
    """Return MCDA configuration objects for each renewable technology.

    Slope thresholds
    ~~~~~~~~~~~~~~~~
    ``TechnologyConfig.slope_max_deg`` is derived as::

        slope_max_deg = country_params["slope_threshold_deg"] + slope_offset_{tech}_deg

    Both terms come from ``country_params`` (``parameters.json``'s per-country
    ``slope_threshold_deg`` and global ``exclusion_thresholds.slope_offset_deg``,
    Bloco 1). When either is absent, conservative defaults are used: 10° for
    ``slope_threshold_deg``, and the ``_SLOPE_OFFSET_DEG`` literals per tech.

    Forest exclusion policy
    ~~~~~~~~~~~~~~~~~~~~~~~
    If ``country_params["forest_as_exclusion"]`` is True (default), ESA
    WorldCover class 10 (Tree cover) is added to ``lc_exclusion_classes``.

    Args:
        country_params: Per-country parameters — accepts ``CountryParams``
                        (Pydantic v2), plain ``dict``, or ``None``.

    Returns:
        Mapping technology identifier → :class:`TechnologyConfig`.
    """
    cp = extract_params_dict(country_params)

    # ── Forest exclusion policy ────────────────────────────────────────
    forest_as_exclusion: bool = (
        str(cp.get("forest_as_exclusion", True)).lower()
        not in ("false", "0", "no")
    )

    base_lc_exclusions: Set[int] = {50, 70, 80, 90, 95}
    if forest_as_exclusion:
        base_lc_exclusions.add(10)
        logger.info(
            "  [suitability] Policy: ESA class 10 (Trees/Forest) "
            "excluded for all technologies."
        )
    else:
        logger.warning(
            "  [suitability] Policy: ESA class 10 (Trees/Forest) "
            "ALLOWED (forest_as_exclusion=False)."
        )

    # ── Country slope base ─────────────────────────────────────────────
    base_slope_deg: float = float(cp.get("slope_threshold_deg", 10.0))

    # Bloco 1 (docs/BACKLOG.md): sourced from CountryParams when available,
    # falling back to the _SLOPE_OFFSET_DEG literals otherwise.
    slope_offset: Dict[str, float] = {
        "solar":   float(cp.get("slope_offset_solar_deg",   _SLOPE_OFFSET_DEG["solar"])),
        "wind":    float(cp.get("slope_offset_wind_deg",    _SLOPE_OFFSET_DEG["wind"])),
        "biomass": float(cp.get("slope_offset_biomass_deg", _SLOPE_OFFSET_DEG["biomass"])),
    }
    logger.info(
        "  [suitability] slope_threshold_deg=%.1f° → "
        "solar≤%.1f°  wind≤%.1f°  biomass≤%.1f°",
        base_slope_deg,
        base_slope_deg + slope_offset["solar"],
        base_slope_deg + slope_offset["wind"],
        base_slope_deg + slope_offset["biomass"],
    )

    # ── Common hard exclusions ─────────────────────────────────────────
    common_exclusions: Dict[str, float] = {
        # lakes_exclusion: compute_lakes_exclusion() only ever emits
        # {0.0, 1.0} (binary land/lake mask) -- any threshold in the open
        # interval (0, 1) is mathematically equivalent (crit_arr < threshold
        # selects exactly the lake pixels either way). Confirmed empirically
        # for 0.3/0.5/0.7 on both PRT and BRA (Bloco 1, docs/BACKLOG.md).
        # Intentionally NOT migrated to parameters.json -- there is no real
        # value to tune here.
        "lakes_exclusion": 0.5,
        "protected_areas": float(cp.get("protected_areas_threshold", 0.99)),
        "proximity_plants": float(cp.get("proximity_plants_threshold", 0.01)),
    }

    return {
        "solar": TechnologyConfig(
            label="Solar PV",
            color="#F9A825",
            intensity="subtle",
            priority_order=[
                "solar_resource",
                "grid_suitability",
                "terrain_score",
                "road_suitability",
                "river_solar",
                "seismic_suitability",
                "pop_suitability",
                "proximity_plants",
            ],
            hard_exclusions=common_exclusions,
            slope_max_deg=base_slope_deg + slope_offset["solar"],
            lc_exclusion_classes=base_lc_exclusions,
        ),
        "wind": TechnologyConfig(
            label="Wind Onshore",
            color="#1565C0",
            intensity="moderate",
            priority_order=[
                "wind_resource",
                "grid_suitability",
                "terrain_score",
                "road_suitability",
                "pop_suitability",
                "proximity_plants",
                "seismic_suitability",
                "river_wind",
            ],
            hard_exclusions=common_exclusions,
            slope_max_deg=base_slope_deg + slope_offset["wind"],
            lc_exclusion_classes=base_lc_exclusions,
        ),
        "biomass": TechnologyConfig(
            label="Biomass / Bioenergy",
            color="#2E7D32",
            intensity="subtle",
            priority_order=[
                "biomass_resource",
                "lc_biomass",
                "road_suitability",
                "river_biomass",
                "grid_suitability",
                "pop_suitability",
                "proximity_plants",
                "terrain_score",
                "seismic_suitability",
            ],
            hard_exclusions=common_exclusions,
            slope_max_deg=base_slope_deg + slope_offset["biomass"],
            lc_exclusion_classes=base_lc_exclusions,
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════
# GEOTIFF OUTPUT UTILITY
# ═══════════════════════════════════════════════════════════════════════════


def _save_tif(
    data: np.ndarray,
    transform: Affine,
    crs: str,
    path: Path,
) -> None:
    """Save a suitability score array to a compressed GeoTIFF.

    NaN values are written as ``NODATA_FLOAT`` (-9999.0).

    Args:
        data:      Float32 score array.
        transform: Affine geotransform.
        crs:       CRS string (e.g. "EPSG:4326").
        path:      Destination file path; parent directories are created.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    out_data = np.where(np.isfinite(data), data, NODATA_FLOAT).astype(np.float32)
    profile = dict(
        driver="GTiff",
        dtype="float32",
        width=data.shape[1],
        height=data.shape[0],
        count=1,
        crs=crs,
        transform=transform,
        nodata=NODATA_FLOAT,
        compress="lzw",
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )
    with safe_raster_write(path, **profile) as dst:
        dst.write(out_data, 1)


# ═══════════════════════════════════════════════════════════════════════════
# PRINCIPAL ORCHESTRATION CLASS
# ═══════════════════════════════════════════════════════════════════════════


class SuitabilityBuilder:
    """Orchestrates Phase 3 of the GeoWorld pipeline."""

    def __init__(self, cfg: Any, outputs_dir: Path) -> None:
        self.cfg = cfg
        self.outputs_dir = Path(outputs_dir)
        self.viz_cfg = cfg.system.get("visualization", {})
        self._admin_gdf: Optional[gpd.GeoDataFrame] = None
        pipeline_dpi = cfg.system.get("pipeline", {}).get("map_dpi_export", 150)
        self.styler = GeoWorldStyler(self.viz_cfg, global_dpi=pipeline_dpi)

    def _find_criteria_dir(self, country_code: str) -> Optional[Path]:
        """Locate criteria TIF directory for *country_code*."""
        base = self.outputs_dir / country_code
        for candidate in [
            base / "criteria_builder" / "tif",
            base / "criteria_builder" / "tifs",
            base / "criteria_builder",
            base / "criteria",
            base,
        ]:
            if candidate.exists() and list(candidate.glob("*.tif")):
                return candidate
        return None

    def _read_owa_vectors(self, country_params_dict: Dict) -> Dict[str, List[float]]:
        """Extract OWA weight vectors from country params with fallback."""
        owa_block = country_params_dict.get("owa", {})
        vectors: Dict[str, List[float]] = {}
        for scenario in _OWA_SCENARIOS:
            raw = owa_block.get(scenario, OWA_DEFAULTS[scenario])
            vectors[scenario] = list(raw)
        return vectors

    def _read_scenario_thresholds(self) -> Dict[str, float]:
        """Read per-scenario suitability thresholds from settings.yaml."""
        scen_cfg = self.cfg.system.get("potential", {}).get("scenarios", {})
        thresholds: Dict[str, float] = {}
        for scenario in _OWA_SCENARIOS:
            offset = float(
                scen_cfg.get(scenario, {}).get("suitability_threshold", 0.0)
            )
            thresholds[scenario] = offset
        return thresholds

    # ------------------------------------------------------------------
    # PUBLIC ENTRY POINT
    # ------------------------------------------------------------------

    def run(
        self,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        criteria_dir: Path,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        lc_aligned: Optional[Path] = None,
        slope_aligned: Optional[Path] = None,
        country_params=None,
    ) -> Dict[str, Any]:
        """Execute Phase 3 for all modelled renewable energy technologies."""
        started_at = datetime.now()
        timings: Dict[str, float] = {}
        criteria_dir = Path(criteria_dir)
        cp = extract_params_dict(country_params)

        if not criteria_dir.exists():
            criteria_dir = self._find_criteria_dir(country_code)
            if criteria_dir is None:
                raise FileNotFoundError(
                    "Criteria directory absent — run Phase 2b first."
                )

        out_base = self.outputs_dir / country_code / "suitability"
        out_tifs = out_base / "tif"
        out_fig = out_base / "figures"
        out_rep = out_base / "reports"
        for d in (out_tifs, out_fig, out_rep):
            d.mkdir(parents=True, exist_ok=True)

        self._admin_gdf = self.styler.load_admin_boundaries(
            country_name, mainland_gdf, Path(self.cfg.raw_path)
        )

        # ── 1. Reference metadata & criteria loading ───────────────────
        with _timer("load_metadata", timings):
            transform, crs, height, width = load_reference_meta(
                criteria_dir, country_code
            )

        with _timer("load_criteria", timings):
            all_criteria = load_all_criteria(
                criteria_dir, country_code, height, width
            )

        if not all_criteria:
            raise RuntimeError(
                "Data load failure: no criteria matrices could be read."
            )

        for name, arr in all_criteria.items():
            finite = arr[np.isfinite(arr)]
            if finite.size:
                logger.info(
                    "  Criterion %-22s: min=%.3f  max=%.3f  "
                    "mean=%.3f  finite=%.1f%%",
                    name,
                    float(np.min(finite)),
                    float(np.max(finite)),
                    float(np.mean(finite)),
                    100.0 * finite.size / arr.size,
                )
            else:
                logger.warning("  Criterion %-22s: no finite values.", name)

        # ── 2. Validate auxiliary rasters ──────────────────────────────
        if lc_aligned:
            validate_raster_shape(lc_aligned, (height, width))
            validate_raster_crs(lc_aligned, crs)
        if slope_aligned:
            validate_raster_shape(slope_aligned, (height, width))
            validate_raster_crs(slope_aligned, crs)

        country_mask = np.zeros((height, width), dtype=bool)
        for arr in all_criteria.values():
            country_mask |= np.isfinite(arr)

        lc_data = load_aux_raster(lc_aligned, height, width)
        slope_data = load_aux_raster(slope_aligned, height, width)

        # ── 3. OWA vectors & scenario thresholds ──────────────────────
        owa_vectors = self._read_owa_vectors(cp)
        scenario_thresholds = self._read_scenario_thresholds()
        logger.info(
            "  [suitability] Scenario thresholds (forwarded to Phase 4): %s",
            {k: f"{v:+.2f}" for k, v in scenario_thresholds.items()},
        )

        results: Dict[str, Any] = {
            "country_code": country_code,
            "timestamp": started_at.isoformat(),
            "techs": {},
            "timings": timings,
            "scenario_thresholds": scenario_thresholds,
        }

        tech_configs = get_technology_configs(country_params)

        # ── 4. Per-technology AHP + TOPSIS + OWA ──────────────────────
        for tech, cfg_tech in tech_configs.items():
            logger.info(
                "\n  %s\n  Technology: %s\n  %s",
                "─" * 50,
                cfg_tech.label,
                "─" * 50,
            )
            with _timer(tech, timings):
                results["techs"][tech] = self._process_technology_topology(
                    tech=tech,
                    cfg_tech=cfg_tech,
                    all_criteria=all_criteria,
                    country_mask=country_mask,
                    lc_data=lc_data,
                    slope_data=slope_data,
                    transform=transform,
                    crs=crs,
                    height=height,
                    width=width,
                    out_tifs=out_tifs,
                    out_fig=out_fig,
                    country_code=country_code,
                    country_name=country_name,
                    mainland_gdf=mainland_gdf,
                    context_gdf=context_gdf,
                    owa_vectors=owa_vectors,
                )

        # ── 5. Comparison map ──────────────────────────────────────────
        with _timer("comparison_map", timings):
            self._plot_comparison(
                results,
                country_name,
                country_code,
                mainland_gdf,
                context_gdf,
                out_fig / f"{country_code}_suitability_comparison.png",
            )

        results["elapsed_total"] = round(
            (datetime.now() - started_at).total_seconds(), 1
        )

        # ── 6. Text report ─────────────────────────────────────────────
        report_text = self._format_report(results, country_name, country_code)
        logger.info("\n%s", report_text)
        rep_path = out_rep / (
            f"{country_code}_suitability_"
            f"{started_at.strftime('%Y%m%d_%H%M%S')}.txt"
        )
        rep_path.write_text(report_text, encoding="utf-8")

        # ── 7. Completeness check ──────────────────────────────────────
        generated = [
            t
            for t in TECH_ORDER
            if t in results["techs"] and results["techs"][t].get("tif_path")
        ]
        missing = [t for t in TECH_ORDER if t not in generated]
        if missing:
            logger.warning("  Missing suitability maps for: %s", ", ".join(missing))
        else:
            logger.info("  All expected suitability maps generated.")

        # ── 8. Artefact persistence ────────────────────────────────────
        artifact_mgr = ArtifactManager(self.outputs_dir, country_code)
        phase_dir = artifact_mgr.phase_dir("suitability")

        _SERIALISABLE_KEYS = (
            "n_total",
            "n_excluded",
            "n_valid",
            "pct_excluded",
            "mean",
            "std",
            "p10",
            "p50",
            "p90",
            "pct_high",
            "CR",
            "cr_ok",
            "weights",
            "weights_json",
            "tif_path",
            "map_path",
            "owa_tif_paths",
        )
        serializable_results: Dict[str, Any] = {
            "country_code": results["country_code"],
            "timestamp": results["timestamp"],
            "elapsed_total": results["elapsed_total"],
            "scenario_thresholds": results["scenario_thresholds"],
            "techs": {},
        }
        for tech, data in results["techs"].items():
            if "error" in data:
                serializable_results["techs"][tech] = {"error": data["error"]}
                continue
            serializable_results["techs"][tech] = {
                k: data.get(k) for k in _SERIALISABLE_KEYS
            }

        artifact_mgr.save_result(
            phase_dir, serializable_results, serializer="pickle"
        )

        from src.utils.utils import collect_directory_files

        files = collect_directory_files(phase_dir)

        artifact_mgr.save_manifest(
            phase_dir,
            "suitability",
            files=files,
            parameters={
                "country_code": country_code,
                "criteria_dir": str(criteria_dir),
                "scenario_thresholds": scenario_thresholds,
                "params": cp,
            },
        )

        return results

    # ------------------------------------------------------------------
    # PRIVATE: SINGLE-TECHNOLOGY PIPELINE
    # ------------------------------------------------------------------

    def _process_technology_topology(
        self,
        tech: str,
        cfg_tech: TechnologyConfig,
        all_criteria: Dict[str, np.ndarray],
        country_mask: np.ndarray,
        lc_data: Optional[np.ndarray],
        slope_data: Optional[np.ndarray],
        transform: Affine,
        crs: str,
        height: int,
        width: int,
        out_tifs: Path,
        out_fig: Path,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        owa_vectors: Dict[str, List[float]],
    ) -> Dict[str, Any]:
        """Apply AHP → hard exclusions → TOPSIS → OWA for one technology.

        Returns:
            Statistics dict, or ``{"error": ..., "weights": {}}`` on failure.
        """
        criteria_names = [c for c in cfg_tech.priority_order if c in all_criteria]
        if not criteria_names:
            logger.warning(
                "  [%s] No criteria in priority order. Available: %s",
                tech,
                list(all_criteria.keys()),
            )
            return {"error": "Criteria matrix empty.", "weights": {}}

        # ── AHP weights ────────────────────────────────────────────────
        weights, lam_max, ci, cr = compute_ahp_weights(
            criteria_names, cfg_tech.priority_order, cfg_tech.intensity
        )
        log_ahp_result(tech, weights, lam_max, ci, cr, cfg_tech.intensity)

        weights_json_path = out_tifs / f"{country_code}_{tech}_weights.json"
        try:
            weights_json_path.write_text(
                json.dumps(
                    {
                        "tech": tech,
                        "country_code": country_code,
                        "intensity": cfg_tech.intensity,
                        "lambda_max": round(lam_max, 6),
                        "ci": round(ci, 6),
                        "cr": round(cr, 6),
                        "cr_ok": bool(cr <= AHP_CR_THRESHOLD),
                        "criteria_count": len(criteria_names),
                        "weights": {k: round(v, 6) for k, v in weights.items()},
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            logger.info("  [%s] AHP weights → %s", tech, weights_json_path.name)
        except Exception as exc:
            logger.warning("  [%s] Weights JSON write failed: %s", tech, exc)

        # ── Valid mask ─────────────────────────────────────────────────
        valid = country_mask.copy()
        for nm in criteria_names:
            valid &= np.isfinite(all_criteria[nm])
        n_before = int(valid.sum())

        # ── Hard exclusions ────────────────────────────────────────────
        excl_result = apply_hard_exclusions(
            valid, all_criteria, cfg_tech, lc_data, slope_data
        )
        valid = excl_result.valid_mask
        n_after = int(valid.sum())

        logger.info(
            "  [%s] Pixels: total=%d | excluded=%d "
            "(slope=%d, lc=%d) | valid=%d (%.1f%%)",
            tech,
            n_before,
            excl_result.n_excluded_total,
            excl_result.slope_excluded,
            excl_result.lc_excluded,
            n_after,
            100.0 * n_after / max(n_before, 1),
        )

        if n_after == 0:
            logger.error(
                "  [%s] All pixels excluded — no valid spatial solution.", tech
            )
            return {"error": "Total matrix exclusion.", "weights": weights}

        tech_criteria = {nm: all_criteria[nm] for nm in criteria_names}

        # ── TOPSIS ─────────────────────────────────────────────────────
        score_2d = topsis_spatial(tech_criteria, weights, valid)

        score_out = np.full((height, width), np.nan, dtype=np.float32)
        score_out[country_mask & ~valid] = TOPSIS_EXCLUSION_SCORE
        score_out[valid] = np.where(
            np.isfinite(score_2d[valid]),
            score_2d[valid],
            np.nan,
        )

        # ── OWA per scenario ───────────────────────────────────────────
        owa_tif_paths: Dict[str, str] = {}
        n_crit = len(criteria_names)

        for scenario in _OWA_SCENARIOS:
            raw_w = owa_vectors.get(scenario, OWA_DEFAULTS[scenario])
            owa_w = prepare_owa_weights(raw_w, n_crit)
            owa_2d = owa_spatial(tech_criteria, owa_w, valid)

            owa_out = np.full((height, width), np.nan, dtype=np.float32)
            owa_out[country_mask & ~valid] = TOPSIS_EXCLUSION_SCORE
            owa_out[valid] = np.where(
                np.isfinite(owa_2d[valid]), owa_2d[valid], np.nan
            )

            owa_path = (
                out_tifs / f"{country_code}_{tech}_suitability_owa_{scenario}.tif"
            )
            _save_tif(owa_out, transform, crs, owa_path)
            owa_tif_paths[scenario] = str(owa_path)
            logger.info(
                "  [%s] OWA %s → %s  (weights: %s)",
                tech,
                scenario,
                owa_path.name,
                [round(float(x), 3) for x in owa_w],
            )

        # ── Statistics (TOPSIS scores on valid pixels) ─────────────────
        valid_scores = score_out[valid]
        valid_scores = valid_scores[np.isfinite(valid_scores)]

        stats: Dict[str, Any] = {
            "n_total": n_before,
            "n_excluded": excl_result.n_excluded_total,
            "n_valid": n_after,
            "pct_excluded": round(
                100.0 * excl_result.n_excluded_total / max(n_before, 1), 2
            ),
            "mean": float(np.nanmean(valid_scores)) if valid_scores.size else 0.0,
            "std": float(np.nanstd(valid_scores)) if valid_scores.size else 0.0,
            "p10": (
                float(np.nanpercentile(valid_scores, 10))
                if valid_scores.size
                else 0.0
            ),
            "p50": (
                float(np.nanpercentile(valid_scores, 50))
                if valid_scores.size
                else 0.0
            ),
            "p90": (
                float(np.nanpercentile(valid_scores, 90))
                if valid_scores.size
                else 0.0
            ),
            "pct_high": (
                float(
                    100.0
                    * (valid_scores >= HIGH_SUITABILITY_THRESHOLD).sum()
                    / max(valid_scores.size, 1)
                )
                if valid_scores.size
                else 0.0
            ),
            "CR": cr,
            "cr_ok": cr <= AHP_CR_THRESHOLD,
            "weights": weights,
            "weights_json": str(weights_json_path),
            "owa_tif_paths": owa_tif_paths,
        }

        # ── Output: TOPSIS GeoTIFF + map ──────────────────────────────
        tif_path = out_tifs / f"{country_code}_{tech}_suitability.tif"
        map_path = out_fig / f"{country_code}_{tech}_suitability.png"

        _save_tif(score_out, transform, crs, tif_path)
        self._plot_suitability(
            score_out,
            transform,
            crs,
            tech,
            cfg_tech,
            country_name,
            mainland_gdf,
            context_gdf,
            stats,
            map_path,
        )

        stats["tif_path"] = str(tif_path)
        stats["map_path"] = str(map_path)
        return stats

    # ------------------------------------------------------------------
    # PRIVATE: VISUALISATION
    # ------------------------------------------------------------------

    def _plot_suitability(
        self,
        score: np.ndarray,
        transform: Affine,
        crs: str,
        tech: str,
        cfg_tech: TechnologyConfig,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        stats: Dict,
        out_path: Path,
    ) -> None:
        """Render cartographic TOPSIS suitability map for one technology."""
        title_main = TECH_META.get(tech, {}).get(
            "label", f"{tech.title()} Suitability"
        )

        cmap_name = {
            "solar": "YlOrRd",
            "wind": "Blues",
            "biomass": "YlGn",
        }.get(tech, "YlOrRd")

        exclude_mask = np.isfinite(score) & (score <= TOPSIS_EXCLUSION_SCORE)

        self.styler.render_raster_map(
            score=score,
            transform=transform,
            crs=crs,
            mainland_gdf=mainland_gdf,
            context_gdf=context_gdf,
            cmap_name=cmap_name,
            vmin=0.001,
            vmax=1.0,
            exclude_mask=exclude_mask,
            exclude_color=(170, 170, 170, 191),
            title_main=title_main,
            title_sub=(f"{country_name}  |  AHP-TOPSIS  |  CR={stats['CR']:.4f}"),
            stats_text=(
                f"Valid: {stats['n_valid']:,} px  |  "
                f"Excl: {stats['n_excluded']:,} px  |  "
                f"Mean: {stats['mean']:.3f}  |  "
                f"P50: {stats['p50']:.3f}  |  "
                f"≥{HIGH_SUITABILITY_THRESHOLD:.2f}: {stats['pct_high']:.1f}%"
            ),
            params_text="",
            cbar_label="Suitability Score (0–1)",
            admin_gdf=self._admin_gdf,
            out_path=out_path,
            right_in_override=1.80,
        )

    def _plot_comparison(
        self,
        results: Dict,
        country_name: str,
        country_code: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        out_path: Path,
    ) -> None:
        """Generate a side-by-side TOPSIS comparison PNG for all technologies."""
        plot_data_list = []
        tmp_dir = out_path.parent

        for tech in TECH_ORDER:
            r = results["techs"].get(tech, {})
            if not r.get("tif_path") or not Path(r["tif_path"]).exists():
                continue

            with rasterio.open(r["tif_path"]) as src:
                score_arr = src.read(1).astype(np.float32)
                if src.nodata is not None:
                    score_arr[score_arr == src.nodata] = np.nan
                t_transform = src.transform
                t_crs = str(src.crs)

            plot_data_list.append(
                {
                    "score": score_arr,
                    "transform": t_transform,
                    "crs": t_crs,
                    "tech": tech,
                    "cfg_tech": TechnologyConfig(
                        label=TECH_LABELS.get(tech, tech),
                        color="#000000",
                        intensity="",
                        priority_order=[],
                        hard_exclusions={},
                        slope_max_deg=0.0,
                        lc_exclusion_classes=set(),
                    ),
                    "country_name": country_name,
                    "mainland_gdf": mainland_gdf,
                    "context_gdf": context_gdf,
                    "stats": r,
                    "out_path": tmp_dir / f"_tmp_{tech}_suit.png",
                }
            )

        if plot_data_list:
            self.styler.create_comparison_via_pil(
                self._plot_suitability,
                plot_data_list,
                country_name,
                f"Renewable Energy Suitability — {country_name}",
                "AHP (Saaty Scale)  +  TOPSIS Spatial MCDA",
                out_path,
                gap_px=max(8, int(600 * 0.008)),
            )
            for item in plot_data_list:
                tmp = item["out_path"]
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # PRIVATE: REPORT
    # ------------------------------------------------------------------

    def _format_report(
        self,
        results: Dict,
        country_name: str,
        code: str,
    ) -> str:
        """Generate the Phase 3 text report via the shared reporting module.

        FIX (DUP_21_suitability): this used to hand-build ~100 lines of
        manually-aligned strings, duplicating the formatting logic already
        centralised in src.utils.reporting (used by lcoe_calculator.py,
        sensitivity_analyzer.py, criteria_builder.py, results_writer.py).
        Same content and ordering as before; only the assembly mechanism
        changed — no numeric or textual content was altered.
        """
        sections: List[ReportSection] = []

        # ── Scenario thresholds (forwarded to Phase 4, not applied here) ──
        thresholds = results.get("scenario_thresholds", {})
        if thresholds:
            sections.append(
                ReportSection(
                    title="SCENARIO THRESHOLDS (forwarded to Phase 4 — not applied here)",
                    rows=[
                        (sc, f"{thr:+.2f}")
                        for sc, thr in thresholds.items()
                    ],
                )
            )

        # ── Per-technology sections ────────────────────────────────────
        tech_labels = {
            "solar": "SOLAR PV",
            "wind": "WIND ONSHORE",
            "biomass": "BIOMASS / BIOENERGY",
        }

        for tech, label in tech_labels.items():
            r = results["techs"].get(tech, {})

            if r.get("error"):
                sections.append(
                    ReportSection(title=label, rows=[("[ERROR]", str(r["error"]))])
                )
                continue

            rows: List[Tuple[str, str]] = [
                ("Total territorial pixels", f"{r.get('n_total', 0):,}"),
                (
                    "Hard exclusions (pixels)",
                    f"{r.get('n_excluded', 0):,}  ({r.get('pct_excluded', 0):.1f}%)",
                ),
                (
                    "Valid pixels (MCDA)",
                    f"{r.get('n_valid', 0):,}  "
                    f"({100.0 - r.get('pct_excluded', 0):.1f}%)",
                ),
                "  TOPSIS Statistics (valid pixels):",
                (
                    "Mean ± Std",
                    f"{r.get('mean', 0):.3f} ± {r.get('std', 0):.3f}",
                ),
                (
                    "P10 / P50 / P90",
                    f"{r.get('p10', 0):.3f} / {r.get('p50', 0):.3f} / "
                    f"{r.get('p90', 0):.3f}",
                ),
                (
                    f"Score ≥ {HIGH_SUITABILITY_THRESHOLD:.2f} (high suitability)",
                    f"{r.get('pct_high', 0):.1f}%",
                ),
                "  AHP — Saaty Scale:",
                (
                    "CR",
                    f"{r.get('CR', 0):.4f}  "
                    f"{'[OK] Consistent' if r.get('cr_ok') else '[WARNING] INCONSISTENT'}",
                ),
            ]

            weights: Dict[str, float] = r.get("weights", {})
            if weights:
                rows.append(f"  {'Criterion':<32} {'Weight':>8}  {'(%)':>6}")
                for crit, w in sorted(weights.items(), key=lambda kv: -kv[1]):
                    rows.append(f"  {crit:<32} {w:>8.4f}  ({w * 100:>5.1f}%)")

            owa_paths = r.get("owa_tif_paths", {})
            if owa_paths:
                rows.append("  OWA GeoTIFFs:")
                for sc, p in owa_paths.items():
                    rows.append((sc, Path(p).name))

            if r.get("weights_json"):
                rows.append(("AHP weights JSON", Path(r["weights_json"]).name))

            sections.append(ReportSection(title=label, rows=rows))

        return build_phase_report(
            title="SUITABILITY REPORT — AHP (Saaty) + TOPSIS + OWA",
            country_name=country_name,
            country_code=code,
            timestamp=results["timestamp"],
            sections=sections,
            timings=results.get("timings", {}),
            elapsed_total=results.get("elapsed_total", 0.0),
            width=68,
        )