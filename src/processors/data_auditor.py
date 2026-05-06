"""
data_auditor.py — Phase 1: Raw Data Quality Audit
==================================================
Audits raw country datasets BEFORE any processing.
Does not modify data. Reads inputs, masks to the actual
country polygon, and reports statistics.

Why polygon masking instead of bounding-box clipping
-----------------------------------------------------
A bounding-box for Portugal [-9.5, 37.0, -6.2, 42.1]
would include ~50,000 km² of western Spain and ~13,000 km²
of Atlantic Ocean. Accurate area estimates require
rasterio.mask() with the real shapefile polygon.

Usage in main.py
----------------
    from src.processors.data_auditor import DataAuditor, get_mainland_gdf

    mainland_gdf = get_mainland_gdf(gdf_border)

    auditor = DataAuditor(cfg)
    auditor.run(
        country_name    = name,
        country_code    = code,
        status          = status,
        elev_path       = elev_path,
        slope_path      = slope_path,
        plants_df       = plants_df,
        country_gdf     = mainland_gdf,
        skip_land_cover = False,
    )
"""

from __future__ import annotations

import math
import os
import platform
import time
import warnings
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds as window_from_bounds
from shapely.geometry import box, mapping

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):  # type: ignore[misc]
        return iterable

from src.core.constants import ESA_CLASS_NAMES, MASK_FILL
from src.utils.utils import get_local_utm_crs

os.environ["GDAL_QUIET"] = "YES"
os.environ["CPL_LOG_ERRORS"] = "OFF"
os.environ["CPL_LOG"] = (
    "NUL" if platform.system() == "Windows" else "/dev/null"
)

import logging  # noqa: E402
logger = logging.getLogger("geoworld.processors.DataAuditor")

_CHUNK_ROWS = 4_000


# ===========================================================================
# GEOMETRY
# ===========================================================================

