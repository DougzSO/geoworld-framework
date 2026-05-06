"""
src/processors/criteria_builder.py
==================================
Phase 2b: Geomorphological and Spatial Criteria Normalisation.

Converts physical units (e.g., Euclidean distance in km, absolute slope in 
degrees, solar irradiance in kWh/m²) into dimensionless continuous Fuzzy 
Suitability Scores [0, 1] for subsequent AHP-TOPSIS aggregation.

Architecture & Optimisation
---------------------------
- **Phase A (Algebraic)**: Fully vectorized NumPy operations (~0.1s per matrix).
- **Phase B (Cartographic)**: Asynchronous Matplotlib Agg rendering via 
  `ThreadPoolExecutor`. Decoupling algebraic constraints from graphical I/O 
  reduces 16-criteria pipeline runtime by ~80%.

Thread-Safety Note
------------------
The Agg backend is re-entrant, but the stateful `pyplot` interface is not.
All cartographic pipelines strictly utilise object-oriented instantiation
(`Figure`, `Axes`) to ensure memory isolation across parallel workers.
"""

from __future__ import annotations

import logging
import math
import os
import platform
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

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
from src.utils.map_styling import GeoWorldStyler
from src.utils.utils import safe_raster_open, safe_raster_write

os.environ["GDAL_QUIET"] = "ON"
os.environ["CPL_LOG"] = "NUL" if platform.system() == "Windows" else "/dev/null"

logger = logging.getLogger("geoworld.processors.CriteriaBuilder")


# ─────────────────────────────────────────────────────────────────────────────
# Timing & I/O Helpers
# ─────────────────────────────────────────────────────────────────────────────


@contextmanager
def _timer(label: str):
    """
    Context manager for fine-grained execution timing.

    Args:
        label: Identifier for the timed operation.

    Yields:
        None
    """
    t0 = time.perf_counter()
    logger.info("  [%s] mathematical projection initiated...", label)
    try:
        yield
    finally:
        logger.info(
            "  [%s] resolved in %.1fs",
            label,
            time.perf_counter() - t0
        )


def _read(path: Path) -> Tuple[np.ndarray, rasterio.DatasetReader]:
    """
    Read raster data and return array with source handle.

    Args:
        path: Path to raster file.

    Returns:
        Tuple of (data array, rasterio source object).
    """
    src = rasterio.open(str(path))
    data = src.read(1).astype(np.float32)
    return data, src


def _valid_mask(data: np.ndarray, nodata: Any) -> np.ndarray:
    """
    Generate boolean mask for valid (finite, non-nodata) pixels.

    Args:
        data: Input array.
        nodata: NoData value to exclude.

    Returns:
        Boolean mask array.
    """
    mask = np.isfinite(data)
    if nodata is not None:
        mask &= (data != float(nodata))
    return mask


