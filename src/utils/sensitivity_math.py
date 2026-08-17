"""
src/utils/sensitivity_math.py
==============================
Pure computational core for Phase 8 (Sensitivity Analysis) -- TOPSIS/data
loaders shared across sub-analyses, plus SA-1 through SA-6 themselves.

Separated from SensitivityAnalyzer to keep the main module lean, mirroring
the pattern already established for sensitivity_plots.py/abatement_plots.py
(REFACTOR-001/002). Every function here is a pure computation -- no
dependency on SensitivityAnalyzer's instance state, no I/O beyond reading
whatever raster/criteria paths it's explicitly given.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import rasterio

logger = logging.getLogger("geoworld.utils.sensitivity_math")


# ─────────────────────────────────────────────────────────────────────────────
# TOPSIS CORE & DATA LOADERS
# ─────────────────────────────────────────────────────────────────────────────


def _topsis_flat(
    mat_valid: np.ndarray,
    weights: np.ndarray,
    chunk_size: int = 500_000,
) -> np.ndarray:
    """Vectorized TOPSIS implementation for valid pixels via chunking."""
    mat = mat_valid.astype(np.float32, copy=True)
    col_norms = np.sqrt((mat ** 2).sum(axis=0, keepdims=True))
    col_norms = np.where(col_norms > 0, col_norms, 1.0)
    mat /= col_norms
    mat *= weights.astype(np.float32)

    pis = mat.max(axis=0)
    nis = mat.min(axis=0)
    scores = np.empty(mat.shape[0], dtype=np.float32)

    for s in range(0, mat.shape[0], chunk_size):
        e = min(s + chunk_size, mat.shape[0])
        c = mat[s:e]
        d_p = np.sqrt(((c - pis) ** 2).sum(axis=1))
        d_n = np.sqrt(((c - nis) ** 2).sum(axis=1))
        den = d_p + d_n
        scores[s:e] = np.where(den > 0, d_n / den, 0.0)

    return scores


def _load_criteria_arrays(
    criteria_dir: Path,
    names: List[str],
    height: int,
    width: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Load criterion rasters into a 2D matrix."""
    arrays: List[np.ndarray] = []

    for name in names:
        tif = criteria_dir / f"{name}.tif"
        if not tif.exists():
            candidates = sorted(criteria_dir.glob(f"*{name}.tif"))
            if not candidates:
                raise FileNotFoundError(f"Criterion not found: {tif}")
            tif = candidates[0]

        with rasterio.open(str(tif)) as src:
            arr = src.read(1).astype(np.float32).ravel()
            if src.nodata is not None:
                arr[arr == float(src.nodata)] = np.nan

        arrays.append(arr)

    mat = np.stack(arrays, axis=1)
    return mat, np.all(np.isfinite(mat) & (mat >= 0), axis=1)


def _balanced_threshold(tech: str, pot_results: Any) -> float:
    """
    Resolve the real balanced-scenario TOPSIS threshold from Phase 4's
    persisted result.

    Single source of truth for the real threshold, shared by SA-2
    (METRIC_013) and by SensitivityAnalyzer._resolve_tech_params() for
    SA-3/SA-6 (BLOCKER-017 fix). _resolve_tech_params() previously had its
    own inline lookup (`getattr(country_params, "suitability_threshold",
    thr)`) that always fell back to a hardcoded 0.60 default -- CountryParams
    has no `suitability_threshold` attribute -- both callers now go through
    this one function instead of maintaining two independent readers of the
    same persisted data.
    """
    if pot_results is None:
        return 0.60
    techs = (
        pot_results.techs if hasattr(pot_results, "techs")
        else pot_results.get("techs", {}) if isinstance(pot_results, dict)
        else {}
    )
    tech_obj = techs.get(tech) if hasattr(techs, "get") else None
    if tech_obj is None:
        return 0.60
    scenarios = (
        tech_obj.scenarios if hasattr(tech_obj, "scenarios")
        else tech_obj.get("scenarios", {}) if isinstance(tech_obj, dict)
        else {}
    )
    balanced = scenarios.get("balanced") if hasattr(scenarios, "get") else None
    if balanced is None:
        return 0.60
    thr = (
        balanced.threshold if hasattr(balanced, "threshold")
        else balanced.get("threshold")
    )
    return float(thr) if thr is not None else 0.60


