"""
src/core/validators.py
======================
Validation utilities for raster data and inter-phase contracts.

All public functions operate on paths (not open dataset handles) to avoid
descriptor leaks. AlignedLayers is imported from schemas, not models.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import rasterio
from rasterio.crs import CRS

from src.core.schemas import AlignedLayers

logger = logging.getLogger("geoworld.core.validators")


class ValidationError(Exception):
    """Raised when a validation check fails."""


# ---------------------------------------------------------------------------
# Raster-level validators
# ---------------------------------------------------------------------------


def validate_raster_exists(path: Optional[Path], layer_name: str) -> Path:
    """
    Verify that a raster file exists on disk.

    Args:
        path: File path to check.
        layer_name: Human-readable layer identifier for error messages.

    Returns:
        Resolved Path object.

    Raises:
        ValidationError: If path is None or file does not exist.
    """
    if path is None or not Path(path).exists():
        raise ValidationError(
            f"Required raster '{layer_name}' not found at {path}"
        )
    return Path(path)


def validate_raster_crs(
    path: Path,
    expected_crs: Optional[Union[str, CRS]] = None,
) -> str:
    """
    Open a raster, read its CRS, and optionally compare against expected.

    Args:
        path: Raster file path.
        expected_crs: CRS string or rasterio.CRS to compare against.

    Returns:
        CRS string of the raster.

    Raises:
        ValidationError: If raster has no CRS or CRS does not match.
    """
    with rasterio.open(path) as src:
        crs = src.crs

    if crs is None:
        raise ValidationError(f"Raster {path} has no CRS.")

    if expected_crs is not None:
        if isinstance(expected_crs, str):
            expected_crs = CRS.from_string(expected_crs)
        if crs != expected_crs:
            raise ValidationError(
                f"Raster {path}: CRS {crs} does not match expected {expected_crs}."
            )

    return crs.to_string()


def validate_raster_shape(
    path: Path,
    expected_shape: Tuple[int, int],
) -> None:
    """
    Verify that a raster has the expected (height, width) dimensions.

    Args:
        path: Raster file path.
        expected_shape: (height, width) tuple.

    Raises:
        ValidationError: If dimensions do not match.
    """
    with rasterio.open(path) as src:
        actual = (src.height, src.width)

    if actual != expected_shape:
        raise ValidationError(
            f"Raster {path}: shape {actual} != expected {expected_shape}."
        )


def validate_raster_values(
    path: Path,
    min_val: Optional[float] = None,
    max_val: Optional[float] = None,
    nodata: Optional[float] = None,
    finite_required: float = 0.01,
) -> Dict[str, Any]:
    """
    Check pixel value range and verify a minimum fraction of finite pixels.

    NoData pixels (from raster metadata or the nodata argument) and extreme
    negative sentinels (< -1e30) are excluded before all checks.

    Args:
        path: Raster file path.
        min_val: Minimum allowed value for finite pixels (optional).
        max_val: Maximum allowed value for finite pixels (optional).
        nodata: Override NoData value; if None, read from raster metadata.
        finite_required: Minimum fraction of finite pixels required [0–1].

    Returns:
        Statistics dict with keys: min, max, mean, std, finite_fraction.

    Raises:
        ValidationError: If finite pixel fraction is below threshold or
                         values are outside [min_val, max_val].
    """
    with rasterio.open(path) as src:
        arr = src.read(1).astype(np.float32)
        file_nodata = src.nodata

    nd = nodata if nodata is not None else file_nodata
    if nd is not None:
        arr = np.where(arr == nd, np.nan, arr)
    arr = np.where(arr < -1e30, np.nan, arr)

    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        raise ValidationError(f"Raster {path} has no finite values.")

    frac = finite.size / arr.size
    if frac < finite_required:
        raise ValidationError(
            f"Raster {path}: only {frac:.2%} finite pixels "
            f"(required {finite_required:.2%})."
        )

    if min_val is not None and finite.min() < min_val:
        raise ValidationError(
            f"Raster {path}: min {finite.min():.4f} < allowed {min_val}."
        )
    if max_val is not None and finite.max() > max_val:
        raise ValidationError(
            f"Raster {path}: max {finite.max():.4f} > allowed {max_val}."
        )

    return {
        "min":            float(finite.min()),
        "max":            float(finite.max()),
        "mean":           float(finite.mean()),
        "std":            float(finite.std()),
        "finite_fraction": float(frac),
    }


# ---------------------------------------------------------------------------
# Phase-level validators
# ---------------------------------------------------------------------------


def validate_aligned_layers(
    aligned: Union[Dict[str, Optional[Path]], AlignedLayers],
    required_layers: List[str],
    expected_shape: Tuple[int, int],
    expected_crs: str = "EPSG:4326",
    check_values: bool = False,
) -> AlignedLayers:
    """
    Validate that required aligned layers exist with correct CRS and shape.

    Accepts either a plain dict (from GridAligner output) or an existing
    AlignedLayers instance.  Always returns a validated AlignedLayers.

    Args:
        aligned: Dict of layer_name → Path, or AlignedLayers instance.
        required_layers: Layer names that must be present and valid.
        expected_shape: (height, width) all rasters must share.
        expected_crs: CRS string all rasters must share (default EPSG:4326).
        check_values: If True, also verify non-negative pixel values for
                      non-elevation layers.

    Returns:
        Validated AlignedLayers instance.

    Raises:
        ValidationError: If any required layer fails existence, CRS, shape,
                         or value checks.
    """
    # Normalise input to AlignedLayers
    if isinstance(aligned, dict):
        aligned_obj = AlignedLayers.from_dict(aligned)
    else:
        aligned_obj = aligned

    for layer in required_layers:
        path = aligned_obj.get(layer)
        validate_raster_exists(path, layer)
        validate_raster_crs(path, expected_crs)
        validate_raster_shape(path, expected_shape)
        if check_values and layer != "elevation":
            validate_raster_values(path, min_val=0.0)

    return aligned_obj


# ---------------------------------------------------------------------------
# Result-level validators
# ---------------------------------------------------------------------------


def validate_result_object(obj: Any, model_class: Any) -> Any:
    """
    Ensure obj is an instance of model_class, coercing from dict if needed.

    Args:
        obj: Object to validate.
        model_class: Expected Pydantic model class.

    Returns:
        Validated model instance.

    Raises:
        ValidationError: If obj is neither an instance of model_class nor
                         a dict that can be coerced into one.
    """
    if isinstance(obj, model_class):
        return obj
    if isinstance(obj, dict):
        try:
            return model_class(**obj)
        except Exception as exc:
            raise ValidationError(
                f"Cannot coerce dict to {model_class.__name__}: {exc}"
            ) from exc
    raise ValidationError(
        f"Expected {model_class.__name__}, got {type(obj).__name__}."
    )