"""
src/processors/criteria_builder.py
==================================
Phase 2b: Geomorphological and Spatial Criteria Normalisation.

Converts physical units (slope degrees, distance km, irradiance kWh/m²)
into dimensionless Fuzzy Suitability Scores [0, 1] for AHP-TOPSIS aggregation.

Architecture
------------
- Phase A (Algebraic): Vectorized NumPy operations.
- Phase B (Cartographic): Async Matplotlib Agg rendering via ThreadPoolExecutor.

Thread-Safety
-------------
All cartographic routines use object-oriented Figure/Axes instantiation
(never the pyplot stateful interface) for memory isolation across workers.
"""

from __future__ import annotations

import logging
import math
import os
import platform
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize as rasterize_feats
from rasterio.transform import Affine
from scipy.ndimage import distance_transform_edt, gaussian_filter
from shapely.geometry import mapping

from src.core.constants import (
    IUCN_FREE_SCORE,
    IUCN_SCORE_DEFAULT,
    IUCN_SCORES,
    NODATA_FLOAT,
    PROXIMITY_DECAY_SIGMA_KM,
    PROXIMITY_SMOOTH_SIGMA_PX,
    TRI_THRESHOLD,
)
from src.core.schemas import AlignedLayers, CountryParams
from src.core.validators import validate_aligned_layers, validate_raster_values
from src.utils.map_styling import GeoWorldStyler
from src.utils.normalization import normalize_percentile
from src.utils.reporting import write_criteria_summary
from src.utils.timing import timer as _timer
from src.utils.utils import safe_raster_open, safe_raster_write

os.environ["GDAL_QUIET"] = "ON"
os.environ["CPL_LOG"] = "NUL" if platform.system() == "Windows" else "/dev/null"

logger = logging.getLogger("geoworld.processors.CriteriaBuilder")

# Type alias: accepts both new typed contract and legacy dict during migration.
ParamsLike = Union[CountryParams, Dict[str, Any]]


# ---------------------------------------------------------------------------
# Internal I/O helpers  (private — specific to this module, not worth moving)
# ---------------------------------------------------------------------------


def _read(path: Path) -> Tuple[np.ndarray, rasterio.DatasetReader]:
    """Open a raster and return (array, open source handle).
    Caller is responsible for closing the source handle.
    """
    src = rasterio.open(str(path))
    return src.read(1).astype(np.float32), src


def _valid_mask(data: np.ndarray, nodata: Any) -> np.ndarray:
    """Boolean mask: True for finite, non-nodata pixels."""
    mask = np.isfinite(data)
    if nodata is not None:
        mask &= data != float(nodata)
    return mask