def _build_ghg_function_from_abatement(
    abat_result: Dict[str, Any],
) -> Tuple[Optional[Callable], Optional[Dict[str, Any]]]:
    """Build GHG function from Phase 7 results for SA-5 Sobol analysis."""
    if (
        not abat_result
        or not abat_result.get("available", False)
        or abat_result.get("co2_avoided_mt", 0.0) <= 0
    ):
        return None, None

    co2_avoided  = float(abat_result["co2_avoided_mt"])
    ef_thermal   = float(abat_result.get("ci_before", 450.0))
    ef_renewable = 50.0
    penetration  = 0.75
    cf_base      = 0.25
    subst_gwh    = co2_avoided * 1e6 / max(ef_thermal, 1.0)

    def ghg_function(
        ef_thermal_gco2_kwh: float,
        cf_renewable: float,
        ef_lifecycle_gco2_kwh: float,
        penetration_factor: float,
    ) -> float:
        # Target substitution implied by the perturbed penetration factor,
        # relative to the Phase 7 baseline penetration.
        target_gwh_sub = (
            subst_gwh
            * min(max(penetration_factor, 0.0), 1.0)
            / max(penetration, 1e-6)
        )
        # Renewable generation = capacity x CF x hours: for a fixed fleet of
        # installed capacity (implied by the Phase 7 baseline), deliverable
        # GWh scales linearly with cf_renewable. This mirrors the ceiling
        # ghg_abatement_calculator.py applies via renew_total_gwh in
        # calc_macc()/_derive_subst_gwh(), which is itself built from Phase
        # 4's capacity-factor-dependent generation figures.
        available_gwh_sub = subst_gwh * max(cf_renewable, 1e-6) / cf_base
        gwh_sub = min(target_gwh_sub, available_gwh_sub)
        return max(
            0.0,
            (gwh_sub * ef_thermal_gco2_kwh / 1e6)
            - (gwh_sub * ef_lifecycle_gco2_kwh / 1e6),
        )

    base_params: Dict[str, Any] = {
        "ef_thermal_gco2_kwh":   {"value": ef_thermal,   "range": 0.15},
        "cf_renewable":          {"value": cf_base,       "range": 0.20},
        "ef_lifecycle_gco2_kwh": {"value": ef_renewable,  "range": 0.50},
        "penetration_factor":    {"value": penetration,   "range": 0.20},
    }

    return ghg_function, base_params


# ─────────────────────────────────────────────────────────────────────────────
# SA-1 TO SA-6 COMPONENTS  (funções puras — sem alteração de lógica)
# ─────────────────────────────────────────────────────────────────────────────


