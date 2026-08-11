"""
tests/unit/test_sensitivity_plots.py
======================================
Smoke tests for src/utils/sensitivity_plots.py (Phase 8 plotting).

Scope: confirm each plotting function runs end-to-end with representative
data and produces a non-empty PNG file. Not visual regression testing —
this module had zero test coverage before REFACTOR-001, so these are a
minimal safety net, not exhaustive rendering checks.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.utils.map_styling import GeoWorldStyler
from src.utils.sensitivity_plots import (
    plot_dashboard,
    plot_sa1_heatmap,
    plot_sa1_tornado,
    plot_sa2_cv,
    plot_sa3_threshold,
    plot_sa4_lcoe,
    plot_sa5_sobol,
    plot_sa6_potential,
)


@pytest.fixture
def styler() -> GeoWorldStyler:
    return GeoWorldStyler({})


def _assert_png_written(path) -> None:
    assert path.exists()
    assert path.stat().st_size > 0
    with open(path, "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n"


def test_plot_sa1_tornado_smoke(styler, tmp_path):
    df = pd.DataFrame({
        "criterion": ["a", "a", "b", "b"],
        "perturbation_pct": [-10.0, 10.0, -10.0, 10.0],
        "weight_base": [0.5, 0.5, 0.5, 0.5],
        "spearman_rho": [0.98, 0.97, 0.90, 0.88],
    })
    out = tmp_path / "sa1_tornado.png"
    plot_sa1_tornado(styler, df, "solar", out)
    _assert_png_written(out)


def test_plot_sa1_heatmap_smoke(styler, tmp_path):
    df = pd.DataFrame({
        "criterion": ["a", "a", "b", "b"],
        "perturbation_pct": [-10.0, 10.0, -10.0, 10.0],
        "spearman_rho": [0.98, 0.97, 0.90, 0.88],
    })
    out = tmp_path / "sa1_heatmap.png"
    plot_sa1_heatmap(styler, df, "solar", out)
    _assert_png_written(out)


def test_plot_sa2_cv_smoke(styler, tmp_path):
    cv = np.array([0.1, 0.2, 0.3, 0.15])
    ci = np.array([0.05, 0.08, 0.09, 0.07])
    out = tmp_path / "sa2_cv.png"
    plot_sa2_cv(styler, cv, ci, "solar", out)
    _assert_png_written(out)


def test_plot_sa3_threshold_smoke(styler, tmp_path):
    df = pd.DataFrame({
        "threshold": [0.5, 0.6, 0.7],
        "potential_gw": [10.0, 8.0, 6.0],
        "elasticity": [np.nan, -0.5, -0.6],
    })
    out = tmp_path / "sa3_threshold.png"
    plot_sa3_threshold(styler, df, "solar", 0.6, out)
    _assert_png_written(out)


def test_plot_sa4_lcoe_smoke(styler, tmp_path):
    df = pd.DataFrame({"lcoe_usd_mwh": np.random.default_rng(1).normal(50, 5, 200)})
    df.attrs["stats"] = {
        "capex_nominal": 760, "opex_nominal": 13, "cf_nominal": 0.19,
        "lcoe_p05": 40, "lcoe_p50": 50, "lcoe_p95": 60,
    }
    out = tmp_path / "sa4_lcoe.png"
    plot_sa4_lcoe(styler, df, "solar", out)
    _assert_png_written(out)


def test_plot_sa5_sobol_smoke(styler, tmp_path):
    df = pd.DataFrame({
        "parameter": ["a", "b"],
        "S1": [0.3, 0.1], "ST": [0.35, 0.15],
        "S1_conf": [0.02, 0.01], "ST_conf": [0.02, 0.01],
    })
    out = tmp_path / "sa5_sobol.png"
    plot_sa5_sobol(styler, df, out)
    _assert_png_written(out)


def test_plot_sa6_potential_smoke(styler, tmp_path):
    df = pd.DataFrame({
        "parameter": ["power_density_mw_km2"] * 3 + ["capacity_factor"] * 3,
        "perturbation_pct": [-30, 0, 30] * 2,
        "delta_gw_pct": [-20, 0, 20, -15, 0, 15],
        "delta_twh_pct": [-25, 0, 25, -18, 0, 18],
    })
    out = tmp_path / "sa6_potential.png"
    plot_sa6_potential(styler, df, "solar", out)
    _assert_png_written(out)


def test_plot_dashboard_smoke_with_no_data(styler, tmp_path):
    """Dashboard must degrade gracefully (each panel prints '<SA-x> not
    executed') when no SA results are available for the technology --
    this is the real-world shape whenever a run_sa* flag is disabled."""
    out = tmp_path / "dashboard.png"
    plot_dashboard(styler, {}, "solar", "Portugal", out)
    _assert_png_written(out)