def get_mainland_gdf(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Return a GeoDataFrame containing only the largest polygon (mainland).

    Removes islands, enclaves, and fragments before any analysis.

    Args:
        gdf: Full country GeoDataFrame

    Returns:
        Single-row GeoDataFrame with largest polygon in EPSG:4326

    Example:
        mainland = get_mainland_gdf(gdf_border)
        # Returns mainland Portugal, excluding Azores and Madeira
    """
    union = (
        gdf.geometry.union_all()
        if hasattr(gdf.geometry, "union_all")
        else gdf.geometry.unary_union
    )

    utm_crs = get_local_utm_crs(union)
    exploded = (
        gdf.to_crs(utm_crs)
        .explode(index_parts=False)
        .reset_index(drop=True)
    )
    idx = exploded.geometry.area.idxmax()

    return exploded.iloc[[idx]].to_crs("EPSG:4326")


def detect_island_nation(
    gdf: gpd.GeoDataFrame,
    threshold_pct: float = 0.60,
) -> bool:
    """
    Detect if a country is an island nation where get_mainland_gdf()
    would discard more than threshold_pct of total territory.

    Args:
        gdf: Country GeoDataFrame
        threshold_pct: Fraction threshold above which country is
                       considered an island nation (default: 0.60)

    Returns:
        True if the largest polygon represents less than
        (1 - threshold_pct) of total territory, indicating
        that use_mainland_only=true would be incorrect
    """
    try:
        utm_crs = get_local_utm_crs(
            gdf.geometry.union_all()
            if hasattr(gdf.geometry, "union_all")
            else gdf.geometry.unary_union
        )
        gdf_proj = gdf.to_crs(utm_crs)
        exploded = (
            gdf_proj.explode(index_parts=False)
            .reset_index(drop=True)
        )

        total_area = float(exploded.geometry.area.sum())
        largest_area = float(exploded.geometry.area.max())

        if total_area == 0:
            return False

        largest_pct = largest_area / total_area
        return largest_pct < (1.0 - threshold_pct)

    except Exception:
        return False


# ===========================================================================
# TIMING
# ===========================================================================

@contextmanager
def _timer(
    label: str,
    timings: Dict[str, float],
) -> Generator[None, None, None]:
    """
    Context manager that measures execution time and stores it in timings.

    Args:
        label: Step label for logging and storage
        timings: Dictionary to store elapsed time
    """
    t0 = time.perf_counter()
    logger.info("  [%s] starting...", label)
    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        timings[label] = round(elapsed, 2)
        logger.info("  [%s] completed in %.1fs", label, elapsed)


# ===========================================================================
# HELPER FUNCTIONS
# ===========================================================================

def _nodata_mask(data: np.ndarray, nodata) -> np.ndarray:
    """
    Return boolean mask where True indicates a valid pixel.

    Safely handles nodata=None, nan, -9999, and float32 overflow.

    Args:
        data: Raster data array
        nodata: NoData sentinel value (may be None, nan, or numeric)

    Returns:
        Boolean array with True for valid pixels
    """
    finite = np.isfinite(data)
    if nodata is None:
        return finite

    try:
        nodata_as_float = float(nodata)
    except (TypeError, ValueError):
        return finite

    if not math.isfinite(nodata_as_float):
        return finite

    with np.errstate(over="ignore", invalid="ignore"):
        nodata_typed = data.dtype.type(nodata_as_float)

    if not np.isfinite(nodata_typed):
        return finite

    return finite & (data != nodata_typed)


def _row_area_km2(
    shape: Tuple[int, int],
    transform: rasterio.Affine,
) -> np.ndarray:
    """
    Compute geodetic area per pixel row in km².

    Returns a 1D array (height,) corrected by latitude cosine.
    Uses a 1D vector rather than 2D to avoid memory issues with
    large tiles (e.g., a 36000x36000 ESA tile in float64 is ~10 GB,
    while a 1D row vector is only ~281 KB).

    For total area: (row_areas * valid_pixels_per_row).sum()
    Per class:      (row_areas * (data == cls).sum(axis=1)).sum()

    Args:
        shape: Tuple of (height, width)
        transform: Rasterio affine transform

    Returns:
        1D array of per-row pixel areas in km²
    """
    height, _ = shape
    res_x = abs(transform.a)
    res_y = abs(transform.e)

    rows = np.arange(height)
    y_top = transform.f + rows * transform.e
    y_bottom = transform.f + (rows + 1) * transform.e
    lat_mid = (y_top + y_bottom) / 2.0
    lat_rad = np.radians(lat_mid)

    lat_km = (
        111132.92
        - 559.82 * np.cos(2 * lat_rad)
        + 1.175 * np.cos(4 * lat_rad)
        - 0.0023 * np.cos(6 * lat_rad)
    ) / 1000.0
    lon_km = (
        111412.84 * np.cos(lat_rad)
        - 93.50 * np.cos(3 * lat_rad)
        + 0.118 * np.cos(5 * lat_rad)
    ) / 1000.0

    km_x = res_x * lon_km
    km_y = res_y * lat_km
    return (km_x * km_y).astype(np.float64)


def _mask_raster_by_polygon(
    src: rasterio.DatasetReader,
    country_gdf: gpd.GeoDataFrame,
) -> Tuple[Optional[np.ndarray], Optional[rasterio.Affine]]:
    """
    Clip raster to country polygon using windowed read strategy.

    Steps:
      1. Compute bounding-box window (avoids reading the entire global file).
      2. Read only that window as float32.
      3. Apply geometry_mask for precise polygon clipping.

    For country-specific files already clipped (e.g., bra_pop_2020.tif),
    the window may still cover most of the file and be too large for RAM.
    In that case, raises MemoryError so the caller can use _stats_chunked().

    Args:
        src: Open rasterio dataset
        country_gdf: Country polygon GeoDataFrame

    Returns:
        Tuple of (masked_data, window_transform), or (None, None) if no
        overlap between polygon and raster extent

    Raises:
        MemoryError: If the windowed read exceeds available memory
    """
    geom_in_src_crs = country_gdf.to_crs(src.crs)
    bounds = geom_in_src_crs.total_bounds

    window = window_from_bounds(
        bounds[0], bounds[1], bounds[2], bounds[3],
        transform=src.transform,
    )
    window = window.intersection(
        rasterio.windows.Window(0, 0, src.width, src.height)
    )

    win_transform = src.window_transform(window)
    data = src.read(1, window=window).astype(np.float32)

    shapes = [mapping(geom) for geom in geom_in_src_crs.geometry]
    poly_mask = geometry_mask(
        shapes,
        out_shape=data.shape,
        transform=win_transform,
        invert=True,
    )
    data[~poly_mask] = MASK_FILL

    return data, win_transform


def _stats_chunked(
    src: rasterio.DatasetReader,
    country_gdf: gpd.GeoDataFrame,
) -> Tuple[Optional[Dict[str, Any]], Optional[rasterio.Affine]]:
    """
    Compute raster statistics by reading in chunks of _CHUNK_ROWS rows.

    Limits RAM usage to approximately 200 MB per chunk, regardless of
    file size. Used as fallback when _mask_raster_by_polygon raises
    MemoryError (e.g., WorldPop 100m for Brazil ~850M pixels, ~3.4 GB).

    Args:
        src: Open rasterio dataset
        country_gdf: Country polygon GeoDataFrame

    Returns:
        Tuple of (stats_dict, transform) or (None, None) on error.
        Stats dict contains: min, max, mean, valid_px, total_px, area_km2
    """
    try:
        geom_in_src_crs = country_gdf.to_crs(src.crs)
        shapes = [mapping(geom) for geom in geom_in_src_crs.geometry]
        transform = src.transform
        nodata = src.nodata
        height = src.height
        width = src.width

        g_min = np.inf
        g_max = -np.inf
        g_sum = 0.0
        g_sum_sq = 0.0
        g_valid_px = 0
        g_total_px = 0
        g_area_km2 = 0.0

        for row_start in range(0, height, _CHUNK_ROWS):
            row_end = min(row_start + _CHUNK_ROWS, height)
            chunk_h = row_end - row_start

            window = rasterio.windows.Window(0, row_start, width, chunk_h)
            win_tf = src.window_transform(window)
            block = src.read(1, window=window).astype(np.float32)

            poly_mask = geometry_mask(
                shapes,
                out_shape=block.shape,
                transform=win_tf,
                invert=True,
            )

            valid_mask = poly_mask & np.isfinite(block)
            if nodata is not None:
                try:
                    nd = np.float32(nodata)
                    if np.isfinite(nd):
                        valid_mask &= (block != nd)
                except (TypeError, ValueError):
                    pass
            valid_mask &= (block != np.float32(MASK_FILL))

            vals = block[valid_mask].astype(np.float64)
            n = vals.size

            if n > 0:
                g_min = min(g_min, float(vals.min()))
                g_max = max(g_max, float(vals.max()))
                g_sum += float(vals.sum())
                g_sum_sq += float((vals ** 2).sum())

            row_areas = _row_area_km2((chunk_h, width), win_tf)
            valid_per_row = valid_mask.sum(axis=1)
            g_area_km2 += float((row_areas * valid_per_row).sum())

            g_valid_px += n
            g_total_px += block.size

        if g_valid_px == 0:
            return {
                "min": None,
                "max": None,
                "mean": None,
                "valid_px": 0,
                "total_px": g_total_px,
                "area_km2": 0.0,
            }, transform

        return {
            "min": round(g_min, 4),
            "max": round(g_max, 4),
            "mean": round(g_sum / g_valid_px, 4),
            "valid_px": g_valid_px,
            "total_px": g_total_px,
            "area_km2": round(g_area_km2, 1),
        }, transform

    except Exception as exc:
        logger.debug("_stats_chunked failed: %s", exc)
        return None, None


# ===========================================================================
# SINGLE RASTER INSPECTION
# ===========================================================================

def inspect_raster(
    path: Path,
    country_gdf: Optional[gpd.GeoDataFrame] = None,
) -> Dict[str, Any]:
    """
    Read metadata and statistics from a single raster file.

    Adaptive memory strategy:
      1. Try _mask_raster_by_polygon (windowed read, fast for most cases).
      2. If MemoryError (e.g., WorldPop Brazil 100m ~3.4 GB), fall back to
         _stats_chunked which reads in ~200 MB blocks.

    Args:
        path: Path to raster file
        country_gdf: Country polygon for masking (uses full file if None)

    Returns:
        Dictionary with metadata and statistics including: name, size_mb,
        crs, resolution, shape, area_km2, min, max, mean, valid_pct, error
    """
    result: Dict[str, Any] = {
        "name": path.name,
        "size_mb": round(path.stat().st_size / (1024 ** 2), 2),
        "crs": None,
        "resolution": None,
        "global_shape": None,
        "analysis_shape": None,
        "masked_by": "full file",
        "nodata": None,
        "min": None,
        "max": None,
        "mean": None,
        "valid_pct": None,
        "area_km2": None,
        "error": None,
    }

    try:
        with rasterio.open(str(path)) as src:
            result["crs"] = str(src.crs)
            result["resolution"] = round(abs(src.res[0]), 8)
            result["global_shape"] = (src.height, src.width)
            result["nodata"] = src.nodata

            if country_gdf is not None:
                data, transform = None, None
                chunked = False
                try:
                    data, transform = _mask_raster_by_polygon(
                        src, country_gdf
                    )
                except MemoryError:
                    pass

                if data is None and not chunked:
                    logger.info(
                        "  [%s] large file — using chunked read "
                        "(%d rows/chunk)",
                        path.stem,
                        _CHUNK_ROWS,
                    )
                    stats, transform = _stats_chunked(src, country_gdf)
                    if stats is None:
                        result["error"] = (
                            "Failed to process raster "
                            "(both windowed and chunked strategies failed)"
                        )
                        return result

                    result["masked_by"] = "country polygon (chunked)"
                    result["analysis_shape"] = (src.height, src.width)
                    result["min"] = stats["min"]
                    result["max"] = stats["max"]
                    result["mean"] = stats["mean"]
                    result["valid_pct"] = (
                        round(
                            100.0 * stats["valid_px"] / stats["total_px"],
                            1,
                        )
                        if stats["total_px"] > 0
                        else 0.0
                    )
                    result["area_km2"] = stats["area_km2"]
                    return result

                if data is None:
                    result["error"] = (
                        "Country polygon does not intersect raster extent"
                    )
                    return result

                result["masked_by"] = "country polygon (windowed)"

            else:
                data = src.read(1).astype(np.float32)
                transform = src.transform

            result["analysis_shape"] = data.shape

            valid_mask = _nodata_mask(data, MASK_FILL)
            if src.nodata is not None and src.nodata != MASK_FILL:
                valid_mask &= _nodata_mask(data, src.nodata)

            valid_data = data[valid_mask]
            total_px = data.size
            valid_px = valid_data.size

            if valid_px > 0:
                result["min"] = round(float(valid_data.min()), 4)
                result["max"] = round(float(valid_data.max()), 4)
                result["mean"] = round(float(valid_data.mean()), 4)
                result["valid_pct"] = round(
                    100.0 * valid_px / total_px, 1
                )

                row_areas = _row_area_km2(data.shape, transform)
                valid_per_row = valid_mask.sum(axis=1)
                result["area_km2"] = round(
                    float((row_areas * valid_per_row).sum()), 1
                )
            else:
                result["valid_pct"] = 0.0
                result["area_km2"] = 0.0

    except Exception as e:
        result["error"] = str(e)
        logger.warning("Error inspecting %s: %s", path.name, e)

    return result


# ===========================================================================
# LAND COVER INSPECTION (multiple tiles)
# ===========================================================================

def inspect_land_cover_tiles(
    tile_paths: List[Path],
    country_gdf: Optional[gpd.GeoDataFrame] = None,
) -> Dict[str, Any]:
    """
    Aggregate statistics across multiple ESA WorldCover tiles.

    Each tile is masked by the real country polygon. Tiles with no
    overlap are skipped automatically. Class area is computed using
    geodetic pixel area at each latitude.

    Args:
        tile_paths: List of ESA WorldCover tile paths
        country_gdf: Country polygon for masking (required for accuracy)

    Returns:
        Dictionary with n_tiles, tiles_used, class_stats, total_area_km2,
        crs_set, res_set, and any errors encountered
    """
    if country_gdf is None:
        logger.warning(
            "[land_cover] country_gdf not provided — cannot mask tiles."
        )
        return {"error": "country_gdf required for tile analysis"}

    class_areas: Dict[int, float] = {}
    total_area = 0.0
    tiles_used = 0
    tiles_skip = 0
    crs_set: set = set()
    res_set: set = set()
    errors: List[str] = []

    country_geom = country_gdf.geometry.union_all()
    country_bounds = country_gdf.total_bounds

    pbar = tqdm(
        tile_paths,
        desc="   [land_cover] Analyzing",
        unit="tile",
        leave=False,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)

        for tile in pbar:
            if hasattr(pbar, "set_postfix"):
                pbar.set_postfix({"status": tile.name[-20:]})
            try:
                with rasterio.open(str(tile)) as src:
                    t_bounds = src.bounds

                    if (
                        t_bounds[2] < country_bounds[0]
                        or t_bounds[0] > country_bounds[2]
                        or t_bounds[3] < country_bounds[1]
                        or t_bounds[1] > country_bounds[3]
                    ):
                        tiles_skip += 1
                        continue

                    tile_box_geom = box(*t_bounds)
                    intersected_geom = country_geom.intersection(tile_box_geom)

                    if intersected_geom.is_empty:
                        tiles_skip += 1
                        continue

                    crs_set.add(str(src.crs))
                    res_set.add(round(abs(src.res[0]), 8))
                    tiles_used += 1

                    row_areas_global = _row_area_km2(
                        (src.height, src.width), src.transform
                    )

                    step = 8192
                    for r in range(0, src.height, step):
                        for c in range(0, src.width, step):
                            window = rasterio.windows.Window(
                                c,
                                r,
                                min(step, src.width - c),
                                min(step, src.height - r),
                            )

                            w_bounds = src.window_bounds(window)
                            if not box(*w_bounds).intersects(
                                intersected_geom
                            ):
                                continue

                            data_block = src.read(1, window=window)
                            if not np.any(data_block > 0):
                                continue

                            win_transform = src.window_transform(window)
                            block_mask = geometry_mask(
                                [intersected_geom],
                                out_shape=data_block.shape,
                                transform=win_transform,
                                invert=True,
                            )

                            valid_mask = (data_block > 0) & block_mask
                            if not np.any(valid_mask):
                                continue

                            row_areas_win = row_areas_global[
                                window.row_off:
                                window.row_off + window.height
                            ]
                            unique_classes = np.unique(
                                data_block[valid_mask]
                            )

                            for cls in unique_classes:
                                cls_mask = (data_block == cls) & block_mask
                                cls_per_row = cls_mask.sum(axis=1)
                                area = float(
                                    (row_areas_win * cls_per_row).sum()
                                )
                                cls_int = int(cls)
                                class_areas[cls_int] = (
                                    class_areas.get(cls_int, 0.0) + area
                                )
                                total_area += area

                    del row_areas_global

            except Exception as e:
                errors.append(f"{tile.name}: {e}")

    if tiles_used == 0:
        logger.warning(
            "[land_cover] No tile overlapped with the country polygon."
        )

    class_stats: Dict[int, Dict] = {}
    for cls, area in sorted(class_areas.items()):
        pct = (
            round(100.0 * area / total_area, 2) if total_area > 0 else 0.0
        )
        class_stats[cls] = {
            "name": ESA_CLASS_NAMES.get(cls, f"Class {cls}"),
            "area_km2": round(area, 1),
            "pct": pct,
        }

    return {
        "n_tiles": len(tile_paths),
        "tiles_used": tiles_used,
        "tiles_skipped": tiles_skip,
        "crs_set": list(crs_set),
        "res_set": list(res_set),
        "class_stats": class_stats,
        "total_area_km2": round(total_area, 1),
        "errors": errors,
    }


# ===========================================================================
# POWER PLANTS INSPECTION
# ===========================================================================

def inspect_power_plants(plants_df: pd.DataFrame) -> Dict[str, Any]:
    """
    Aggregate statistics from existing power generation plants.

    Args:
        plants_df: DataFrame with power plant data

    Returns:
        Dictionary with total_plants, total_capacity_mw, by_fuel breakdown
    """
    if plants_df is None or plants_df.empty:
        return {"error": "Data not available", "total_plants": 0}

    result: Dict[str, Any] = {
        "total_plants": len(plants_df),
        "total_capacity_mw": 0.0,
        "by_fuel": {},
        "error": None,
    }
    try:
        df = plants_df.copy()
        df.columns = [c.strip().lower() for c in df.columns]

        cap_col = next(
            (
                c for c in ["capacity_mw", "capacity_in_mw"]
                if c in df.columns
            ),
            None,
        )
        if cap_col:
            result["total_capacity_mw"] = round(
                float(
                    pd.to_numeric(df[cap_col], errors="coerce").sum()
                ),
                1,
            )

        fuel_col = next(
            (
                c for c in ["primary_fuel", "fuel1", "fuel"]
                if c in df.columns
            ),
            None,
        )
        if fuel_col and cap_col:
            by_fuel = (
                df.groupby(fuel_col)[cap_col]
                .apply(
                    lambda x: pd.to_numeric(x, errors="coerce").sum()
                )
                .sort_values(ascending=False)
            )
            result["by_fuel"] = {
                str(f): round(float(c), 1)
                for f, c in by_fuel.items()
                if float(c or 0) > 0
            }
    except Exception as e:
        result["error"] = str(e)

    return result


# ===========================================================================
# CONSISTENCY DIAGNOSTICS
# ===========================================================================

def diagnose_consistency(
    raster_meta: Dict[str, Dict],
    expected_resolutions: Dict[str, float],
    res_tolerance: float,
) -> List[str]:
    """
    Check for divergent CRS and unexpected resolutions across layers.

    Args:
        raster_meta: Dictionary mapping layer names to inspection results
        expected_resolutions: Expected resolution in degrees per layer
        res_tolerance: Fractional tolerance for resolution comparison

    Returns:
        List of alert strings describing consistency issues found
    """
    alerts = []

    crs_set = {
        m["crs"]
        for m in raster_meta.values()
        if m.get("crs") and not m.get("error")
    }
    if len(crs_set) > 1:
        alerts.append(
            "DIVERGENT CRS across layers — "
            "pipeline will reproject to EPSG:4326."
        )

    for layer, meta in raster_meta.items():
        if meta.get("error") or not meta.get("resolution"):
            continue
        expected = expected_resolutions.get(layer)
        if not expected:
            continue
        ratio = meta["resolution"] / expected
        if ratio < (1 - res_tolerance) or ratio > (1 + res_tolerance):
            alerts.append(
                f"UNEXPECTED RESOLUTION [{layer}]: "
                f"{meta['resolution']:.6f}° "
                f"(expected ~{expected:.6f}°, ratio={ratio:.1f}x)"
            )

    return alerts


# ===========================================================================
# DATA AUDITOR — MAIN CLASS
# ===========================================================================

class DataAuditor:
    """
    Audit raw country data before any processing.

    Requires country_gdf for geographically accurate area calculations:
        mainland_gdf = get_mainland_gdf(gdf_border)

    Args:
        cfg: ConfigLoader instance
    """

    def __init__(self, cfg):
        """
        Initialize the data auditor.

        Args:
            cfg: ConfigLoader instance providing paths and settings
        """
        self.cfg = cfg
        self.reports_dir = cfg.base_dir / "outputs" / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)

        audit_cfg = cfg.system.get("audit", {})
        self.expected_res = audit_cfg.get(
            "expected_resolutions",
            {
                "land_cover": 0.0001,
                "solar": 0.0083,
                "wind": 0.0083,
                "elevation": 0.005,
                "slope": 0.005,
            },
        )
        self.res_tolerance = audit_cfg.get("resolution_tolerance", 0.5)

    def run(
        self,
        country_name: str,
        country_code: str,
        status: Dict[str, Any],
        elev_path: Optional[Path],
        slope_path: Optional[Path],
        pop_path: Optional[Path] = None,
        plants_df: Optional[pd.DataFrame] = None,
        country_gdf: Optional[gpd.GeoDataFrame] = None,
        skip_land_cover: bool = False,
    ) -> Dict[str, Any]:
        """
        Run full data quality audit for a country.

        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            status: Data availability dict from DataOrchestrator
            elev_path: Path to elevation raster
            slope_path: Path to slope raster
            pop_path: Path to population raster (optional)
            plants_df: Power plants DataFrame (optional)
            country_gdf: Country polygon GeoDataFrame (recommended)
            skip_land_cover: If True, skip ESA tile analysis (~96s)

        Returns:
            Audit results dictionary with rasters, land_cover,
            power_plants, alerts, summary, and timings
        """
        if country_gdf is None:
            logger.warning(
                "country_gdf not provided. Areas will cover entire file. "
                "Use: mainland_gdf = get_mainland_gdf(gdf_border)"
            )

        logger.info("Audit: %s (%s)", country_name, country_code)
        timings: Dict[str, float] = {}
        started_at = datetime.now()

        audit: Dict[str, Any] = {
            "country": country_code,
            "timestamp": started_at.isoformat(),
            "rasters": {},
            "land_cover": None,
            "power_plants": None,
            "alerts": [],
            "summary": {},
            "timings": timings,
            "skipped": [],
        }

        # Island nation detection (before any processing)
        if country_gdf is not None and detect_island_nation(country_gdf):
            msg = (
                "ISLAND NATION DETECTED: largest polygon represents "
                "< 40% of total territory. "
                "Verify that use_mainland_only=false is set in settings.yaml."
            )
            logger.warning("  %s", msg)
            audit["alerts"].append(msg)

        # Phase 1: Rasters
        wind_files = status.get("Wind", [])
        raster_map = {
            "solar": status.get("Solar"),
            "elevation": elev_path,
            "population": pop_path,
            "slope": slope_path,
            "wind": wind_files[0] if wind_files else None,
            "seismic": status.get("Seismic"),
        }

        for layer, path in raster_map.items():
            if path and Path(path).exists():
                with _timer(layer, timings):
                    audit["rasters"][layer] = inspect_raster(
                        Path(path), country_gdf=country_gdf
                    )

                    if layer == "solar":
                        solar_mean = audit["rasters"][layer].get("mean")
                        if (
                            solar_mean is not None
                            and not (1.0 < solar_mean < 10.0)
                        ):
                            msg = (
                                f"PVOUT appears to be in incorrect units "
                                f"(mean {solar_mean:.1f}). "
                                f"Expected: kWh/m2/day (~2 to 8). "
                                f"Is the file in kWh/kWp/yr?"
                            )
                            logger.error("  %s", msg)
                            audit["alerts"].append(msg)
            else:
                audit["rasters"][layer] = {"error": "File not found"}
                timings[layer] = 0.0

        # Phase 1b: Vector layers (lakes/rivers) — presence and size only
        for vname, vkey in [("lakes", "Lakes"), ("rivers", "Rivers")]:
            vpath = status.get(vkey)
            if vpath and Path(vpath).exists():
                size_mb = round(Path(vpath).stat().st_size / 1e6, 1)
                audit[vname] = {
                    "name": Path(vpath).name,
                    "size_mb": size_mb,
                    "found": True,
                }
            else:
                audit[vname] = {"found": False}
            timings[vname] = 0.0

        # Phase 2: Land Cover
        lc_tiles = status.get("Land Cover", [])
        if skip_land_cover:
            audit["land_cover"] = {"skipped": True}
            audit["skipped"].append("land_cover")
            timings["land_cover"] = 0.0
            logger.info("  [land_cover] SKIP enabled.")
        elif lc_tiles:
            with _timer("land_cover", timings):
                audit["land_cover"] = inspect_land_cover_tiles(
                    lc_tiles, country_gdf=country_gdf
                )
        else:
            audit["land_cover"] = {"error": "Tiles not found"}
            timings["land_cover"] = 0.0

        # Phase 3: Power plants
        if plants_df is not None:
            with _timer("power_plants", timings):
                audit["power_plants"] = inspect_power_plants(plants_df)
        else:
            timings["power_plants"] = 0.0

        # Phase 4: Consistency diagnostics
        audit["alerts"].extend(
            diagnose_consistency(
                audit["rasters"],
                self.expected_res,
                self.res_tolerance,
            )
        )

        # Slope threshold inactivity check
        slope_meta = audit["rasters"].get("slope", {})
        if (
            not slope_meta.get("error")
            and slope_meta.get("max") is not None
        ):
            slope_max_obs = float(slope_meta["max"])
            try:
                country_params_for_audit = self.cfg.get_country(country_code)
                slope_threshold = float(
                    country_params_for_audit.get("slope_threshold_deg", 15.0)
                )
            except Exception:
                slope_threshold = 15.0

            if slope_max_obs < slope_threshold:
                audit["alerts"].append(
                    f"INACTIVE CRITERION [slope]: "
                    f"threshold={slope_threshold:.1f}° > "
                    f"max observed slope={slope_max_obs:.2f}°. "
                    f"The slope criterion excludes NO pixels for "
                    f"{country_code}. "
                    f"Consider reducing slope_threshold_deg in "
                    f"parameters.json."
                )
                logger.warning(
                    "  SLOPE THRESHOLD INACTIVE: %.1f° > max=%.2f°. "
                    "Criterion has no effect for %s.",
                    slope_threshold,
                    slope_max_obs,
                    country_code,
                )

        audit["summary"] = self._build_summary(audit, len(wind_files))
        audit["elapsed_total"] = round(
            (datetime.now() - started_at).total_seconds(), 1
        )

        report_text = self._format_report(audit, country_name, country_code)
        print(report_text)
        saved_path = self._save_report(report_text, country_code, started_at)
        logger.info("Report saved: %s", saved_path)
        return audit

    def _build_summary(self, audit: Dict, n_wind: int) -> Dict:
        """
        Build a concise summary dictionary from audit results.

        Args:
            audit: Full audit results dictionary
            n_wind: Number of wind data files found

        Returns:
            Summary dictionary with key metrics
        """
        rasters = audit["rasters"]
        lc = audit.get("land_cover") or {}
        pp = audit.get("power_plants") or {}

        s: Dict[str, Any] = {
            "layers_ok": [],
            "layers_missing": [],
            "n_wind_files": n_wind,
            "lc_tiles_used": lc.get("tiles_used", 0),
            "lc_tiles_total": lc.get("n_tiles", 0),
            "lc_total_area_km2": lc.get("total_area_km2", 0),
            "lc_classes": len(lc.get("class_stats", {})),
            "solar_range": None,
            "elev_range": None,
            "slope_range": None,
            "pop_range": None,
            "seismic_range": None,
            "total_plants": pp.get("total_plants", 0),
            "total_cap_mw": pp.get("total_capacity_mw", 0),
            "n_alerts": len(audit.get("alerts", [])),
        }

        range_map = {
            "solar": "solar_range",
            "elevation": "elev_range",
            "slope": "slope_range",
            "population": "pop_range",
            "seismic": "seismic_range",
        }

        for layer, meta in rasters.items():
            if meta.get("error"):
                s["layers_missing"].append(layer)
            else:
                s["layers_ok"].append(layer)
                if meta.get("min") is not None and layer in range_map:
                    s[range_map[layer]] = (meta["min"], meta["max"])

        return s

    def _format_report(
        self,
        audit: Dict,
        country_name: str,
        code: str,
    ) -> str:
        """
        Format the full audit report as a human-readable text string.

        Report text uses English for end-user readability.

        Args:
            audit: Full audit results dictionary
            country_name: Full country name
            code: Country ISO code

        Returns:
            Formatted report string
        """
        lines = []
        W = 64

        def sep(c="="):
            lines.append(c * W)

        def blank():
            lines.append("")

        def row(lbl, val, ind=4):
            lines.append(f"{' '*ind}{lbl:<33}: {val}")

        sep()
        lines.append("  DATA AUDIT REPORT")
        lines.append(f"  {country_name} ({code})")
        lines.append(f"  {audit['timestamp'][:19].replace('T', ' ')}")
        if audit.get("skipped"):
            lines.append(f"  Skipped steps: {audit['skipped']}")
        sep()

        blank()
        lines.append("  RASTERS")
        sep("-")
        timings = audit.get("timings", {})

        for layer, meta in audit["rasters"].items():
            t = timings.get(layer, 0)
            if meta.get("error"):
                lines.append(
                    f"  [MISSING] {layer.upper():<12}: {meta['error']}"
                )
                continue

            lines.append(f"  [OK] {layer.upper()}  [{t:.1f}s]")
            row("File", meta.get("name", "—"))
            row("Size", f"{meta.get('size_mb', '—')} MB")
            row("CRS", meta.get("crs", "—"))
            row("Resolution", f"{meta.get('resolution', '—')}°")
            row("Mask used", meta.get("masked_by", "—"))

            if meta.get("global_shape") != meta.get("analysis_shape"):
                row("Global shape", str(meta.get("global_shape")))
                row("Shape in country", str(meta.get("analysis_shape")))
            else:
                row("Shape", str(meta.get("analysis_shape")))

            if meta.get("area_km2") is not None:
                row("Valid area", f"{meta['area_km2']:>10,.0f} km²")
            if meta.get("min") is not None:
                row("Min / Max", f"{meta['min']} / {meta['max']}")
                row("Mean", f"{meta['mean']}")
                row("Valid pixels", f"{meta['valid_pct']}%")
            blank()

        sep("-")
        lines.append("  HYDROLOGY (VECTORS)")
        sep("-")
        for vname, label in [
            ("lakes", "HydroLAKES"),
            ("rivers", "HydroRIVERS"),
        ]:
            v = audit.get(vname, {})
            if v.get("found"):
                lines.append(f"  [OK] {label}")
                row("File", v["name"])
                row(
                    "Size",
                    f"{v['size_mb']} MB  (global — crop in GridAligner)",
                )
            else:
                lines.append(f"  [MISSING] {label:<12}: not found")
        blank()

        sep("-")
        lc = audit.get("land_cover") or {}
        t_lc = timings.get("land_cover", 0)

        if lc.get("skipped"):
            lines.append("  [SKIP] LAND COVER: skipped (skip_land_cover=True)")
        elif lc.get("error"):
            lines.append(f"  [MISSING] LAND COVER: {lc['error']}")
        else:
            lines.append(
                f"  [OK] LAND COVER (ESA WorldCover)  [{t_lc:.1f}s]"
            )
            row("Total tiles", lc["n_tiles"])
            row(
                "Tiles used",
                f"{lc['tiles_used']}  "
                f"({lc.get('tiles_skipped', 0)} without overlap)",
            )
            row("CRS", ", ".join(lc.get("crs_set", [])))
            row(
                "Resolution(s)",
                ", ".join(str(r) for r in lc.get("res_set", [])),
            )
            row(
                "Total area",
                f"{lc.get('total_area_km2', 0):>10,.0f} km²",
            )
            blank()
            lines.append(
                "    Code | Class                         |"
                "    km²     |    %"
            )
            lines.append("    " + "-" * 58)
            for cls, info in sorted(lc.get("class_stats", {}).items()):
                lines.append(
                    f"    {cls:>6} | {info['name']:<31}| "
                    f"{info['area_km2']:>10,.1f} | {info['pct']:>5.1f}%"
                )
            for err in lc.get("errors", []):
                lines.append(f"  [WARNING]  {err}")

        blank()
        sep("-")
        pp = audit.get("power_plants") or {}
        t_pp = timings.get("power_plants", 0)
        lines.append(f"  EXISTING POWER PLANTS  [{t_pp:.1f}s]")
        sep("-")
        if pp.get("error") and not pp.get("total_plants"):
            lines.append(f"  [MISSING] {pp['error']}")
        else:
            row("Total plants", pp.get("total_plants", 0))
            row(
                "Total capacity",
                f"{pp.get('total_capacity_mw', 0):,.0f} MW",
            )
            if pp.get("by_fuel"):
                blank()
                lines.append("    Fuel                           |    MW")
                lines.append("    " + "-" * 38)
                for fuel, cap in list(pp["by_fuel"].items())[:12]:
                    lines.append(
                        f"    {fuel:<32}| {cap:>8,.0f}"
                    )

        blank()
        sep("-")
        lines.append("  CONSISTENCY ALERTS")
        sep("-")
        alerts = audit.get("alerts", [])
        if not alerts:
            lines.append("  [OK] No alerts — data consistent.")
        else:
            for alert in alerts:
                lines.append(f"  [WARNING]  {alert}")

        blank()
        sep()
        lines.append("  SUMMARY")
        sep("-")
        s = audit["summary"]
        row("Layers OK", ", ".join(s["layers_ok"]) or "none")
        row("Missing layers", ", ".join(s["layers_missing"]) or "none")
        row(
            "LC tiles (used)",
            f"{s['lc_tiles_used']} / {s['lc_tiles_total']}",
        )
        row("Land cover area", f"{s['lc_total_area_km2']:,.0f} km²")
        row("LC classes", s["lc_classes"])
        row("Wind files", s["n_wind_files"])

        range_labels = {
            "solar_range": ("Solar PVOUT (kWh/m²/d)", "{0} – {1}"),
            "elev_range": ("Elevation (m)", "{0:.0f} – {1:.0f}"),
            "slope_range": ("Slope (°)", "{0:.1f} – {1:.1f}"),
            "pop_range": ("Population (people/pixel)", "{0:.1f} – {1:.1f}"),
            "seismic_range": (
                "Seismicity (hazard)",
                "{0:.4f} – {1:.4f}",
            ),
        }
        for key, (label, fmt) in range_labels.items():
            if s.get(key):
                row(label, fmt.format(*s[key]))

        lakes_ok = audit.get("lakes", {}).get("found", False)
        rivers_ok = audit.get("rivers", {}).get("found", False)
        row("HydroLAKES", "[OK] found" if lakes_ok else "[--] missing")
        row(
            "HydroRIVERS",
            "[OK] found" if rivers_ok else "[--] missing",
        )
        row("Power plants", s["total_plants"])
        row("Installed capacity", f"{s['total_cap_mw']:,.0f} MW")
        row("Alerts", s["n_alerts"])

        blank()
        lines.append("  TIME PER STEP")
        sep("-")
        for step, t in timings.items():
            lines.append(f"    {step:<22}: {t:>6.1f}s")
        row("TOTAL", f"{audit.get('elapsed_total', 0):.1f}s", ind=4)
        sep()
        blank()

        return "\n".join(lines)

    def _save_report(
        self,
        text: str,
        code: str,
        ts: datetime,
    ) -> Path:
        """
        Save audit report to disk as a text file.

        Args:
            text: Formatted report text
            code: Country ISO code
            ts: Timestamp for filename

        Returns:
            Path to saved report file
        """
        p = (
            self.reports_dir
            / f"audit_{code}_{ts.strftime('%Y%m%d_%H%M%S')}.txt"
        )
        p.write_text(text, encoding="utf-8")
        return p