def sa1_oat_weight_sensitivity(
    criteria_dir: Path,
    criteria_names: List[str],
    base_weights: Dict[str, float],
    height: int,
    width: int,
    perturbations: List[float] = (
        -0.30, -0.20, -0.10, 0.10, 0.20, 0.30
    ),
    max_pixels: int = 500_000,
) -> pd.DataFrame:
    """
    SA-1: One-At-a-Time weight perturbation with Spearman rank correlation.

    Args:
        criteria_dir:   Path to normalized criteria TIFs.
        criteria_names: List of criterion names.
        base_weights:   Dictionary of base AHP weights.
        height:         Grid height.
        width:          Grid width.
        perturbations:  List of relative perturbations (e.g., -0.10 = -10%).
        max_pixels:     Maximum pixels to sample for computational efficiency.

    Returns:
        DataFrame with columns: criterion, perturbation_pct, weight_base,
        weight_perturbed, spearman_rho, rank_shift_mean, robust.
    """
    from scipy.stats import spearmanr

    mat, vmask = _load_criteria_arrays(criteria_dir, criteria_names, height, width)
    mat_v = mat[vmask]
    n = mat_v.shape[0]

    rng = np.random.default_rng(42)
    mat_s = (
        mat_v[rng.choice(n, size=min(n, max_pixels), replace=False)]
        if n > max_pixels else mat_v
    )

    w0 = np.array([base_weights[c] for c in criteria_names], dtype=np.float64)
    w0 /= w0.sum()

    r0 = (
        _topsis_flat(mat_s, w0.astype(np.float32))
        .argsort().argsort().astype(np.float32)
    )

    rows: List[Dict[str, Any]] = []

    for i, crit in enumerate(criteria_names):
        for delta in perturbations:
            new_wi = float(np.clip(w0[i] * (1 + delta), 1e-6, 1.0))
            other  = w0.sum() - w0[i]

            if other > 1e-10:
                wp = w0 * ((1.0 - new_wi) / other)
            else:
                wp = np.full_like(w0, 1.0 / len(criteria_names))

            wp[i]  = new_wi
            wp    /= wp.sum()

            rp = (
                _topsis_flat(mat_s, wp.astype(np.float32))
                .argsort().argsort().astype(np.float32)
            )
            rho, _ = spearmanr(r0, rp)

            rows.append({
                "criterion":        crit,
                "perturbation_pct": round(delta * 100, 0),
                "weight_base":      round(float(w0[i]), 4),
                "weight_perturbed": round(float(wp[i]), 4),
                "spearman_rho":     round(float(rho), 4),
                "rank_shift_mean":  round(float(np.abs(r0 - rp).mean()), 2),
                "robust":           bool(rho >= 0.95),
            })

    return pd.DataFrame(rows)


