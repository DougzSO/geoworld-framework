"""
src/processors/grid_aligner.py
==============================
Phase 2a: Spatial Harmonization and Grid Alignment.

Reprojects all raw heterogeneous geospatial datasets (rasters and
vectors) into a unified reference grid defined by the target
geometry's spatial extent.

Scientific constraints:
    - Enforces strict topological matching (same Affine transform,
      dimensions, CRS).
    - Calculates Euclidean distances using WGS84 ellipsoid isotropic
      approximations.
    - Executes AHP matrix aggregation for multi-height wind resource
      harmonization.
"""

from __future__ import annotations

import json
import logging
import time
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import rasterio
import scipy.ndimage as ndimage
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import reproject
from shapely.geometry import mapping
from shapely.strtree import STRtree

from src.core.constants import (
    AHP_RANDOM_INDEX,
    NODATA_FLOAT,
    NODATA_UINT8,
    WIND_AHP_MATRIX,
    WIND_HEIGHT_KEYS,
)
from src.utils.utils import safe_raster_open, safe_raster_write
from src.utils.timing import timer as _timer
from src.utils.logging_utils import gdal_quiet

logger = logging.getLogger("geoworld.processors.GridAligner")


# ===========================================================================
# DATA CLASSES & CORE GEOMETRY
# ===========================================================================

@dataclass
class GridContext:
    """
    Encapsulates the spatial reference grid.

    Prevents data clumps in function signatures by grouping all
    grid parameters into a single coherent object.

    Attributes:
        transform: Rasterio affine transform
        width: Grid width in pixels
        height: Grid height in pixels
        crs: Coordinate reference system string
        country_mask: Boolean array masking the country polygon
    """
    transform: rasterio.Affine
    width: int
    height: int
    crs: str
    country_mask: np.ndarray


def build_reference_grid(
    mainland_geometry: gpd.GeoDataFrame,
    resolution_deg: float,
) -> GridContext:
    """
    Construct the master spatial reference grid from target geometry.

    Ensures pixel-perfect alignment by snapping bounds to resolution
    multiples.

    Args:
        mainland_geometry: GeoDataFrame with the target country geometry
        resolution_deg: Grid resolution in decimal degrees

    Returns:
        GridContext with aligned transform, dimensions, and country mask
    """
    minx, miny, maxx, maxy = mainland_geometry.total_bounds

    minx = np.floor(minx / resolution_deg) * resolution_deg
    miny = np.floor(miny / resolution_deg) * resolution_deg
    maxx = np.ceil(maxx / resolution_deg) * resolution_deg
    maxy = np.ceil(maxy / resolution_deg) * resolution_deg

    width = int(round((maxx - minx) / resolution_deg))
    height = int(round((maxy - miny) / resolution_deg))
    crs = "EPSG:4326"
    transform = transform_from_bounds(minx, miny, maxx, maxy, width, height)

    logger.info(
        "  Grid Extent: %dx%d px | res=%.4f° | "
        "bounds=[%.3f, %.3f, %.3f, %.3f]",
        height, width, resolution_deg,
        minx, miny, maxx, maxy,
    )

    shapes = [
        (mapping(geom), 1) for geom in mainland_geometry.geometry
    ]
    country_mask = rasterize(
        shapes,
        out_shape=(height, width),
        transform=transform,
        fill=0,
        dtype=np.uint8,
        all_touched=False,
    ).astype(bool)

    return GridContext(transform, width, height, crs, country_mask)


