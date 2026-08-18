"""
tests/unit/test_data_auditor.py
================================
Regression guard for INVAR-001 (Invariant Validation Project).

Before the fix, DataAuditor.run()'s slope-threshold-inactivity check
wrapped self.cfg.get_country(country_code) in a bare `except Exception`,
silently defaulting slope_threshold to 15.0 regardless of *why*
get_country() failed -- a missing country, malformed OWA weights, or any
other broken parameters.json entry all produced the same silent
"success" result.

The fix replaces the bare except with ConfigLoader.has_country(), a
deterministic membership check, and removes exception handling around
get_country() entirely: only "no override configured" falls back to
15.0 now; any failure building an *existing* country's params (a real
pydantic ValidationError here, standing in for malformed OWA weights or
any other structurally invalid entry) propagates out of run() instead.

Two cases:
  - No override configured -> falls back to 15.0, no exception.
  - Override configured but broken -> propagates, not swallowed.
"""

import numpy as np
import pytest
import rasterio
from pydantic import ValidationError
from rasterio.transform import Affine

from src.core.schemas import TechParams
from src.processors.data_auditor import DataAuditor


class _FakeCfg:
    """
    Minimal cfg stand-in exercising only what DataAuditor.run()'s
    slope-threshold check touches: has_country()/get_country(), plus the
    base_dir/system attributes __init__ needs. Not a ConfigLoader
    subclass -- duck-typing is sufficient, same convention as
    test_results_writer.py's _FakeConfig.
    """

    def __init__(self, base_dir, configured: bool, broken: bool = False):
        self.base_dir = base_dir
        self.system = {}
        self._configured = configured
        self._broken = broken

    def has_country(self, code: str) -> bool:
        return self._configured

    def get_country(self, code: str):
        if not self._broken:
            raise AssertionError(
                "get_country() should not be reached in this scenario"
            )
        # Genuine pydantic ValidationError -- the real failure mode for a
        # malformed country entry (e.g. malformed OWA weights, or any
        # other field failing schema validation while building
        # CountryParams). Proves propagation is not tied to ConfigError
        # specifically.
        return TechParams(
            land_use_factor=-1.0,  # outside Field(..., ge=0.0, le=1.0)
            power_density_mw_km2=5.0,
            threshold=0.5,
        )


def _write_slope_tif(path, max_value):
    path.parent.mkdir(parents=True, exist_ok=True)
    height, width = 4, 4
    transform = Affine(0.01, 0.0, -9.5, 0.0, -0.01, 39.5)
    arr = np.full((height, width), 5.0, dtype=np.float32)
    arr[0, 0] = max_value
    with rasterio.open(
        str(path), "w", driver="GTiff", height=height, width=width, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(arr, 1)
    return path


def test_slope_threshold_falls_back_when_country_not_configured(tmp_path):
    """No parameters.json entry -> deterministic 15.0 fallback, no exception."""
    slope_path = _write_slope_tif(tmp_path / "slope.tif", max_value=10.0)
    cfg = _FakeCfg(base_dir=tmp_path, configured=False)
    auditor = DataAuditor(cfg)

    result = auditor.run(
        country_name="Nowhereland",
        country_code="ZZZ",
        status={},
        elev_path=None,
        slope_path=slope_path,
        skip_land_cover=True,
    )

    assert result["rasters"]["slope"]["error"] is None
    # slope max (10.0) < fallback threshold (15.0) -> inactive-criterion
    # alert fires, confirming the fallback path actually ran rather than
    # being skipped/short-circuited elsewhere.
    assert any(
        "INACTIVE CRITERION [slope]" in a for a in result["alerts"]
    )


def test_broken_country_config_propagates_instead_of_defaulting(tmp_path):
    """
    Country IS configured but get_country() fails to build (a field
    failing Pydantic validation -- the real failure mode for malformed
    OWA weights or any other structurally invalid country entry). This
    must propagate out of run(), not be swallowed into
    slope_threshold = 15.0 the way the pre-fix bare `except Exception`
    did -- this is the case that actually proves INVAR-001 is fixed.
    """
    slope_path = _write_slope_tif(tmp_path / "slope.tif", max_value=10.0)
    cfg = _FakeCfg(base_dir=tmp_path, configured=True, broken=True)
    auditor = DataAuditor(cfg)

    with pytest.raises(ValidationError):
        auditor.run(
            country_name="Brokenland",
            country_code="BRK",
            status={},
            elev_path=None,
            slope_path=slope_path,
            skip_land_cover=True,
        )