def sa2_monte_carlo_weights(
    criteria_dir: Path,
    criteria_names: List[str],
    base_weights: Dict[str, float],
    height: int,
    width: int,
    threshold: float,
    n_samples: int = 1000,
    concentration: float = 20.0,
    max_pixels: int = 300_000,
    seed: int = 42,
) -> Dict[str, Any]:
    """
    SA-2: Monte Carlo weight sampling (Dirichlet) with decision robustness.

    METRIC_013: reports threshold-crossing, not raw-score stability.
    Earlier versions measured `stable_fraction` -- the fraction of pixels
    whose 90% CI band on the continuous TOPSIS score was narrower than
    0.10. That answered "is the score numerically stable", not "does the
    framework's actual apt/not-apt decision (a fixed threshold cutoff,
    e.g. 0.75 for the balanced scenario) change under weight uncertainty".
    A prototype comparing both metrics on PRT/BRA found stable_fraction
    couldn't distinguish the two countries on wind (4.4% vs. 4.3%) while
    threshold-crossing revealed a real, large difference (PRT wind: 72.1%
    of apt-by-base-weights pixels sit in a 25-75% "boundary" zone, barely
    2.3% are decisive either way; BRA wind: 53.1% decisive) -- see
    scratchpad/threshold_crossing_prototype.py for the full comparison.

    Args:
        criteria_dir:   Path to normalized criteria TIFs.
        criteria_names: List of criterion names.
        base_weights:   Dictionary of base AHP weights.
        height:         Grid height.
        width:          Grid width.
        threshold:      The real scenario TOPSIS threshold (e.g. Phase 4's
                         balanced-scenario cutoff) pixels are classified
                         against -- not a free parameter, must match
                         whatever threshold the framework's own apt/not-apt
                         decision actually uses for this tech/country.
        n_samples:      Number of Monte Carlo weight samples.
        concentration:  Dirichlet concentration parameter controlling how
                         tightly the 1000 sampled weight vectors cluster
                         around `base_weights` (higher = tighter, closer to
                         the AHP base weights; lower = more dispersed).
                         Not a physically-derived value -- picked as a
                         plausible "AHP elicitation noise" magnitude, not
                         validated against any external calibration. The
                         same prototype that motivated this metric also
                         swept concentration in {10, 20, 40} for BRA/solar
                         and found the reported "decisive" fraction ranges
                         from 17.9% (concentration=10) to 45.4%
                         (concentration=40) -- roughly a 2.5x swing. The
                         qualitative PRT-vs-BRA comparison held up across
                         that range, but absolute percentages from this
                         function should not be treated as precise without
                         stating which concentration produced them.
        max_pixels:     Maximum pixels to sample.
        seed:           Random seed for reproducibility.

    Returns:
        Dictionary with keys: cv, ci_width, crossing_fraction, apt_base_mask,
        n_apt_base, decisive_fraction, boundary_fraction, moderate_fraction,
        threshold, weight_samples, n_samples, concentration,
        is_pixel_sampled, n_pixels_used.
    """
    rng = np.random.default_rng(seed)
    mat, vmask = _load_criteria_arrays(criteria_dir, criteria_names, height, width)
    mat_v   = mat[vmask]
    n       = mat_v.shape[0]
    sampled = n > max_pixels
    mat_s   = (
        mat_v[rng.choice(n, size=min(n, max_pixels), replace=False)]
        if sampled else mat_v
    )

    w0 = np.array([base_weights[c] for c in criteria_names], dtype=np.float64)
    w0 /= w0.sum()

    score_base = _topsis_flat(mat_s, w0.astype(np.float32))

    ws = rng.dirichlet(concentration * w0, size=n_samples)

    all_s = np.zeros((n_samples, mat_s.shape[0]), dtype=np.float32)
    for i, w in enumerate(ws):
        all_s[i] = _topsis_flat(mat_s, w.astype(np.float32))

    p05, p50, p95 = np.percentile(all_s, [5, 50, 95], axis=0)
    std = all_s.std(axis=0)
    cv  = np.where(p50 > 0.01, std / p50, 0.0)

    # METRIC_013: per-pixel fraction of the 1000 Dirichlet samples where the
    # score stays above `threshold`, restricted to pixels apt under the
    # unperturbed base weights -- the population the framework's own
    # scenario classification actually cares about.
    crossing_fraction = (all_s > threshold).mean(axis=0)
    apt_base_mask = score_base > threshold
    n_apt_base    = int(apt_base_mask.sum())

    if n_apt_base > 0:
        cf_apt = crossing_fraction[apt_base_mask]
        decisive_fraction = float(((cf_apt > 0.95) | (cf_apt < 0.05)).mean())
        boundary_fraction = float(((cf_apt >= 0.25) & (cf_apt <= 0.75)).mean())
        moderate_fraction = float(1.0 - decisive_fraction - boundary_fraction)
    else:
        decisive_fraction = boundary_fraction = moderate_fraction = float("nan")

    return {
        "cv":                cv,
        "ci_width":          p95 - p05,
        "crossing_fraction": crossing_fraction,
        "apt_base_mask":     apt_base_mask,
        "n_apt_base":        n_apt_base,
        "decisive_fraction": decisive_fraction,
        "boundary_fraction": boundary_fraction,
        "moderate_fraction": moderate_fraction,
        "threshold":         threshold,
        "weight_samples":  (
            pd.DataFrame(ws, columns=criteria_names)
            .assign(sample_id=range(n_samples))
        ),
        "n_samples":       n_samples,
        "concentration":   concentration,
        "is_pixel_sampled": sampled,
        "n_pixels_used":   mat_s.shape[0],
    }