def _load_and_clip_vector_data(
    shp_path: Path,
    mainland_geometry: gpd.GeoDataFrame,
    label: str,
    buffer_deg: float = 0.05,
) -> Optional[gpd.GeoDataFrame]:
    """
    Load, clip, and harmonize vector data to the country bounding box.

    Reduces I/O latency and memory overhead for large shapefiles by
    applying a bounding-box spatial filter before loading.

    Args:
        shp_path: Path to the source shapefile
        mainland_geometry: Country GeoDataFrame for bounds reference
        label: Descriptive label for logging
        buffer_deg: Bounding box buffer in degrees (default: 0.05)

    Returns:
        Clipped GeoDataFrame in country CRS, or None if empty or failed
    """
    try:
        minx, miny, maxx, maxy = mainland_geometry.total_bounds
        bbox = (
            minx - buffer_deg,
            miny - buffer_deg,
            maxx + buffer_deg,
            maxy + buffer_deg,
        )

        logger.info(
            "    [%s] Executing spatial bounding box query...", label
        )
        vector_data = gpd.read_file(shp_path, bbox=bbox)

        if vector_data.empty:
            return None

        if vector_data.crs != mainland_geometry.crs:
            vector_data = vector_data.to_crs(mainland_geometry.crs)

        logger.info(
            "    [%s] %d topological segments isolated.",
            label,
            len(vector_data),
        )
        return vector_data
    except Exception as e:
        logger.error(
            "    [%s] Driver error during vector subsetting: %s",
            label, e,
        )
        return None


# ===========================================================================
# SPATIAL REPROJECTION & HARMONIZATION
# ===========================================================================

def _reproject_to_grid(
    src_path: Path,
    out_path: Path,
    grid: GridContext,
    resampling: Resampling = Resampling.bilinear,
    nodata_out: float = NODATA_FLOAT,
    dtype_out: str = "float32",
) -> Path:
    """
    Reproject a source raster into the GridContext reference frame.

    Args:
        src_path: Path to the source raster
        out_path: Path for the reprojected output raster
        grid: Target GridContext
        resampling: Rasterio resampling algorithm (default: bilinear)
        nodata_out: NoData value for output raster
        dtype_out: Output data type (float32 or uint8)

    Returns:
        Path to the reprojected output raster
    """
    if dtype_out == "uint8":
        data_out = np.zeros(
            (grid.height, grid.width), dtype=np.uint8
        )
        nd_out = int(nodata_out) if nodata_out else 0
    else:
        data_out = np.full(
            (grid.height, grid.width), nodata_out, dtype=np.float32
        )
        nd_out = nodata_out

    with safe_raster_open(src_path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=data_out,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            resampling=resampling,
            src_nodata=src.nodata,
            dst_nodata=nd_out,
        )

    data_out[~grid.country_mask] = nd_out

    profile = dict(
        driver="GTiff",
        dtype=dtype_out,
        width=grid.width,
        height=grid.height,
        count=1,
        crs=grid.crs,
        transform=grid.transform,
        nodata=nd_out,
        compress="lzw",
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )
    with safe_raster_write(out_path, **profile) as dst:
        dst.write(data_out, 1)

    return out_path


# ===========================================================================
# RESOURCE-SPECIFIC COMPUTATIONS
# ===========================================================================

def _compute_ahp_weights(
    matrix: np.ndarray,
) -> Tuple[np.ndarray, float]:
    """
    Compute AHP weights via the Principal Eigenvector method.

    Args:
        matrix: Saaty pairwise comparison matrix (n x n)

    Returns:
        Tuple of (normalized weight vector, Consistency Ratio)
    """
    col_sum = matrix.sum(axis=0)
    weights = (matrix / col_sum).mean(axis=1)
    lam_max = float((matrix @ weights / weights).mean())
    n = matrix.shape[0]
    ci = (lam_max - n) / (n - 1)
    ri = AHP_RANDOM_INDEX.get(n, 1.12)
    rc = ci / ri if ri > 0 else 0.0
    return weights, rc


