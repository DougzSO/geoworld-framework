"""
src/utils/raster_io.py
======================
Spatial Georaster I/O and diagnostics helpers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

import numpy as np
import rasterio
from rasterio.transform import Affine


logger = logging.getLogger("geoworld.utils.raster_io")


def get_raster_meta(path: Union[str, Path]) -> Tuple[Tuple[int, int], str]:
    """Retrieves basic boundary dimensions and coordinate system projection."""
    with rasterio.open(str(path)) as src:
        return (src.height, src.width), str(src.crs)


def load_reference_meta(
    criteria_dir: Path,
    country_code: str,
) -> Tuple[Affine, str, int, int]:
    """Loads standardized grid affine transform, CRS and grid size."""
    for preferred in ("solar_resource", "wind_resource", "terrain_score"):
        for path in criteria_dir.glob("*.tif"):
            if preferred in path.stem.lower():
                with rasterio.open(str(path)) as src:
                    return src.transform, str(src.crs), src.height, src.width

    tif_files = sorted(criteria_dir.glob("*.tif"))
    if not tif_files:
        raise FileNotFoundError(f"No GeoTIFF criteria files in {criteria_dir}.")
    with rasterio.open(str(tif_files[0])) as src:
        return src.transform, str(src.crs), src.height, src.width


def load_all_criteria(
    criteria_dir: Path,
    code: str,
    height: int,
    width: int,
) -> Dict[str, np.ndarray]:
    """Reads all float criteria rasters and normalises value ranges to [0, 1]."""
    criteria: Dict[str, np.ndarray] = {}
    for path in criteria_dir.glob("*.tif"):
        try:
            with rasterio.open(str(path)) as src:
                arr = src.read(1).astype(np.float32)
                if src.nodata is not None:
                    arr[arr == src.nodata] = np.nan

            if arr.shape != (height, width):
                logger.warning(
                    "  [raster_io] Shape mismatch for %s. Skipped.", path.name
                )
                continue

            arr[arr < 0.0] = np.nan
            finite_mask = np.isfinite(arr)

            if finite_mask.any():
                arr_max = float(arr[finite_mask].max())
                if arr_max > 1.0:
                    arr_min = float(arr[finite_mask].min())
                    rng     = arr_max - arr_min
                    if rng > 0.0:
                        arr = (arr - arr_min) / rng
                    else:
                        arr[finite_mask] = 0.0

            arr = np.clip(arr, 0.0, 1.0)
            arr[~finite_mask] = np.nan
            clean_name = path.stem.lower().replace(f"{code.lower()}_", "")
            criteria[clean_name] = arr

        except Exception as exc:
            logger.warning("  [raster_io] Bypassed raster %s: %s", path.name, exc)

    return criteria


def load_aux_raster(
    path: Optional[Path],
    height: int,
    width: int,
) -> Optional[np.ndarray]:
    """Loads auxiliary files (DEM, LC) preserving unscaled native ranges."""
    if not path or not Path(path).exists():
        return None
    try:
        with rasterio.open(str(path)) as src:
            arr = src.read(1).astype(np.float32)
            if src.nodata is not None:
                arr[arr == src.nodata] = np.nan
            if arr.shape != (height, width):
                return None
            return arr
    except Exception:
        return None