def _save_criterion(
    score: np.ndarray,
    out_path: Path,
    transform: Affine,
    crs: str,
    dtype: str = "float32",
    nodata_val: Any = NODATA_FLOAT,
) -> None:
    """
    Write normalized criterion raster to disk with LZW compression.

    Args:
        score: Suitability score array [0, 1].
        out_path: Output file path.
        transform: Affine geotransform.
        crs: Coordinate reference system string.
        dtype: Output data type (default: float32).
        nodata_val: NoData value to encode.

    Returns:
        None
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(
        driver="GTiff",
        dtype=dtype,
        width=score.shape[1],
        height=score.shape[0],
        count=1,
        crs=crs,
        transform=transform,
        nodata=nodata_val,
        compress="lzw",
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )
    with safe_raster_write(out_path, **profile) as dst:
        dst.write(score.astype(dtype), 1)


# ─────────────────────────────────────────────────────────────────────────────
# Mathematical Normalisation & Fuzzy Logic
# ─────────────────────────────────────────────────────────────────────────────


def normalize_percentile(
    data: np.ndarray,
    valid: np.ndarray,
    p_low: float = 5.0,
    p_high: float = 95.0,
) -> np.ndarray:
    """
    Min-max normalisation using percentile clipping to suppress structural outliers.

    Mathematical Formulation
    ------------------------
    .. math::
        S(x) = \\begin{cases} 
        0 & x \\le P_{low} \\\\
        \\frac{x - P_{low}}{P_{high} - P_{low}} & P_{low} < x < P_{high} \\\\
        1 & x \\ge P_{high} 
        \\end{cases}

    Args:
        data: Raw input array.
        valid: Boolean mask indicating valid pixels.
        p_low: Lower percentile threshold (default: 5.0).
        p_high: Upper percentile threshold (default: 95.0).

    Returns:
        Normalized score array [0, 1] with NODATA_FLOAT for invalid pixels.
    """
    score = np.full(data.shape, NODATA_FLOAT, dtype=np.float32)
    if not valid.any():
        return score

    vals = data[valid].astype(np.float64)
    v_low = np.percentile(vals, p_low)
    v_high = np.percentile(vals, p_high)

    if v_high <= v_low:
        score[valid] = 0.5
        return score

    norm = (vals - v_low) / (v_high - v_low)
    score[valid] = np.clip(norm, 0.0, 1.0).astype(np.float32)
    return score


def compute_solar_resource(
    solar_path: Path,
    params: Dict
) -> Tuple[np.ndarray, Affine, str]:
    """
    Compute solar resource suitability from irradiance data.

    Args:
        solar_path: Path to solar irradiance raster (kWh/m²).
        params: Configuration parameters dictionary.

    Returns:
        Tuple of (suitability score, affine transform, CRS string).
    """
    data, src = _read(solar_path)
    valid = _valid_mask(data, src.nodata) & (data > 0)
    tf, crs = src.transform, str(src.crs)
    src.close()

    score = normalize_percentile(
        data,
        valid,
        float(params.get("normalization_min_percentile", 5)),
        float(params.get("normalization_max_percentile", 95))
    )

    pvout_weight = float(params.get("solar_pvout_weight", 1.0))
    if pvout_weight != 1.0:
        logger.info(
            "  [solar_resource] pvout_weight=%.2f applied natively.",
            pvout_weight
        )
        score = np.where(
            np.isfinite(score),
            score * pvout_weight,
            score
        ).astype(np.float32)

    return score, tf, crs


def compute_wind_resource(
    wind_path: Path,
    params: Dict
) -> Tuple[np.ndarray, Affine, str]:
    """
    Compute wind resource suitability from wind speed data.

    Args:
        wind_path: Path to wind speed raster.
        params: Configuration parameters dictionary.

    Returns:
        Tuple of (suitability score, affine transform, CRS string).
    """
    data, src = _read(wind_path)
    valid = _valid_mask(data, src.nodata) & (data > 0)
    tf, crs = src.transform, str(src.crs)
    src.close()

    return normalize_percentile(
        data,
        valid,
        float(params.get("normalization_min_percentile", 5)),
        float(params.get("normalization_max_percentile", 95))
    ), tf, crs


def compute_terrain_score(
    slope_path: Path,
    elev_path: Optional[Path],
    params: Dict
) -> Tuple[np.ndarray, Affine, str]:
    """
    Compound terrain suitability incorporating Slope and Topographic Roughness Index (TRI).

    Mathematical Formulation
    ------------------------
    TRI evaluates micro-scale ruggedness:
    .. math:: TRI = \\sqrt{\\sum_{i=1}^{8} (E_c - E_i)^2}

    Final Score (Fuzzy Logic Composition):
    .. math:: S_{terrain} = 0.6 \\times S_{slope} + 0.4 \\times S_{TRI}

    Args:
        slope_path: Path to slope raster (degrees).
        elev_path: Optional path to elevation raster for TRI calculation.
        params: Configuration parameters dictionary.

    Returns:
        Tuple of (combined terrain suitability, affine transform, CRS string).
    """
    threshold = float(params.get("slope_threshold_deg", 7.0))
    slope, src_s = _read(slope_path)
    valid_s = _valid_mask(slope, src_s.nodata) & (slope >= 0)
    tf, crs = src_s.transform, str(src_s.crs)
    src_s.close()

    score_s = np.full(slope.shape, NODATA_FLOAT, dtype=np.float32)
    score_s[valid_s] = np.clip(
        1.0 - slope[valid_s] / threshold,
        0.0,
        1.0
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
                        pad[
                            1 + di: 1 + di + elev.shape[0],
                            1 + dj: 1 + dj + elev.shape[1]
                        ] - elev
                    ) ** 2
            tri = np.sqrt(tri)

            st = np.full(elev.shape, NODATA_FLOAT, dtype=np.float32)
            st[valid_e] = np.clip(
                1.0 - tri[valid_e] / TRI_THRESHOLD,
                0.0,
                1.0
            ).astype(np.float32)
            score_tri = st
        except Exception as e:
            logger.warning("  TRI morphological failure: %s", e)

    if score_tri is not None:
        combined = np.full(slope.shape, NODATA_FLOAT, dtype=np.float32)
        both = (score_s != NODATA_FLOAT) & (score_tri != NODATA_FLOAT)
        only_s = (score_s != NODATA_FLOAT) & (score_tri == NODATA_FLOAT)
        combined[both] = (
            0.6 * score_s[both] + 0.4 * score_tri[both]
        ).astype(np.float32)
        combined[only_s] = score_s[only_s]
    else:
        combined = score_s

    return combined, tf, crs


def compute_slope_degrees(slope_path: Path) -> Tuple[np.ndarray, Affine, str]:
    """
    Pass-through for absolute slope degrees (utilised for cartography mappings).

    Args:
        slope_path: Path to slope raster (degrees).

    Returns:
        Tuple of (slope array, affine transform, CRS string).
    """
    data, src = _read(slope_path)
    valid = _valid_mask(data, src.nodata) & (data >= 0)
    tf, crs = src.transform, str(src.crs)
    src.close()

    out = np.full(data.shape, NODATA_FLOAT, dtype=np.float32)
    out[valid] = data[valid]
    return out, tf, crs


def compute_linear_proximity_suitability(
    dist_path: Path,
    params: Dict,
    max_dist_key: str,
    default_max_km: float,
) -> Tuple[np.ndarray, Affine, str]:
    """
    Unified function for linear feature proximity (roads, grid).

    Mathematical Formulation (Linear Decay)
    ---------------------------------------
    .. math::
        S_{linear}(d) = \\max\\left(0, 1 - \\frac{d}{d_{max}}\\right)

    Args:
        dist_path: Path to distance raster (km).
        params: Configuration parameters dictionary.
        max_dist_key: Parameter key for maximum distance threshold.
        default_max_km: Default maximum distance if parameter not found.

    Returns:
        Tuple of (proximity suitability, affine transform, CRS string).
    """
    with safe_raster_open(dist_path) as src:
        dist_data = src.read(1).astype(np.float32)
        transform, crs, nodata = src.transform, str(src.crs), src.nodata

    max_dist_km = float(params.get(max_dist_key, default_max_km))
    continent_mask = (dist_data != float(nodata)) & np.isfinite(dist_data)
    dist_data[~continent_mask] = NODATA_FLOAT

    raw = np.full(dist_data.shape, NODATA_FLOAT, dtype=np.float32)
    raw[continent_mask] = np.clip(
        1.0 - dist_data[continent_mask] / max_dist_km,
        0.0,
        1.0
    ).astype(np.float32)

    score = normalize_percentile(raw, continent_mask, p_low=5.0, p_high=95.0)
    score[~continent_mask] = NODATA_FLOAT
    return score, transform, crs


def compute_road_suitability(
    roads_path: Path,
    params: Dict
) -> Tuple[np.ndarray, Affine, str]:
    """
    Compute proximity suitability to road network.

    Args:
        roads_path: Path to road distance raster.
        params: Configuration parameters dictionary.

    Returns:
        Tuple of (road proximity score, affine transform, CRS string).
    """
    return compute_linear_proximity_suitability(
        roads_path,
        params,
        "road_max_dist_km",
        5.0
    )


def compute_grid_suitability(
    grid_path: Path,
    params: Dict
) -> Tuple[np.ndarray, Affine, str]:
    """
    Compute proximity suitability to electrical grid infrastructure.

    Args:
        grid_path: Path to grid distance raster.
        params: Configuration parameters dictionary.

    Returns:
        Tuple of (grid proximity score, affine transform, CRS string).
    """
    return compute_linear_proximity_suitability(
        grid_path,
        params,
        "grid_max_dist_km",
        20.0
    )


def compute_proximity_plants(
    plants_path: Path,
    transform: Affine,
    plants_df: Optional[pd.DataFrame] = None
) -> Tuple[np.ndarray, Affine, str]:
    """
    Calculate Euclidean distance decay from existing power infrastructure.

    Mathematical Formulation (Exponential Decay)
    --------------------------------------------
    .. math::
        S(d) = \\exp\\left( \\frac{-d}{\\sigma_{km}} \\right)

    Args:
        plants_path: Path to rasterized power plants binary mask.
        transform: Affine geotransform.
        plants_df: Optional DataFrame with plant attributes (fuel type, location).

    Returns:
        Tuple of (proximity score, affine transform, CRS string).
    """
    with safe_raster_open(plants_path) as src:
        plants_raster = src.read(1).astype(np.uint8)
        crs = str(src.crs)

    height, width = plants_raster.shape
    continent_mask = (plants_raster != 255)

    lat_center = transform.f + transform.e * (height / 2)
    lat_rad = math.radians(lat_center)
    lon_km_per_deg = (
        111412.84 * math.cos(lat_rad)
        - 93.50 * math.cos(3 * lat_rad)
        + 0.118 * math.cos(5 * lat_rad)
    ) / 1000.0
    res_km = abs(transform.a) * lon_km_per_deg

    mask_ren = np.ones((height, width), dtype=np.uint8)
    mask_oth = np.ones((height, width), dtype=np.uint8)

    if plants_df is not None and not plants_df.empty:
        df = plants_df.copy()
        df.columns = [c.strip().lower() for c in df.columns]
        fuel_col = next(
            (c for c in ["primary_fuel", "fuel1", "fuel"] if c in df.columns),
            None
        )
        lat_col = next(
            (c for c in ["latitude", "lat"] if c in df.columns),
            None
        )
        lon_col = next(
            (c for c in ["longitude", "lon"] if c in df.columns),
            None
        )

        if all([fuel_col, lat_col, lon_col]):
            ren_fuels = ["solar", "wind", "biomass", "waste"]
            inv_trans = ~transform

            for _, row in df.dropna(subset=[lat_col, lon_col]).iterrows():
                col, r_idx = inv_trans * (row[lon_col], row[lat_col])
                col, r_idx = int(col), int(r_idx)

                if 0 <= r_idx < height and 0 <= col < width:
                    fuel = str(row[fuel_col]).lower()
                    if any(f in fuel for f in ren_fuels):
                        mask_ren[r_idx, col] = 0
                    else:
                        mask_oth[r_idx, col] = 0
    else:
        mask_oth = (plants_raster != 1).astype(np.uint8)

    dist_ren = distance_transform_edt(mask_ren) * res_km
    dist_oth = distance_transform_edt(mask_oth) * res_km

    s_ren = 1.0 - np.exp(-dist_ren / PROXIMITY_DECAY_SIGMA_KM)
    s_oth = np.exp(-dist_oth / PROXIMITY_DECAY_SIGMA_KM)

    raw = np.maximum(s_ren, s_oth).astype(np.float32)
    raw[plants_raster == 1] = 0.0

    sigma_px = (
        PROXIMITY_SMOOTH_SIGMA_PX
        if res_km == 0
        else max(0.5, PROXIMITY_SMOOTH_SIGMA_PX / res_km)
    )
    raw = gaussian_filter(raw, sigma=sigma_px)
    raw = np.clip(raw, 0.0, 1.0)
    raw[~continent_mask] = NODATA_FLOAT

    if (plants_raster == 1).sum() == 0:
        raw[continent_mask] = 0.3

    land_valid = continent_mask & (raw != NODATA_FLOAT)
    score = normalize_percentile(raw, land_valid, p_low=5.0, p_high=95.0)

    score[plants_raster == 1] = 0.0
    score[~continent_mask] = NODATA_FLOAT

    return score, transform, crs


def compute_land_cover_scores(
    lc_path: Path,
    land_suitability: Any
) -> Dict[str, Tuple[np.ndarray, Affine, str]]:
    """
    Map land cover classes to technology-specific suitability scores.

    Args:
        lc_path: Path to land cover classification raster.
        land_suitability: DataFrame or dict with land cover class suitability.

    Returns:
        Dictionary mapping technology names to (score, transform, CRS) tuples.
    """
    with safe_raster_open(lc_path) as src:
        lc_data = src.read(1).astype(np.int16)
        transform = src.transform
        crs = str(src.crs)
        nodata = int(src.nodata) if src.nodata is not None else 0

    if isinstance(land_suitability, dict):
        df = pd.DataFrame.from_dict(land_suitability, orient='index')
    elif isinstance(land_suitability, list):
        df = pd.DataFrame(land_suitability)
    else:
        df = land_suitability.copy()

    if df.index.name != "Class_Code" and "Class_Code" in df.columns:
        df = df.set_index("Class_Code")
    df.index = df.index.astype(int)

    lookup = {
        int(code): {
            "biomass": float(
                row.get("Biomass", row.get("biomass", 0.0)) or 0.0
            )
        }
        for code, row in df.iterrows()
    }

    scores = {"biomass": np.full(lc_data.shape, NODATA_FLOAT, dtype=np.float32)}
    valid = (lc_data != nodata) & (lc_data > 0) & (lc_data != 255)

    for cls in np.unique(lc_data[valid]):
        cls_mask = (lc_data == cls) & valid
        scores["biomass"][cls_mask] = float(
            lookup.get(int(cls), {"biomass": 0.0})["biomass"]
        )

    return {tech: (arr, transform, crs) for tech, arr in scores.items()}


def compute_biomass_resource(
    lc_path: Path,
    yield_by_lc: Dict[int, float],
    params: Dict
) -> Tuple[np.ndarray, Affine, str]:
    """
    Compute biomass resource potential from land cover and yield parameters.

    Args:
        lc_path: Path to land cover raster.
        yield_by_lc: Dictionary mapping land cover codes to biomass yields.
        params: Configuration parameters dictionary.

    Returns:
        Tuple of (biomass suitability score, affine transform, CRS string).
    """
    with safe_raster_open(lc_path) as src:
        lc_data = src.read(1).astype(np.int16)
        transform = src.transform
        crs = str(src.crs)
        nodata = int(src.nodata) if src.nodata is not None else 0

    raw = np.full(lc_data.shape, NODATA_FLOAT, dtype=np.float32)
    valid_base = (lc_data != nodata) & (lc_data > 0)

    for cls, y in yield_by_lc.items():
        raw[(lc_data == cls) & valid_base] = float(y)
    raw[(raw == NODATA_FLOAT) & valid_base] = 0.0

    sigma = float(params.get("biomass_smooth_sigma", 1.0))
    if sigma > 0:
        raw_smooth = raw.copy()
        raw_smooth[~valid_base] = 0.0
        raw_smooth = gaussian_filter(raw_smooth, sigma=sigma).astype(np.float32)
        raw_smooth[~valid_base] = NODATA_FLOAT
        raw = raw_smooth

    return normalize_percentile(
        raw,
        valid_base,
        float(params.get("normalization_min_percentile", 5)),
        float(params.get("normalization_max_percentile", 95))
    ), transform, crs


def compute_protected_areas(
    wdpa_path: Optional[Path],
    mainland_gdf: gpd.GeoDataFrame,
    transform: Affine,
    width: int,
    height: int,
    crs: str,
    as_exclusion: bool = True,
) -> Tuple[np.ndarray, Affine, str]:
    """
    Rasterize protected areas (WDPA) with IUCN category-based scoring.

    Args:
        wdpa_path: Path to WDPA shapefile or directory.
        mainland_gdf: GeoDataFrame of country mainland boundaries.
        transform: Affine geotransform.
        width: Raster width in pixels.
        height: Raster height in pixels.
        crs: Target coordinate reference system.
        as_exclusion: If True, IUCN Ia/Ib/II receive score of 0.

    Returns:
        Tuple of (protected area score, affine transform, CRS string).
    """
    mainland_mask = np.zeros((height, width), dtype=np.uint8)
    mainland_union = mainland_gdf.to_crs(crs).unary_union
    rasterize_feats(
        [(mapping(mainland_union), 1)],
        out_shape=(height, width),
        transform=transform,
        out=mainland_mask,
        dtype=np.uint8
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
            or [
                p for p in wdpa_path.rglob("*.shp")
                if "point" not in p.name.lower()
            ]
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
            (
                c for c in ["IUCN_CAT", "iucn_cat", "IUCN", "DESIGNATION"]
                if c in gdf.columns
            ),
            None
        )
        if iucn_col:
            gdf["_iucn_score"] = (
                gdf[iucn_col]
                .str.lower()
                .str.strip()
                .map(lambda c: IUCN_SCORES.get(c, IUCN_SCORE_DEFAULT))
            )
            if as_exclusion:
                strict = (
                    gdf[iucn_col]
                    .str.lower()
                    .str.strip()
                    .isin(["ia", "ib", "ii"])
                )
                gdf.loc[strict, "_iucn_score"] = 0.0
        else:
            gdf["_iucn_score"] = IUCN_SCORE_DEFAULT

        gdf = gdf.sort_values("_iucn_score", ascending=False).reset_index(drop=True)
        shapes = (
            (mapping(geom), float(sv))
            for geom, sv in zip(gdf.geometry, gdf["_iucn_score"])
        )
        temp = np.full((height, width), IUCN_FREE_SCORE, dtype=np.float32)
        rasterize_feats(
            shapes,
            out_shape=(height, width),
            transform=transform,
            out=temp,
            dtype=np.float32
        )
        score[mainland_mask > 0] = temp[mainland_mask > 0]

    except Exception as e:
        logger.warning("  Protected areas rasterization anomaly: %s", e)
        score[mainland_mask > 0] = IUCN_FREE_SCORE

    return score, transform, crs


def compute_population_suitability(
    pop_path: Path,
    params: Dict
) -> Tuple[np.ndarray, Affine, str]:
    """
    Logarithmic socio-demographic penalty for extreme density clustering.

    Args:
        pop_path: Path to population density raster.
        params: Configuration parameters dictionary.

    Returns:
        Tuple of (population suitability, affine transform, CRS string).
    """
    with safe_raster_open(pop_path) as src:
        pop = src.read(1)
        tf = src.transform
        crs = src.crs
        nodata = src.nodata

    threshold = params.get("pop_density_threshold", 300.0)
    score = np.clip(
        1.0 - (np.log1p(np.clip(pop, 0, threshold)) / np.log1p(threshold)),
        0.0,
        1.0
    ).astype(np.float32)

    if nodata is not None:
        score[pop == float(nodata)] = NODATA_FLOAT
    score[pop < 0] = NODATA_FLOAT
    return score, tf, str(crs)


def compute_lakes_exclusion(lakes_path: Path) -> Tuple[np.ndarray, Affine, str]:
    """
    Binary exclusion mask for large water bodies.

    Args:
        lakes_path: Path to lakes binary raster.

    Returns:
        Tuple of (exclusion score [0 or 1], affine transform, CRS string).
    """
    with safe_raster_open(lakes_path) as src:
        lake_mask = src.read(1).astype(np.uint8)
        transform = src.transform
        crs = str(src.crs)

    country_pixels = (lake_mask != 255)
    score = np.full(lake_mask.shape, NODATA_FLOAT, dtype=np.float32)
    score[country_pixels & (lake_mask == 0)] = 1.0
    score[country_pixels & (lake_mask == 1)] = 0.0
    return score, transform, crs


def compute_river_suitability(
    rivers_path: Path,
    params: Dict,
    tech: str = "solar"
) -> Tuple[np.ndarray, Affine, str]:
    """
    Technology-specific river proximity suitability.

    Args:
        rivers_path: Path to river distance raster (km).
        params: Configuration parameters dictionary.
        tech: Technology type ("solar", "wind", or "biomass").

    Returns:
        Tuple of (river suitability score, affine transform, CRS string).
    """
    with safe_raster_open(rivers_path) as src:
        dist_km = src.read(1).astype(np.float32)
        transform = src.transform
        crs = str(src.crs)
        nodata = src.nodata

    continent_mask = (
        (dist_km != float(nodata if nodata is not None else NODATA_FLOAT))
        & np.isfinite(dist_km)
    )
    score = np.full(dist_km.shape, NODATA_FLOAT, dtype=np.float32)

    if tech == "biomass":
        max_dist = float(params.get("river_max_dist_biomass_km", 10.0))
        score[continent_mask] = np.clip(
            1.0 - dist_km[continent_mask] / max_dist,
            0.0,
            1.0
        ).astype(np.float32)
    else:
        buffer_km = float(params.get("river_safety_buffer_km", 0.5))
        score[continent_mask & (dist_km < buffer_km)] = 0.0
        score[continent_mask & (dist_km >= buffer_km)] = 1.0

    return score, transform, crs


def compute_seismic_suitability(seismic_path: Path) -> Tuple[np.ndarray, Affine, str]:
    """
    Seismic risk inversion (low risk = high suitability).

    Args:
        seismic_path: Path to seismic hazard raster.

    Returns:
        Tuple of (seismic suitability, affine transform, CRS string).
    """
    with safe_raster_open(seismic_path) as src:
        data = src.read(1).astype(np.float32)
        transform = src.transform
        crs = str(src.crs)
        nodata = src.nodata

    valid = _valid_mask(data, nodata) & (data >= 0)
    normalized = normalize_percentile(data, valid, p_low=2.0, p_high=98.0)
    score = np.full(data.shape, NODATA_FLOAT, dtype=np.float32)
    valid_norm = valid & (normalized != NODATA_FLOAT)
    score[valid_norm] = np.clip(1.0 - normalized[valid_norm], 0.0, 1.0)
    return score, transform, crs


# ─────────────────────────────────────────────────────────────────────────────
# Reporting & Master Class Orchestration
# ─────────────────────────────────────────────────────────────────────────────


def write_criteria_summary(
    criteria: Dict[str, np.ndarray],
    country_code: str,
    country_name: str,
    report_dir: Path
) -> Path:
    """
    Generate statistical summary report for all criteria.

    Args:
        criteria: Dictionary of criterion names to score arrays.
        country_code: ISO country code.
        country_name: Full country name.
        report_dir: Output directory for report.

    Returns:
        Path to generated summary file.
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / f"criteria_summary_{country_code}.txt"
    header = [
        "=" * 60 + "\n",
        f"  CRITERIA SUMMARY — {country_name} ({country_code})\n",
        f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
        "=" * 60 + "\n\n"
    ]
    body = []

    for name, score in criteria.items():
        valid = score[
            np.isfinite(score) & (score != NODATA_FLOAT) & (score >= 0)
        ]
        if not len(valid):
            body.append(f"  {name}: no data\n\n")
            continue

        bins = [0, 0.2, 0.4, 0.6, 0.8, 1.001]
        counts = np.histogram(valid, bins=bins)[0]
        total = len(valid)
        p10, p50, p90 = np.percentile(valid, [10, 50, 90])

        body.append(
            f"  {name}\n"
            f"    Valid pixels: {total:>10,}\n"
            f"    Mean±Std    : {valid.mean():.4f} ± {valid.std():.4f}\n"
            f"    P10/P50/P90 : {p10:.3f} / {p50:.3f} / {p90:.3f}\n"
        )
        body.append("    Range       |    Count    |     %\n")
        body.append("    " + "-" * 38 + "\n")
        for lbl, cnt in zip(
            ["0.0–0.2", "0.2–0.4", "0.4–0.6", "0.6–0.8", "0.8–1.0"],
            counts
        ):
            body.append(
                f"    {lbl:<13} | {cnt:>10,} | {100*cnt/total:>5.1f}%\n"
            )
        body.append(f"    Top 10%     : score > {p90:.3f}\n\n")

    out.write_text("".join(header + body), encoding="utf-8")
    return out