def _combine_wind_layers(
    wind_paths: List[Path],
    out_path: Path,
    grid: GridContext,
) -> Path:
    """
    Aggregate multiple wind height models using AHP-derived weights.

    Maps available wind rasters to height keys (200m, 100m, 50m).
    If all three heights are present, uses AHP matrix weights. Falls
    back to uniform weights if only a subset is available or if the
    consistency ratio exceeds 0.10.

    Args:
        wind_paths: List of wind raster paths at various heights
        out_path: Output path for combined wind raster
        grid: Target GridContext

    Returns:
        Path to the combined wind output raster
    """
    mapped: Dict[str, Path] = {}
    for p in wind_paths:
        name = p.name.lower()
        for key in WIND_HEIGHT_KEYS:
            if key in name:
                mapped[key] = p
                break
        else:
            if WIND_HEIGHT_KEYS[1] not in mapped:
                mapped[WIND_HEIGHT_KEYS[1]] = p

    present = [k for k in WIND_HEIGHT_KEYS if k in mapped]

    if len(present) == 3:
        matrix = np.array(WIND_AHP_MATRIX, dtype=np.float64)
        w_arr, rc = _compute_ahp_weights(matrix)
        weight_map = dict(zip(WIND_HEIGHT_KEYS, w_arr))
        if rc > 0.10:
            logger.warning(
                "  RC=%.3f > 0.10 — Falling back to uniform weights.",
                rc,
            )
            weight_map = {k: 1 / len(present) for k in present}
    else:
        weight_map = {k: 1.0 / len(present) for k in present}

    combined = np.zeros((grid.height, grid.width), dtype=np.float64)
    weight_acc = np.zeros(
        (grid.height, grid.width), dtype=np.float64
    )

    for key in present:
        layer = np.full(
            (grid.height, grid.width), NODATA_FLOAT, dtype=np.float32
        )
        with safe_raster_open(mapped[key]) as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=layer,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=grid.transform,
                dst_crs=grid.crs,
                resampling=Resampling.bilinear,
                src_nodata=src.nodata,
                dst_nodata=NODATA_FLOAT,
            )
        valid = (
            (layer != NODATA_FLOAT)
            & np.isfinite(layer)
            & (layer >= 0)
        )
        w = weight_map[key]
        combined[valid] += layer[valid].astype(np.float64) * w
        weight_acc[valid] += w

    result = np.full(
        (grid.height, grid.width), NODATA_FLOAT, dtype=np.float32
    )
    has = weight_acc > 0
    result[has] = (combined[has] / weight_acc[has]).astype(np.float32)
    result[~grid.country_mask] = NODATA_FLOAT

    profile = dict(
        driver="GTiff",
        dtype="float32",
        width=grid.width,
        height=grid.height,
        count=1,
        crs=grid.crs,
        transform=grid.transform,
        nodata=NODATA_FLOAT,
        compress="lzw",
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )
    with safe_raster_write(out_path, **profile) as dst:
        dst.write(result, 1)

    return out_path


def _mosaic_land_cover(
    lc_tiles: List[Path],
    out_path: Path,
    grid: GridContext,
    mainland_geometry: gpd.GeoDataFrame,
) -> Optional[Path]:
    """
    Build ESA WorldCover mosaic from multiple tiles.

    Reprojects and merges tiles onto the reference grid using
    nearest-neighbour resampling. Tiles with no overlap are skipped.

    Args:
        lc_tiles: List of ESA WorldCover tile paths
        out_path: Output path for the mosaicked raster
        grid: Target GridContext
        mainland_geometry: Country GeoDataFrame for bounds check

    Returns:
        Path to mosaicked land cover raster, or None if no tiles used
    """
    lc_out = np.zeros((grid.height, grid.width), dtype=np.uint8)
    bounds_main = tuple(
        float(b) for b in mainland_geometry.total_bounds
    )
    used = skipped = 0

    for tile in lc_tiles:
        try:
            with rasterio.open(str(tile)) as src:
                tile_bounds = tuple(src.bounds)
                if (
                    tile_bounds[2] < bounds_main[0]
                    or tile_bounds[0] > bounds_main[2]
                    or tile_bounds[3] < bounds_main[1]
                    or tile_bounds[1] > bounds_main[3]
                ):
                    skipped += 1
                    continue

                tile_reprojected = np.zeros(
                    (grid.height, grid.width), dtype=np.uint8
                )
                reproject(
                    source=rasterio.band(src, 1),
                    destination=tile_reprojected,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=grid.transform,
                    dst_crs=grid.crs,
                    resampling=Resampling.nearest,
                    src_nodata=0,
                    dst_nodata=0,
                    warp_mem_limit=1024,
                )
                mask_fill = (tile_reprojected > 0) & (lc_out == 0)
                lc_out[mask_fill] = tile_reprojected[mask_fill]
                used += 1
        except Exception:
            skipped += 1

    if used == 0:
        return None

    lc_out[~grid.country_mask] = NODATA_UINT8

    profile = dict(
        driver="GTiff",
        dtype="uint8",
        width=grid.width,
        height=grid.height,
        count=1,
        crs=grid.crs,
        transform=grid.transform,
        nodata=NODATA_UINT8,
        compress="lzw",
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )
    with safe_raster_write(out_path, **profile) as dst:
        dst.write(lc_out, 1)

    return out_path