def sa3_threshold_sweep(
    suitability_tif: Path,
    pixel_area_km2_func: Callable,
    transform: Any,
    width: int,
    height: int,
    thresholds: Optional[List[float]] = None,
    power_density_mw_km2: float = 30.0,
    land_use_factor: float = 0.20,
    capacity_factor: float = 0.22,
) -> pd.DataFrame:
    """
    SA-3: Threshold sweep for area and potential elasticity.

    Args:
        suitability_tif:      Path to suitability raster.
        pixel_area_km2_func:  Function to compute pixel areas.
        transform:            Rasterio affine transform.
        width:                Grid width.
        height:                Grid height.
        thresholds:           List of thresholds (default 0.30–0.85 @ 0.05).
        power_density_mw_km2: Power density (MW/km²).
        land_use_factor:      Land use factor [0–1].
        capacity_factor:      Capacity factor [0–1].

    Returns:
        DataFrame with columns: threshold, area_apt_km2, potential_gw,
        generation_twh, n_pixels, elasticity.
    """
    if thresholds is None:
        thresholds = [round(t, 2) for t in np.arange(0.30, 0.85, 0.05)]

    with rasterio.open(str(suitability_tif)) as src:
        score = src.read(1).astype(np.float32)
        if src.nodata is not None:
            score[score == float(src.nodata)] = np.nan

    pxa  = pixel_area_km2_func(transform, width, height)
    rows: List[Dict[str, Any]] = []

    for thr in thresholds:
        mask = np.isfinite(score) & (score >= thr)
        area = float(pxa[mask].sum())
        gw   = area * land_use_factor * power_density_mw_km2 / 1000.0
        twh  = gw * capacity_factor * 8.760
        rows.append({
            "threshold":      thr,
            "area_apt_km2":   round(area, 1),
            "potential_gw":   round(gw,   3),
            "generation_twh": round(twh,  3),
            "n_pixels":       int(mask.sum()),
        })

    df    = pd.DataFrame(rows)
    elast = np.full(len(df), np.nan)

    for i in range(1, len(df) - 1):
        da = (
            (df["area_apt_km2"].iloc[i + 1] - df["area_apt_km2"].iloc[i - 1])
            / max(df["area_apt_km2"].iloc[i], 1.0)
        )
        dt = (
            (df["threshold"].iloc[i + 1] - df["threshold"].iloc[i - 1])
            / df["threshold"].iloc[i]
        )
        elast[i] = (da / dt) if abs(dt) > 1e-10 else np.nan

    df["elasticity"] = np.round(elast, 3)
    return df


def sa4_lcoe_uncertainty(
    base_capex_usd_kw: float,
    base_opex_usd_kw_yr: float,
    lifetime: int,
    discount_rate: float,
    capacity_factor: float,
    n_samples: int = 10_000,
    capex_variation: float = 0.15,
    opex_variation: float = 0.15,
    cf_variation: float = 0.10,
    seed: int = 42,
) -> pd.DataFrame:
    """
    SA-4: LCOE Monte Carlo uncertainty with triangular distributions.

    Args:
        base_capex_usd_kw:   Nominal CAPEX (USD/kW).
        base_opex_usd_kw_yr: Nominal OPEX (USD/kW/yr).
        lifetime:            Project lifetime (years).
        discount_rate:       Discount rate (decimal, e.g., 0.06).
        capacity_factor:     Nominal capacity factor [0–1].
        n_samples:           Number of MC samples.
        capex_variation:     Relative variation for CAPEX (±15% default).
        opex_variation:      Relative variation for OPEX.
        cf_variation:        Relative variation for CF.
        seed:                Random seed.

    Returns:
        DataFrame with columns: capex, opex, cf, lcoe_usd_mwh.
        Stats stored in df.attrs["stats"].
    """
    rng = np.random.default_rng(seed)

    def _tri(val: float, var: float) -> np.ndarray:
        return rng.triangular(val * (1 - var), val, val * (1 + var), size=n_samples)

    capex = _tri(base_capex_usd_kw,   capex_variation)
    opex  = _tri(base_opex_usd_kw_yr, opex_variation)
    cf    = np.clip(_tri(capacity_factor, cf_variation), 0.01, 0.99)

    if abs(discount_rate) < 1e-9:
        crf = 1.0 / max(lifetime, 1)
    else:
        crf = (
            discount_rate * (1 + discount_rate) ** lifetime
            / ((1 + discount_rate) ** lifetime - 1)
        )

    lcoe = (capex * crf + opex) / (cf * 8760) * 1000

    df = pd.DataFrame({"capex": capex, "opex": opex, "cf": cf, "lcoe_usd_mwh": lcoe})
    df.attrs["stats"] = {
        "lcoe_p05":      round(float(np.percentile(lcoe,  5)), 2),
        "lcoe_p25":      round(float(np.percentile(lcoe, 25)), 2),
        "lcoe_p50":      round(float(np.percentile(lcoe, 50)), 2),
        "lcoe_p75":      round(float(np.percentile(lcoe, 75)), 2),
        "lcoe_p95":      round(float(np.percentile(lcoe, 95)), 2),
        "lcoe_mean":     round(float(lcoe.mean()),             2),
        "lcoe_std":      round(float(lcoe.std()),              2),
        "ci90_width":    round(
            float(np.percentile(lcoe, 95) - np.percentile(lcoe, 5)), 2
        ),
        "capex_nominal": base_capex_usd_kw,
        "opex_nominal":  base_opex_usd_kw_yr,
        "cf_nominal":    capacity_factor,
        "discount_rate": discount_rate,
        "lifetime":      lifetime,
        "n_samples":     n_samples,
    }
    return df


