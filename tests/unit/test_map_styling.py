"""
tests/unit/test_map_styling.py
===============================
Regression guard for BLOCKER-013 (Suitability rendering crash for
large countries).

Before the fix, GeoWorldStyler.render_raster_map() downsampled `score`
via PIL whenever a country's grid exceeded max_display_px (1200,
unconfigured in settings.yaml so the fallback always applies), but never
resized `exclude_mask` to match -- excl_rgba[exclude_mask, :3] then
indexed a boolean mask still at native resolution against an array
built from the smaller, downsampled score, raising an unhandled
IndexError that aborted the whole pipeline run from Phase 3 onward.
5 of 7 countries in parameters.json exceed the threshold; only
Portugal-scale countries never triggered the downsample branch at all.

This test exercises the real downsample branch with a synthetic
raster above the threshold -- no pipeline run needed -- and checks
both halves of the fix: exclude_mask's resampled shape matches the
also-resampled score, and the overlay stays strictly binary (nearest-
neighbor, not blended) rather than just happening to be the right
shape.
"""

import numpy as np
import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from rasterio.transform import Affine
from shapely.geometry import box

from src.utils.map_styling import GeoWorldStyler


def test_exclude_mask_downsample_matches_score_shape_and_stays_boolean(
    monkeypatch,
):
    """
    Grid exceeds the default max_raster_display_px (1200) in both
    dimensions, forcing the downsample branch exclude_mask previously
    didn't participate in.
    """
    H, W = 1500, 1600
    exclude_color = (170, 170, 170, 191)

    styler = GeoWorldStyler(settings_vis={"layout": {}, "colors": {}}, global_dpi=72)

    rng = np.random.default_rng(0)
    score = rng.random((H, W)).astype(np.float32)
    exclude_mask = score <= 0.05

    transform = Affine(0.01, 0.0, -50.0, 0.0, -0.01, 10.0)
    mainland_gdf = gpd.GeoDataFrame(
        {"geometry": [box(-50.0, 10.0 - 0.01 * H, -50.0 + 0.01 * W, 10.0)]},
        crs="EPSG:4326",
    )

    captured = {}
    real_imshow = Axes.imshow

    def spy_imshow(self, arr, *args, **kwargs):
        arr_np = np.asarray(arr)
        if "excl_rgba" not in captured and arr_np.ndim == 3 and arr_np.shape[-1] == 4:
            captured["excl_rgba"] = arr_np
        elif "score_arr" not in captured and arr_np.ndim == 2:
            captured["score_arr"] = arr_np
        return real_imshow(self, arr, *args, **kwargs)

    monkeypatch.setattr(Axes, "imshow", spy_imshow)

    fig = styler.render_raster_map(
        score=score,
        transform=transform,
        crs="EPSG:4326",
        mainland_gdf=mainland_gdf,
        context_gdf=None,
        exclude_mask=exclude_mask,
        exclude_color=exclude_color,
        out_path=None,
    )
    plt.close(fig)

    assert "excl_rgba" in captured, "exclusion overlay was never rendered"
    assert "score_arr" in captured, "score raster was never rendered"

    excl_rgba = captured["excl_rgba"]
    score_arr = captured["score_arr"]

    # The fix's core assertion: exclude_mask's resampled shape must
    # match the shape score actually ended up at after downsampling --
    # this is exactly the pair that mismatched pre-fix.
    assert score_arr.shape != (H, W), "test raster did not exceed max_display_px"
    assert excl_rgba.shape[:2] == score_arr.shape

    # Strictly boolean after resampling: the alpha channel may only be
    # 0 (not excluded) or exactly exclude_color's alpha (excluded) --
    # a continuous/bilinear resize would blend intermediate values in
    # at exclusion boundaries instead.
    alpha_values = set(np.unique(excl_rgba[..., 3]).tolist())
    assert alpha_values <= {0, exclude_color[3]}