# ===========================================================================
# GEODESIC DISTANCE TRANSFORMS
# ===========================================================================

def _calculate_wgs84_isotropic_distance(
    feature_mask_inv: np.ndarray,
    grid: GridContext,
) -> np.ndarray:
    """
    Compute Euclidean distance with WGS84 ellipsoid correction.

    Converts pixel-space distance transform to kilometres using
    Bowring equations for the WGS84 ellipsoid at the grid centroid,
    avoiding the computational cost of full UTM reprojection.

    Scale factors (metres per degree) at latitude phi:
        Lat: 111132.92 - 559.82*cos(2*phi) + 1.175*cos(4*phi)
        Lon: 111412.84*cos(phi) - 93.5*cos(3*phi) + 0.118*cos(5*phi)

    Args:
        feature_mask_inv: Binary array where 0=feature, 1=background
        grid: GridContext for transform and dimensions

    Returns:
        Float32 array of distances in kilometres
    """
    dist_pixels = ndimage.distance_transform_edt(feature_mask_inv)

    lat_center = (
        grid.transform.f + grid.transform.e * (grid.height / 2)
    )
    lat_rad = math.radians(lat_center)

    lat_km_deg = (
        111132.92
        - 559.82 * math.cos(2 * lat_rad)
        + 1.175 * math.cos(4 * lat_rad)
    ) / 1000.0
    lon_km_deg = (
        111412.84 * math.cos(lat_rad)
        - 93.50 * math.cos(3 * lat_rad)
        + 0.118 * math.cos(5 * lat_rad)
    ) / 1000.0

    px_scale_km = math.sqrt(
        abs(grid.transform.a) * lon_km_deg
        * abs(grid.transform.e) * lat_km_deg
    )
    return (dist_pixels * px_scale_km).astype(np.float32)


def _align_linear_features(
    shp_path: Path,
    out_path: Path,
    mainland_geometry: gpd.GeoDataFrame,
    grid: GridContext,
    label: str = "features",
    max_dist_km: float = 100.0,
) -> Optional[Path]:
    """
    Rasterize linear features and compute geographic distance raster.

    Args:
        shp_path: Path to the source shapefile
        out_path: Output path for the distance raster
        mainland_geometry: Country GeoDataFrame
        grid: Target GridContext
        label: Feature type label for logging
        max_dist_km: Maximum distance to encode in kilometres

    Returns:
        Path to output distance raster, or None if failed
    """
    vector_data = _load_and_clip_vector_data(
        shp_path, mainland_geometry, label, buffer_deg=0.01
    )
    if vector_data is None:
        return None

    try:
        tree = STRtree(vector_data.geometry.values)
        intersect_indices = tree.query(
            mainland_geometry.union_all(), predicate="intersects"
        )
        if len(intersect_indices) == 0:
            return None

        gdf_intersect = vector_data.iloc[intersect_indices]
        tolerance = abs(grid.transform.a) * 0.5
        shapes = [
            (mapping(g), 1)
            for g in gdf_intersect.geometry.simplify(tolerance)
            if not g.is_empty
        ]

        feature_mask = rasterize(
            shapes=shapes,
            out_shape=(grid.height, grid.width),
            transform=grid.transform,
            fill=0,
            all_touched=True,
            dtype=np.uint8,
        )
        feature_mask = (
            feature_mask & grid.country_mask.astype(np.uint8)
        )

        dist_km = _calculate_wgs84_isotropic_distance(
            (feature_mask == 0).astype(np.uint8), grid
        )
        dist_km = np.clip(dist_km, 0, max_dist_km)
        dist_km[~grid.country_mask] = NODATA_FLOAT

        profile = dict(
            driver="GTiff",
            dtype="float32",
            width=grid.width,
            height=grid.height,
            count=1,
            crs=grid.crs,
            transform=grid.transform,
            nodata=NODATA_FLOAT,
            compress="lzw",
            tiled=True,
            blockxsize=256,
            blockysize=256,
        )
        with safe_raster_write(out_path, **profile) as dst:
            dst.write(dist_km, 1)

        return out_path
    except Exception as e:
        logger.error(
            "    [%s] Topology processing failed: %s", label, e
        )
        return None