def sa5_sobol_ghg(
    base_params: Dict[str, Any],
    ghg_function: Callable,
    n_samples: int = 1024,
    seed: int = 42,
) -> Optional[pd.DataFrame]:
    """
    SA-5: Sobol global sensitivity indices for GHG abatement function.

    Args:
        base_params:  Dictionary of parameters with 'value' and 'range' keys.
        ghg_function: Parametric GHG function.
        n_samples:    Number of Sobol samples (default 1024).
        seed:         Random seed.

    Returns:
        DataFrame with columns: parameter, S1, S1_conf, ST, ST_conf, dominant.
        Returns None if SALib is not installed.
    """
    try:
        from SALib.sample import saltelli
        from SALib.analyze import sobol
    except ImportError:
        logger.warning("[SA-5] SALib not installed. Skipping (pip install SALib).")
        return None

    import inspect

    _saltelli_accepts_seed = "seed" in inspect.signature(saltelli.sample).parameters
    _sobol_accepts_seed    = "seed" in inspect.signature(sobol.analyze).parameters

    names = list(base_params.keys())
    prob = {
        "num_vars": len(names),
        "names":    names,
        "bounds": [
            [
                base_params[p]["value"] * (1 - base_params[p].get("range", 0.15)),
                base_params[p]["value"] * (1 + base_params[p].get("range", 0.15)),
            ]
            for p in names
        ],
    }

    if _saltelli_accepts_seed:
        X = saltelli.sample(prob, n_samples, calc_second_order=False, seed=seed)
    else:
        np.random.seed(seed)
        X = saltelli.sample(prob, n_samples, calc_second_order=False)

    Y = np.zeros(X.shape[0], dtype=np.float64)
    for i, row in enumerate(X):
        try:
            Y[i] = float(ghg_function(**dict(zip(names, row))))
        except Exception:
            Y[i] = np.nan

    if np.isnan(Y).any():
        med   = np.nanmedian(Y)
        n_nan = int(np.isnan(Y).sum())
        logger.warning(
            "[SA-5] %d samples resulted in NaN — imputed with median (%.4f).",
            n_nan, med,
        )
        Y = np.where(np.isnan(Y), med, Y)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if _sobol_accepts_seed:
            Si = sobol.analyze(prob, Y, calc_second_order=False, seed=seed)
        else:
            np.random.seed(seed)
            Si = sobol.analyze(prob, Y, calc_second_order=False)

    return (
        pd.DataFrame({
            "parameter": names,
            "S1":        np.round(Si["S1"],      4),
            "S1_conf":   np.round(Si["S1_conf"], 4),
            "ST":        np.round(Si["ST"],      4),
            "ST_conf":   np.round(Si["ST_conf"], 4),
            "dominant":  np.array(Si["S1"]) > 0.10,
        })
        .sort_values("ST", ascending=False)
        .reset_index(drop=True)
    )


