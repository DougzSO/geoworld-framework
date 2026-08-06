"""
src/utils/utils.py
==================
Shared geospatial utilities for the GeoWorld pipeline.

Provides:
  - get_module_logger()           : Convenience wrapper for named loggers.
  - safe_raster_open()            : Context manager for safe raster reading.
  - safe_raster_write()           : Context manager for safe raster writing.
  - reproject_to_target()         : Reproject a raster to a target grid.
  - get_intersecting_files()      : Find rasters intersecting a bounding box.
  - is_geographic_crs()           : Check if a CRS is geographic.
  - get_local_utm_crs()           : Compute local UTM EPSG from geometry centroid.
  - get_mainland_bounds()         : Extract mainland bounds from a GeoDataFrame.
  - compute_pixel_area_geodesic() : Compute per-pixel area using WGS84 ellipsoid.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union, Dict

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject

from src.core.constants import NODATA_FLOAT
from src.utils.geo_stats import build_pixel_area_array as _build_pixel_area_array

logger = logging.getLogger("geoworld.utils")


def get_module_logger(name: str) -> logging.Logger:
    """
    Return a named logger.

    Args:
        name: Logger name (typically __name__ of the calling module).

    Returns:
        Configured Logger instance.
    """
    return logging.getLogger(name)


@contextmanager
def safe_raster_open(file_path: Union[str, Path]) -> Any:
    """
    Open a raster file safely with automatic closing.

    Args:
        file_path: Path to the raster file.

    Yields:
        Open rasterio DatasetReader.

    Raises:
        FileNotFoundError: If the raster file does not exist.
    """
    src = None

    try:
        file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(
                "Raster not found: %s",
                file_path
            )

        src = rasterio.open(str(file_path))
        yield src

    finally:
        if src is not None:
            src.close()


@contextmanager
def safe_raster_write(file_path: Union[str, Path], **kwargs: Any) -> Any:
    """
    Open a raster file for writing safely with automatic closing.

    Applies LZW compression and tiling by default.

    Args:
        file_path: Destination path for the raster file.
        **kwargs: Additional keyword arguments passed to rasterio.open().

    Yields:
        Open rasterio DatasetWriter.
    """
    dst = None
    kwargs.setdefault("compress", "lzw")
    kwargs.setdefault("tiled", True)

    try:
        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        dst = rasterio.open(str(file_path), "w", **kwargs)
        yield dst

    finally:
        if dst is not None:
            dst.close()


def reproject_to_target(
    src_path: Union[str, Path],
    target_transform: rasterio.Affine,
    target_crs: str,
    target_shape: Tuple[int, int],
    resampling: Resampling = Resampling.bilinear,
    src_nodata: Optional[float] = None,
    dst_nodata: float = NODATA_FLOAT,
) -> np.ndarray:
    """
    Reproject a raster to a specific target grid.

    Args:
        src_path: Path to the source raster file.
        target_transform: Affine transform of the target grid.
        target_crs: CRS of the target grid (e.g., 'EPSG:4326').
        target_shape: Shape (height, width) of the target grid.
        resampling: Resampling algorithm (default: bilinear).
        src_nodata: Source nodata value. If None, read from source metadata.
        dst_nodata: Destination nodata value (default: NODATA_FLOAT).

    Returns:
        Reprojected array of shape target_shape, dtype float32.
    """
    data = np.full(target_shape, dst_nodata, dtype=np.float32)

    with safe_raster_open(src_path) as src:
        if src_nodata is None:
            src_nodata = src.nodata

        reproject(
            source=rasterio.band(src, 1),
            destination=data,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=target_transform,
            dst_crs=target_crs,
            resampling=resampling,
            src_nodata=src_nodata,
            dst_nodata=dst_nodata,
        )

    return data


def get_intersecting_files(
    directory: Path,
    bounds: Tuple[float, float, float, float],
    pattern: str = "*.tif",
) -> List[Path]:
    """
    Find rasters in a directory that intersect the given bounding box.

    Args:
        directory: Directory to search for raster files.
        bounds: Bounding box as (minx, miny, maxx, maxy).
        pattern: Glob pattern for raster file discovery (default: '*.tif').

    Returns:
        List of paths to intersecting raster files.
    """
    directory = Path(directory)
    intersecting = []

    for f in directory.glob(pattern):
        if "InputQuality" in f.name:
            continue

        try:
            with safe_raster_open(f) as src:
                if not rasterio.coords.disjoint_bounds(src.bounds, bounds):
                    intersecting.append(f)

        except (rasterio.errors.RasterioIOError, Exception) as exc:
            logger.debug(
                "Failed to read bbox of %s: %s",
                f.name,
                exc
            )
            continue

    return intersecting


def is_geographic_crs(crs: Any) -> bool:
    """
    Check whether a CRS is geographic (e.g., WGS84).

    Args:
        crs: CRS object or string to evaluate.

    Returns:
        True if the CRS is geographic, False otherwise.
    """
    if crs is None:
        return False

    try:
        return crs.is_geographic
    except Exception:
        return "4326" in str(crs)


def get_local_utm_crs(geometry: Any) -> str:
    """
    Compute the local UTM EPSG code from the centroid of a geometry.

    Args:
        geometry: Shapely geometry, GeoSeries, or GeoDataFrame.

    Returns:
        EPSG code string for the local UTM zone (e.g., 'EPSG:32629').
    """
    if isinstance(geometry, gpd.GeoDataFrame):
        geometry = (
            geometry.geometry.union_all()
            if hasattr(geometry.geometry, "union_all")
            else geometry.geometry.unary_union
        )

    if not isinstance(geometry, gpd.GeoSeries):
        geometry = gpd.GeoSeries([geometry], crs="EPSG:4326")

    centroid = (
        geometry.union_all().centroid
        if hasattr(geometry, "union_all")
        else geometry.unary_union.centroid
    )

    lon = centroid.x
    lat = centroid.y

    utm_zone = int((lon + 180) // 6) + 1
    hemisphere = 326 if lat >= 0 else 327

    return f"EPSG:{hemisphere}{utm_zone:02d}"


def get_mainland_bounds(
    gdf: gpd.GeoDataFrame,
    buffer_percent: float = 0.05,
) -> Tuple[float, float, float, float]:
    """
    Return bounding box of the mainland (largest polygon) with a margin.

    Projects to local UTM to identify the largest geometry by area,
    then returns bounds in EPSG:4326 with a percentage buffer applied.

    Args:
        gdf: GeoDataFrame of country geometry.
        buffer_percent: Fractional buffer to add around the bounds.

    Returns:
        Tuple of (minx, miny, maxx, maxy) in EPSG:4326.
    """
    union_geom = (
        gdf.geometry.union_all()
        if hasattr(gdf.geometry, "union_all")
        else gdf.geometry.unary_union
    )
    temp_crs = get_local_utm_crs(union_geom)

    gdf_proj = gdf.to_crs(temp_crs)
    exploded = gdf_proj.explode(index_parts=False).reset_index(drop=True)
    largest_idx = exploded.geometry.area.idxmax()

    largest_geom_proj = exploded.iloc[[largest_idx]].to_crs("EPSG:4326")
    minx, miny, maxx, maxy = largest_geom_proj.total_bounds

    width = maxx - minx
    height = maxy - miny

    return (
        minx - width * buffer_percent,
        miny - height * buffer_percent,
        maxx + width * buffer_percent,
        maxy + height * buffer_percent,
    )


def compute_pixel_area_geodesic(
    transform: rasterio.Affine,
    width: int,
    height: int,
) -> np.ndarray:
    """Per-pixel area (km²) array — delegates to geo_stats canonical impl.

    Deprecated:
        Import :func:`src.utils.geo_stats.build_pixel_area_array` directly.
        This wrapper exists only for backward compatibility with call sites
        that import from ``src.utils.utils``.

    Args:
        transform: Affine transform of the raster grid.
        width:     Number of columns in the raster grid.
        height:    Number of rows in the raster grid.

    Returns:
        Float64 array of shape (height, width) with pixel areas in km².
    """
    return _build_pixel_area_array(transform, height, width)

def collect_directory_files(
    directory: Path,
    patterns: Optional[list] = None,
) -> Dict[str, str]:
    """
    Scan a directory and return a mapping of relative paths to absolute paths.
    
    Args:
        directory: Directory to scan
        patterns: List of glob patterns (default: ['*.tif', '*.json', '*.pkl'])
        
    Returns:
        Dict mapping relative_path -> absolute_path
    """
    directory = Path(directory)
    if not directory.exists():
        return {}
    
    if patterns is None:
        patterns = ['*.tif', '*.json', '*.pkl', '*.csv', '*.txt']
    
    files = {}
    for pattern in patterns:
        for file_path in directory.rglob(pattern):
            if file_path.is_file():
                rel_path = str(file_path.relative_to(directory))
                files[rel_path] = str(file_path)
    
    return files


def ensure_output_dirs(base_dir: Path, subdirs: list) -> Dict[str, Path]:
    """
    Create multiple subdirectories safely.
    
    Args:
        base_dir: Base output directory
        subdirs: List of subdirectory names to create
        
    Returns:
        Dict mapping subfolder_name -> Path
    """
    resolved = {}
    for sub in subdirs:
        p = Path(base_dir) / sub
        p.mkdir(parents=True, exist_ok=True)
        resolved[sub] = p
    return resolved