def _align_lakes(
    shp_path: Path,
    out_path: Path,
    mainland_geometry: gpd.GeoDataFrame,
    grid: GridContext,
) -> Optional[Path]:
    """
    Rasterize inland water bodies as binary hard-exclusion masks.

    Args:
        shp_path: Path to the lakes shapefile
        out_path: Output path for the lake mask raster
        mainland_geometry: Country GeoDataFrame
        grid: Target GridContext

    Returns:
        Path to output mask raster, or None if no lakes found
    """
    lakes_data = _load_and_clip_vector_data(
        shp_path, mainland_geometry, "lakes"
    )
    if lakes_data is None:
        return None

    shapes = [
        (mapping(g), 1)
        for g in lakes_data.geometry
        if g and not g.is_empty
    ]
    if not shapes:
        return None

    lake_mask = rasterize(
        shapes=shapes,
        out_shape=(grid.height, grid.width),
        transform=grid.transform,
        fill=0,
        all_touched=True,
        dtype=np.uint8,
    )
    lake_mask[~grid.country_mask] = NODATA_UINT8

    profile = dict(
        driver="GTiff",
        dtype="uint8",
        width=grid.width,
        height=grid.height,
        count=1,
        crs=grid.crs,
        transform=grid.transform,
        nodata=NODATA_UINT8,
        compress="lzw",
    )
    with safe_raster_write(out_path, **profile) as dst:
        dst.write(lake_mask, 1)
    return out_path


def _align_rivers(
    shp_path: Path,
    out_path: Path,
    mainland_geometry: gpd.GeoDataFrame,
    grid: GridContext,
) -> Optional[Path]:
    """
    Rasterize river networks and compute geodesic distance boundaries.

    Args:
        shp_path: Path to the rivers shapefile
        out_path: Output path for the river distance raster
        mainland_geometry: Country GeoDataFrame
        grid: Target GridContext

    Returns:
        Path to output distance raster, or None if no rivers found
    """
    rivers_data = _load_and_clip_vector_data(
        shp_path, mainland_geometry, "rivers"
    )
    if rivers_data is None:
        return None

    tolerance = abs(grid.transform.a) * 0.5
    shapes = [
        (mapping(g), 1)
        for g in rivers_data.geometry.simplify(tolerance)
        if g and not g.is_empty
    ]
    river_mask = rasterize(
        shapes=shapes,
        out_shape=(grid.height, grid.width),
        transform=grid.transform,
        fill=0,
        all_touched=True,
        dtype=np.uint8,
    )

    dist_km = _calculate_wgs84_isotropic_distance(
        (river_mask == 0).astype(np.uint8), grid
    )
    dist_km = np.clip(dist_km, 0, 50)
    dist_km[~grid.country_mask] = NODATA_FLOAT

    profile = dict(
        driver="GTiff",
        dtype="float32",
        width=grid.width,
        height=grid.height,
        count=1,
        crs=grid.crs,
        transform=grid.transform,
        nodata=NODATA_FLOAT,
        compress="lzw",
    )
    with safe_raster_write(out_path, **profile) as dst:
        dst.write(dist_km, 1)
    return out_path


# ===========================================================================
# MASTER CLASS ORCHESTRATION
# ===========================================================================