def sa6_potential_sensitivity(
    suitability_tif: Path,
    pixel_area_km2_func: Callable,
    transform: Any,
    width: int,
    height: int,
    base_power_density: float,
    base_land_use: float,
    base_cf: float,
    base_threshold: float = 0.60,
    perturbations: List[float] = (
        -0.30, -0.20, -0.10, 0.10, 0.20, 0.30
    ),
) -> pd.DataFrame:
    """
    SA-6: OAT sensitivity for potential parameters.

    Args:
        suitability_tif:      Path to suitability raster.
        pixel_area_km2_func:  Function to compute pixel areas.
        transform:            Rasterio affine transform.
        width:                Grid width.
        height:                Grid height.
        base_power_density:   Base power density (MW/km²).
        base_land_use:        Base land use factor [0–1].
        base_cf:              Base capacity factor [0–1].
        base_threshold:       Suitability threshold for base case.
        perturbations:        List of relative perturbations.

    Returns:
        DataFrame with columns: parameter, perturbation_pct, base_value,
        perturbed_value, potential_gw, generation_twh, delta_gw_pct,
        delta_twh_pct, elasticity_gw, elasticity_twh.
        Base values stored in df.attrs.
    """
    with rasterio.open(str(suitability_tif)) as src:
        score = src.read(1).astype(np.float32)
        if src.nodata is not None:
            score[score == float(src.nodata)] = np.nan

    pxa      = pixel_area_km2_func(transform, width, height)
    area     = float(pxa[np.isfinite(score) & (score >= base_threshold)].sum())
    base_gw  = area * base_land_use * base_power_density / 1000.0
    base_twh = base_gw * base_cf * 8.760

    params_map = {
        "power_density_mw_km2": base_power_density,
        "land_use_factor":      base_land_use,
        "capacity_factor":      base_cf,
    }

    rows: List[Dict[str, Any]] = []
    for param, base_val in params_map.items():
        for delta in perturbations:
            pval = base_val * (1.0 + delta)

            if param == "power_density_mw_km2":
                gw  = area * base_land_use * pval / 1000.0
                twh = gw * base_cf * 8.760
            elif param == "land_use_factor":
                gw  = area * pval * base_power_density / 1000.0
                twh = gw * base_cf * 8.760
            else:  # capacity_factor
                gw  = base_gw
                twh = base_gw * pval * 8.760

            dgw  = (gw  - base_gw)  / base_gw  * 100.0 if base_gw  > 0 else 0.0
            dtwh = (twh - base_twh) / base_twh * 100.0 if base_twh > 0 else 0.0

            rows.append({
                "parameter":        param,
                "perturbation_pct": round(delta * 100.0, 0),
                "base_value":       round(base_val, 5),
                "perturbed_value":  round(pval,     5),
                "potential_gw":     round(gw,       3),
                "generation_twh":   round(twh,      3),
                "delta_gw_pct":     round(dgw,      2),
                "delta_twh_pct":    round(dtwh,     2),
                "elasticity_gw":    round(dgw  / (delta * 100.0), 3) if abs(delta) > 1e-9 else None,
                "elasticity_twh":   round(dtwh / (delta * 100.0), 3) if abs(delta) > 1e-9 else None,
            })

    df = pd.DataFrame(rows)
    df.attrs.update({
        "base_gw":  round(base_gw,  3),
        "base_twh": round(base_twh, 3),
    })
    return df


def _sfmt(val: Any, fmt_str: str, default: str = "—") -> str:
    """Safe formatting helper."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return default
    try:
        return format(float(val), fmt_str)
    except (ValueError, TypeError):
        return str(val)
