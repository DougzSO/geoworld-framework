"""
src/utils/raster_io.py
======================
Spatial Georaster I/O and diagnostics helpers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import rasterio
from rasterio.transform import Affine


logger = logging.getLogger("geoworld.utils.raster_io")


def find_suitability_tif(
    suitability_dir: Path,
    tech: str,
    country_code: str,
    scenario: str = "balanced",
    allow_owa_fallback: bool = True,
) -> Optional[Path]:
    """
    Locate the Phase 3 suitability GeoTIFF for *tech* (single source of
    truth for TOPSIS-vs-OWA raster discovery — BLOCKER-006).

    Phase 3 produces two surfaces per technology:
      - TOPSIS (primary):   {CODE}_{tech}_suitability.tif
      - OWA, per scenario:  {CODE}_{tech}_suitability_owa_{scenario}.tif

    TOPSIS is the primary suitability surface end-to-end (D4,
    docs/memory/09-decisions.md); OWA is a secondary/comparative surface
    and must never silently take precedence. This function always tries
    TOPSIS first, regardless of *allow_owa_fallback*.

    Args:
        suitability_dir:    Phase 3 output directory (tif subfolder).
        tech:                Technology key ("solar", "wind", "biomass").
        country_code:        ISO-3166 alpha-3 code.
        scenario:            OWA scenario to fall back to, if enabled.
        allow_owa_fallback:  If False, only TOPSIS candidates are tried
            (caller wants a strict TOPSIS-or-nothing lookup, e.g. Phase 4
            and sensitivity threshold/parameter sweeps, which have never
            used OWA and should keep failing closed if TOPSIS is absent).

    Returns:
        Path to the resolved TIF, or None if nothing matched.
    """
    candidates: List[Path] = [
        suitability_dir / f"{country_code}_{tech}_suitability.tif",
        suitability_dir / f"{country_code.lower()}_{tech}_suitability.tif",
    ]
    if allow_owa_fallback:
        candidates += [
            suitability_dir / f"{country_code}_{tech}_suitability_owa_{scenario}.tif",
            suitability_dir
            / f"{country_code.lower()}_{tech}_suitability_owa_{scenario}.tif",
        ]

    for path in candidates:
        if path.exists():
            logger.debug("  [%s] suitability TIF: %s", tech, path.name)
            return path

    # Recursive, exact-stem fallback for unexpected subfolder layouts.
    # Uses an exact stem match rather than a `*suitability*.tif` wildcard,
    # which would also match OWA files and could silently return one via
    # filesystem iteration order (the precedence bug BLOCKER-006 fixes).
    topsis_stem = f"{country_code}_{tech}_suitability"
    match = next(
        (p for p in suitability_dir.rglob("*.tif") if p.stem == topsis_stem),
        None,
    )
    if match is not None:
        logger.debug("  [%s] suitability TIF (stem match): %s", tech, match.name)
        return match

    if allow_owa_fallback:
        owa_stem = f"{country_code}_{tech}_suitability_owa_{scenario}"
        match = next(
            (p for p in suitability_dir.rglob("*.tif") if p.stem == owa_stem),
            None,
        )
        if match is not None:
            logger.debug(
                "  [%s] suitability TIF (OWA stem match): %s", tech, match.name
            )
            return match

    return None


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