class GridAligner:
    """
    Phase 2a orchestrator for spatial harmonization and grid alignment.

    Reprojects all raw datasets into a unified reference grid and
    caches results to disk for downstream phases.

    Args:
        cfg: ConfigLoader instance
        processed_dir: Root directory for processed outputs
    """

    def __init__(self, cfg, processed_dir: Path):
        """
        Initialize GridAligner with configuration.

        Args:
            cfg: ConfigLoader instance
            processed_dir: Root directory for processed outputs
        """
        self.cfg = cfg
        self.processed_dir = Path(processed_dir)
        self.res_cfg = cfg.geospatial.get("resolutions", {})
        self.base_res = self.res_cfg.get("suitability", "adaptive")
        self.adaptive_cfg = self.res_cfg.get(
            "adaptive",
            {
                "target_pixels": 50000,
                "min_deg": 0.001,
                "max_deg": 0.05,
            },
        )
        self.resolution = 0.01

    def run(
        self,
        country_code: str,
        mainland_gdf: gpd.GeoDataFrame,
        **kwargs,
    ) -> Dict[str, Optional[Path]]:
        """
        Run full grid alignment for all available datasets.

        Computes adaptive resolution if configured, builds the
        reference grid, and reprojects each dataset. Previously
        aligned files are loaded from cache if dimensions match.

        Args:
            country_code: ISO-3166-alpha-3 code
            mainland_gdf: Mainland geometry GeoDataFrame
            **kwargs: Dataset paths — elev_path, slope_path,
                      solar_path, wind_paths, lc_tiles, pop_path,
                      grid_path, roads_path, lakes_path, rivers_path,
                      seismic_path, plants_df

        Returns:
            Dictionary mapping layer names to aligned raster paths
        """
        out_dir = self.processed_dir / country_code
        out_dir.mkdir(parents=True, exist_ok=True)

        if str(self.base_res).lower() == "adaptive":
            minx, miny, maxx, maxy = mainland_gdf.total_bounds
            lat_mid_rad = math.radians((miny + maxy) / 2.0)
            lat_km = (
                111132.92
                - 559.82 * math.cos(2 * lat_mid_rad)
            ) / 1000.0
            lon_km = (
                111412.84 * math.cos(lat_mid_rad)
                - 93.50 * math.cos(3 * lat_mid_rad)
            ) / 1000.0
            area_km2 = (
                (maxx - minx) * lon_km * (maxy - miny) * lat_km
            )
            target_px = self.adaptive_cfg.get("target_pixels", 50000)

            computed_res = math.sqrt(area_km2 / target_px) / math.sqrt(
                lat_km * lon_km
            )
            self.resolution = float(
                np.clip(
                    computed_res,
                    self.adaptive_cfg.get("min_deg", 0.001),
                    self.adaptive_cfg.get("max_deg", 0.05),
                )
            )
        else:
            self.resolution = float(self.base_res)

        grid = build_reference_grid(mainland_gdf, self.resolution)
        aligned: Dict[str, Optional[Path]] = {}
        timings: Dict[str, float] = {}

        def _path(suffix: str) -> Path:
            return out_dir / f"{country_code}_{suffix}_aligned.tif"

        def _exists(p: Optional[Path]) -> bool:
            return bool(p and Path(p).exists())

        def _execute_or_load(
            label: str,
            fn,
            condition: bool = True,
        ):
            p = _path(label)
            if p.exists():
                with safe_raster_open(p) as _src:
                    if (
                        (_src.height, _src.width)
                        == (grid.height, grid.width)
                    ):
                        logger.info(
                            "    %s: cached.", label
                        )
                        return p
                p.unlink()
            return fn() if condition else None

        with _timer("elevation", timings), gdal_quiet():
            aligned["elevation"] = _execute_or_load(
                "elevation",
                lambda: _reproject_to_grid(
                    kwargs.get("elev_path"),
                    _path("elevation"),
                    grid,
                ),
                _exists(kwargs.get("elev_path")),
            )

        with _timer("slope", timings), gdal_quiet():
            aligned["slope"] = _execute_or_load(
                "slope",
                lambda: _reproject_to_grid(
                    kwargs.get("slope_path"),
                    _path("slope"),
                    grid,
                ),
                _exists(kwargs.get("slope_path")),
            )

        with _timer("solar", timings), gdal_quiet():
            aligned["solar"] = _execute_or_load(
                "solar",
                lambda: _reproject_to_grid(
                    kwargs.get("solar_path"),
                    _path("solar"),
                    grid,
                ),
                _exists(kwargs.get("solar_path")),
            )

        with _timer("wind", timings), gdal_quiet():
            aligned["wind"] = _execute_or_load(
                "wind",
                lambda: _combine_wind_layers(
                    kwargs.get("wind_paths", []),
                    _path("wind"),
                    grid,
                ),
                bool(kwargs.get("wind_paths")),
            )

        with _timer("land_cover", timings):
            aligned["land_cover"] = _execute_or_load(
                "land_cover",
                lambda: _mosaic_land_cover(
                    kwargs.get("lc_tiles", []),
                    _path("lc"),
                    grid,
                    mainland_gdf,
                ),
                bool(kwargs.get("lc_tiles")),
            )

        with _timer("population", timings), gdal_quiet():
            aligned["population"] = _execute_or_load(
                "population",
                lambda: _reproject_to_grid(
                    kwargs.get("pop_path"),
                    _path("population"),
                    grid,
                ),
                _exists(kwargs.get("pop_path")),
            )

        with _timer("grid_distance", timings):
            aligned["grid"] = _execute_or_load(
                "grid",
                lambda: _align_linear_features(
                    kwargs.get("grid_path"),
                    _path("grid"),
                    mainland_gdf,
                    grid,
                    "grid",
                ),
                _exists(kwargs.get("grid_path")),
            )

        with _timer("roads", timings):
            aligned["roads"] = _execute_or_load(
                "roads",
                lambda: _align_linear_features(
                    kwargs.get("roads_path"),
                    _path("roads"),
                    mainland_gdf,
                    grid,
                    "roads",
                ),
                _exists(kwargs.get("roads_path")),
            )

        with _timer("lakes", timings):
            aligned["lakes"] = _execute_or_load(
                "lakes",
                lambda: _align_lakes(
                    kwargs.get("lakes_path"),
                    _path("lakes"),
                    mainland_gdf,
                    grid,
                ),
                _exists(kwargs.get("lakes_path")),
            )

        with _timer("rivers", timings):
            aligned["rivers"] = _execute_or_load(
                "rivers",
                lambda: _align_rivers(
                    kwargs.get("rivers_path"),
                    _path("rivers"),
                    mainland_gdf,
                    grid,
                ),
                _exists(kwargs.get("rivers_path")),
            )

        with _timer("seismic", timings), gdal_quiet():
            aligned["seismic"] = _execute_or_load(
                "seismic",
                lambda: _reproject_to_grid(
                    kwargs.get("seismic_path"),
                    _path("seismic"),
                    grid,
                    resampling=Resampling.bilinear,
                ),
                _exists(kwargs.get("seismic_path")),
            )

        self._save_grid_metadata(country_code, out_dir, grid)
        self._verify_alignment(aligned, grid)
        return aligned

    def _save_grid_metadata(
        self,
        code: str,
        out_dir: Path,
        grid: GridContext,
    ) -> None:
        """
        Save grid parameters to JSON for reproducibility.

        Args:
            code: Country ISO code
            out_dir: Output directory
            grid: GridContext with spatial parameters
        """
        meta = {
            "country_code": code,
            "crs": grid.crs,
            "resolution_deg": self.resolution,
            "width": grid.width,
            "height": grid.height,
            "transform": list(grid.transform)[:6],
            "n_valid_pixels": int(grid.country_mask.sum()),
        }
        (out_dir / f"{code}_grid_metadata.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )

    def _verify_alignment(
        self,
        aligned: Dict[str, Optional[Path]],
        grid: GridContext,
    ) -> bool:
        """
        Verify topological congruence of all aligned rasters.

        Checks that every output raster matches the reference grid
        dimensions before yielding to the next pipeline phase.

        Args:
            aligned: Dictionary of aligned layer paths
            grid: Reference GridContext

        Returns:
            True if all rasters pass the check

        Raises:
            RuntimeError: If any raster dimensions do not match the grid
        """
        for name, path in aligned.items():
            if path and Path(path).exists():
                with safe_raster_open(path) as src:
                    if (src.height, src.width) != (
                        grid.height, grid.width
                    ):
                        raise RuntimeError(
                            f"Topology mismatch: {name} failed "
                            f"grid harmonization."
                        )
        logger.info(
            "  All geometries mapped to congruent matrix "
            "shape=(%d, %d)",
            grid.height,
            grid.width,
        )
        return True