"""
src/processors/suitability_builder.py
=====================================
Phase 3: Multi-Criteria Spatial Decision Analysis for Renewable Energy Siting.

Generates final suitability maps (0–1) for Solar PV, Wind Onshore, and Biomass 
using spatially explicit TOPSIS with AHP-derived weights (Saaty scale).

Scientific Framework
--------------------
This module implements a two-stage Multi-Criteria Decision Analysis (MCDA):

**Stage 1: Analytic Hierarchy Process (AHP)**
Derives criterion weights from a pairwise comparison matrix using the 
geometric mean approximation (Saaty, 1980).

**Stage 2: Technique for Order of Preference by Similarity to Ideal Solution (TOPSIS)**
Ranks each pixel based on its Euclidean distance to the Positive Ideal Solution 
(PIS) and Negative Ideal Solution (NIS) (Hwang & Yoon, 1981).

References
----------
.. [1] Saaty, T.L. (1980). The Analytic Hierarchy Process. McGraw-Hill.
.. [2] Hwang, C.L., & Yoon, K. (1981). Multiple Attribute Decision Making. Springer.
.. [3] Al Garni, H.Z., & Awasthi, A. (2017). Renew. Sustain. Energy Rev. 76:641–662.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")

import numpy as np
import rasterio
from PIL import Image as _PIL_Image
from rasterio.transform import Affine

from src.core.constants import (
    AHP_CR_THRESHOLD,
    AHP_RANDOM_INDEX,
    AHP_SCALE_TABLE,
    NODATA_FLOAT,
    TECH_LABELS,
    TECH_META,
    TECH_ORDER,
)
from src.utils.map_styling import GeoWorldStyler
from src.utils.timing import timer as _timer
from src.utils.utils import safe_raster_write

logger = logging.getLogger("geoworld.processors.SuitabilityBuilder")

# ═══════════════════════════════════════════════════════════════════════════
# SCIENTIFIC CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════

TOPSIS_EXCLUSION_SCORE = 0.0
WEIGHT_SUM_TOLERANCE = 1e-6


# ═══════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class ExclusionResult:
    """
    Encapsulates the output of the hard exclusion filtering stage.

    Attributes:
        valid_mask: Boolean array indicating valid (non-excluded) pixels.
        n_excluded_total: Total count of excluded pixels.
        log: List of exclusion events with criterion metadata.
        slope_excluded: Count of pixels excluded by slope threshold.
        lc_excluded: Count of pixels excluded by land cover class.
    """

    valid_mask: np.ndarray
    n_excluded_total: int
    log: List[Dict[str, Any]] = field(default_factory=list)
    slope_excluded: int = 0
    lc_excluded: int = 0


@dataclass
class TechnologyConfig:
    """
    Configuration container for a single renewable technology.

    Attributes:
        label: Human-readable technology name.
        color: Hex color code for visualization.
        intensity: AHP Saaty scale intensity ("subtle", "moderate", "strong").
        priority_order: Ordered list of criteria names (highest to lowest priority).
        hard_exclusions: Dict mapping criterion names to minimum threshold scores.
        slope_max_deg: Maximum allowable slope in degrees.
        lc_exclusion_classes: Set of land cover class codes to exclude.
    """

    label: str
    color: str
    intensity: str
    priority_order: List[str]
    hard_exclusions: Dict[str, float]
    slope_max_deg: float
    lc_exclusion_classes: Set[int]


# ═══════════════════════════════════════════════════════════════════════════
# AHP — ANALYTIC HIERARCHY PROCESS (SAATY SCALE)
# ═══════════════════════════════════════════════════════════════════════════


def build_saaty_matrix(
    criteria: List[str],
    priority_order: List[str],
    intensity: str = "moderate",
) -> np.ndarray:
    """
    Construct an AHP pairwise comparison matrix using the Saaty Scale.

    Args:
        criteria: List of criterion names to include in the matrix.
        priority_order: Ordered list defining relative importance (first = highest).
        intensity: Saaty scale intensity level ("subtle", "moderate", "strong").

    Returns:
        Square pairwise comparison matrix (n x n).
    """
    scale = AHP_SCALE_TABLE.get(intensity, AHP_SCALE_TABLE["moderate"])
    n = len(criteria)
    full_order = priority_order + [c for c in criteria if c not in priority_order]
    rank = {c: i for i, c in enumerate(full_order)}

    mat = np.ones((n, n), dtype=np.float64)
    for i, ci in enumerate(criteria):
        for j, cj in enumerate(criteria):
            if i == j:
                continue
            diff = rank[cj] - rank[ci]
            dist = abs(diff)
            val = float(scale.get(dist, 9))
            mat[i, j] = val if diff > 0 else 1.0 / val

    return mat


def ahp_weights(
    matrix: np.ndarray
) -> Tuple[np.ndarray, float, float, float]:
    """
    Calculate AHP weights using the geometric mean method.

    Mathematical Formulation
    ------------------------
    For a pairwise comparison matrix :math:`A` of size :math:`n \\times n`:

    .. math::
        w_i = \\frac{\\left( \\prod_{j=1}^{n} a_{ij} \\right)^{1/n}}{\\sum_{k=1}^{n} \\left( \\prod_{j=1}^{n} a_{kj} \\right)^{1/n}}

    The Consistency Index (CI) and Consistency Ratio (CR) are:

    .. math::
        \\lambda_{max} = \\sum_{j=1}^{n} \\left( \\sum_{i=1}^{n} a_{ij} \\right) w_j

    .. math::
        CI = \\frac{\\lambda_{max} - n}{n - 1}, \\quad CR = \\frac{CI}{RI_n}

    where :math:`RI_n` is the Random Index for matrix size `n`.

    Args:
        matrix: Square pairwise comparison matrix (n x n).

    Returns:
        Tuple of (weights array, lambda_max, CI, CR).
    """
    n = matrix.shape[0]
    geo_mean = np.exp(np.log(matrix + 1e-12).mean(axis=1))
    weights = geo_mean / geo_mean.sum()

    col_sum = matrix.sum(axis=0)
    lam_max = float((col_sum * weights).sum())

    ci = (lam_max - n) / max(n - 1, 1)
    ri = AHP_RANDOM_INDEX.get(n, 1.59)
    cr = ci / ri if ri > 0 else 0.0

    return weights, lam_max, ci, cr


def compute_ahp_weights(
    criteria: List[str],
    priority_order: List[str],
    intensity: str = "moderate",
) -> Tuple[Dict[str, float], float, float, float]:
    """
    High-level interface to compute AHP weights from an ordinal priority list.

    Args:
        criteria: List of available criterion names.
        priority_order: Ordered list defining relative importance.
        intensity: Saaty scale intensity ("subtle", "moderate", "strong").

    Returns:
        Tuple of (weights dict, lambda_max, CI, CR).
    """
    mat = build_saaty_matrix(criteria, priority_order, intensity)
    w, lam, ci, cr = ahp_weights(mat)
    return {c: float(w[i]) for i, c in enumerate(criteria)}, lam, ci, cr


def log_ahp_result(
    tech: str,
    weights: Dict[str, float],
    lam_max: float,
    ci: float,
    cr: float,
    intensity: str,
) -> None:
    """
    Log the results of AHP consistency and weighting calculations.

    Args:
        tech: Technology identifier.
        weights: Dictionary of criterion weights.
        lam_max: Principal eigenvalue (lambda_max).
        ci: Consistency Index.
        cr: Consistency Ratio.
        intensity: Saaty scale intensity used.

    Returns:
        None
    """
    status = "[OK] Consistent" if cr <= AHP_CR_THRESHOLD else "[WARNING] INCONSISTENT"
    logger.info("  [%s] AHP Saaty (%s) — %s", tech, intensity, status)
    logger.info("  [%s]   λ_max=%.4f | CI=%.4f | CR=%.4f", tech, lam_max, ci, cr)
    for name, w in sorted(weights.items(), key=lambda x: -x[1]):
        logger.info(
            "  [%s]   %-30s: %.4f  (%.1f%%)",
            tech,
            name,
            w,
            w * 100
        )


# ═══════════════════════════════════════════════════════════════════════════
# HARD EXCLUSION LOGIC (MODULARIZED)
# ═══════════════════════════════════════════════════════════════════════════


def apply_hard_exclusions(
    base_valid_mask: np.ndarray,
    all_criteria: Dict[str, np.ndarray],
    cfg_tech: TechnologyConfig,
    lc_data: Optional[np.ndarray],
    slope_data: Optional[np.ndarray],
) -> ExclusionResult:
    """
    Apply all hard exclusion constraints and return the updated validity mask.

    This function is a pure transformation: it takes input data and configuration,
    and returns a result object without side effects, enabling isolated unit testing.

    Args:
        base_valid_mask: Initial boolean mask of valid pixels.
        all_criteria: Dictionary of all available criterion arrays.
        cfg_tech: Technology configuration object.
        lc_data: Optional land cover raster array.
        slope_data: Optional slope raster array (degrees).

    Returns:
        ExclusionResult object with updated mask and exclusion statistics.
    """
    valid = base_valid_mask.copy()
    n_before = int(valid.sum())
    exclusion_log: List[Dict[str, Any]] = []

    for crit_name, threshold in cfg_tech.hard_exclusions.items():
        if crit_name not in all_criteria:
            continue
        crit_arr = all_criteria[crit_name]
        excl_mask = np.isfinite(crit_arr) & (crit_arr < threshold)
        n_excl = int(excl_mask.sum())
        valid &= ~excl_mask
        exclusion_log.append(
            {
                "criterion": crit_name,
                "threshold": threshold,
                "n_excluded": n_excl
            }
        )

    slope_excluded = 0
    if slope_data is not None:
        slope_excl = np.isfinite(slope_data) & (
            slope_data > cfg_tech.slope_max_deg
        )
        slope_excluded = int(slope_excl.sum())
        valid &= ~slope_excl

    lc_excluded = 0
    if lc_data is not None and cfg_tech.lc_exclusion_classes:
        lc_safe = np.where(np.isfinite(lc_data), lc_data, -1.0)
        lc_excl = (
            np.isin(
                np.round(lc_safe).astype(np.int32),
                list(cfg_tech.lc_exclusion_classes)
            )
            & np.isfinite(lc_data)
        )
        lc_excluded = int(lc_excl.sum())
        valid &= ~lc_excl

    n_excluded_total = n_before - int(valid.sum())

    return ExclusionResult(
        valid_mask=valid,
        n_excluded_total=n_excluded_total,
        log=exclusion_log,
        slope_excluded=slope_excluded,
        lc_excluded=lc_excluded,
    )


# ═══════════════════════════════════════════════════════════════════════════
# MEMORY-OPTIMIZED VECTORISED SPATIAL TOPSIS
# ═══════════════════════════════════════════════════════════════════════════


def topsis_spatial(
    criteria_arrays: Dict[str, np.ndarray],
    weights: Dict[str, float],
    valid_mask: np.ndarray,
    chunk_size: int = 500_000,
) -> np.ndarray:
    """
    Executes a highly memory-optimized, spatially explicit TOPSIS evaluation.

    Mathematical Formulation
    ------------------------
    1. Vector Normalization:
       .. math:: r_{ij} = \\frac{x_{ij}}{\\sqrt{\\sum_{i=1}^{m} x_{ij}^2}}
    2. Weight Application:
       .. math:: v_{ij} = w_j \\cdot r_{ij}
    3. Separation Distances from Positive Ideal Solution (A+) and Negative Ideal (A-):
       .. math:: d_i^+ = \\sqrt{ \\sum_{j=1}^{n} (v_{ij} - v_j^+)^2 }
       .. math:: d_i^- = \\sqrt{ \\sum_{j=1}^{n} (v_{ij} - v_j^-)^2 }
    4. Relative Closeness Score (S_i):
       .. math:: S_i = \\frac{d_i^-}{d_i^+ + d_i^-}

    Memory Architecture
    -------------------
    Avoids holistic stacking (np.stack) of arrays. Computes norms sequentially per 
    criterion (O(N) memory bound), followed by chunked spatial distance evaluation.

    Args:
        criteria_arrays: Dictionary mapping criterion names to score arrays.
        weights: Dictionary of AHP-derived weights (must sum to 1.0).
        valid_mask: Boolean mask indicating pixels to evaluate.
        chunk_size: Number of pixels to process per iteration (memory control).

    Returns:
        2D array of TOPSIS scores [0, 1] with NaN for invalid pixels.

    Raises:
        ValueError: If weights do not sum to 1.0 within tolerance.
    """
    weight_sum = sum(weights.values())
    if abs(weight_sum - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise ValueError(
            f"AHP weights must sum to 1.0 (got {weight_sum:.6f}). "
            f"Ensure compute_ahp_weights() was called correctly."
        )

    names = list(criteria_arrays.keys())
    height, width = next(iter(criteria_arrays.values())).shape
    flat_valid = valid_mask.ravel()
    n_valid = int(flat_valid.sum())

    if n_valid == 0:
        return np.full((height, width), np.nan, dtype=np.float32)

    col_norms = np.empty(len(names), dtype=np.float32)
    pis = np.empty(len(names), dtype=np.float32)
    nis = np.empty(len(names), dtype=np.float32)

    for i, nm in enumerate(names):
        vec = criteria_arrays[nm].ravel()[flat_valid].astype(np.float32)
        norm = np.sqrt(np.sum(vec ** 2))
        norm = norm if norm > 0 else 1.0
        col_norms[i] = norm

        vec_norm = (vec / norm) * weights[nm]
        pis[i] = vec_norm.max()
        nis[i] = vec_norm.min()
        del vec, vec_norm

    score_valid = np.empty(n_valid, dtype=np.float32)
    w_array = np.array([weights[nm] for nm in names], dtype=np.float32)

    for start in range(0, n_valid, chunk_size):
        end = min(start + chunk_size, n_valid)
        chunk_len = end - start

        chunk = np.empty((chunk_len, len(names)), dtype=np.float32)
        for i, nm in enumerate(names):
            vec_slice = criteria_arrays[nm].ravel()[flat_valid][start:end]
            chunk[:, i] = (vec_slice / col_norms[i]) * w_array[i]

        d_pis_c = np.sqrt(((chunk - pis) ** 2).sum(axis=1))
        d_nis_c = np.sqrt(((chunk - nis) ** 2).sum(axis=1))

        denom = d_pis_c + d_nis_c
        score_valid[start:end] = np.where(denom > 0, d_nis_c / denom, 0.0)
        del chunk

    score_flat = np.full(height * width, np.nan, dtype=np.float32)
    score_flat[flat_valid] = score_valid

    return score_flat.reshape(height, width)


# ═══════════════════════════════════════════════════════════════════════════
# TECHNOLOGY CONFIGURATION FACTORY
# ═══════════════════════════════════════════════════════════════════════════


def get_technology_configs(
    country_params: Optional[Dict] = None
) -> Dict[str, TechnologyConfig]:
    """
    Return MCDA configuration objects for each renewable technology.

    Args:
        country_params: Optional dictionary with country-specific parameters.

    Returns:
        Dictionary mapping technology names to TechnologyConfig objects.
    """
    forest_as_exclusion = True
    if country_params is not None:
        raw = country_params.get("forest_as_exclusion", True)
        forest_as_exclusion = str(raw).lower() not in ("false", "0", "no")

    base_lc_exclusions = {50, 70, 80, 90, 95}
    if forest_as_exclusion:
        base_lc_exclusions.add(10)
        logger.info(
            "  [suitability] Policy Active: ESA class 10 (Trees/Forest) "
            "blocked for Solar/Wind/Biomass."
        )
    else:
        logger.warning(
            "  [suitability] Policy Inactive: ESA class 10 (Trees/Forest) "
            "ALLOWED for Solar/Wind/Biomass."
        )

    common_exclusions = {
        "lakes_exclusion": 0.5,
        "protected_areas": 0.99,
        "proximity_plants": 0.01
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
                "proximity_plants"
            ],
            hard_exclusions=common_exclusions,
            slope_max_deg=15.0,
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
                "river_wind"
            ],
            hard_exclusions=common_exclusions,
            slope_max_deg=20.0,
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
                "seismic_suitability"
            ],
            hard_exclusions=common_exclusions,
            slope_max_deg=30.0,
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
    path: Path
) -> None:
    """
    Save suitability scores to a compressed GeoTIFF. NaN propagates as NoData.

    Args:
        data: Suitability score array.
        transform: Affine geotransform.
        crs: Coordinate reference system string.
        path: Output file path.

    Returns:
        None
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
    """
    Orchestrates Phase 3 of the GeoWorld pipeline.

    Ingests Phase 2b criteria rasters, applies AHP Saaty scaling, 
    evaluates spatial TOPSIS, and exports TIF distributions and PNG maps.
    """

    def __init__(self, cfg: Any, outputs_dir: Path):
        """
        Initialize SuitabilityBuilder.

        Args:
            cfg: Global configuration object.
            outputs_dir: Base output directory path.
        """
        self.cfg = cfg
        self.outputs_dir = Path(outputs_dir)
        self.viz_cfg = cfg.system.get("visualization", {})
        self._admin_gdf: Optional[gpd.GeoDataFrame] = None
        pipeline_dpi = cfg.system.get("pipeline", {}).get("map_dpi_export", 150)
        self.styler = GeoWorldStyler(self.viz_cfg, global_dpi=pipeline_dpi)

    def _find_criteria_dir(self, country_code: str) -> Optional[Path]:
        """
        Locate criteria TIF directory for a given country.

        Args:
            country_code: ISO country code.

        Returns:
            Path to criteria directory, or None if not found.
        """
        base = self.outputs_dir / country_code
        candidates = [
            base / "criteria_builder" / "tif",
            base / "criteria_builder" / "tifs",
            base / "criteria_builder",
            base / "criteria",
            base
        ]
        for path in candidates:
            if path.exists() and list(path.glob("*.tif")):
                return path
        return None

    def run(
        self,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        criteria_dir: Path,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        lc_aligned: Optional[Path] = None,
        slope_aligned: Optional[Path] = None,
        country_params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Execute Phase 3 for all modelled renewable energy technologies.

        Args:
            country_code: ISO country code.
            country_name: Full country name.
            mainland_gdf: Country boundary GeoDataFrame.
            criteria_dir: Path to Phase 2b criteria TIF directory.
            context_gdf: Optional neighboring countries for context.
            lc_aligned: Optional path to aligned land cover raster.
            slope_aligned: Optional path to aligned slope raster.
            country_params: Optional country-specific parameters.

        Returns:
            Dictionary containing results for all technologies and metadata.

        Raises:
            FileNotFoundError: If criteria directory is missing or empty.
            RuntimeError: If no valid criteria matrices can be loaded.
        """
        started_at = datetime.now()
        timings: Dict[str, float] = {}
        criteria_dir = Path(criteria_dir)

        if not criteria_dir.exists():
            criteria_dir = self._find_criteria_dir(country_code)
            if criteria_dir is None:
                raise FileNotFoundError("Criteria directory absent. Halt pipeline.")

        out_base = self.outputs_dir / country_code / "suitability"
        out_tifs = out_base / "tifs"
        out_maps = out_base / "maps"
        out_rep = out_base / "reports"

        for d in [out_tifs, out_maps, out_rep]:
            d.mkdir(parents=True, exist_ok=True)

        self._admin_gdf = self.styler.load_admin_boundaries(
            country_name,
            mainland_gdf,
            Path(self.cfg.raw_path)
        )
        transform, crs, height, width = self._load_reference_meta(
            criteria_dir,
            country_code
        )

        with _timer("load_criteria", timings):
            all_criteria = self._load_all_criteria(
                criteria_dir,
                country_code,
                height,
                width
            )

        if not all_criteria:
            raise RuntimeError("Data load failure: Matrices unreadable.")

        country_mask = np.zeros((height, width), dtype=bool)
        for arr in all_criteria.values():
            country_mask |= np.isfinite(arr)

        lc_data = self._load_aux_raster(lc_aligned, height, width)
        slope_data = self._load_aux_raster(slope_aligned, height, width)

        results = {
            "country": country_code,
            "timestamp": started_at.isoformat(),
            "techs": {},
            "timings": timings
        }

        tech_configs = get_technology_configs(country_params)

        for tech, cfg_tech in tech_configs.items():
            logger.info(
                "\n  %s\n  Technology: %s\n  %s",
                "─" * 50,
                cfg_tech.label,
                "─" * 50
            )
            with _timer(tech, timings):
                results["techs"][tech] = self._process_technology_topology(
                    tech,
                    cfg_tech,
                    all_criteria,
                    country_mask,
                    lc_data,
                    slope_data,
                    transform,
                    crs,
                    height,
                    width,
                    out_tifs,
                    out_maps,
                    country_code,
                    country_name,
                    mainland_gdf,
                    context_gdf
                )

        with _timer("comparison_map", timings):
            self._plot_comparison(
                results,
                country_name,
                country_code,
                mainland_gdf,
                context_gdf,
                out_maps / f"{country_code}_suitability_comparison.png"
            )

        results["elapsed_total"] = round(
            (datetime.now() - started_at).total_seconds(),
            1
        )
        report_text = self._format_report(results, country_name, country_code)
        logger.info("\n%s", report_text)

        rep_path = out_rep / (
            f"{country_code}_suitability_"
            f"{started_at.strftime('%Y%m%d_%H%M%S')}.txt"
        )
        rep_path.write_text(report_text, encoding="utf-8")

        return results

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
        out_maps: Path,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
    ) -> Dict[str, Any]:
        """
        Applies AHP-TOPSIS pipeline for one technology and persists outputs.

        Args:
            tech: Technology identifier ("solar", "wind", "biomass").
            cfg_tech: Technology configuration object.
            all_criteria: Dictionary of all available criterion arrays.
            country_mask: Boolean mask of country extent.
            lc_data: Optional land cover array.
            slope_data: Optional slope array.
            transform: Affine geotransform.
            crs: Coordinate reference system string.
            height: Raster height in pixels.
            width: Raster width in pixels.
            out_tifs: Output directory for TIF files.
            out_maps: Output directory for PNG maps.
            country_code: ISO country code.
            country_name: Full country name.
            mainland_gdf: Country boundary GeoDataFrame.
            context_gdf: Optional neighboring countries GeoDataFrame.

        Returns:
            Dictionary containing statistics and output paths for the technology.
        """
        criteria_names = [
            c for c in cfg_tech.priority_order if c in all_criteria
        ]
        if not criteria_names:
            logger.warning(
                "  [%s] No criteria found in priority order. Available: %s",
                tech,
                list(all_criteria.keys()),
            )
            return {"error": "Criteria matrix empty.", "weights": {}}

        weights, lam_max, ci, cr = compute_ahp_weights(
            criteria_names,
            cfg_tech.priority_order,
            cfg_tech.intensity,
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
            logger.info("  [%s] AHP weights saved: %s", tech, weights_json_path.name)
        except Exception as exc:
            logger.warning("  [%s] Failed to save weights JSON: %s", tech, exc)

        valid = country_mask.copy()
        for nm in criteria_names:
            valid &= np.isfinite(all_criteria[nm])
        n_before = int(valid.sum())

        excl_result = apply_hard_exclusions(
            valid,
            all_criteria,
            cfg_tech,
            lc_data,
            slope_data
        )
        valid = excl_result.valid_mask
        n_after = int(valid.sum())

        logger.info(
            "  [%s] Pixels: total=%d | excluded=%d (slope=%d, lc=%d) | "
            "valid=%d (%.1f%%)",
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
                "  [%s] All pixels excluded — no valid spatial solution.",
                tech,
            )
            return {
                "error": "Total matrix exclusion. No valid spatial solutions.",
                "weights": weights,
            }

        score_2d = topsis_spatial(
            {nm: all_criteria[nm] for nm in criteria_names},
            weights,
            valid,
        )

        score_out = np.full((height, width), np.nan, dtype=np.float32)
        score_out[country_mask & ~valid] = TOPSIS_EXCLUSION_SCORE
        score_out[valid] = np.where(
            np.isfinite(score_2d[valid]),
            score_2d[valid],
            TOPSIS_EXCLUSION_SCORE,
        )

        valid_scores = score_out[valid]
        valid_scores = valid_scores[np.isfinite(valid_scores)]

        stats = {
            "n_total": n_before,
            "n_excluded": excl_result.n_excluded_total,
            "n_valid": n_after,
            "pct_excluded": round(
                100.0 * excl_result.n_excluded_total / max(n_before, 1),
                2
            ),
            "mean": float(np.nanmean(valid_scores)),
            "std": float(np.nanstd(valid_scores)),
            "p10": float(np.nanpercentile(valid_scores, 10)),
            "p50": float(np.nanpercentile(valid_scores, 50)),
            "p90": float(np.nanpercentile(valid_scores, 90)),
            "pct_high": float(
                100.0 * (valid_scores >= 0.6).sum() / max(valid_scores.size, 1)
            ),
            "CR": cr,
            "cr_ok": cr <= AHP_CR_THRESHOLD,
            "weights": weights,
            "weights_json": str(weights_json_path),
        }

        tif_path = out_tifs / f"{country_code}_{tech}_suitability.tif"
        map_path = out_maps / f"{country_code}_{tech}_suitability.png"

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

    def _load_reference_meta(
        self,
        criteria_dir: Path,
        code: str,
    ) -> Tuple[Affine, str, int, int]:
        """
        Load spatial metadata from reference criterion raster.

        Args:
            criteria_dir: Path to criteria TIF directory.
            code: ISO country code.

        Returns:
            Tuple of (affine transform, CRS string, height, width).

        Raises:
            FileNotFoundError: If no valid TIF files found in directory.
        """
        for name in ["solar_resource", "wind_resource", "terrain_score"]:
            for path in criteria_dir.glob("*.tif"):
                if name in path.stem.lower():
                    with rasterio.open(str(path)) as src:
                        return src.transform, str(src.crs), src.height, src.width

        tif_files = sorted(criteria_dir.glob("*.tif"))
        if not tif_files:
            raise FileNotFoundError(
                f"No GeoTIFF files found in {criteria_dir}. "
                f"Run Phase 2b (CriteriaBuilder) first."
            )
        with rasterio.open(str(tif_files[0])) as src:
            return src.transform, str(src.crs), src.height, src.width

    def _load_all_criteria(
        self,
        criteria_dir: Path,
        code: str,
        height: int,
        width: int
    ) -> Dict[str, np.ndarray]:
        """
        Load all criterion rasters from directory.

        Args:
            criteria_dir: Path to criteria TIF directory.
            code: ISO country code.
            height: Expected raster height.
            width: Expected raster width.

        Returns:
            Dictionary mapping criterion names to normalized arrays.
        """
        criteria = {}
        for path in criteria_dir.glob("*.tif"):
            try:
                with rasterio.open(str(path)) as src:
                    arr = src.read(1).astype(np.float32)
                    if src.nodata is not None:
                        arr[arr == src.nodata] = np.nan
                    arr[arr < 0] = np.nan
                    if arr.shape == (height, width):
                        clean_name = path.stem.lower().replace(
                            f"{code.lower()}_",
                            ""
                        )
                        criteria[clean_name] = arr
            except Exception as e:
                logger.warning(
                    "  Bypassing corrupted matrix %s: %s",
                    path.name,
                    e
                )
        return criteria

    def _load_aux_raster(
        self,
        path: Optional[Path],
        height: int,
        width: int
    ) -> Optional[np.ndarray]:
        """
        Load auxiliary raster (land cover, slope) for exclusion logic.

        Args:
            path: Path to auxiliary raster.
            height: Expected raster height.
            width: Expected raster width.

        Returns:
            Normalized array, or None if loading fails.
        """
        if not path or not Path(path).exists():
            return None
        try:
            with rasterio.open(str(path)) as src:
                arr = src.read(1).astype(np.float32)
                if src.nodata is not None:
                    arr[arr == src.nodata] = np.nan
                return arr if arr.shape == (height, width) else None
        except Exception:
            return None

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
        out_path: Path
    ) -> None:
        """
        Render cartographic visualization of suitability scores.

        Args:
            score: Suitability score array [0, 1].
            transform: Affine geotransform.
            crs: Coordinate reference system string.
            tech: Technology identifier.
            cfg_tech: Technology configuration object.
            country_name: Country display name.
            mainland_gdf: Country boundary GeoDataFrame.
            context_gdf: Optional neighboring countries GeoDataFrame.
            stats: Statistics dictionary.
            out_path: Output PNG path.

        Returns:
            None
        """
        title = TECH_META.get(tech, {}).get(
            "label",
            f"{tech.title()} Suitability"
        )
        h, w = score.shape
        r_minx, r_maxy = transform.c, transform.f
        extent = [
            r_minx,
            r_minx + transform.a * w,
            r_maxy + transform.e * h,
            r_maxy
        ]
        v_minx, v_miny, v_maxx, v_maxy = mainland_gdf.total_bounds

        max_display_px = self.viz_cfg.get("layout", {}).get(
            "max_raster_display_px",
            1200
        )
        if max(h, w) > max_display_px:
            scale = max_display_px / max(h, w)
            score_pil = _PIL_Image.fromarray(
                np.where(np.isfinite(score), score, -9999.0).astype(np.float32)
            ).resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                _PIL_Image.NEAREST
            )
            score = np.array(score_pil, dtype=np.float32)
            score[score == -9999.0] = np.nan

        fig, ax = self.styler.create_figure(
            v_minx,
            v_maxx,
            v_miny,
            v_maxy,
            right_in_override=1.80
        )
        self.styler.draw_basemap(
            ax,
            crs,
            mainland_gdf,
            context_gdf,
            self._admin_gdf,
            extent=extent
        )

        excl_rgba = np.zeros((score.shape[0], score.shape[1], 4), dtype=np.uint8)
        excl_mask = np.isfinite(score) & (score <= 0.0)
        excl_rgba[excl_mask, :3] = [170, 170, 170]
        excl_rgba[excl_mask, 3] = 191
        ax.imshow(
            excl_rgba,
            extent=extent,
            origin="upper",
            zorder=2,
            interpolation="nearest",
            aspect="auto"
        )

        cmap_name = {
            "solar": "YlOrRd",
            "wind": "Blues",
            "biomass": "YlGn"
        }.get(tech, "YlOrRd")
        cmap = self.styler.make_cmap(
            cmap_name,
            bad="none",
            under="#AAAAAA",
            vmin_frac=0.08,
            vmax_frac=1.0
        )
        im = ax.imshow(
            np.where(np.isfinite(score) & (score > 0.0), score, np.nan),
            extent=extent,
            origin="upper",
            cmap=cmap,
            vmin=0.001,
            vmax=1.0,
            zorder=3,
            interpolation="bilinear",
            aspect="auto"
        )

        self.styler.add_decorations(ax, v_minx, v_maxx, v_miny, v_maxy)
        self.styler.add_colorbar(fig, im, "Suitability Score (0–1)", extend="neither")

        if self._admin_gdf is not None:
            self.styler.draw_admin_labels(
                ax,
                self._admin_gdf,
                v_minx,
                v_maxx,
                v_miny,
                v_maxy
            )

        self.styler.add_standard_title(
            fig,
            title_main=title,
            title_sub=(
                f"{country_name}  |  AHP-TOPSIS  |  CR={stats['CR']:.4f}"
            )
        )
        self.styler.add_standard_footer(
            fig,
            stats_text=(
                f"Valid: {stats['n_valid']:,} px  |  "
                f"Excl: {stats['n_excluded']:,} px  |  "
                f"Mean: {stats['mean']:.3f}  |  "
                f"P50: {stats['p50']:.3f}  |  "
                f"≥0.6: {stats['pct_high']:.1f}%"
            ),
            crs_metadata=f"CRS: {crs or 'EPSG:4326'}"
        )

        self.styler.save(fig, out_path)

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
        Generates a side-by-side comparison PNG for all technologies.

        Args:
            results: Results dictionary from run() method.
            country_name: Full country name.
            country_code: ISO country code.
            mainland_gdf: Country boundary GeoDataFrame.
            context_gdf: Optional neighboring countries GeoDataFrame.
            out_path: Output PNG path.

        Returns:
            None
        """
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

            plot_data_list.append({
                "score": score_arr,
                "transform": src.transform,
                "crs": str(src.crs),
                "tech": tech,
                "cfg_tech": TechnologyConfig(
                    label=TECH_LABELS.get(tech, tech),
                    color="#000000",
                    intensity="",
                    priority_order=[],
                    hard_exclusions={},
                    slope_max_deg=0,
                    lc_exclusion_classes=set(),
                ),
                "country_name": country_name,
                "mainland_gdf": mainland_gdf,
                "context_gdf": context_gdf,
                "stats": r,
                "out_path": tmp_dir / f"_tmp_{tech}_suit.png",
            })

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

    def _format_report(
        self,
        results: Dict,
        country_name: str,
        code: str
    ) -> str:
        """
        Generates the Phase 3 text report.

        Args:
            results: Results dictionary from run() method.
            country_name: Full country name.
            code: ISO country code.

        Returns:
            Formatted report string.
        """
        lines = [
            "=" * 68,
            f"  SUITABILITY REPORT — AHP (Saaty) + TOPSIS MCDA\n"
            f"  {country_name} ({code})\n"
            f"  {results['timestamp'][:19].replace('T', ' ')}",
            "=" * 68,
        ]

        tech_labels = {
            "solar": "SOLAR PV",
            "wind": "WIND ONSHORE",
            "biomass": "BIOMASS / BIOENERGY",
        }

        for tech, label in tech_labels.items():
            r = results["techs"].get(tech, {})
            lines += ["", "-" * 68, f"  {label}", "-" * 68]

            if r.get("error"):
                lines.append(f"  [ERROR] {r['error']}")
                continue

            lines += [
                f"    Total territorial pixels            : "
                f"{r.get('n_total', 0):,}",
                f"    Hard exclusions (pixels)            : "
                f"{r.get('n_excluded', 0):,}  "
                f"({r.get('pct_excluded', 0):.1f}%)",
                f"    Valid pixels (TOPSIS)               : "
                f"{r.get('n_valid', 0):,}  "
                f"({100.0 - r.get('pct_excluded', 0):.1f}%)",
                "",
                f"    TOPSIS Statistics (valid pixels):",
                f"      Mean ± Std                        : "
                f"{r.get('mean', 0):.3f} ± {r.get('std', 0):.3f}",
                f"      P10 / P50 / P90                   : "
                f"{r.get('p10', 0):.3f} / {r.get('p50', 0):.3f} / "
                f"{r.get('p90', 0):.3f}",
                f"      Score >= 0.75 (high suitability)   : "
                f"{r.get('pct_high', 0):.1f}%",
                "",
                f"    AHP — Saaty Scale:",
                f"      CR (Consistency Ratio)            : "
                f"{r.get('CR', 0):.4f}  "
                f"{'[OK] Consistent' if r.get('cr_ok') else '[WARNING] INCONSISTENT'}",
            ]

            weights: Dict[str, float] = r.get("weights", {})
            if weights:
                lines.append(
                    f"      {'Criterion':<32} {'Weight':>8}  {'(%)':>6}"
                )
                lines.append("      " + "─" * 50)
                for crit, w in sorted(weights.items(), key=lambda kv: -kv[1]):
                    lines.append(
                        f"      {crit:<32} {w:>8.4f}  ({w * 100:>5.1f}%)"
                    )

            if r.get("weights_json"):
                lines.append(
                    f"\n    Weights JSON: {Path(r['weights_json']).name}"
                )

        lines += [
            "",
            "=" * 68,
            "  TIMINGS BY STAGE",
            "-" * 68,
        ]
        for step, t in results.get("timings", {}).items():
            lines.append(f"    {step:<24}: {t:>6.1f}s")
        lines += [
            f"    {'TOTAL':<24}: {results.get('elapsed_total', 0):.1f}s",
            "=" * 68,
            "",
        ]

        return "\n".join(lines)