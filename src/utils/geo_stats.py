"""
src/utils/geo_stats.py
=======================
Generic geospatial array statistics — pixel area and zonal aggregation.

Public API
----------
build_pixel_area_array() : 2-D float64 array of per-pixel areas (km²).
pixel_area_km2()         : Scalar area (km²) for a single latitude centre.
zonal_stats_raster()     : Zonal aggregation by administrative polygon.

All area calculations use the WGS84 ellipsoidal series (Helmert, 1880).
``pixel_area_km2`` is a thin scalar wrapper around the same coefficients
used by ``build_pixel_area_array``, guaranteeing numerical identity.
"""

from __future__ import annotations

import math
from typing import Optional

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio.features import rasterize
from rasterio.transform import Affine
from shapely.geometry import mapping


# ═══════════════════════════════════════════════════════════════════════════
# WGS84 COEFFICIENTS  (Helmert 1880 series — degrees → km)
# ═══════════════════════════════════════════════════════════════════════════

def _lat_km(phi_rad: float | np.ndarray) -> float | np.ndarray:
    """Km per degree of latitude at geodetic latitude *phi_rad* (radians)."""
    return (
        111_132.92
        - 559.82 * np.cos(2 * phi_rad)
        + 1.175  * np.cos(4 * phi_rad)
        - 0.0023 * np.cos(6 * phi_rad)
    ) / 1_000.0


def _lon_km(phi_rad: float | np.ndarray) -> float | np.ndarray:
    """Km per degree of longitude at geodetic latitude *phi_rad* (radians)."""
    return (
        111_412.84 * np.cos(phi_rad)
        - 93.50    * np.cos(3 * phi_rad)
        + 0.118    * np.cos(5 * phi_rad)
    ) / 1_000.0


# ═══════════════════════════════════════════════════════════════════════════
# PIXEL AREA — public API
# ═══════════════════════════════════════════════════════════════════════════

def build_pixel_area_array(
    transform: Affine, height: int, width: int,
) -> np.ndarray:
    """Per-pixel area array (km²) via WGS84 ellipsoidal series.

    Rows share the same area value; the result is broadcast to
    ``(height, width)`` with O(height) memory.

    Args:
        transform: Affine geotransform of the raster.
        height:    Raster height in pixels.
        width:     Raster width in pixels.

    Returns:
        Float64 array of shape (height, width) with per-pixel areas in km².
    """
    phi = np.radians(
        transform.f + transform.e * (np.arange(height) + 0.5)
    )
    row_areas = abs(transform.e) * _lat_km(phi) * abs(transform.a) * _lon_km(phi)
    return np.broadcast_to(row_areas.reshape(-1, 1), (height, width)).copy()


def pixel_area_km2(transform: Affine, lat_center: float) -> float:
    """Scalar pixel area (km²) for a single latitude centre.

    Uses the same WGS84 coefficients as :func:`build_pixel_area_array`,
    so a scalar computed here is numerically identical to the corresponding
    row value of that array.

    Args:
        transform:   Affine geotransform of the raster.
        lat_center:  Pixel-centre latitude in decimal degrees.

    Returns:
        Pixel area in km².
    """
    phi = math.radians(lat_center)
    return (
        abs(transform.e) * _lat_km(phi)
        * abs(transform.a) * _lon_km(phi)
    )


# ═══════════════════════════════════════════════════════════════════════════
# ZONAL STATISTICS
# ═══════════════════════════════════════════════════════════════════════════

def zonal_stats_raster(
    value_arr: np.ndarray,
    valid_mask: np.ndarray,
    admin_gdf: Optional[gpd.GeoDataFrame],
    transform: Affine,
    crs: str,
    value_name: str = "value",
    sort_by: Optional[str] = None,
) -> pd.DataFrame:
    """Aggregate values within administrative polygons.

    Args:
        value_arr:  Raster of values to aggregate (e.g. capacity MW/px).
        valid_mask: Boolean mask — True for pixels to include.
        admin_gdf:  Administrative boundary GeoDataFrame.
        transform:  Affine geotransform of *value_arr*.
        crs:        CRS string for reprojection of *admin_gdf*.
        value_name: Prefix for output column names.
        sort_by:    Column suffix to sort by (``"sum"`` or ``"mean"``).
                    Defaults to ``"sum"`` (appropriate for additive quantities).

    Returns:
        DataFrame with per-region aggregates; empty if *admin_gdf* is None.
    """
    if admin_gdf is None or admin_gdf.empty:
        return pd.DataFrame()

    try:
        gdf = admin_gdf.to_crs(crs).copy()
    except Exception:
        gdf = admin_gdf.copy()

    gdf = gdf.reset_index(drop=True)
    height, width = value_arr.shape

    admin_mask = rasterize(
        ((mapping(geom), int(i)) for i, geom in enumerate(gdf.geometry)),
        out_shape=(height, width),
        transform=transform,
        fill=-1,
        dtype=np.int32,
        all_touched=False,
    )

    records = []
    for i, row in gdf.iterrows():
        combined = (admin_mask == i) & valid_mask & np.isfinite(value_arr)
        n = int(combined.sum())
        if n == 0:
            continue
        vals = value_arr[combined]
        records.append({
            "admin_name":          str(row.get("_admin_name", row.get("NAME_1", ""))),
            f"{value_name}_sum":   float(vals.sum()),
            f"{value_name}_mean":  float(vals.mean()),
            f"{value_name}_count": n,
            f"{value_name}_p90":   float(np.percentile(vals, 90)),
        })

    df = pd.DataFrame(records)
    if df.empty:
        return df

    sort_col = f"{value_name}_{sort_by or 'sum'}"
    if sort_col not in df.columns:
        sort_col = f"{value_name}_mean"
    return df.sort_values(sort_col, ascending=False).reset_index(drop=True)