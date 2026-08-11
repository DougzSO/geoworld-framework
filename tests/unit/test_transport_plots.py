"""
tests/unit/test_transport_plots.py
=====================================
Smoke tests for src/utils/transport_plots.py (Phase 9 plotting).

Scope: confirm each plotting function runs end-to-end with representative
data and produces a non-empty PNG file. Not visual regression testing —
this module had zero test coverage before REFACTOR-002, so these are a
minimal safety net, not exhaustive rendering checks.
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, Polygon

from src.utils.map_styling import GeoWorldStyler
from src.utils.transport_plots import (
    plot_emissions_trajectory,
    plot_fleet_transition,
    plot_hub_map,
    plot_renewable_need,
)

_YEARS = list(range(2025, 2051, 5))
_SCENARIOS = ("reference", "accelerated", "conservative")


@pytest.fixture
def styler() -> GeoWorldStyler:
    return GeoWorldStyler({})


def _assert_png_written(path) -> None:
    assert path.exists()
    assert path.stat().st_size > 0
    with open(path, "rb") as f:
        assert f.read(8) == b"\x89PNG\r\n\x1a\n"


def test_plot_emissions_trajectory_smoke(styler, tmp_path):
    rows = [
        {
            "scenario": sc, "year": y,
            "co2_total_mtco2eq": 10.0, "co2_baseline_mtco2eq": 15.0,
            "co2_avoided_mtco2eq": 5.0,
        }
        for sc in _SCENARIOS for y in _YEARS
    ]
    out = tmp_path / "emissions.png"
    plot_emissions_trajectory(styler, pd.DataFrame(rows), "Portugal", "PRT", out)
    _assert_png_written(out)


def test_plot_fleet_transition_smoke(styler, tmp_path):
    rows = [
        {
            "scenario": sc, "year": y, "total_fleet": 1000,
            "stock_ice": 600, "stock_hev": 100, "stock_phev": 100,
            "stock_bev": 150, "stock_fcev": 50,
        }
        for sc in _SCENARIOS for y in _YEARS
    ]
    out = tmp_path / "fleet.png"
    plot_fleet_transition(styler, pd.DataFrame(rows), "Portugal", "PRT", out)
    _assert_png_written(out)


def test_plot_fleet_transition_skips_when_reference_scenario_missing(styler, tmp_path):
    """The function returns early (no file written) when the 'reference'
    scenario is absent -- this is real, documented early-return behavior,
    not an error path."""
    df = pd.DataFrame([{
        "scenario": "accelerated", "year": 2025, "total_fleet": 1000,
        "stock_ice": 600, "stock_hev": 100, "stock_phev": 100,
        "stock_bev": 150, "stock_fcev": 50,
    }])
    out = tmp_path / "fleet_empty.png"
    plot_fleet_transition(styler, df, "Portugal", "PRT", out)
    assert not out.exists()


def test_plot_renewable_need_smoke(styler, tmp_path):
    rows = [
        {
            "scenario": sc, "year": y,
            "ev_electricity_demand_twh": 5.0, "re_needed_total_gw": 2.0,
            "annual_capex_bn_usd": 0.5, "abatement_cost_usd_tco2": -10.0,
            "re_avail_solar_gw": 20.0, "re_avail_wind_gw": 5.0,
        }
        for sc in _SCENARIOS for y in _YEARS
    ]
    out = tmp_path / "renewable.png"
    plot_renewable_need(styler, pd.DataFrame(rows), "Portugal", "PRT", out)
    _assert_png_written(out)


def test_plot_hub_map_smoke(styler, tmp_path):
    mainland = gpd.GeoDataFrame(
        {"geometry": [Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])]}, crs="EPSG:4326"
    )
    hubs = gpd.GeoDataFrame({
        "geometry": [Point(0.3, 0.3), Point(0.6, 0.6)],
        "suitability_score": [0.8, 0.6],
        "hub_radius_km": [10.0, 10.0],
        "capex_usd": [500_000.0, 500_000.0],
        "primary_source": ["Solar", "Wind"],
    }, crs="EPSG:4326")
    out = tmp_path / "hubs.png"
    plot_hub_map(styler, None, hubs, mainland, None, "Portugal", "PRT", None, out)
    _assert_png_written(out)
