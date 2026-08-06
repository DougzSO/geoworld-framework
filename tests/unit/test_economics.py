"""
tests/unit/test_economics.py
=============================
Unit tests for src/utils/economics.py (LCOE, CRF, supply curve, CF bounds).

Scope: pure-function behaviour of the financial core, anchored where
possible in the real Portugal (PRT) technology parameters and their
CHECKPOINT-1-validated printed values (checkpoint-1-full-run tag,
2026-08-06 full-run report). No country fixtures, no I/O
(see QI-002 for integration-level tests).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.utils.economics import (
    capital_recovery_factor,
    compute_cf_bounds,
    compute_lcoe,
    compute_supply_curve,
    extract_supply_curve_thresholds,
    modulate_capacity_factor,
)


# ─────────────────────────────────────────────────────────────────────────
# capital_recovery_factor
# ─────────────────────────────────────────────────────────────────────────

def test_crf_standard_case():
    # CRF = r(1+r)^n / [(1+r)^n - 1]
    crf = capital_recovery_factor(0.06, 25)
    assert crf == pytest.approx(0.078227, abs=1e-6)


def test_crf_zero_rate_is_simple_reciprocal_of_lifetime():
    assert capital_recovery_factor(0.0, 25) == pytest.approx(1.0 / 25.0)


def test_crf_negative_rate_falls_back_to_simple_reciprocal():
    """rate <= 0 is an explicit guarded branch: no discounting."""
    assert capital_recovery_factor(-0.01, 25) == pytest.approx(1.0 / 25.0)


def test_crf_lifetime_floor_is_one():
    """lifetime is floored at 1 via max(lifetime, 1) in the rate<=0 branch."""
    assert capital_recovery_factor(0.0, 0) == pytest.approx(1.0)


# ─────────────────────────────────────────────────────────────────────────
# compute_lcoe
# ─────────────────────────────────────────────────────────────────────────

def test_compute_lcoe_zero_capacity_factor_is_nan():
    assert np.isnan(compute_lcoe(760, 13, 0.0643, 0.0))


def test_compute_lcoe_negative_capacity_factor_is_nan():
    assert np.isnan(compute_lcoe(760, 13, 0.0643, -0.1))


def test_compute_lcoe_zero_crf_is_nan():
    assert np.isnan(compute_lcoe(760, 13, 0.0, 0.19))


def test_compute_lcoe_formula_matches_manual_calculation():
    # LCOE = (CAPEX*CRF + OPEX) / (CF * hours_year) * 1000
    capex, opex, crf, cf = 760.0, 13.0, 0.0643, 0.19
    expected = (capex * crf + opex) / (cf * 8760) * 1000.0
    assert compute_lcoe(capex, opex, crf, cf) == pytest.approx(expected)


@pytest.mark.parametrize(
    "tech,capex,opex,rate,lifetime,cf,expected_base_lcoe",
    [
        # CHECKPOINT-1 full run (PRT), 2026-08-06, tag checkpoint-1-full-run:
        # "[<tech>] CRF=... | base_cf=... | base_lcoe=... USD/MWh" log lines,
        # reproduced here purely from real PRT technology parameters through
        # capital_recovery_factor() + compute_lcoe() (no synthetic fixture).
        ("solar", 760.0, 13.0, 0.06, 25, 0.190, 43.5),
        ("wind", 1360.0, 44.0, 0.06, 25, 0.300, 57.2),
        ("biomass", 2720.0, 109.0, 0.07, 20, 0.730, 57.2),
    ],
)
def test_compute_lcoe_regression_checkpoint1_prt_base_lcoe(
    tech, capex, opex, rate, lifetime, cf, expected_base_lcoe
):
    crf = capital_recovery_factor(rate, lifetime)
    lcoe = compute_lcoe(capex, opex, crf, cf)
    assert round(lcoe, 1) == expected_base_lcoe, (
        f"{tech}: expected CHECKPOINT-1 base LCOE {expected_base_lcoe}, got {round(lcoe, 1)}"
    )


# ─────────────────────────────────────────────────────────────────────────
# modulate_capacity_factor
# ─────────────────────────────────────────────────────────────────────────

def test_modulate_capacity_factor_docstring_example():
    base_cf = 0.19
    source = np.array([[0.5, 1.0], [1.5, np.nan]])
    out = modulate_capacity_factor(base_cf, source, 1.0, 0.0, 0.95)
    assert out[0, 0] == pytest.approx(0.095, abs=1e-6)
    assert out[0, 1] == pytest.approx(0.19, abs=1e-6)
    assert out[1, 0] == pytest.approx(0.285, abs=1e-6)
    assert np.isnan(out[1, 1])


def test_modulate_capacity_factor_nonpositive_mean_returns_base_everywhere():
    """source_mean <= 0 is an explicit guarded branch."""
    source = np.array([[0.5, 1.0], [1.5, np.nan]])
    out = modulate_capacity_factor(0.19, source, 0.0, 0.0, 0.95)
    assert np.allclose(out, 0.19)


def test_modulate_capacity_factor_clamps_to_ceiling_and_floor():
    source = np.array([10.0, 0.001])
    out = modulate_capacity_factor(0.5, source, 1.0, 0.1, 0.9)
    assert out[0] == pytest.approx(0.9)   # clamped at ceiling
    assert out[1] == pytest.approx(0.1)   # clamped at floor


def test_modulate_capacity_factor_preserves_nan():
    source = np.array([np.nan, 0.5])
    out = modulate_capacity_factor(0.19, source, 1.0, 0.0, 0.95)
    assert np.isnan(out[0])
    assert not np.isnan(out[1])


# ─────────────────────────────────────────────────────────────────────────
# compute_cf_bounds
# ─────────────────────────────────────────────────────────────────────────

def test_compute_cf_bounds_solar_relative_range():
    floor, ceiling = compute_cf_bounds(0.19, "solar")
    assert floor == pytest.approx(0.19 * 0.40)
    assert ceiling == pytest.approx(0.19 * 1.80)


def test_compute_cf_bounds_biomass_narrower_range():
    """Biomass uses a dedicated (narrower) rule: floor=0.70x, ceiling=0.95x
    capped by the absolute ceiling."""
    floor, ceiling = compute_cf_bounds(0.73, "biomass")
    assert floor == pytest.approx(0.73 * 0.70)
    assert ceiling == pytest.approx(0.73 * 0.95)


def test_compute_cf_bounds_explicit_overrides_take_precedence():
    floor, ceiling = compute_cf_bounds(0.73, "biomass", cf_ceiling_override=0.90, cf_floor_override=0.20)
    assert floor == pytest.approx(0.20)
    assert ceiling == pytest.approx(0.90)


def test_compute_cf_bounds_absolute_ceiling_caps_relative_range():
    """Solar/wind with a high base CF can exceed cf_absolute_ceiling via the
    1.8x relative multiplier; the absolute cap must win."""
    floor, ceiling = compute_cf_bounds(0.60, "wind")
    assert ceiling == pytest.approx(0.95)  # 0.60*1.80=1.08 > 0.95 cap
    assert floor == pytest.approx(0.24)


# ─────────────────────────────────────────────────────────────────────────
# compute_supply_curve
# ─────────────────────────────────────────────────────────────────────────

def test_compute_supply_curve_docstring_example():
    lcoe = np.array([[50.0, 100.0], [80.0, 120.0]])
    cap = np.array([[10.0, 5.0], [20.0, 8.0]])
    sc = compute_supply_curve(lcoe, cap)

    assert list(sc["lcoe_usd_mwh"]) == pytest.approx([50.0, 80.0, 100.0, 120.0])
    assert list(sc["capacity_mw"]) == pytest.approx([10.0, 20.0, 5.0, 8.0])
    assert list(sc["cum_capacity_gw"]) == pytest.approx([0.010, 0.030, 0.035, 0.043], abs=1e-6)


def test_compute_supply_curve_excludes_invalid_pairs():
    """NaN and non-positive lcoe/capacity pixels must be dropped."""
    lcoe = np.array([50.0, np.nan, -10.0, 60.0])
    cap = np.array([10.0, 5.0, 5.0, 0.0])
    sc = compute_supply_curve(lcoe, cap)
    # Only the (50.0, 10.0) pair is fully valid: index 1 has NaN lcoe,
    # index 2 has negative lcoe, index 3 has zero capacity.
    assert len(sc) == 1
    assert sc["lcoe_usd_mwh"].iloc[0] == pytest.approx(50.0)


def test_compute_supply_curve_empty_when_no_valid_pairs():
    lcoe = np.array([[np.nan, -1.0]])
    cap = np.array([[5.0, 5.0]])
    sc = compute_supply_curve(lcoe, cap)
    assert sc.empty
    assert list(sc.columns) == ["lcoe_usd_mwh", "capacity_mw", "cum_capacity_gw"]


def test_compute_supply_curve_is_monotonically_nondecreasing():
    rng = np.random.default_rng(3)
    lcoe = rng.uniform(20, 150, size=(10, 10))
    cap = rng.uniform(1, 50, size=(10, 10))
    sc = compute_supply_curve(lcoe, cap)
    diffs = np.diff(sc["cum_capacity_gw"].to_numpy())
    assert np.all(diffs >= 0)


# ─────────────────────────────────────────────────────────────────────────
# extract_supply_curve_thresholds
# ─────────────────────────────────────────────────────────────────────────

def test_extract_supply_curve_thresholds_docstring_example():
    lcoe = np.array([[50.0, 100.0], [80.0, 120.0]])
    cap = np.array([[10.0, 5.0], [20.0, 8.0]])
    sc = compute_supply_curve(lcoe, cap)
    result = extract_supply_curve_thresholds(sc, [50, 100])
    assert result[50] == pytest.approx(0.010, abs=1e-6)
    assert result[100] == pytest.approx(0.035, abs=1e-6)


def test_extract_supply_curve_thresholds_default_list_covers_all_thresholds():
    lcoe = np.array([[50.0, 100.0], [80.0, 120.0]])
    cap = np.array([[10.0, 5.0], [20.0, 8.0]])
    sc = compute_supply_curve(lcoe, cap)
    result = extract_supply_curve_thresholds(sc)
    assert set(result.keys()) == {40, 50, 60, 70, 80, 100, 120, 150}
    assert result[40] == pytest.approx(0.0)          # below cheapest pixel
    assert result[150] == pytest.approx(0.043, abs=1e-6)  # above all pixels


def test_extract_supply_curve_thresholds_empty_curve_returns_all_zero():
    empty_sc = pd.DataFrame(columns=["lcoe_usd_mwh", "capacity_mw", "cum_capacity_gw"])
    result = extract_supply_curve_thresholds(empty_sc, [40, 50, 60])
    assert result == {40: 0.0, 50: 0.0, 60: 0.0}