def _save_criterion(
    score: np.ndarray,
    out_path: Path,
    transform: Affine,
    crs: str,
    dtype: str = "float32",
    nodata_val: Any = NODATA_FLOAT,
) -> None:
    """Write a normalised criterion raster to disk with LZW compression."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(
        driver="GTiff", dtype=dtype,
        width=score.shape[1], height=score.shape[0],
        count=1, crs=crs, transform=transform,
        nodata=nodata_val, compress="lzw",
        tiled=True, blockxsize=256, blockysize=256,
    )
    with safe_raster_write(out_path, **profile) as dst:
        dst.write(score.astype(dtype), 1)


def _param(params: ParamsLike, key: str, default: Any = None) -> Any:
    """Unified parameter accessor for CountryParams and legacy dict."""
    return params.get(key, default)


# ---------------------------------------------------------------------------
# Per-criterion computation functions
# ---------------------------------------------------------------------------


def compute_solar_resource(
    solar_path: Path,
    params: ParamsLike,
) -> Tuple[np.ndarray, Affine, str]:
    """Solar resource suitability from irradiance raster."""
    data, src = _read(solar_path)
    valid = _valid_mask(data, src.nodata) & (data > 0)
    tf, crs = src.transform, str(src.crs)
    src.close()

    score = normalize_percentile(
        data, valid,
        float(_param(params, "normalization_min_percentile", 5)),
        float(_param(params, "normalization_max_percentile", 95)),
    )
    pvout_weight = float(_param(params, "solar_pvout_weight", 1.0))
    if pvout_weight != 1.0:
        score = np.where(
            np.isfinite(score), score * pvout_weight, score
        ).astype(np.float32)
    return score, tf, crs


def compute_wind_resource(
    wind_path: Path,
    params: ParamsLike,
) -> Tuple[np.ndarray, Affine, str]:
    """Wind resource suitability from wind speed / power-density raster."""
    data, src = _read(wind_path)
    valid = _valid_mask(data, src.nodata) & (data > 0)
    tf, crs = src.transform, str(src.crs)
    src.close()
    return (
        normalize_percentile(
            data, valid,
            float(_param(params, "normalization_min_percentile", 5)),
            float(_param(params, "normalization_max_percentile", 95)),
        ),
        tf, crs,
    )


def compute_terrain_score(
    slope_path: Path,
    elev_path: Optional[Path],
    params: ParamsLike,
) -> Tuple[np.ndarray, Affine, str]:
    """Compound terrain suitability: 0.6 × slope_score + 0.4 × TRI_score.

    TRI = sqrt(sum_8neighbours((E_centre − E_i)²))
    """
    threshold = float(_param(params, "slope_threshold_deg", 7.0))
    slope, src_s = _read(slope_path)
    valid_s = _valid_mask(slope, src_s.nodata) & (slope >= 0)
    tf, crs = src_s.transform, str(src_s.crs)
    src_s.close()

    score_s = np.full(slope.shape, NODATA_FLOAT, dtype=np.float32)
    score_s[valid_s] = np.clip(
        1.0 - slope[valid_s] / threshold, 0.0, 1.0
    ).astype(np.float32)

    score_tri = None
    if elev_path and Path(elev_path).exists():
        try:
            elev, src_e = _read(elev_path)
            valid_e = _valid_mask(elev, src_e.nodata)
            src_e.close()
            pad = np.pad(elev, 1, mode="edge")
            tri = np.zeros_like(elev, dtype=np.float32)
            for di in [-1, 0, 1]:
                for dj in [-1, 0, 1]:
                    if di == 0 and dj == 0:
                        continue
                    tri += (
                        pad[1 + di: 1 + di + elev.shape[0],
                            1 + dj: 1 + dj + elev.shape[1]] - elev
                    ) ** 2
            tri = np.sqrt(tri)
            st = np.full(elev.shape, NODATA_FLOAT, dtype=np.float32)
            st[valid_e] = np.clip(
                1.0 - tri[valid_e] / TRI_THRESHOLD, 0.0, 1.0
            ).astype(np.float32)
            score_tri = st
        except Exception as exc:
            logger.warning("  TRI computation failed: %s", exc)

    if score_tri is not None:
        combined = np.full(slope.shape, NODATA_FLOAT, dtype=np.float32)
        both   = (score_s != NODATA_FLOAT) & (score_tri != NODATA_FLOAT)
        only_s = (score_s != NODATA_FLOAT) & (score_tri == NODATA_FLOAT)
        combined[both]   = (0.6 * score_s[both] + 0.4 * score_tri[both]).astype(np.float32)
        combined[only_s] = score_s[only_s]
    else:
        combined = score_s
    return combined, tf, crs


def compute_slope_degrees(slope_path: Path) -> Tuple[np.ndarray, Affine, str]:
    """Pass-through: absolute slope in degrees (used for cartography)."""
    data, src = _read(slope_path)
    valid = _valid_mask(data, src.nodata) & (data >= 0)
    tf, crs = src.transform, str(src.crs)
    src.close()
    out = np.full(data.shape, NODATA_FLOAT, dtype=np.float32)
    out[valid] = data[valid]
    return out, tf, crs


def compute_linear_proximity_suitability(
    dist_path: Path,
    params: ParamsLike,
    max_dist_key: str,
    default_max_km: float,
) -> Tuple[np.ndarray, Affine, str]:
    """Linear decay proximity score: S(d) = max(0, 1 − d / d_max).

    Used for roads and grid distance layers.
    """
    with safe_raster_open(dist_path) as src:
        dist_data = src.read(1).astype(np.float32)
        transform, crs, nodata = src.transform, str(src.crs), src.nodata

    max_dist_km = float(_param(params, max_dist_key, default_max_km))
    nd    = float(nodata if nodata is not None else NODATA_FLOAT)
    valid = (dist_data != nd) & np.isfinite(dist_data)
    dist_data[~valid] = NODATA_FLOAT

    raw = np.full(dist_data.shape, NODATA_FLOAT, dtype=np.float32)
    raw[valid] = np.clip(
        1.0 - dist_data[valid] / max_dist_km, 0.0, 1.0
    ).astype(np.float32)

    score = normalize_percentile(raw, valid, p_low=5.0, p_high=95.0)
    score[~valid] = NODATA_FLOAT
    return score, transform, crs


def compute_road_suitability(
    roads_path: Path, params: ParamsLike
) -> Tuple[np.ndarray, Affine, str]:
    """Road-network proximity suitability."""
    return compute_linear_proximity_suitability(
        roads_path, params, "road_max_dist_km", 5.0
    )


def compute_grid_suitability(
    grid_path: Path, params: ParamsLike
) -> Tuple[np.ndarray, Affine, str]:
    """Power-grid proximity suitability."""
    return compute_linear_proximity_suitability(
        grid_path, params, "grid_max_dist_km", 20.0
    )


def compute_proximity_plants(
    plants_path: Path,
    transform: Affine,
    plants_df: Optional[pd.DataFrame] = None,
) -> Tuple[np.ndarray, Affine, str]:
    """Exponential decay proximity score from existing power infrastructure.

    S_renewable = 1 − exp(−d / σ)  |  S_thermal = exp(−d / σ)
    Combined    = max(S_renewable, S_thermal)
    """
    with safe_raster_open(plants_path) as src:
        plants_raster = src.read(1).astype(np.uint8)
        crs = str(src.crs)

    height, width  = plants_raster.shape
    continent_mask = plants_raster != 255

    lat_rad = math.radians(transform.f + transform.e * (height / 2))
    lon_km  = (
        111412.84 * math.cos(lat_rad)
        - 93.50   * math.cos(3 * lat_rad)
        + 0.118   * math.cos(5 * lat_rad)
    ) / 1000.0
    res_km = abs(transform.a) * lon_km

    mask_ren = np.ones((height, width), dtype=np.uint8)
    mask_oth = np.ones((height, width), dtype=np.uint8)

    if plants_df is not None and not plants_df.empty:
        df = plants_df.copy()
        df.columns = [c.strip().lower() for c in df.columns]
        fuel_col = next((c for c in ["primary_fuel", "fuel1", "fuel"] if c in df.columns), None)
        lat_col  = next((c for c in ["latitude", "lat"]               if c in df.columns), None)
        lon_col  = next((c for c in ["longitude", "lon"]              if c in df.columns), None)
        if all([fuel_col, lat_col, lon_col]):
            ren_fuels = ["solar", "wind", "biomass", "waste"]
            inv_trans = ~transform
            for _, row in df.dropna(subset=[lat_col, lon_col]).iterrows():
                col_px, row_px = inv_trans * (row[lon_col], row[lat_col])
                col_px, row_px = int(col_px), int(row_px)
                if 0 <= row_px < height and 0 <= col_px < width:
                    fuel = str(row[fuel_col]).lower()
                    if any(f in fuel for f in ren_fuels):
                        mask_ren[row_px, col_px] = 0
                    else:
                        mask_oth[row_px, col_px] = 0
    else:
        mask_oth = (plants_raster != 1).astype(np.uint8)

    dist_ren = distance_transform_edt(mask_ren) * res_km
    dist_oth = distance_transform_edt(mask_oth) * res_km
    s_ren    = 1.0 - np.exp(-dist_ren / PROXIMITY_DECAY_SIGMA_KM)
    s_oth    = np.exp(-dist_oth / PROXIMITY_DECAY_SIGMA_KM)
    raw      = np.maximum(s_ren, s_oth).astype(np.float32)
    raw[plants_raster == 1] = 0.0

    sigma_px = (
        max(0.5, PROXIMITY_SMOOTH_SIGMA_PX / res_km) if res_km > 0
        else PROXIMITY_SMOOTH_SIGMA_PX
    )
    raw = gaussian_filter(raw, sigma=sigma_px)
    raw = np.clip(raw, 0.0, 1.0)
    raw[~continent_mask] = NODATA_FLOAT

    if (plants_raster == 1).sum() == 0:
        raw[continent_mask] = 0.3

    land_valid = continent_mask & (raw != NODATA_FLOAT)
    score = normalize_percentile(raw, land_valid, p_low=5.0, p_high=95.0)
    score[plants_raster == 1] = 0.0
    score[~continent_mask]    = NODATA_FLOAT
    return score, transform, crs


def compute_land_cover_scores(
    lc_path: Path,
    land_suitability: Any,
) -> Dict[str, Tuple[np.ndarray, Affine, str]]:
    """Map ESA land-cover classes to technology suitability scores."""
    with safe_raster_open(lc_path) as src:
        lc_data   = src.read(1).astype(np.int16)
        transform = src.transform
        crs       = str(src.crs)
        nodata    = int(src.nodata) if src.nodata is not None else 0

    if isinstance(land_suitability, dict):
        df = pd.DataFrame.from_dict(land_suitability, orient="index")
    elif isinstance(land_suitability, list):
        df = pd.DataFrame(land_suitability)
    else:
        df = land_suitability.copy()

    if df.index.name != "Class_Code" and "Class_Code" in df.columns:
        df = df.set_index("Class_Code")
    df.index = df.index.astype(int)

    lookup = {
        int(code): float(row.get("biomass", row.get("Biomass", 0.0)) or 0.0)
        for code, row in df.iterrows()
    }
    biomass_score = np.full(lc_data.shape, NODATA_FLOAT, dtype=np.float32)
    valid = (lc_data != nodata) & (lc_data > 0) & (lc_data != 255)
    for cls in np.unique(lc_data[valid]):
        biomass_score[(lc_data == cls) & valid] = lookup.get(int(cls), 0.0)
    return {"biomass": (biomass_score, transform, crs)}


def compute_biomass_resource(
    lc_path: Path,
    yield_by_lc: Dict[int, float],
    params: ParamsLike,
) -> Tuple[np.ndarray, Affine, str]:
    """Biomass resource potential from land-cover-specific yield parameters."""
    with safe_raster_open(lc_path) as src:
        lc_data   = src.read(1).astype(np.int16)
        transform = src.transform
        crs       = str(src.crs)
        nodata    = int(src.nodata) if src.nodata is not None else 0

    yields_int: Dict[int, float] = {int(k): float(v) for k, v in yield_by_lc.items()}
    raw        = np.full(lc_data.shape, NODATA_FLOAT, dtype=np.float32)
    valid_base = (lc_data != nodata) & (lc_data > 0)

    for cls, y in yields_int.items():
        raw[(lc_data == cls) & valid_base] = y
    raw[(raw == NODATA_FLOAT) & valid_base] = 0.0

    valid_vals = raw[valid_base & (raw != NODATA_FLOAT)]
    if valid_vals.size > 0:
        logger.info(
            "  [biomass_resource] mean=%.4f  std=%.4f  n=%d",
            float(np.mean(valid_vals)), float(np.std(valid_vals)), valid_vals.size,
        )
    else:
        logger.warning("  [biomass_resource] No valid biomass yield pixels found.")

    sigma = float(_param(params, "biomass_smooth_sigma", 1.0))
    if sigma > 0:
        tmp = raw.copy()
        tmp[~valid_base] = 0.0
        tmp = gaussian_filter(tmp, sigma=sigma).astype(np.float32)
        tmp[~valid_base] = NODATA_FLOAT
        raw = tmp

    return (
        normalize_percentile(
            raw, valid_base,
            float(_param(params, "normalization_min_percentile", 5)),
            float(_param(params, "normalization_max_percentile", 95)),
        ),
        transform, crs,
    )


def compute_protected_areas(
    wdpa_path: Optional[Path],
    mainland_gdf: gpd.GeoDataFrame,
    transform: Affine,
    width: int,
    height: int,
    crs: str,
    as_exclusion: bool = True,
) -> Tuple[np.ndarray, Affine, str]:
    """Rasterize WDPA protected areas with IUCN category-based scoring.

    Strict categories (Ia, Ib, II) receive score 0.0 when as_exclusion=True.
    Unprotected mainland pixels receive IUCN_FREE_SCORE (1.0).
    """
    mainland_mask  = np.zeros((height, width), dtype=np.uint8)
    mainland_union = mainland_gdf.to_crs(crs).unary_union
    rasterize_feats(
        [(mapping(mainland_union), 1)],
        out_shape=(height, width), transform=transform,
        out=mainland_mask, dtype=np.uint8,
    )
    score = np.full((height, width), NODATA_FLOAT, dtype=np.float32)

    if wdpa_path is None or not Path(wdpa_path).exists():
        score[mainland_mask > 0] = IUCN_FREE_SCORE
        return score, transform, crs

    wdpa_path = Path(wdpa_path)
    if wdpa_path.is_dir():
        candidates = (
            list(wdpa_path.rglob("*polygon*.shp"))
            or list(wdpa_path.rglob("*_0.shp"))
            or [p for p in wdpa_path.rglob("*.shp") if "point" not in p.name.lower()]
        )
        if not candidates:
            score[mainland_mask > 0] = IUCN_FREE_SCORE
            return score, transform, crs
        wdpa_path = candidates[0]

    try:
        gdf = gpd.read_file(str(wdpa_path)).to_crs(crs)
        gdf = gdf[gdf.intersects(mainland_union)].copy()
        if gdf.empty:
            score[mainland_mask > 0] = IUCN_FREE_SCORE
            return score, transform, crs

        gdf["geometry"] = gdf.geometry.intersection(mainland_union)
        gdf = gdf[~gdf.geometry.is_empty].copy()

        iucn_col = next(
            (c for c in ["IUCN_CAT", "iucn_cat", "IUCN", "DESIGNATION"] if c in gdf.columns),
            None,
        )
        if iucn_col:
            gdf["_score"] = (
                gdf[iucn_col].str.lower().str.strip()
                .map(lambda c: IUCN_SCORES.get(c, IUCN_SCORE_DEFAULT))
            )
            if as_exclusion:
                strict = gdf[iucn_col].str.lower().str.strip().isin(["ia", "ib", "ii"])
                gdf.loc[strict, "_score"] = 0.0
        else:
            gdf["_score"] = IUCN_SCORE_DEFAULT

        gdf    = gdf.sort_values("_score", ascending=False).reset_index(drop=True)
        shapes = (
            (mapping(geom), float(sv))
            for geom, sv in zip(gdf.geometry, gdf["_score"])
        )
        temp = np.full((height, width), IUCN_FREE_SCORE, dtype=np.float32)
        rasterize_feats(
            shapes, out_shape=(height, width),
            transform=transform, out=temp, dtype=np.float32,
        )
        score[mainland_mask > 0] = temp[mainland_mask > 0]

    except Exception as exc:
        logger.warning("  Protected areas rasterization failed: %s", exc)
        score[mainland_mask > 0] = IUCN_FREE_SCORE

    return score, transform, crs


def compute_population_suitability(
    pop_path: Path, params: ParamsLike
) -> Tuple[np.ndarray, Affine, str]:
    """Logarithmic penalty for high population density."""
    with safe_raster_open(pop_path) as src:
        pop, tf, crs, nodata = src.read(1), src.transform, src.crs, src.nodata

    threshold = float(_param(params, "pop_density_threshold", 300.0))
    score = np.clip(
        1.0 - np.log1p(np.clip(pop, 0, threshold)) / np.log1p(threshold),
        0.0, 1.0,
    ).astype(np.float32)
    if nodata is not None:
        score[pop == float(nodata)] = NODATA_FLOAT
    score[pop < 0] = NODATA_FLOAT
    return score, tf, str(crs)


def compute_lakes_exclusion(lakes_path: Path) -> Tuple[np.ndarray, Affine, str]:
    """Binary exclusion mask: lake pixels → 0, land pixels → 1."""
    with safe_raster_open(lakes_path) as src:
        lake_mask = src.read(1).astype(np.uint8)
        transform = src.transform
        crs       = str(src.crs)

    country = lake_mask != 255
    score   = np.full(lake_mask.shape, NODATA_FLOAT, dtype=np.float32)
    score[country & (lake_mask == 0)] = 1.0
    score[country & (lake_mask == 1)] = 0.0
    return score, transform, crs


def compute_river_suitability(
    rivers_path: Path, params: ParamsLike, tech: str = "solar"
) -> Tuple[np.ndarray, Affine, str]:
    """Technology-specific river proximity suitability.

    Biomass: linear access score (closer = better up to max_dist).
    Solar/Wind: riparian setback (too close = 0, beyond buffer = 1).
    """
    with safe_raster_open(rivers_path) as src:
        dist_km   = src.read(1).astype(np.float32)
        transform = src.transform
        crs       = str(src.crs)
        nodata    = src.nodata

    nd    = float(nodata if nodata is not None else NODATA_FLOAT)
    valid = (dist_km != nd) & np.isfinite(dist_km)
    score = np.full(dist_km.shape, NODATA_FLOAT, dtype=np.float32)

    if tech == "biomass":
        max_dist = float(_param(params, "river_max_dist_biomass_km", 10.0))
        score[valid] = np.clip(
            1.0 - dist_km[valid] / max_dist, 0.0, 1.0
        ).astype(np.float32)
    else:
        buffer_km = float(_param(params, "river_safety_buffer_km", 0.5))
        score[valid & (dist_km < buffer_km)]  = 0.0
        score[valid & (dist_km >= buffer_km)] = 1.0
    return score, transform, crs


def compute_seismic_suitability(seismic_path: Path) -> Tuple[np.ndarray, Affine, str]:
    """Seismic risk inversion: low hazard → high suitability."""
    with safe_raster_open(seismic_path) as src:
        data      = src.read(1).astype(np.float32)
        transform = src.transform
        crs       = str(src.crs)
        nodata    = src.nodata

    valid      = _valid_mask(data, nodata) & (data >= 0)
    normalized = normalize_percentile(data, valid, p_low=2.0, p_high=98.0)
    score      = np.full(data.shape, NODATA_FLOAT, dtype=np.float32)
    valid_norm = valid & (normalized != NODATA_FLOAT)
    score[valid_norm] = np.clip(1.0 - normalized[valid_norm], 0.0, 1.0)
    return score, transform, crs


# ---------------------------------------------------------------------------
# CriteriaBuilder — orchestration class
# ---------------------------------------------------------------------------


class CriteriaBuilder:
    """Phase 2b orchestrator: normalises physical layers into criteria scores.

    Accepts both the new CountryParams contract and the legacy dict so that
    the rest of the pipeline does not need to be migrated simultaneously.
    """

    def __init__(self, cfg: Any, outputs_dir: Path) -> None:
        self.cfg         = cfg
        self.outputs_dir = Path(outputs_dir)
        self.viz_cfg     = cfg.system.get("visualization", {})
        self.c_meta      = self.viz_cfg.get("criteria_meta", {})
        self.p_colors    = self.viz_cfg.get("plant_colors", {})
        self._admin_gdf: Optional[gpd.GeoDataFrame] = None
        self.styler = GeoWorldStyler(
            self.viz_cfg,
            global_dpi=cfg.system.get("pipeline", {}).get("map_dpi_export", 150),
        )

    # ------------------------------------------------------------------
    # Map rendering  (phase-specific — stays in this module)
    # ------------------------------------------------------------------
    #
    # KNOWN DEBT (DUP_22_pending): GeoWorldStyler.render_raster_map() already
    # unifies the create_figure → draw_basemap → imshow → decorations →
    # colorbar → admin_labels → title → footer → save sequence, and is in
    # production use in suitability_builder.py and lcoe_calculator.py.
    # _plot_criterion_map() below was NOT migrated to it, because it has
    # behaviour render_raster_map() does not support yet:
    #   - a dedicated branch for "protected_areas" (ListedColormap overlay +
    #     custom legend, no colorbar at all)
    #   - per-criterion conditional interpolation method and vmax (e.g.
    #     `slope_degrees` has no fixed vmax since it is not normalised to [0,1])
    #   - a mean/std/IQR stats strip (add_stats_strip), not the plain
    #     stats_text string render_raster_map() expects
    # Migrating this safely requires extending render_raster_map()'s API
    # (or accepting a pre-built overlay/legend callback) and a visual
    # side-by-side diff of the affected PNGs before merging — not a
    # find-and-replace. Left as-is intentionally; do not migrate piecemeal.

    def _plot_criterion_map(
        self,
        score: np.ndarray,
        transform: Affine,
        crs: str,
        criterion: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        out_path: Path,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        vmin: Optional[float] = None,
        vmax: Optional[float] = None,
    ) -> None:
        meta = self.c_meta.get(
            criterion,
            [criterion.replace("_", " ").title(), "Score (0–1)", "RdYlGn_r", False],
        )
        title, unit, cmap_name, invert = meta[0], meta[1], meta[2], meta[3]

        plot_data = score.copy().astype(np.float64)
        plot_data[(score == NODATA_FLOAT) | (score < 0)] = np.nan

        h_px, w_px = score.shape
        extent = [
            transform.c,
            transform.c + transform.a * w_px,
            transform.f + transform.e * h_px,
            transform.f,
        ]
        v_minx, v_miny, v_maxx, v_maxy = mainland_gdf.total_bounds

        fig, ax = self.styler.create_figure(v_minx, v_maxx, v_miny, v_maxy)
        self.styler.draw_basemap(ax, crs, mainland_gdf, context_gdf, self._admin_gdf)

        if criterion == "protected_areas":
            mainland_gdf.to_crs(crs).plot(
                ax=ax, color="#E8E8E8", edgecolor="none", zorder=2
            )
            restricted = plot_data.copy()
            restricted[restricted >= 1.0] = np.nan
            restricted[np.isfinite(restricted)] = 0.5
            cmap_r = mcolors.ListedColormap([self.styler.CONTOUR_P90_COLOR])
            cmap_r.set_bad(color="none")
            ax.imshow(
                restricted, extent=extent, origin="upper", cmap=cmap_r,
                vmin=0.0, vmax=1.0, zorder=3, interpolation="nearest", alpha=0.85,
            )
            mainland_gdf.to_crs(crs).boundary.plot(
                ax=ax, color=self.styler.country_border, linewidth=0.8, zorder=4
            )
            self.styler.add_standard_legend(
                ax,
                [
                    mpatches.Patch(
                        color=self.styler.CONTOUR_P90_COLOR, alpha=0.85,
                        label="Protected / Restricted (WDPA)",
                    ),
                    mpatches.Patch(color="#E8E8E8", label="Free territory"),
                ],
                "lower_right",
            )
        else:
            interp   = (
                "bilinear"
                if criterion in ("proximity_plants", "protected_areas", "solar_resource")
                else "nearest"
            )
            vmax_val = vmax if vmax is not None else (
                None if criterion == "slope_degrees" else 1.0
            )
            im = ax.imshow(
                plot_data, extent=extent, origin="upper",
                cmap=self.styler.make_cmap(cmap_name, reverse=invert, under="none", bad="none"),
                vmin=vmin or 0.0, vmax=vmax_val, zorder=2, interpolation=interp,
            )
            self.styler.add_colorbar(fig, im, unit, extend="neither")

        self.styler.add_decorations(ax, v_minx, v_maxx, v_miny, v_maxy)
        self.styler.add_standard_footer(fig, crs_metadata=f"CRS: {crs or 'EPSG:4326'}")

        if self._admin_gdf is not None:
            self.styler.draw_admin_labels(
                ax, self._admin_gdf, v_minx, v_maxx, v_miny, v_maxy,
                max_labels=self.viz_cfg.get("layout", {}).get("admin_max_labels", 12),
                zorder=8,
            )
        self.styler.add_standard_title(fig, title_main=title, title_sub=country_name)

        valid_data = plot_data[np.isfinite(plot_data)]
        if len(valid_data) > 0:
            self.styler.add_stats_strip(
                fig,
                {
                    "mean": valid_data.mean(), "std": valid_data.std(),
                    "p25": np.percentile(valid_data, 25),
                    "p75": np.percentile(valid_data, 75),
                },
                format_template=(
                    f"Valid pixels: {len(valid_data):,}  |  "
                    "Mean: {mean:.3f} ± {std:.3f}  |  IQR: {p25:.3f}–{p75:.3f}"
                ),
            )
        self.styler.save(fig, out_path)

    def _plot_power_plants_map(
        self,
        plants_df: pd.DataFrame,
        mainland_gdf: gpd.GeoDataFrame,
        transform: Affine,
        crs: str,
        country_name: str,
        out_path: Path,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
    ) -> None:
        minx, miny, maxx, maxy = mainland_gdf.total_bounds
        fig, ax = self.styler.create_figure(
            minx, maxx, miny, maxy, right_in_override=self.styler.LEFT_IN
        )
        self.styler.draw_basemap(ax, crs, mainland_gdf, context_gdf, self._admin_gdf)
        mainland_gdf.to_crs(crs).plot(
            ax=ax, color="#FFFFFF",
            edgecolor=self.styler.country_border, linewidth=0.8, zorder=2,
        )

        if plants_df is not None and not plants_df.empty:
            df = plants_df.copy()
            df.columns = [c.strip().lower() for c in df.columns]
            lat_col  = next((c for c in ["latitude",  "lat"]          if c in df.columns), None)
            lon_col  = next((c for c in ["longitude", "lon", "long"]  if c in df.columns), None)
            fuel_col = next((c for c in ["primary_fuel", "fuel1", "fuel"] if c in df.columns), None)
            cap_col  = next((c for c in ["capacity_mw", "capacity", "mw"] if c in df.columns), None)

            if lat_col and lon_col:
                df[lat_col] = pd.to_numeric(df[lat_col], errors="coerce")
                df[lon_col] = pd.to_numeric(df[lon_col], errors="coerce")
                df = df.dropna(subset=[lat_col, lon_col])
                fuel_handles: Dict[str, Any] = {}

                for _, row in df.iterrows():
                    fuel = next(
                        (k for k in self.p_colors if k.lower() in str(row.get(fuel_col, "")).lower()),
                        "Other",
                    )
                    msize = (
                        float(np.clip(
                            4.0 + np.log1p(
                                pd.to_numeric(row.get(cap_col, 0), errors="coerce")
                            ) * 0.9,
                            4.0, 14.0,
                        ))
                        if cap_col else 5.0
                    )
                    ax.plot(
                        float(row[lon_col]), float(row[lat_col]), "o",
                        color=self.p_colors.get(fuel, "#78909C"),
                        markersize=msize, zorder=5,
                        markeredgewidth=0.6, markeredgecolor="white", alpha=0.90,
                    )
                    if fuel not in fuel_handles:
                        fuel_handles[fuel] = mpatches.Patch(
                            color=self.p_colors.get(fuel, "#78909C"), label=fuel
                        )
                if fuel_handles:
                    self.styler.add_standard_legend(
                        ax, list(fuel_handles.values()), "upper_right"
                    )

        self.styler.add_standard_title(
            fig, title_main="Existing Power Plants", title_sub=country_name
        )
        self.styler.add_decorations(ax, minx, maxx, miny, maxy, fixed_scalebar_km=60)
        self.styler.add_standard_footer(fig, crs_metadata=f"CRS: {crs or 'EPSG:4326'}")
        if self._admin_gdf is not None:
            self.styler.draw_admin_labels(
                ax, self._admin_gdf, minx, maxx, miny, maxy,
                max_labels=self.viz_cfg.get("layout", {}).get("admin_max_labels", 12),
                zorder=8,
            )
        self.styler.save(fig, out_path)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        aligned: Union[AlignedLayers, Dict[str, Optional[Path]]],
        country_params: ParamsLike,
        land_suit: Any,
        wdpa_path: Optional[Path] = None,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        plants_df: Optional[pd.DataFrame] = None,
        rivers_path: Optional[Path] = None,
        lakes_path: Optional[Path] = None,
        seismic_path: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Execute Phase 2b: build normalised criteria from aligned rasters.

        Returns:
            Dict with keys: country_code, criteria, tif_dir, fig_dir,
            report_dir, n_criteria, timestamp.
        """
        # ── 1. Normalise aligned to AlignedLayers ───────────────────────
        if not isinstance(aligned, AlignedLayers):
            aligned = AlignedLayers.from_dict(
                aligned if isinstance(aligned, dict) else dict(aligned)
            )

        # ── 2. Validate required layers ─────────────────────────────────
        required = ["elevation", "slope", "solar", "land_cover"]
        ref_path = next(
            (p for p in aligned.to_dict().values() if p and Path(p).exists()), None
        )
        if ref_path is None:
            raise RuntimeError("No aligned raster found to determine reference grid.")

        with safe_raster_open(ref_path) as src:
            expected_shape = (src.height, src.width)
            expected_crs   = str(src.crs)

        validated = validate_aligned_layers(
            aligned,
            required_layers=required,
            expected_shape=expected_shape,
            expected_crs=expected_crs,
            check_values=False,
        )
        for layer in required:
            path = validated.get(layer)
            if path:
                try:
                    st = validate_raster_values(path, finite_required=0.001)
                    logger.info(
                        "  Input %-12s: min=%.2f  max=%.2f  mean=%.2f  finite=%.1f%%",
                        layer, st["min"], st["max"], st["mean"],
                        st["finite_fraction"] * 100,
                    )
                except Exception as exc:
                    logger.warning("  Stats unavailable for %s: %s", layer, exc)

        if not validated.get("plants"):
            logger.warning("  No plants raster found; 'proximity_plants' will be skipped.")

        # ── 3. Output directories ────────────────────────────────────────
        base       = self.outputs_dir / country_code / "criteria_builder"
        tif_dir    = base / "tif"
        fig_dir    = base / "figures"
        report_dir = base / "reports"
        for d in (tif_dir, fig_dir, report_dir):
            d.mkdir(parents=True, exist_ok=True)

        logger.info("Phase 2b — Criteria Builder: %s", country_code)
        t0 = time.perf_counter()

        # ── 4. Reference metadata ────────────────────────────────────────
        with safe_raster_open(ref_path) as src:
            transform = src.transform
            crs       = str(src.crs)
            height    = src.height
            width     = src.width

        self._admin_gdf = self.styler.load_admin_boundaries(
            country_name, mainland_gdf, Path(self.cfg.raw_path)
        )

        # ── 5. Resolve biomass yields ────────────────────────────────────
        yield_raw: Dict = (
            _param(country_params, "_biomass_yields")
            or _param(country_params, "biomass_yield_by_land_cover")
            or {}
        )

        # ── 6. Compute criteria ──────────────────────────────────────────
        criteria: Dict[str, np.ndarray] = {}
        _render_queue: List[Callable]   = []

        def _enqueue(score, tf, c, name, **plot_kw):
            criteria[name] = score
            _save_criterion(score, tif_dir / f"{name}.tif", tf, c)
            _render_queue.append(
                partial(
                    self._plot_criterion_map,
                    score, tf, c, name, country_name, mainland_gdf,
                    fig_dir / f"{name}.png", context_gdf, **plot_kw,
                )
            )

        if validated.get("solar"):
            with _timer("solar_resource"):
                _enqueue(*compute_solar_resource(validated.solar, country_params), "solar_resource")

        if validated.get("wind"):
            with _timer("wind_resource"):
                _enqueue(*compute_wind_resource(validated.wind, country_params), "wind_resource")

        if validated.get("slope"):
            with _timer("terrain_score"):
                _enqueue(
                    *compute_terrain_score(validated.slope, validated.elevation, country_params),
                    "terrain_score",
                )
            with _timer("slope_degrees"):
                s_deg, tf_s, c_s = compute_slope_degrees(validated.slope)
                _save_criterion(s_deg, tif_dir / "slope_degrees.tif", tf_s, c_s)
                valid_s  = s_deg[np.isfinite(s_deg) & (s_deg != NODATA_FLOAT) & (s_deg >= 0)]
                vmax_val = float(np.percentile(valid_s, 98)) if len(valid_s) else 45.0
                _render_queue.append(
                    partial(
                        self._plot_criterion_map,
                        s_deg, tf_s, c_s, "slope_degrees",
                        country_name, mainland_gdf,
                        fig_dir / "slope_degrees.png", context_gdf,
                        vmin=0.0, vmax=vmax_val,
                    )
                )

        if validated.get("land_cover"):
            with _timer("land_cover"):
                lc_scores = compute_land_cover_scores(validated.land_cover, land_suit)
            if "biomass" in lc_scores:
                _enqueue(*lc_scores["biomass"], "lc_biomass")
            if yield_raw:
                with _timer("biomass_resource"):
                    _enqueue(
                        *compute_biomass_resource(validated.land_cover, yield_raw, country_params),
                        "biomass_resource",
                    )

        if validated.get("plants"):
            with _timer("proximity_plants"):
                _enqueue(
                    *compute_proximity_plants(validated.plants, transform, plants_df),
                    "proximity_plants",
                )

        with _timer("protected_areas"):
            as_excl = bool(_param(country_params, "protected_as_exclusion", True))
            _enqueue(
                *compute_protected_areas(
                    wdpa_path, mainland_gdf, transform, width, height, crs, as_excl,
                ),
                "protected_areas",
            )

        if validated.get("population"):
            with _timer("pop_suitability"):
                _enqueue(
                    *compute_population_suitability(validated.population, country_params),
                    "pop_suitability",
                )

        if validated.get("roads"):
            with _timer("road_suitability"):
                _enqueue(*compute_road_suitability(validated.roads, country_params), "road_suitability")

        if validated.get("lakes"):
            with _timer("lakes_exclusion"):
                _enqueue(*compute_lakes_exclusion(validated.lakes), "lakes_exclusion")

        if validated.get("rivers"):
            for tech, name in [
                ("solar",   "river_solar"),
                ("wind",    "river_wind"),
                ("biomass", "river_biomass"),
            ]:
                with _timer(name):
                    _enqueue(
                        *compute_river_suitability(validated.rivers, country_params, tech=tech),
                        name,
                    )

        if validated.get("seismic"):
            with _timer("seismic_suitability"):
                _enqueue(*compute_seismic_suitability(validated.seismic), "seismic_suitability")

        if validated.get("grid"):
            with _timer("grid_suitability"):
                _enqueue(*compute_grid_suitability(validated.grid, country_params), "grid_suitability")

        if plants_df is not None and validated.get("plants"):
            _render_queue.append(
                partial(
                    self._plot_power_plants_map,
                    plants_df, mainland_gdf, transform, crs, country_name,
                    fig_dir / "power_plants.png", context_gdf,
                )
            )

        logger.info(
            "  Computation: %.1fs | %d maps queued",
            time.perf_counter() - t0, len(_render_queue),
        )

        # ── 7. Render maps in parallel ───────────────────────────────────
        if not self.cfg.system.get("pipeline", {}).get("skip_criteria_maps", False):
            n_workers = self.viz_cfg.get("render_workers", 4)
            logger.info("  Rendering %d maps (%d worker(s))...", len(_render_queue), n_workers)
            t_render = time.perf_counter()
            with ThreadPoolExecutor(max_workers=n_workers) as executor:
                list(executor.map(lambda fn: fn(), _render_queue))
            logger.info(
                "  Render: %.1fs | Phase total: %.1fs",
                time.perf_counter() - t_render,
                time.perf_counter() - t0,
            )

        # ── 8. Summary report ────────────────────────────────────────────
        write_criteria_summary(criteria, country_code, country_name, report_dir)

        logger.info("=" * 62)
        logger.info("  CRITERIA BUILDER — %s | Output: %s", country_code, base)
        logger.info("=" * 62)
        for name, score in criteria.items():
            valid = score[np.isfinite(score) & (score != NODATA_FLOAT) & (score >= 0)]
            if len(valid):
                logger.info(
                    "  [OK] %-24s: mean=%.3f | >=0.6: %5.1f%% | n=%s",
                    name, valid.mean(), 100 * (valid >= 0.6).mean(), f"{len(valid):,}",
                )
        logger.info("  Total: %.1fs", time.perf_counter() - t0)
        logger.info("=" * 62)

        # ── 9. Verify completeness ───────────────────────────────────────
        expected = [
            "solar_resource", "wind_resource", "terrain_score", "lc_biomass",
            "biomass_resource", "protected_areas", "pop_suitability",
            "road_suitability", "lakes_exclusion", "river_solar", "river_wind",
            "river_biomass", "seismic_suitability", "grid_suitability",
        ]
        missing = [c for c in expected if c not in criteria]
        if missing:
            logger.warning("  Missing criteria: %s", ", ".join(missing))
        else:
            logger.info("  All expected criteria generated successfully.")

        return {
            "country_code": country_code,
            "criteria":     criteria,
            "tif_dir":      tif_dir,
            "fig_dir":      fig_dir,
            "report_dir":   report_dir,
            "n_criteria":   len(criteria),
            "timestamp":    datetime.now().isoformat(),
        }