class CriteriaBuilder:
    """
    Orchestrates Phase 2b: Parallel criteria mathematical projection and cartographic render.
    """

    def __init__(self, cfg: Any, outputs_dir: Path):
        """
        Initialize CriteriaBuilder.

        Args:
            cfg: Global configuration object.
            outputs_dir: Base output directory path.
        """
        self.cfg = cfg
        self.outputs_dir = Path(outputs_dir)
        self.viz_cfg = cfg.system.get("visualization", {})
        self.c_meta = self.viz_cfg.get("criteria_meta", {})
        self.p_colors = self.viz_cfg.get("plant_colors", {})
        self._admin_gdf: Optional[gpd.GeoDataFrame] = None
        self.styler = GeoWorldStyler(
            self.viz_cfg,
            global_dpi=cfg.system.get("pipeline", {}).get("map_dpi_export", 150)
        )

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
        vmax: Optional[float] = None
    ) -> None:
        """
        Render cartographic visualization for a single criterion.

        Args:
            score: Suitability score array [0, 1].
            transform: Affine geotransform.
            crs: Coordinate reference system string.
            criterion: Criterion identifier.
            country_name: Country display name.
            mainland_gdf: Country boundary GeoDataFrame.
            out_path: Output PNG path.
            context_gdf: Optional neighboring countries for context.
            vmin: Minimum value for colormap scaling.
            vmax: Maximum value for colormap scaling.

        Returns:
            None
        """
        meta = self.c_meta.get(
            criterion,
            [criterion.replace("_", " ").title(), "Score (0–1)", "RdYlGn_r", False]
        )
        title, unit, cmap_name, invert = meta[0], meta[1], meta[2], meta[3]

        plot_data = score.copy().astype(np.float64)
        plot_data[(score == NODATA_FLOAT) | (score < 0)] = np.nan

        height_px, width_px = score.shape
        extent = [
            transform.c,
            transform.c + transform.a * width_px,
            transform.f + transform.e * height_px,
            transform.f
        ]
        v_minx, v_miny, v_maxx, v_maxy = mainland_gdf.total_bounds

        fig, ax = self.styler.create_figure(v_minx, v_maxx, v_miny, v_maxy)
        self.styler.draw_basemap(ax, crs, mainland_gdf, context_gdf, self._admin_gdf)

        if criterion == "protected_areas":
            mainland_gdf.to_crs(crs).plot(
                ax=ax,
                color="#E8E8E8",
                edgecolor="none",
                zorder=2
            )
            restricted = plot_data.copy()
            restricted[restricted >= 1.0] = np.nan
            restricted[np.isfinite(restricted)] = 0.5
            cmap_r = mcolors.ListedColormap([self.styler.CONTOUR_P90_COLOR])
            cmap_r.set_bad(color="none")

            ax.imshow(
                restricted,
                extent=extent,
                origin="upper",
                cmap=cmap_r,
                vmin=0.0,
                vmax=1.0,
                zorder=3,
                interpolation="nearest",
                alpha=0.85
            )
            mainland_gdf.to_crs(crs).boundary.plot(
                ax=ax,
                color=self.styler.country_border,
                linewidth=0.8,
                zorder=4
            )
            self.styler.add_standard_legend(
                ax,
                [
                    mpatches.Patch(
                        color=self.styler.CONTOUR_P90_COLOR,
                        alpha=0.85,
                        label="Protected / Restricted (WDPA)"
                    ),
                    mpatches.Patch(color="#E8E8E8", label="Free territory")
                ],
                "lower_right"
            )
        else:
            interp = "bilinear" if criterion in (
                "proximity_plants",
                "protected_areas",
                "solar_resource"
            ) else "nearest"
            vmax_val = vmax if vmax is not None else (
                None if criterion == "slope_degrees" else 1.0
            )

            im = ax.imshow(
                plot_data,
                extent=extent,
                origin="upper",
                cmap=self.styler.make_cmap(
                    cmap_name,
                    reverse=invert,
                    under="none",
                    bad="none"
                ),
                vmin=vmin or 0.0,
                vmax=vmax_val,
                zorder=2,
                interpolation=interp
            )
            self.styler.add_colorbar(fig, im, unit, extend="neither")

        self.styler.add_decorations(ax, v_minx, v_maxx, v_miny, v_maxy)
        self.styler.add_standard_footer(fig, crs_metadata=f"CRS: {crs or 'EPSG:4326'}")

        if self._admin_gdf is not None:
            self.styler.draw_admin_labels(
                ax,
                self._admin_gdf,
                v_minx,
                v_maxx,
                v_miny,
                v_maxy,
                max_labels=self.viz_cfg.get("layout", {}).get("admin_max_labels", 12),
                zorder=8
            )

        self.styler.add_standard_title(fig, title_main=title, title_sub=country_name)

        valid_data = plot_data[np.isfinite(plot_data)]
        if len(valid_data) > 0:
            self.styler.add_stats_strip(
                fig,
                {
                    "mean": valid_data.mean(),
                    "std": valid_data.std(),
                    "p25": np.percentile(valid_data, 25),
                    "p75": np.percentile(valid_data, 75)
                },
                format_template=(
                    f"Valid pixels: {len(valid_data):,}  |  "
                    "Mean: {mean:.3f} ± {std:.3f}  |  IQR: {p25:.3f}–{p75:.3f}"
                )
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
        context_gdf: Optional[gpd.GeoDataFrame] = None
    ) -> None:
        """
        Render map showing existing power plant infrastructure.

        Args:
            plants_df: DataFrame with plant locations and attributes.
            mainland_gdf: Country boundary GeoDataFrame.
            transform: Affine geotransform.
            crs: Coordinate reference system string.
            country_name: Country display name.
            out_path: Output PNG path.
            context_gdf: Optional neighboring countries for context.

        Returns:
            None
        """
        minx, miny, maxx, maxy = mainland_gdf.total_bounds
        fig, ax = self.styler.create_figure(
            minx,
            maxx,
            miny,
            maxy,
            right_in_override=self.styler.LEFT_IN
        )
        self.styler.draw_basemap(
            ax,
            crs,
            mainland_gdf,
            context_gdf,
            self._admin_gdf
        )
        mainland_gdf.to_crs(crs).plot(
            ax=ax,
            color="#FFFFFF",
            edgecolor=self.styler.country_border,
            linewidth=0.8,
            zorder=2
        )

        if plants_df is not None and not plants_df.empty:
            df = plants_df.copy()
            df.columns = [c.strip().lower() for c in df.columns]
            lat_col = next(
                (c for c in ["latitude", "lat"] if c in df.columns),
                None
            )
            lon_col = next(
                (c for c in ["longitude", "lon", "long"] if c in df.columns),
                None
            )
            fuel_col = next(
                (c for c in ["primary_fuel", "fuel1", "fuel"] if c in df.columns),
                None
            )
            cap_col = next(
                (c for c in ["capacity_mw", "capacity", "mw"] if c in df.columns),
                None
            )

            if lat_col and lon_col:
                df[lat_col] = pd.to_numeric(df[lat_col], errors="coerce")
                df[lon_col] = pd.to_numeric(df[lon_col], errors="coerce")
                df = df.dropna(subset=[lat_col, lon_col])
                fuel_handles = {}

                for _, row in df.iterrows():
                    fuel = next(
                        (
                            k for k in self.p_colors
                            if k.lower() in str(row.get(fuel_col, "")).lower()
                        ),
                        "Other"
                    )
                    if cap_col:
                        cap_val = pd.to_numeric(row.get(cap_col, 0), errors="coerce")
                        msize = float(
                            np.clip(4.0 + np.log1p(cap_val) * 0.9, 4.0, 14.0)
                        )
                    else:
                        msize = 5.0

                    ax.plot(
                        float(row[lon_col]),
                        float(row[lat_col]),
                        "o",
                        color=self.p_colors.get(fuel, "#78909C"),
                        markersize=msize,
                        zorder=5,
                        markeredgewidth=0.6,
                        markeredgecolor="white",
                        alpha=0.90
                    )
                    if fuel not in fuel_handles:
                        fuel_handles[fuel] = mpatches.Patch(
                            color=self.p_colors.get(fuel, "#78909C"),
                            label=fuel
                        )

                if fuel_handles:
                    self.styler.add_standard_legend(
                        ax,
                        list(fuel_handles.values()),
                        "upper_right"
                    )

        self.styler.add_standard_title(
            fig,
            title_main="Existing Power Plants",
            title_sub=country_name
        )
        self.styler.add_decorations(
            ax,
            minx,
            maxx,
            miny,
            maxy,
            fixed_scalebar_km=60
        )
        self.styler.add_standard_footer(
            fig,
            crs_metadata=f"CRS: {crs or 'EPSG:4326'}"
        )
        if self._admin_gdf is not None:
            self.styler.draw_admin_labels(
                ax,
                self._admin_gdf,
                minx,
                maxx,
                miny,
                maxy,
                max_labels=self.viz_cfg.get("layout", {}).get("admin_max_labels", 12),
                zorder=8
            )
        self.styler.save(fig, out_path)

    def run(
        self,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        aligned: Dict[str, Optional[Path]],
        params: Dict,
        land_suit: pd.DataFrame,
        wdpa_path: Optional[Path] = None,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        plants_df: Optional[pd.DataFrame] = None,
        rivers_path: Optional[Path] = None,
        lakes_path: Optional[Path] = None,
        seismic_path: Optional[Path] = None
    ) -> Dict[str, np.ndarray]:
        """
        Execute Phase 2b: Build normalized criteria from aligned rasters.

        Args:
            country_code: ISO country code.
            country_name: Full country name.
            mainland_gdf: Country mainland boundary GeoDataFrame.
            aligned: Dictionary of aligned raster paths by layer type.
            params: Technology-specific parameters dictionary.
            land_suit: Land cover suitability lookup table.
            wdpa_path: Optional path to WDPA protected areas data.
            context_gdf: Optional GeoDataFrame of neighboring countries.
            plants_df: Optional DataFrame of existing power plants.
            rivers_path: Optional path to rivers distance raster.
            lakes_path: Optional path to lakes exclusion raster.
            seismic_path: Optional path to seismic hazard raster.

        Returns:
            Dictionary mapping criterion names to normalized score arrays.
        """
        base = self.outputs_dir / country_code / "criteria_builder"
        tif_dir = base / "tif"
        png_dir = base / "png"
        report_dir = base / "reports"
        for d in [tif_dir, png_dir, report_dir]:
            d.mkdir(parents=True, exist_ok=True)

        logger.info("\nPhase 2b — Criteria Builder: %s", country_code)
        t0 = time.perf_counter()

        ref_path = next(
            (p for p in aligned.values() if p and Path(p).exists()),
            None
        )
        with safe_raster_open(ref_path) as src:
            transform = src.transform
            crs = str(src.crs)
            height = src.height
            width = src.width

        self._admin_gdf = self.styler.load_admin_boundaries(
            country_name,
            mainland_gdf,
            Path(self.cfg.raw_path)
        )
        if self._admin_gdf is not None:
            logger.info(
                "  Admin boundaries: %d regions loaded",
                len(self._admin_gdf)
            )
        else:
            logger.info("  Admin boundaries: Not available")

        criteria = {}
        _render_queue = []

        def _enqueue(
            score: np.ndarray,
            tf: Affine,
            c: str,
            name: str,
            **plot_kw
        ) -> None:
            criteria[name] = score
            _save_criterion(score, tif_dir / f"{name}.tif", tf, c)
            _render_queue.append(
                partial(
                    self._plot_criterion_map,
                    score,
                    tf,
                    c,
                    name,
                    country_name,
                    mainland_gdf,
                    png_dir / f"{name}.png",
                    context_gdf,
                    **plot_kw
                )
            )

        if aligned.get("solar"):
            with _timer("solar_resource"):
                _enqueue(
                    *compute_solar_resource(aligned["solar"], params),
                    "solar_resource"
                )

        if aligned.get("wind"):
            with _timer("wind_resource"):
                _enqueue(
                    *compute_wind_resource(aligned["wind"], params),
                    "wind_resource"
                )

        if aligned.get("slope"):
            with _timer("terrain_score"):
                _enqueue(
                    *compute_terrain_score(
                        aligned["slope"],
                        aligned.get("elevation"),
                        params
                    ),
                    "terrain_score"
                )
            with _timer("slope_degrees"):
                s_deg, tf_s, c_s = compute_slope_degrees(aligned["slope"])
                valid_s = s_deg[
                    np.isfinite(s_deg)
                    & (s_deg != NODATA_FLOAT)
                    & (s_deg >= 0)
                ]
                _save_criterion(s_deg, tif_dir / "slope_degrees.tif", tf_s, c_s)
                vmax_val = (
                    float(np.percentile(valid_s, 98))
                    if len(valid_s)
                    else 45.0
                )
                _render_queue.append(
                    partial(
                        self._plot_criterion_map,
                        s_deg,
                        tf_s,
                        c_s,
                        "slope_degrees",
                        country_name,
                        mainland_gdf,
                        png_dir / "slope_degrees.png",
                        context_gdf,
                        vmin=0.0,
                        vmax=vmax_val
                    )
                )

        if aligned.get("land_cover"):
            with _timer("land_cover"):
                lc_scores = compute_land_cover_scores(
                    aligned["land_cover"],
                    land_suit
                )
            if "biomass" in lc_scores:
                _enqueue(*lc_scores["biomass"], "lc_biomass")

            yield_raw = params.get(
                "_biomass_yields",
                params.get("biomass_yield_by_land_cover", {})
            )
            if yield_raw:
                with _timer("biomass_resource"):
                    _enqueue(
                        *compute_biomass_resource(
                            aligned["land_cover"],
                            yield_raw,
                            params
                        ),
                        "biomass_resource"
                    )

        if aligned.get("plants"):
            with _timer("proximity_plants"):
                _enqueue(
                    *compute_proximity_plants(
                        aligned["plants"],
                        transform,
                        plants_df
                    ),
                    "proximity_plants"
                )

        with _timer("protected_areas"):
            as_excl = str(
                params.get("protected_as_exclusion", "True")
            ).lower() == "true"
            _enqueue(
                *compute_protected_areas(
                    wdpa_path,
                    mainland_gdf,
                    transform,
                    width,
                    height,
                    crs,
                    as_excl
                ),
                "protected_areas"
            )

        if aligned.get("population"):
            with _timer("pop_suitability"):
                _enqueue(
                    *compute_population_suitability(aligned["population"], params),
                    "pop_suitability"
                )

        if aligned.get("roads"):
            with _timer("road_suitability"):
                _enqueue(
                    *compute_road_suitability(aligned["roads"], params),
                    "road_suitability"
                )

        if aligned.get("lakes"):
            with _timer("lakes_exclusion"):
                _enqueue(*compute_lakes_exclusion(aligned["lakes"]), "lakes_exclusion")

        if aligned.get("rivers"):
            with _timer("river_solar"):
                _enqueue(
                    *compute_river_suitability(aligned["rivers"], params, tech="solar"),
                    "river_solar"
                )
            with _timer("river_wind"):
                _enqueue(
                    *compute_river_suitability(aligned["rivers"], params, tech="wind"),
                    "river_wind"
                )
            with _timer("river_biomass"):
                _enqueue(
                    *compute_river_suitability(
                        aligned["rivers"],
                        params,
                        tech="biomass"
                    ),
                    "river_biomass"
                )

        if aligned.get("seismic"):
            with _timer("seismic_suitability"):
                _enqueue(
                    *compute_seismic_suitability(aligned["seismic"]),
                    "seismic_suitability"
                )

        if aligned.get("grid"):
            with _timer("grid_suitability"):
                _enqueue(
                    *compute_grid_suitability(aligned["grid"], params),
                    "grid_suitability"
                )

        if plants_df is not None and aligned.get("plants"):
            _render_queue.append(
                partial(
                    self._plot_power_plants_map,
                    plants_df,
                    mainland_gdf,
                    transform,
                    crs,
                    country_name,
                    png_dir / "power_plants.png",
                    context_gdf
                )
            )

        logger.info(
            "  Computation: %.1fs | %d maps queued for rendering",
            time.perf_counter() - t0,
            len(_render_queue)
        )

        if not self.cfg.system.get("pipeline", {}).get("skip_criteria_maps", False):
            n_workers = self.viz_cfg.get("render_workers", 4)
            logger.info(
                "  Rendering %d maps across %d worker(s)...",
                len(_render_queue),
                n_workers
            )
            t_render = time.perf_counter()
            with ThreadPoolExecutor(max_workers=n_workers) as executor:
                list(executor.map(lambda fn: fn(), _render_queue))
            logger.info(
                "  Render: %.1fs (%s) | Phase total: %.1fs",
                time.perf_counter() - t_render,
                "parallel" if n_workers > 1 else "sequential",
                time.perf_counter() - t0
            )

        write_criteria_summary(criteria, country_code, country_name, report_dir)

        logger.info(
            "\n%s\n  CRITERIA BUILDER — %s\n  Output: %s\n%s",
            "=" * 62,
            country_code,
            base,
            "=" * 62
        )
        for name, score in criteria.items():
            valid = score[
                np.isfinite(score) & (score != NODATA_FLOAT) & (score >= 0)
            ]
            if len(valid):
                logger.info(
                    "  [OK] %-24s: mean=%.3f | >=0.6: %5.1f%% | n=%s",
                    name,
                    valid.mean(),
                    100 * (valid >= 0.6).mean(),
                    f"{len(valid):,}"
                )
        logger.info(
            "  Total time: %.1fs\n%s",
            time.perf_counter() - t0,
            "=" * 62
        )

        return criteria