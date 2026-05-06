"""
sensitivity_analyzer.py — Phase 8: Sensitivity Analysis
==========================================================
Sensitivity analysis for the GeoWorld pipeline targeting Q1 publications.

SA-1 · OAT Weight Sensitivity: Perturbs AHP weights (±10 to 30%). Spearman ρ.
SA-2 · Monte Carlo AHP: Dirichlet distributions on weights (spatial robustness).
SA-3 · Threshold Sweep: Area elasticity vs. spatial suitability constraints.
SA-4 · LCOE Uncertainty: Triangular Monte Carlo for CAPEX, OPIX, CF.
SA-5 · Potential Sensitivity: Parameter elasticities (Power Density, Capacity Factor).
SA-6 · Sobol Global Sensitivity: GHG Abatement indices (S1 and ST).

References:
  - Saltelli et al. (2008). Global Sensitivity Analysis.
  - Malczewski (1999). GIS and Multicriteria Decision Analysis.
"""

from __future__ import annotations

import json
import logging
import time
import warnings
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.gridspec as gridspec
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio

logger = logging.getLogger("geoworld.processors.SensitivityAnalyzer")

# ── Visual palette ──────────────────────────────────────────────────────────
_TECH_COLOR = {
    "solar": "#F9A825",
    "wind": "#1565C0",
    "biomass": "#2E7D32"
}
_TECH_LABEL = {
    "solar": "Solar PV",
    "wind": "Wind Onshore",
    "biomass": "Biomass / Bioenergy"
}
_FIG_BG = "#FAFAFA"
_GRID_C = "#E5E7EB"
_TEXT_C = "#111827"
_MUTE_C = "#6B7280"

# ─────────────────────────────────────────────────────────────────────────────
# TOPSIS CORE & DATA LOADERS
# ─────────────────────────────────────────────────────────────────────────────


def _topsis_flat(
    mat_valid: np.ndarray,
    weights: np.ndarray,
    chunk_size: int = 500_000
) -> np.ndarray:
    """
    Vectorized TOPSIS implementation for valid pixels via chunking.

    Args:
        mat_valid: 2D array (n_pixels, n_criteria) of valid criterion values.
        weights: 1D array (n_criteria,) of AHP weights.
        chunk_size: Number of pixels to process per chunk.

    Returns:
        1D array (n_pixels,) of TOPSIS scores [0-1].
    """
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
    width: int
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Load criterion rasters into a 2D matrix.

    Args:
        criteria_dir: Directory containing criterion TIFs.
        names: List of criterion names (without .tif extension).
        height: Grid height (for validation).
        width: Grid width (for validation).

    Returns:
        Tuple of (matrix (n_pixels, n_criteria), valid_mask (n_pixels,)).

    Raises:
        FileNotFoundError: If any criterion TIF is not found.
    """
    arrays = []

    for name in names:
        tif = criteria_dir / f"{name}.tif"
        if not tif.exists():
            candidates = sorted(criteria_dir.glob(f"*{name}.tif"))
            if not candidates:
                raise FileNotFoundError(
                    "Criterion not found: %s",
                    tif
                )
            tif = candidates[0]

        with rasterio.open(str(tif)) as src:
            arr = src.read(1).astype(np.float32).ravel()
            if src.nodata is not None:
                arr[arr == float(src.nodata)] = np.nan

        arrays.append(arr)

    mat = np.stack(arrays, axis=1)
    return mat, np.all(np.isfinite(mat) & (mat >= 0), axis=1)


def _build_ghg_function_from_abatement(
    abat_result: Dict
) -> Tuple[Optional[Callable], Optional[Dict]]:
    """
    Builds GHG function from Phase 7 results for SA-5 Sobol analysis.

    Args:
        abat_result: Dictionary from Phase 7 (GHG Abatement Calculator).

    Returns:
        Tuple of (ghg_function, base_params) or (None, None) if unavailable.
    """
    if (
        not abat_result
        or not abat_result.get("available", False)
        or abat_result.get("co2_avoided_mt", 0.0) <= 0
    ):
        return None, None

    co2_avoided = abat_result["co2_avoided_mt"]
    ef_thermal = abat_result.get("ci_before", 450.0)
    ef_renewable = 50.0
    penetration = 0.75
    subst_gwh = co2_avoided * 1e6 / max(ef_thermal, 1.0)

    def ghg_function(
        ef_thermal_gco2_kwh: float,
        cf_renewable: float,
        ef_lifecycle_gco2_kwh: float,
        penetration_factor: float
    ) -> float:
        """
        Parametric GHG abatement function.

        Args:
            ef_thermal_gco2_kwh: Thermal emission factor (gCO2/kWh).
            cf_renewable: Renewable capacity factor (not used directly).
            ef_lifecycle_gco2_kwh: Renewable lifecycle emissions (gCO2/kWh).
            penetration_factor: Penetration rate [0-1].

        Returns:
            GHG abatement in MtCO2.
        """
        gwh_sub = (
            subst_gwh
            * min(max(penetration_factor, 0.0), 1.0)
            / max(penetration, 1e-6)
        )
        return max(
            0.0,
            (gwh_sub * ef_thermal_gco2_kwh / 1e6)
            - (gwh_sub * ef_lifecycle_gco2_kwh / 1e6)
        )

    base_params = {
        "ef_thermal_gco2_kwh": {"value": ef_thermal, "range": 0.15},
        "cf_renewable": {"value": 0.25, "range": 0.20},
        "ef_lifecycle_gco2_kwh": {"value": ef_renewable, "range": 0.50},
        "penetration_factor": {"value": penetration, "range": 0.20},
    }

    return ghg_function, base_params


# ─────────────────────────────────────────────────────────────────────────────
# SA-1 TO SA-6 COMPONENTS
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
        criteria_dir: Path to normalized criteria TIFs.
        criteria_names: List of criterion names.
        base_weights: Dictionary of base AHP weights.
        height: Grid height.
        width: Grid width.
        perturbations: List of relative perturbations (e.g., -0.10 = -10%).
        max_pixels: Maximum pixels to sample for computational efficiency.

    Returns:
        DataFrame with columns: criterion, perturbation_pct, weight_base,
        weight_perturbed, spearman_rho, rank_shift_mean, robust.
    """
    from scipy.stats import spearmanr

    mat, vmask = _load_criteria_arrays(
        criteria_dir,
        criteria_names,
        height,
        width
    )
    mat_v = mat[vmask]
    n = mat_v.shape[0]

    if n > max_pixels:
        mat_s = mat_v[
            np.random.default_rng(42).choice(
                n,
                size=min(n, max_pixels),
                replace=False
            )
        ]
    else:
        mat_s = mat_v

    w0 = np.array(
        [base_weights[c] for c in criteria_names],
        dtype=np.float64
    )
    w0 /= w0.sum()

    r0 = (
        _topsis_flat(mat_s, w0.astype(np.float32))
        .argsort()
        .argsort()
        .astype(np.float32)
    )

    rows = []

    for i, crit in enumerate(criteria_names):
        for delta in perturbations:
            new_wi = float(np.clip(w0[i] * (1 + delta), 1e-6, 1.0))
            other = w0.sum() - w0[i]

            if other > 1e-10:
                wp = w0 * ((1.0 - new_wi) / other)
            else:
                wp = np.full_like(w0, 1.0 / len(criteria_names))

            wp[i] = new_wi
            wp /= wp.sum()

            rp = (
                _topsis_flat(mat_s, wp.astype(np.float32))
                .argsort()
                .argsort()
                .astype(np.float32)
            )

            rho, _ = spearmanr(r0, rp)

            rows.append({
                "criterion": crit,
                "perturbation_pct": round(delta * 100, 0),
                "weight_base": round(float(w0[i]), 4),
                "weight_perturbed": round(float(wp[i]), 4),
                "spearman_rho": round(float(rho), 4),
                "rank_shift_mean": round(float(np.abs(r0 - rp).mean()), 2),
                "robust": bool(rho >= 0.95),
            })

    return pd.DataFrame(rows)


def sa2_monte_carlo_weights(
    criteria_dir: Path,
    criteria_names: List[str],
    base_weights: Dict[str, float],
    height: int,
    width: int,
    n_samples: int = 1000,
    concentration: float = 20.0,
    max_pixels: int = 300_000,
    seed: int = 42,
) -> Dict:
    """
    SA-2: Monte Carlo weight sampling (Dirichlet) with spatial uncertainty.

    Args:
        criteria_dir: Path to normalized criteria TIFs.
        criteria_names: List of criterion names.
        base_weights: Dictionary of base AHP weights.
        height: Grid height.
        width: Grid width.
        n_samples: Number of Monte Carlo weight samples.
        concentration: Dirichlet concentration parameter.
        max_pixels: Maximum pixels to sample.
        seed: Random seed for reproducibility.

    Returns:
        Dictionary with keys: cv, ci_width, stable_fraction, weight_samples,
        n_samples, concentration, is_pixel_sampled, n_pixels_used.
    """
    rng = np.random.default_rng(seed)
    mat, vmask = _load_criteria_arrays(
        criteria_dir,
        criteria_names,
        height,
        width
    )
    mat_v = mat[vmask]
    n = mat_v.shape[0]
    sampled = n > max_pixels

    if sampled:
        mat_s = mat_v[
            rng.choice(n, size=min(n, max_pixels), replace=False)
        ]
    else:
        mat_s = mat_v

    w0 = np.array(
        [base_weights[c] for c in criteria_names],
        dtype=np.float64
    )
    w0 /= w0.sum()
    ws = rng.dirichlet(concentration * w0, size=n_samples)
    all_s = np.zeros((n_samples, mat_s.shape[0]), dtype=np.float32)

    for i, w in enumerate(ws):
        all_s[i] = _topsis_flat(mat_s, w.astype(np.float32))

    p05, p50, p95 = np.percentile(all_s, [5, 50, 95], axis=0)
    std = all_s.std(axis=0)
    cv = np.where(p50 > 0.01, std / p50, 0.0)

    return {
        "cv": cv,
        "ci_width": p95 - p05,
        "stable_fraction": float(((p95 - p05) < 0.10).mean()),
        "weight_samples": pd.DataFrame(
            ws,
            columns=criteria_names
        ).assign(sample_id=range(n_samples)),
        "n_samples": n_samples,
        "concentration": concentration,
        "is_pixel_sampled": sampled,
        "n_pixels_used": mat_s.shape[0],
    }


def sa3_threshold_sweep(
    suitability_tif: Path,
    pixel_area_km2_func: Callable,
    transform,
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
        suitability_tif: Path to suitability raster.
        pixel_area_km2_func: Function to compute pixel areas.
        transform: Rasterio affine transform.
        width: Grid width.
        height: Grid height.
        thresholds: List of thresholds to sweep (default 0.30-0.85 @ 0.05).
        power_density_mw_km2: Power density (MW/km²).
        land_use_factor: Land use factor [0-1].
        capacity_factor: Capacity factor [0-1].

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

    pxa = pixel_area_km2_func(transform, width, height)
    rows = []

    for thr in thresholds:
        mask = np.isfinite(score) & (score >= thr)
        area = float(pxa[mask].sum())
        gw = area * land_use_factor * power_density_mw_km2 / 1000.0
        twh = gw * capacity_factor * 8.760

        rows.append({
            "threshold": thr,
            "area_apt_km2": round(area, 1),
            "potential_gw": round(gw, 3),
            "generation_twh": round(twh, 3),
            "n_pixels": int(mask.sum())
        })

    df = pd.DataFrame(rows)
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
        if abs(dt) > 1e-10:
            elast[i] = da / dt
        else:
            elast[i] = np.nan

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
        base_capex_usd_kw: Nominal CAPEX (USD/kW).
        base_opex_usd_kw_yr: Nominal OPEX (USD/kW/yr).
        lifetime: Project lifetime (years).
        discount_rate: Discount rate (decimal, e.g., 0.06).
        capacity_factor: Nominal capacity factor [0-1].
        n_samples: Number of MC samples.
        capex_variation: Relative variation for CAPEX (e.g., 0.15 = ±15%).
        opex_variation: Relative variation for OPEX.
        cf_variation: Relative variation for CF.
        seed: Random seed.

    Returns:
        DataFrame with columns: capex, opex, cf, lcoe_usd_mwh.
        Stats stored in df.attrs["stats"].
    """
    rng = np.random.default_rng(seed)

    def _tri(val, var):
        return rng.triangular(
            val * (1 - var),
            val,
            val * (1 + var),
            size=n_samples
        )

    capex = _tri(base_capex_usd_kw, capex_variation)
    opex = _tri(base_opex_usd_kw_yr, opex_variation)
    cf = np.clip(_tri(capacity_factor, cf_variation), 0.01, 0.99)

    if abs(discount_rate) < 1e-9:
        crf = 1.0 / max(lifetime, 1)
    else:
        crf = (
            discount_rate * (1 + discount_rate) ** lifetime
            / ((1 + discount_rate) ** lifetime - 1)
        )

    lcoe = (capex * crf + opex) / (cf * 8760) * 1000

    df = pd.DataFrame({
        "capex": capex,
        "opex": opex,
        "cf": cf,
        "lcoe_usd_mwh": lcoe
    })

    df.attrs["stats"] = {
        "lcoe_p05": round(float(np.percentile(lcoe, 5)), 2),
        "lcoe_p25": round(float(np.percentile(lcoe, 25)), 2),
        "lcoe_p50": round(float(np.percentile(lcoe, 50)), 2),
        "lcoe_p75": round(float(np.percentile(lcoe, 75)), 2),
        "lcoe_p95": round(float(np.percentile(lcoe, 95)), 2),
        "lcoe_mean": round(float(lcoe.mean()), 2),
        "lcoe_std": round(float(lcoe.std()), 2),
        "ci90_width": round(
            float(np.percentile(lcoe, 95) - np.percentile(lcoe, 5)),
            2
        ),
        "capex_nominal": base_capex_usd_kw,
        "opex_nominal": base_opex_usd_kw_yr,
        "cf_nominal": capacity_factor,
        "discount_rate": discount_rate,
        "lifetime": lifetime,
        "n_samples": n_samples,
    }

    return df


def sa5_sobol_ghg(
    base_params: Dict,
    ghg_function: Callable,
    n_samples: int = 1024,
    seed: int = 42
) -> Optional[pd.DataFrame]:
    """
    SA-5: Sobol global sensitivity indices for GHG abatement function.

    Args:
        base_params: Dictionary of parameters with 'value' and 'range' keys.
        ghg_function: Parametric GHG function.
        n_samples: Number of Sobol samples (default 1024).
        seed: Random seed.

    Returns:
        DataFrame with columns: parameter, S1, S1_conf, ST, ST_conf, dominant.
        Returns None if SALib is not installed.
    """
    try:
        from SALib.sample import saltelli
        from SALib.analyze import sobol
    except ImportError:
        logger.warning(
            "[SA-5] SALib not installed. "
            "Skipping SA-5 (pip install SALib)."
        )
        return None

    import inspect

    _saltelli_sig = inspect.signature(saltelli.sample)
    _sobol_sig = inspect.signature(sobol.analyze)
    _saltelli_accepts_seed = "seed" in _saltelli_sig.parameters
    _sobol_accepts_seed = "seed" in _sobol_sig.parameters

    names = list(base_params.keys())
    prob = {
        "num_vars": len(names),
        "names": names,
        "bounds": [
            [
                base_params[p]["value"]
                * (1 - base_params[p].get("range", 0.15)),
                base_params[p]["value"]
                * (1 + base_params[p].get("range", 0.15)),
            ]
            for p in names
        ],
    }

    if _saltelli_accepts_seed:
        X = saltelli.sample(
            prob,
            n_samples,
            calc_second_order=False,
            seed=seed
        )
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
        med = np.nanmedian(Y)
        n_nan = int(np.isnan(Y).sum())
        logger.warning(
            "[SA-5] %d samples resulted in NaN — "
            "imputed with median (%s).",
            n_nan,
            f"{med:.4f}"
        )
        Y = np.where(np.isnan(Y), med, Y)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if _sobol_accepts_seed:
            Si = sobol.analyze(
                prob,
                Y,
                calc_second_order=False,
                seed=seed
            )
        else:
            np.random.seed(seed)
            Si = sobol.analyze(prob, Y, calc_second_order=False)

    return (
        pd.DataFrame({
            "parameter": names,
            "S1": np.round(Si["S1"], 4),
            "S1_conf": np.round(Si["S1_conf"], 4),
            "ST": np.round(Si["ST"], 4),
            "ST_conf": np.round(Si["ST_conf"], 4),
            "dominant": np.array(Si["S1"]) > 0.10,
        })
        .sort_values("ST", ascending=False)
        .reset_index(drop=True)
    )


def sa6_potential_sensitivity(
    suitability_tif: Path,
    pixel_area_km2_func: Callable,
    transform,
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
        suitability_tif: Path to suitability raster.
        pixel_area_km2_func: Function to compute pixel areas.
        transform: Rasterio affine transform.
        width: Grid width.
        height: Grid height.
        base_power_density: Base power density (MW/km²).
        base_land_use: Base land use factor [0-1].
        base_cf: Base capacity factor [0-1].
        base_threshold: Suitability threshold for base case.
        perturbations: List of relative perturbations.

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

    pxa = pixel_area_km2_func(transform, width, height)
    area = float(
        pxa[np.isfinite(score) & (score >= base_threshold)].sum()
    )
    base_gw = area * base_land_use * base_power_density / 1000.0
    base_twh = base_gw * base_cf * 8.760

    rows = []
    params_map = {
        "power_density_mw_km2": base_power_density,
        "land_use_factor": base_land_use,
        "capacity_factor": base_cf
    }

    for param, base_val in params_map.items():
        for delta in perturbations:
            pval = base_val * (1.0 + delta)

            if param == "power_density_mw_km2":
                gw = area * base_land_use * pval / 1000.0
                twh = gw * base_cf * 8.760
            elif param == "land_use_factor":
                gw = area * pval * base_power_density / 1000.0
                twh = gw * base_cf * 8.760
            else:
                gw = base_gw
                twh = base_gw * pval * 8.760

            if base_gw > 0:
                dgw = (gw - base_gw) / base_gw * 100.0
            else:
                dgw = 0.0

            if base_twh > 0:
                dtwh = (twh - base_twh) / base_twh * 100.0
            else:
                dtwh = 0.0

            if abs(delta) > 1e-9:
                elasticity_gw = dgw / (delta * 100.0)
                elasticity_twh = dtwh / (delta * 100.0)
            else:
                elasticity_gw = None
                elasticity_twh = None

            rows.append({
                "parameter": param,
                "perturbation_pct": round(delta * 100.0, 0),
                "base_value": round(base_val, 5),
                "perturbed_value": round(pval, 5),
                "potential_gw": round(gw, 3),
                "generation_twh": round(twh, 3),
                "delta_gw_pct": round(dgw, 2),
                "delta_twh_pct": round(dtwh, 2),
                "elasticity_gw": (
                    round(elasticity_gw, 3) if elasticity_gw else None
                ),
                "elasticity_twh": (
                    round(elasticity_twh, 3) if elasticity_twh else None
                ),
            })

    df = pd.DataFrame(rows)
    df.attrs.update({
        "base_gw": round(base_gw, 3),
        "base_twh": round(base_twh, 3),
        "base_area": round(area, 1)
    })

    return df


# ─────────────────────────────────────────────────────────────────────────────
# PLOTS AND DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────


def _watermark(fig: plt.Figure) -> None:
    """Add GeoWorld watermark to figure."""
    fig.text(
        0.99,
        0.005,
        f"GeoWorld Framework · {date.today()}",
        ha="right",
        fontsize=6.5,
        color=_MUTE_C,
        style="italic"
    )


def _draw_kpis(ax: plt.Axes, kpis: List[Tuple[str, str]]) -> None:
    """
    Draw KPI summary panel.

    Args:
        ax: Matplotlib axes.
        kpis: List of (label, value) tuples.
    """
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.add_patch(
        mpatches.FancyBboxPatch(
            (0.01, 0.01),
            0.98,
            0.97,
            boxstyle="round,pad=0.02",
            facecolor="white",
            edgecolor="#D1D5DB",
            lw=1
        )
    )

    y = 0.92

    for lbl, val in kpis:
        ax.text(
            0.04,
            y,
            lbl + ":",
            fontsize=7.5,
            va="top",
            color=_MUTE_C
        )
        ax.text(
            0.96,
            y,
            val,
            fontsize=7.5,
            va="top",
            ha="right",
            color=_TEXT_C,
            fontweight="bold"
        )
        y -= 0.11
        if y < 0.04:
            break


def _fig_sa1_tornado(
    df: pd.DataFrame,
    tech: str,
    out_path: Path
) -> None:
    """
    Generate SA-1 tornado plot.

    Args:
        df: SA-1 results DataFrame.
        tech: Technology name.
        out_path: Output file path.
    """
    summary = (
        df.groupby("criterion")
        .agg(
            rho_min=("spearman_rho", "min"),
            weight=("weight_base", "first")
        )
        .reset_index()
        .sort_values("rho_min")
    )

    fig, ax = plt.subplots(
        figsize=(10, max(4, len(summary) * 0.48 + 1.6)),
        dpi=130
    )
    fig.patch.set_facecolor(_FIG_BG)
    ax.set_facecolor(_FIG_BG)

    for idx, (_, row) in enumerate(summary.iterrows()):
        r = row["rho_min"]

        if r >= 0.95:
            color = "#16a34a"
        elif r >= 0.90:
            color = "#d97706"
        else:
            color = "#dc2626"

        ax.barh(idx, 1.0 - r, 0.60, color=color, alpha=0.82)
        ax.text(
            1.0 - r + 0.002,
            idx,
            f"ρ={r:.3f} (w={row['weight']:.3f})",
            va="center",
            fontsize=8.5,
            color=_TEXT_C
        )

    ax.set_yticks(range(len(summary)))
    ax.set_yticklabels(summary["criterion"], fontsize=9)
    ax.set_xlabel("1 − ρ_min (higher = more sensitive)", fontsize=10)
    ax.axvline(0.05, color="#dc2626", ls="--")
    ax.axvline(0.10, color="#d97706", ls=":")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", color=_GRID_C, lw=0.7)

    ax.set_title(
        f"SA-1 · OAT Weight Sensitivity — {_TECH_LABEL.get(tech, tech)}",
        fontsize=12,
        fontweight="bold",
        pad=10
    )

    _watermark(fig)
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_sa1_heatmap(
    df: pd.DataFrame,
    tech: str,
    out_path: Path
) -> None:
    """
    Generate SA-1 heatmap.

    Args:
        df: SA-1 results DataFrame.
        tech: Technology name.
        out_path: Output file path.
    """
    pivot = df.pivot_table(
        index="criterion",
        columns="perturbation_pct",
        values="spearman_rho",
        aggfunc="mean"
    )
    pivot = pivot.loc[pivot.min(axis=1).sort_values().index]

    fig, ax = plt.subplots(
        figsize=(12, max(4, len(pivot) * 0.45 + 1.5)),
        dpi=130
    )
    fig.patch.set_facecolor(_FIG_BG)

    im = ax.imshow(
        pivot.values,
        cmap="RdYlGn",
        vmin=0.85,
        vmax=1.0,
        aspect="auto"
    )
    cbar = fig.colorbar(im, ax=ax, shrink=0.75, pad=0.02)
    cbar.set_label("Spearman ρ", fontsize=9)

    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(
        [f"{int(v):+d}%" for v in pivot.columns],
        fontsize=9
    )
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index, fontsize=9)

    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            v = pivot.values[i, j]
            text_color = "black" if v > 0.92 else "white"
            ax.text(
                j,
                i,
                f"{v:.3f}",
                ha="center",
                va="center",
                fontsize=7,
                color=text_color
            )

    ax.tick_params(top=True, bottom=False, labelbottom=False)
    ax.set_title(
        f"SA-1 · Spearman ρ Heatmap — {_TECH_LABEL.get(tech, tech)}",
        fontsize=12,
        fontweight="bold",
        pad=10
    )

    _watermark(fig)
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_sa2_cv(
    cv: np.ndarray,
    ci: np.ndarray,
    tech: str,
    out_path: Path
) -> None:
    """
    Generate SA-2 CV distribution plots.

    Args:
        cv: Coefficient of variation array.
        ci: Confidence interval width array.
        tech: Technology name.
        out_path: Output file path.
    """
    color = _TECH_COLOR.get(tech, "#2563EB")
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.8), dpi=130)
    fig.patch.set_facecolor(_FIG_BG)

    for ax, data, title, xlabel, ref_val, ref_lbl in [
        (
            ax1,
            cv[np.isfinite(cv) & (cv >= 0)],
            "Coefficient of Variation (CV)",
            "CV per pixel",
            0.25,
            "CV=0.25"
        ),
        (
            ax2,
            ci[np.isfinite(ci) & (ci >= 0)],
            "90% CI Width",
            "P95 − P05 per pixel",
            0.10,
            "CI90=0.10"
        )
    ]:
        ax.set_facecolor(_FIG_BG)

        if data.size > 0:
            ax.hist(
                data,
                bins=60,
                color=color,
                alpha=0.75,
                edgecolor="white",
                lw=0.4
            )
            med = float(np.median(data))
            p90 = float(np.percentile(data, 90))

            ax.axvline(
                med,
                color="#111827",
                ls="--",
                label=f"Median={med:.3f}"
            )
            ax.axvline(
                p90,
                color="#dc2626",
                ls=":",
                label=f"P90={p90:.3f}"
            )
            ax.axvline(
                ref_val,
                color="#d97706",
                ls="-.",
                alpha=0.7,
                label=ref_lbl
            )
            ax.legend(fontsize=8.5)

        ax.set_title(title, fontsize=10, fontweight="bold")
        ax.set_xlabel(xlabel, fontsize=9)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color=_GRID_C, lw=0.7)

    fig.suptitle(
        f"SA-2 · MC AHP Uncertainty — {_TECH_LABEL.get(tech, tech)}",
        fontsize=12,
        fontweight="bold",
        y=1.02
    )

    _watermark(fig)
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_sa3_threshold(
    df: pd.DataFrame,
    tech: str,
    base_thr: float,
    out_path: Path
) -> None:
    """
    Generate SA-3 threshold sweep plot.

    Args:
        df: SA-3 results DataFrame.
        tech: Technology name.
        base_thr: Base threshold value.
        out_path: Output file path.
    """
    color = _TECH_COLOR.get(tech, "#2563EB")
    fig, ax1 = plt.subplots(figsize=(10, 5.5), dpi=130)
    fig.patch.set_facecolor(_FIG_BG)
    ax1.set_facecolor(_FIG_BG)

    l1, = ax1.plot(
        df["threshold"],
        df["potential_gw"],
        color=color,
        lw=2.5,
        marker="o",
        ms=5,
        label="Potential (GW)"
    )
    ax1.fill_between(
        df["threshold"],
        0,
        df["potential_gw"],
        color=color,
        alpha=0.10
    )
    ax1.axvline(
        base_thr,
        color="#374151",
        ls="--",
        alpha=0.8,
        label=f"Base ({base_thr:.2f})"
    )
    ax1.set_xlabel("Suitability Threshold", fontsize=10)
    ax1.set_ylabel(
        "Technical Potential (GW)",
        fontsize=10,
        color=color
    )

    ax2 = ax1.twinx()
    el = df["elasticity"].dropna()
    l2, = ax2.plot(
        df.loc[el.index, "threshold"],
        el,
        color="#7C3AED",
        lw=1.8,
        ls="--",
        marker="s",
        ms=4,
        label="Elasticity ε"
    )
    ax2.axhline(-1.0, color="#7C3AED", ls=":", alpha=0.5)
    ax2.set_ylabel("Elasticity ε", fontsize=9, color="#7C3AED")

    ax1.legend(
        [l1, l2],
        [l1.get_label(), l2.get_label()],
        fontsize=9,
        loc="upper right"
    )
    ax1.set_title(
        f"SA-3 · Threshold Sweep — {_TECH_LABEL.get(tech, tech)}",
        fontsize=12,
        fontweight="bold"
    )
    ax1.grid(color=_GRID_C, lw=0.6, alpha=0.8)

    _watermark(fig)
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_sa4_lcoe(
    df: pd.DataFrame,
    tech: str,
    out_path: Path
) -> None:
    """
    Generate SA-4 LCOE uncertainty plot.

    Args:
        df: SA-4 results DataFrame (with .attrs["stats"]).
        tech: Technology name.
        out_path: Output file path.
    """
    stats = df.attrs.get("stats", {})
    color = _TECH_COLOR.get(tech, "#F59E0B")

    fig, (ax_h, ax_s) = plt.subplots(
        1,
        2,
        figsize=(13, 5.5),
        dpi=130,
        gridspec_kw={"width_ratios": [2.5, 1]}
    )
    fig.patch.set_facecolor(_FIG_BG)
    ax_h.set_facecolor(_FIG_BG)

    lc = df["lcoe_usd_mwh"].values
    ax_h.hist(
        lc,
        bins=80,
        color=color,
        alpha=0.75,
        edgecolor="white",
        lw=0.3
    )

    p05 = stats.get("lcoe_p05", np.percentile(lc, 5))
    p50 = stats.get("lcoe_p50", np.percentile(lc, 50))
    p95 = stats.get("lcoe_p95", np.percentile(lc, 95))

    ax_h.axvline(
        p05,
        color="#1D4ED8",
        ls="--",
        label=f"P5 = {p05:.1f} USD/MWh"
    )
    ax_h.axvline(
        p50,
        color="#111827",
        ls="-",
        label=f"P50 = {p50:.1f} USD/MWh"
    )
    ax_h.axvline(
        p95,
        color="#DC2626",
        ls="--",
        label=f"P95 = {p95:.1f} USD/MWh"
    )

    ax_h.spines[["top", "right"]].set_visible(False)
    ax_h.set_xlabel("LCOE (USD/MWh)", fontsize=10)
    ax_h.legend(fontsize=9)

    ax_s.axis("off")
    ax_s.add_patch(
        mpatches.FancyBboxPatch(
            (0.01, 0.01),
            0.98,
            0.98,
            boxstyle="round,pad=0.02",
            facecolor="white",
            edgecolor="#D1D5DB"
        )
    )

    y = 0.97
    capex_nom = stats.get("capex_nominal", 0)
    opex_nom = stats.get("opex_nominal", 0)
    cf_nom = stats.get("cf_nominal", 0)

    for lbl, val in [
        ("Nominal CAPEX", f"${capex_nom:.0f}/kW" if capex_nom else "—"),
        ("Nominal OPEX", f"${opex_nom:.0f}/kW/yr" if opex_nom else "—"),
        ("Nominal CF", f"{cf_nom:.3f}" if cf_nom else "—"),
        ("P50", f"{p50:.1f} USD/MWh"),
        ("CI90", f"{p95 - p05:.1f} USD/MWh")
    ]:
        ax_s.text(0.04, y, lbl + ":", fontsize=8.5, color=_MUTE_C)
        ax_s.text(
            0.96,
            y,
            val,
            fontsize=8.5,
            ha="right",
            fontweight="bold"
        )
        y -= 0.08

    fig.suptitle(
        f"SA-4 · LCOE Uncertainty — {_TECH_LABEL.get(tech, tech)}",
        fontsize=12,
        fontweight="bold"
    )

    _watermark(fig)
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_sa5_sobol(df: pd.DataFrame, out_path: Path) -> None:
    """
    Generate SA-5 Sobol indices barplot.

    Args:
        df: SA-5 Sobol results DataFrame.
        out_path: Output file path.
    """
    fig, ax = plt.subplots(figsize=(9, 5), dpi=130)
    fig.patch.set_facecolor(_FIG_BG)
    ax.set_facecolor(_FIG_BG)

    x = np.arange(len(df))
    w = 0.36

    ax.bar(
        x - w / 2,
        df["S1"],
        w,
        color="#2563EB",
        label="S1 (1st order)",
        alpha=0.82
    )
    ax.bar(
        x + w / 2,
        df["ST"],
        w,
        color="#7C3AED",
        label="ST (total)",
        alpha=0.82
    )
    ax.errorbar(
        x - w / 2,
        df["S1"],
        yerr=df["S1_conf"],
        fmt="none",
        color="#1D4ED8",
        capsize=4
    )
    ax.errorbar(
        x + w / 2,
        df["ST"],
        yerr=df["ST_conf"],
        fmt="none",
        color="#5B21B6",
        capsize=4
    )
    ax.axhline(
        0.10,
        color="#DC2626",
        ls="--",
        label="S1=0.10 (dominant)"
    )

    for i, v in enumerate(df["ST"]):
        ax.text(
            i + w / 2,
            v + 0.01,
            f"{v:.3f}",
            ha="center",
            fontsize=8
        )

    ax.set_xticks(x)
    ax.set_xticklabels(df["parameter"], rotation=12, ha="right", fontsize=9)
    ax.set_title(
        "SA-5 · Sobol Indices (GHG Abatement Function)",
        fontsize=12,
        fontweight="bold"
    )
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(fontsize=9)

    _watermark(fig)
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_sa6_potential(
    df: pd.DataFrame,
    tech: str,
    out_path: Path
) -> None:
    """
    Generate SA-6 potential sensitivity plots.

    Args:
        df: SA-6 results DataFrame.
        tech: Technology name.
        out_path: Output file path.
    """
    fig, (ax_l, ax_b) = plt.subplots(1, 2, figsize=(14, 5.5), dpi=130)
    fig.patch.set_facecolor(_FIG_BG)
    ax_l.set_facecolor(_FIG_BG)
    ax_b.set_facecolor(_FIG_BG)

    p_colors = {
        "power_density_mw_km2": "#DC2626",
        "land_use_factor": "#2563EB",
        "capacity_factor": "#16A34A"
    }
    p_labels = {
        "power_density_mw_km2": "Power Density",
        "land_use_factor": "Land Use",
        "capacity_factor": "Capacity Factor"
    }

    for param in df["parameter"].unique():
        sub = df[df["parameter"] == param].sort_values("perturbation_pct")
        c = p_colors.get(param, "#888")
        lbl = p_labels.get(param, param)

        ax_l.plot(
            sub["perturbation_pct"],
            sub["delta_gw_pct"],
            color=c,
            lw=2.2,
            marker="o",
            label=f"{lbl} (GW)"
        )

        if param == "capacity_factor":
            ax_l.plot(
                sub["perturbation_pct"],
                sub["delta_twh_pct"],
                color=c,
                ls="--",
                marker="^",
                label=f"{lbl} (TWh)"
            )

    ax_l.axhline(0, color="#374151")
    ax_l.axvline(0, color="#D1D5DB", ls="--")
    ax_l.set_title(
        "OAT Curves — ΔGW (and ΔTWH for CF)",
        fontsize=10,
        fontweight="bold"
    )
    ax_l.set_xlabel("Perturbation (%)", fontsize=9)
    ax_l.set_ylabel("ΔPotential (%)", fontsize=9)
    ax_l.legend(fontsize=8.5)
    ax_l.spines[["top", "right"]].set_visible(False)

    s30 = (
        df[df["perturbation_pct"] == 30.0]
        .assign(abs_dgw=lambda x: x["delta_gw_pct"].abs())
        .sort_values("abs_dgw", ascending=False)
    )

    bars = ax_b.barh(
        [p_labels.get(p, p) for p in s30["parameter"]],
        s30["abs_dgw"],
        color=[p_colors.get(p, "#888") for p in s30["parameter"]],
        alpha=0.82
    )

    for bar, v in zip(bars, s30["abs_dgw"]):
        ax_b.text(
            bar.get_width() + 0.5,
            bar.get_y() + bar.get_height() / 2,
            f"{v:.1f}%",
            va="center",
            fontsize=9,
            fontweight="bold"
        )

    ax_b.set_title(
        "Elasticity at +30% perturbation",
        fontsize=10,
        fontweight="bold"
    )
    ax_b.spines[["top", "right"]].set_visible(False)

    fig.suptitle(
        f"SA-6 · Potential Sensitivity — {_TECH_LABEL.get(tech, tech)}",
        fontsize=12,
        fontweight="bold"
    )

    _watermark(fig)
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_dashboard(
    rs: Dict,
    tech: str,
    country_name: str,
    out_path: Path
) -> None:
    """
    Generate comprehensive sensitivity dashboard.

    Args:
        rs: Complete results dictionary (all technologies).
        tech: Technology name.
        country_name: Country name for title.
        out_path: Output file path.
    """
    td = rs.get(tech, {})
    color = _TECH_COLOR.get(tech, "#2563EB")

    fig = plt.figure(figsize=(22, 12), dpi=120)
    fig.patch.set_facecolor(_FIG_BG)

    gs = gridspec.GridSpec(
        2,
        4,
        figure=fig,
        hspace=0.46,
        wspace=0.38,
        left=0.05,
        right=0.97,
        top=0.91,
        bottom=0.06
    )

    axes = [fig.add_subplot(gs[r, c]) for r in range(2) for c in range(4)]

    for ax, lt in zip(axes, "abcdefgh"):
        ax.set_facecolor(_FIG_BG)
        ax.text(
            0.02,
            0.97,
            f"({lt})",
            transform=ax.transAxes,
            fontsize=9.5,
            fontweight="bold",
            va="top",
            zorder=10
        )

    # (a) SA-1 Tornado
    ax = axes[0]
    ax.set_title(
        "SA-1 · OAT Sensitivity\n(1−ρ_min per criterion)",
        fontsize=9,
        fontweight="bold"
    )

    if "sa1" in td and "_df" in td["sa1"]:
        sm = (
            td["sa1"]["_df"]
            .groupby("criterion")["spearman_rho"]
            .min()
            .reset_index()
            .sort_values("spearman_rho")
            .head(8)
        )
        cl = [
            "#16a34a" if r >= 0.95
            else "#d97706" if r >= 0.90
            else "#dc2626"
            for r in sm["spearman_rho"]
        ]
        ax.barh(
            range(len(sm)),
            1 - sm["spearman_rho"],
            color=cl,
            alpha=0.82
        )
        ax.set_yticks(range(len(sm)))
        ax.set_yticklabels(sm["criterion"], fontsize=7.5)
        ax.axvline(0.05, color="#dc2626", ls="--", alpha=0.6)
        ax.set_xlabel("1 − ρ_min", fontsize=8)
    else:
        ax.text(
            0.5,
            0.5,
            "SA-1 not executed",
            ha="center",
            color=_MUTE_C,
            fontsize=9
        )

    ax.spines[["top", "right"]].set_visible(False)

    # (b) SA-2 CV
    ax = axes[1]
    ax.set_title(
        "SA-2 · MC AHP\nCV Distribution",
        fontsize=9,
        fontweight="bold"
    )

    if "sa2" in td and "_cv" in td["sa2"]:
        cv_c = td["sa2"]["_cv"]
        cv_c = cv_c[np.isfinite(cv_c) & (cv_c >= 0)]

        if cv_c.size > 0:
            ax.hist(
                cv_c,
                bins=45,
                color=color,
                alpha=0.75,
                edgecolor="white"
            )
            ax.axvline(
                float(np.median(cv_c)),
                color="#111827",
                ls="--",
                label=f"Med={float(np.median(cv_c)):.3f}"
            )
            ax.legend(fontsize=7.5)
    else:
        ax.text(
            0.5,
            0.5,
            "SA-2 not executed",
            ha="center",
            color=_MUTE_C,
            fontsize=9
        )

    ax.set_xlabel("CV per pixel", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # (c) SA-3 Threshold
    ax = axes[2]
    ax.set_title(
        "SA-3 · Threshold Sweep\nPotential vs Threshold",
        fontsize=9,
        fontweight="bold"
    )

    if "sa3" in td and "_df" in td["sa3"]:
        df3 = td["sa3"]["_df"]
        ax.plot(
            df3["threshold"],
            df3["potential_gw"],
            color=color,
            lw=2.0,
            marker="o"
        )
        ax.fill_between(
            df3["threshold"],
            0,
            df3["potential_gw"],
            color=color,
            alpha=0.10
        )
        ax.axvline(
            td["sa3"].get("base_threshold", 0.60),
            color="#374151",
            ls="--",
            alpha=0.8
        )
    else:
        ax.text(
            0.5,
            0.5,
            "SA-3 not executed",
            ha="center",
            color=_MUTE_C,
            fontsize=9
        )

    ax.set_xlabel("Threshold", fontsize=8)
    ax.set_ylabel("GW", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # (d) SA-4 LCOE
    ax = axes[3]
    ax.set_title(
        "SA-4 · LCOE MC\n$/MWh Histogram",
        fontsize=9,
        fontweight="bold"
    )

    if "sa4" in td and "_df" in td["sa4"]:
        lc = td["sa4"]["_df"]["lcoe_usd_mwh"].values
        st = td["sa4"]["_df"].attrs.get("stats", td["sa4"])

        ax.hist(lc, bins=50, color=color, alpha=0.75, edgecolor="white")

        for p, pc, l in [
            (st.get("lcoe_p05", 0), "#1D4ED8", "P5"),
            (st.get("lcoe_p50", 0), "#111827", "P50"),
            (st.get("lcoe_p95", 0), "#DC2626", "P95")
        ]:
            ax.axvline(
                p,
                color=pc,
                ls="--" if l != "P50" else "-",
                label=f"{l}={p:.0f}"
            )

        ax.legend(fontsize=7.5)
    else:
        ax.text(
            0.5,
            0.5,
            "SA-4 not executed",
            ha="center",
            color=_MUTE_C,
            fontsize=9
        )

    ax.set_xlabel("LCOE ($/MWh)", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # (e) SA-6 OAT
    ax = axes[4]
    ax.set_title(
        "SA-6 · Potential Sensitivity\nΔGW (%) per parameter",
        fontsize=9,
        fontweight="bold"
    )

    p_c = {
        "power_density_mw_km2": "#DC2626",
        "land_use_factor": "#2563EB",
        "capacity_factor": "#16A34A"
    }
    p_s = {
        "power_density_mw_km2": "PD",
        "land_use_factor": "LU",
        "capacity_factor": "CF"
    }

    if "sa6" in td and "_df" in td["sa6"]:
        df6 = td["sa6"]["_df"]

        for param in df6["parameter"].unique():
            sub = df6[df6["parameter"] == param].sort_values(
                "perturbation_pct"
            )
            ax.plot(
                sub["perturbation_pct"],
                sub["delta_gw_pct"],
                color=p_c.get(param, "#888"),
                label=p_s.get(param, param)
            )

        ax.axhline(0, color="#374151")
        ax.legend(fontsize=7.5)
    else:
        ax.text(
            0.5,
            0.5,
            "SA-6 not executed",
            ha="center",
            color=_MUTE_C,
            fontsize=9
        )

    ax.set_xlabel("Perturbation (%)", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # (f) SA-1 Heatmap
    ax = axes[5]
    ax.set_title(
        "SA-1 · Heatmap ρ\n(Top 6 criteria)",
        fontsize=9,
        fontweight="bold"
    )

    if "sa1" in td and "_df" in td["sa1"]:
        df1 = td["sa1"]["_df"]
        piv = (
            df1[
                df1["criterion"].isin(
                    df1.groupby("criterion")["spearman_rho"]
                    .min()
                    .sort_values()
                    .head(6)
                    .index
                )
            ]
            .pivot_table(
                index="criterion",
                columns="perturbation_pct",
                values="spearman_rho",
                aggfunc="mean"
            )
        )
        piv = piv.loc[piv.min(axis=1).sort_values().index]

        if not piv.empty:
            ax.imshow(
                piv.values,
                cmap="RdYlGn",
                vmin=0.85,
                vmax=1.0,
                aspect="auto"
            )
            ax.set_xticks(range(len(piv.columns)))
            ax.set_xticklabels(
                [f"{int(v)}%" for v in piv.columns],
                fontsize=6.5
            )
            ax.set_yticks(range(len(piv.index)))
            ax.set_yticklabels(piv.index, fontsize=7)
    else:
        ax.text(
            0.5,
            0.5,
            "SA-1 not executed",
            ha="center",
            color=_MUTE_C,
            fontsize=9
        )

    # (g) SA1+SA2 KPIs
    ax = axes[6]
    ax.axis("off")
    ax.set_title("SA-1 + SA-2 Summary", fontsize=9, fontweight="bold")
    kpis_g = []

    if "sa1" in td:
        kpis_g += [
            ("SA-1 Robust criteria", str(td["sa1"].get("n_robust", "—"))),
            (
                "SA-1 Sensitive criteria",
                str(td["sa1"].get("n_sensitive", "—"))
            ),
            (
                "SA-1 Global min ρ",
                f"{td['sa1'].get('rho_min_global', 0.0):.4f}"
            )
        ]

    if "sa2" in td:
        kpis_g += [
            (
                "SA-2 Stable pixels",
                f"{td['sa2'].get('stable_fraction_90ci', 0) * 100:.1f}%"
            ),
            ("SA-2 Mean CV", f"{td['sa2'].get('mean_cv', 0):.4f}")
        ]

    _draw_kpis(ax, kpis_g)

    # (h) SA3+SA4+SA6 KPIs
    ax = axes[7]
    ax.axis("off")
    ax.set_title(
        "SA-3 + SA-4 + SA-6 Summary",
        fontsize=9,
        fontweight="bold"
    )
    kpis_h = []

    if "sa3" in td:
        df3 = td["sa3"].get("_df")
        bt = td["sa3"].get("base_threshold", 0.6)

        if df3 is not None and not df3[
            abs(df3["threshold"] - bt) < 0.001
        ].empty:
            kpis_h.append(
                (
                    "SA-3 GW @ balanced",
                    f"{df3[abs(df3['threshold'] - bt) < 0.001]['potential_gw'].values[0]:.2f}"
                )
            )

    if "sa4" in td:
        kpis_h += [
            ("SA-4 P50 $/MWh", f"{td['sa4'].get('lcoe_p50', '—')}"),
            ("SA-4 CI90", f"{td['sa4'].get('ci90_width', '—')} $/MWh")
        ]

    if "sa6" in td:
        kpis_h += [("SA-6 Base GW", str(td["sa6"].get("base_gw", "—")))]

    _draw_kpis(ax, kpis_h)

    fig.suptitle(
        f"Sensitivity Dashboard — {_TECH_LABEL.get(tech, tech)} · {country_name}",
        fontsize=14,
        fontweight="bold",
        y=0.97
    )

    _watermark(fig)
    fig.savefig(str(out_path), dpi=120, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────


def _sfmt(val: Any, fmt_str: str, default: str = "—") -> str:
    """
    Safe formatting helper.

    Args:
        val: Value to format.
        fmt_str: Format string (e.g., '.4f').
        default: Default string if formatting fails.

    Returns:
        Formatted string.
    """
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return default

    try:
        return format(float(val), fmt_str)
    except (ValueError, TypeError):
        return str(val)


class SensitivityAnalyzer:
    """Orchestrates Phase 8 evaluations (SA-1 to SA-6) with unified reporting."""

    def __init__(self, cfg, outputs_dir: Path):
        """
        Initialize SensitivityAnalyzer.

        Args:
            cfg: Configuration object from config_loader.
            outputs_dir: Base outputs directory path.
        """
        self.cfg = cfg
        self.outputs_dir = Path(outputs_dir)

    def _resolve_tech_params(
        self,
        tech: str,
        pot_results: Optional[Dict]
    ) -> Tuple[float, float, float, float]:
        """
        Resolves technology parameters from config and Phase 4 results.

        Args:
            tech: Technology name.
            pot_results: Phase 4 results dictionary (optional).

        Returns:
            Tuple of (power_density_mw_km2, land_use_factor,
            capacity_factor, threshold).
        """
        tp = (
            self.cfg.system
            .get("potential", {})
            .get("technologies", {})
            .get(tech, {})
        )

        pd_mw = float(tp.get("power_density_mw_km2", 30.0))
        luf = float(tp.get("land_use_factor", 0.20))
        cf = float(tp.get("capacity_factor_max", 0.22))
        thr = float(tp.get("base_threshold", 0.60))

        if pot_results:
            pp = (
                pot_results
                .get("techs", {})
                .get(tech, {})
                .get("params", {})
            )
            pd_mw = float(pp.get("power_density_mw_km2", pd_mw))
            luf = float(pp.get("land_use_factor", luf))
            cf = float(pp.get("capacity_factor", cf))
            thr = float(
                pp.get("thresholds", {}).get("balanced", thr)
            )

        return pd_mw, luf, cf, thr

    @staticmethod
    def _parse_weights_from_report(
        report_path: Path,
        tech: str
    ) -> Dict[str, float]:
        """
        Attempt to extract weights from a Phase 3 text report.

        Args:
            report_path: Path to report file.
            tech: Technology name (unused but kept for signature).

        Returns:
            Dictionary of criterion weights.
        """
        weights = {}

        try:
            text = report_path.read_text(encoding="utf-8")
            import re

            for line in text.split("\n"):
                m = re.match(
                    r'\s+(\w[\w_]+)\s*[:|]\s*(\d+\.?\d*)',
                    line
                )
                if m:
                    name = m.group(1)
                    val = float(m.group(2))
                    if 0 < val <= 1:
                        weights[name] = val

        except Exception:
            pass

        return weights

    def _load_suitability_from_disk(
        self,
        country_code: str,
        criteria_dir: Path
    ) -> Dict[str, Any]:
        """
        Loads suitability results from disk when Phase 3 was previously executed.

        Args:
            country_code: ISO country code.
            criteria_dir: Path to criteria directory.

        Returns:
            Dictionary with 'techs' key containing technology data.
        """
        techs: Dict[str, Any] = {}
        suitability_dir = self.outputs_dir / country_code / "suitability"

        tif_search_dirs = [
            suitability_dir / "tifs",
            suitability_dir,
            suitability_dir / "data",
        ]
        json_search_dirs = [
            suitability_dir / "tifs",
            suitability_dir / "data",
            suitability_dir,
        ]

        def _find_file(
            filename: str,
            search_dirs: List[Path]
        ) -> Optional[Path]:
            for d in search_dirs:
                p = d / filename
                if p.exists():
                    return p
            return None

        for tech in ["solar", "wind", "biomass"]:

            # 1. Locate suitability TIF
            tif_names = [
                f"{country_code}_{tech}_suitability.tif",
                f"{tech}_suitability.tif",
            ]
            suit_tif = None

            for name in tif_names:
                suit_tif = _find_file(name, tif_search_dirs)
                if suit_tif:
                    break

            if suit_tif is None:
                logger.debug("[%s] Suitability TIF not found.", tech)
                continue

            logger.info(
                "[%s] TIF found: %s",
                tech,
                suit_tif.relative_to(self.outputs_dir)
            )

            # 2. Locate weights JSON
            json_names = [
                f"{country_code}_{tech}_weights.json",
                f"{tech}_weights.json",
                f"{country_code}_{tech}_ahp_weights.json",
            ]
            weights_json = None

            for name in json_names:
                weights_json = _find_file(name, json_search_dirs)
                if weights_json:
                    break

            weights: Dict[str, float] = {}

            if weights_json:
                try:
                    raw = json.loads(
                        weights_json.read_text(encoding="utf-8")
                    )

                    if isinstance(raw, dict):
                        if "weights" in raw and isinstance(
                            raw["weights"], dict
                        ):
                            weights = {
                                str(k): float(v)
                                for k, v in raw["weights"].items()
                            }
                        else:
                            weights = {
                                str(k): float(v)
                                for k, v in raw.items()
                                if isinstance(v, (int, float))
                                and not isinstance(v, bool)
                            }

                    logger.info(
                        "[%s] %d weights loaded from %s",
                        tech,
                        len(weights),
                        weights_json.name
                    )

                except Exception as exc:
                    logger.warning(
                        "[%s] Failed to read weights JSON (%s): %s",
                        tech,
                        weights_json.name,
                        exc
                    )

            # 3. Fallback: infer criteria from TIFs in criteria_dir
            if not weights:
                logger.info(
                    "[%s] Weights JSON not found — "
                    "inferring criteria from %s",
                    tech,
                    criteria_dir
                )

                crit_tifs = sorted(criteria_dir.glob("*.tif"))
                crit_names: List[str] = []

                for tif_path in crit_tifs:
                    name = tif_path.stem

                    for prefix in (
                        f"{country_code}_{tech}_",
                        f"{country_code}_",
                        f"{tech}_",
                    ):
                        if name.startswith(prefix):
                            name = name[len(prefix):]
                            break

                    crit_names.append(name)

                if crit_names:
                    equal_w = 1.0 / len(crit_names)
                    weights = {n: equal_w for n in crit_names}
                    logger.warning(
                        "[%s] Using %d criteria with equal weights "
                        "(fallback — SA-1/SA-2 results may not reflect "
                        "actual AHP weights)",
                        tech,
                        len(weights)
                    )

            # 4. Register or discard technology
            if weights:
                techs[tech] = {"weights": weights, "error": False}
            else:
                logger.warning(
                    "[%s] No weights available — skipping technology.",
                    tech
                )

        if not techs:
            logger.warning("No technologies found with valid data on disk.")
            return {}

        return {"techs": techs}

    def run(
        self,
        country_code: str,
        suitability_results: Optional[Dict],
        criteria_dir: Path,
        country_name: str = "",
        lcoe_params: Optional[Dict] = None,
        pot_results: Optional[Dict] = None,
        abat_result: Optional[Dict] = None,
        run_sa1: bool = True,
        run_sa2: bool = True,
        run_sa3: bool = True,
        run_sa4: bool = True,
        run_sa5: bool = False,
        run_sa6: bool = True,
        n_mc_samples: int = 1000,
        sa5_ghg_function: Optional[Callable] = None,
        sa5_base_params: Optional[Dict] = None,
    ) -> Dict:
        """
        Execute sensitivity analysis suite (SA-1 to SA-6).

        Args:
            country_code: ISO country code.
            suitability_results: Phase 3 results dictionary.
            criteria_dir: Path to normalized criteria directory.
            country_name: Full country name for reporting.
            lcoe_params: LCOE parameters (optional).
            pot_results: Phase 4 results (optional).
            abat_result: Phase 7 results (optional).
            run_sa1: Enable SA-1 (OAT weight).
            run_sa2: Enable SA-2 (MC AHP).
            run_sa3: Enable SA-3 (threshold sweep).
            run_sa4: Enable SA-4 (LCOE uncertainty).
            run_sa5: Enable SA-5 (Sobol GHG).
            run_sa6: Enable SA-6 (potential sensitivity).
            n_mc_samples: Number of MC samples for SA-2/SA-4.
            sa5_ghg_function: Custom GHG function for SA-5.
            sa5_base_params: Custom base parameters for SA-5.

        Returns:
            Dictionary with results for each technology and SA module.
        """
        out_dir = self.outputs_dir / country_code / "sensitivity"
        out_dir.mkdir(parents=True, exist_ok=True)

        logger.info("=" * 62)
        logger.info(
            "SENSITIVITY ANALYZER — Phase 8 (%s)",
            country_code
        )
        logger.info("Output: %s", out_dir)
        logger.info("=" * 62)

        results_sa: Dict[str, Any] = {}
        t0_total = time.perf_counter()

        # ── Load suitability from disk if not in memory ──────────────────
        if not suitability_results or not suitability_results.get("techs"):
            logger.info(
                "Suitability results not available in memory. "
                "Attempting to load from disk..."
            )
            suitability_results = self._load_suitability_from_disk(
                country_code,
                Path(criteria_dir)
            )

            if (
                not suitability_results
                or not suitability_results.get("techs")
            ):
                logger.error(
                    "No suitability data found. "
                    "Run Phase 3 (SuitabilityBuilder) first."
                )
                return {}

        # ── Reference grid ───────────────────────────────────────────────
        ref_tif = next(Path(criteria_dir).glob("*.tif"), None)

        if not ref_tif:
            logger.error(
                "No criteria TIF found. "
                "Run Phase 2b (CriteriaBuilder) first."
            )
            return {}

        with rasterio.open(str(ref_tif)) as src:
            height = src.height
            width = src.width
            transform = src.transform

        logger.info("Reference grid: %d×%d pixels", height, width)
        logger.info(
            "Technologies to analyze: %s",
            list(suitability_results.get("techs", {}).keys())
        )

        # ── SA-5: build ghg_function only if needed ──────────────────────
        if run_sa5 and sa5_ghg_function is None and abat_result:
            sa5_ghg_function, sa5_base_params = (
                _build_ghg_function_from_abatement(abat_result)
            )

            if sa5_ghg_function:
                logger.info(
                    "[SA-5] GHG function built from Phase 7 "
                    "abatement results."
                )
            else:
                logger.warning(
                    "[SA-5] Could not build GHG function "
                    "(abatement result empty or CO2=0)."
                )

        # ══════════════════════════════════════════════════════════════════
        # Main loop: each technology
        # ══════════════════════════════════════════════════════════════════
        for tech, tech_data in suitability_results.get("techs", {}).items():
            if tech_data.get("error"):
                logger.warning("[%s] Marked with error — skipping.", tech)
                continue

            weights = tech_data.get("weights", {})

            if not weights:
                logger.warning(
                    "[%s] No AHP weights available — skipping.",
                    tech
                )
                continue

            logger.info(
                "[%s] Starting SA suite (%d criteria)...",
                tech,
                len(weights)
            )
            results_sa[tech] = {}

            # SA-1 ─────────────────────────────────────────────────────────
            if run_sa1:
                t0 = time.perf_counter()
                try:
                    logger.info("[%s] SA-1: OAT weight perturbation...", tech)

                    df1 = sa1_oat_weight_sensitivity(
                        Path(criteria_dir),
                        list(weights.keys()),
                        weights,
                        height,
                        width,
                    )

                    df1.to_csv(
                        out_dir / f"{country_code}_{tech}_sa1_oat.csv",
                        index=False,
                    )

                    rb = df1.groupby("criterion")["spearman_rho"].min()

                    results_sa[tech]["sa1"] = {
                        "n_robust": int((rb >= 0.95).sum()),
                        "n_sensitive": len(weights) - int((rb >= 0.95).sum()),
                        "most_sensitive": str(rb.idxmin()),
                        "rho_min_global": float(rb.min()),
                        "elapsed_s": round(time.perf_counter() - t0, 1),
                        "_df": df1,
                    }

                    _fig_sa1_tornado(
                        df1,
                        tech,
                        out_dir / f"{country_code}_{tech}_sa1_tornado.png"
                    )
                    _fig_sa1_heatmap(
                        df1,
                        tech,
                        out_dir / f"{country_code}_{tech}_sa1_heatmap.png"
                    )

                    logger.info(
                        "[%s] SA-1 complete: rho_min=%s, most_sensitive=%s",
                        tech,
                        f"{rb.min():.4f}",
                        rb.idxmin()
                    )

                except Exception as exc:
                    logger.error(
                        "[%s] SA-1 failed: %s",
                        tech,
                        exc,
                        exc_info=True
                    )

            # SA-2 ─────────────────────────────────────────────────────────
            if run_sa2:
                t0 = time.perf_counter()
                try:
                    logger.info(
                        "[%s] SA-2: Monte Carlo AHP (%d samples)...",
                        tech,
                        n_mc_samples
                    )

                    mc = sa2_monte_carlo_weights(
                        Path(criteria_dir),
                        list(weights.keys()),
                        weights,
                        height,
                        width,
                        n_samples=n_mc_samples,
                    )

                    results_sa[tech]["sa2"] = {
                        "stable_fraction_90ci": mc["stable_fraction"],
                        "mean_cv": float(mc["cv"].mean()),
                        "mean_ci90_width": float(mc["ci_width"].mean()),
                        "elapsed_s": round(time.perf_counter() - t0, 1),
                        "_cv": mc["cv"],
                    }

                    _fig_sa2_cv(
                        mc["cv"],
                        mc["ci_width"],
                        tech,
                        out_dir / f"{country_code}_{tech}_sa2_cv_dist.png"
                    )

                    logger.info(
                        "[%s] SA-2 complete: stable_fraction=%s",
                        tech,
                        f"{mc['stable_fraction']:.1%}"
                    )

                except Exception as exc:
                    logger.error(
                        "[%s] SA-2 failed: %s",
                        tech,
                        exc,
                        exc_info=True
                    )

            # SA-3 ─────────────────────────────────────────────────────────
            if run_sa3:
                t0 = time.perf_counter()
                try:
                    from src.utils.utils import (
                        compute_pixel_area_geodesic as _paf,
                    )

                    suit_tif = (
                        self.outputs_dir
                        / country_code
                        / "suitability"
                        / "tifs"
                        / f"{country_code}_{tech}_suitability.tif"
                    )

                    if not suit_tif.exists():
                        logger.warning(
                            "[%s] SA-3: Suitability TIF not found — skipping.",
                            tech
                        )
                    else:
                        logger.info("[%s] SA-3: threshold sweep...", tech)

                        pd_mw, luf, cf, base_thr = (
                            self._resolve_tech_params(tech, pot_results)
                        )

                        df3 = sa3_threshold_sweep(
                            suit_tif,
                            _paf,
                            transform,
                            width,
                            height,
                            power_density_mw_km2=pd_mw,
                            land_use_factor=luf,
                            capacity_factor=cf,
                        )

                        df3.to_csv(
                            out_dir
                            / f"{country_code}_{tech}_sa3_threshold_sweep.csv",
                            index=False,
                        )

                        results_sa[tech]["sa3"] = {
                            "base_threshold": base_thr,
                            "elapsed_s": round(time.perf_counter() - t0, 1),
                            "_df": df3,
                        }

                        _fig_sa3_threshold(
                            df3,
                            tech,
                            base_thr,
                            out_dir / f"{country_code}_{tech}_sa3_curve.png"
                        )

                        logger.info(
                            "[%s] SA-3 complete: %d thresholds evaluated",
                            tech,
                            len(df3)
                        )

                except Exception as exc:
                    logger.error(
                        "[%s] SA-3 failed: %s",
                        tech,
                        exc,
                        exc_info=True
                    )

            # SA-4 ─────────────────────────────────────────────────────────
            if run_sa4:
                t0 = time.perf_counter()
                try:
                    _SA4_DEFAULTS = {
                        "solar": {
                            "capex": 850,
                            "opex": 15,
                            "life": 25,
                            "dr": 0.06,
                            "cf": 0.18
                        },
                        "wind": {
                            "capex": 1400,
                            "opex": 40,
                            "life": 25,
                            "dr": 0.06,
                            "cf": 0.28
                        },
                        "biomass": {
                            "capex": 2500,
                            "opex": 100,
                            "life": 30,
                            "dr": 0.07,
                            "cf": 0.75
                        },
                    }

                    defs = _SA4_DEFAULTS.get(tech, _SA4_DEFAULTS["solar"])

                    logger.info(
                        "[%s] SA-4: LCOE Monte Carlo (%d samples)...",
                        tech,
                        max(n_mc_samples, 10_000)
                    )

                    lc = (
                        self.cfg.system
                        .get("lcoe", {})
                        .get("technologies", {})
                        .get(tech, {})
                    )

                    df4 = sa4_lcoe_uncertainty(
                        base_capex_usd_kw=float(
                            lc.get(
                                "capex_usd_kw",
                                lc.get("base_capex_usd_kw", defs["capex"])
                            )
                        ),
                        base_opex_usd_kw_yr=float(
                            lc.get(
                                "opex_usd_kw_yr",
                                lc.get("base_opex_usd_kw_yr", defs["opex"])
                            )
                        ),
                        lifetime=int(
                            lc.get("lifetime_years", defs["life"])
                        ),
                        discount_rate=float(
                            lc.get("discount_rate", defs["dr"])
                        ),
                        capacity_factor=float(
                            lc.get("capacity_factor", defs["cf"])
                        ),
                        n_samples=max(n_mc_samples, 10_000),
                    )

                    df4.to_csv(
                        out_dir / f"{country_code}_{tech}_sa4_lcoe_mc.csv",
                        index=False,
                    )

                    results_sa[tech]["sa4"] = {
                        **df4.attrs.get("stats", {}),
                        "elapsed_s": round(time.perf_counter() - t0, 1),
                        "_df": df4,
                    }

                    _fig_sa4_lcoe(
                        df4,
                        tech,
                        out_dir / f"{country_code}_{tech}_sa4_lcoe_hist.png"
                    )

                    st = df4.attrs.get("stats", {})

                    logger.info(
                        "[%s] SA-4 complete: P50=$%s/MWh, CI90=$%s",
                        tech,
                        f"{st.get('lcoe_p50', 0):.1f}",
                        f"{st.get('ci90_width', 0):.1f}"
                    )

                except Exception as exc:
                    logger.error(
                        "[%s] SA-4 failed: %s",
                        tech,
                        exc,
                        exc_info=True
                    )

            # SA-6 ─────────────────────────────────────────────────────────
            if run_sa6:
                t0 = time.perf_counter()
                try:
                    from src.utils.utils import (
                        compute_pixel_area_geodesic as _paf,
                    )

                    suit_tif = (
                        self.outputs_dir
                        / country_code
                        / "suitability"
                        / "tifs"
                        / f"{country_code}_{tech}_suitability.tif"
                    )

                    if not suit_tif.exists():
                        logger.warning(
                            "[%s] SA-6: Suitability TIF not found — skipping.",
                            tech
                        )
                    else:
                        logger.info(
                            "[%s] SA-6: potential parameter sensitivity...",
                            tech
                        )

                        pd_mw, luf, cf, base_thr = (
                            self._resolve_tech_params(tech, pot_results)
                        )

                        df6 = sa6_potential_sensitivity(
                            suit_tif,
                            _paf,
                            transform,
                            width,
                            height,
                            pd_mw,
                            luf,
                            cf,
                            base_thr,
                        )

                        df6.to_csv(
                            out_dir
                            / f"{country_code}_{tech}_sa6_potential_oat.csv",
                            index=False,
                        )

                        results_sa[tech]["sa6"] = {
                            "base_gw": df6.attrs.get("base_gw", 0),
                            "base_twh": df6.attrs.get("base_twh", 0),
                            "base_area": df6.attrs.get("base_area", 0),
                            "elapsed_s": round(time.perf_counter() - t0, 1),
                            "_df": df6,
                        }

                        _fig_sa6_potential(
                            df6,
                            tech,
                            out_dir / f"{country_code}_{tech}_sa6_potential.png"
                        )

                        logger.info(
                            "[%s] SA-6 complete: base=%s GW",
                            tech,
                            f"{df6.attrs.get('base_gw', 0):.1f}"
                        )

                except Exception as exc:
                    logger.error(
                        "[%s] SA-6 failed: %s",
                        tech,
                        exc,
                        exc_info=True
                    )

            # Dashboard per technology ─────────────────────────────────────
            try:
                _fig_dashboard(
                    results_sa,
                    tech,
                    country_name or country_code,
                    out_dir / f"{country_code}_{tech}_dashboard.png",
                )

                logger.info(
                    "[%s] Dashboard saved: %s",
                    tech,
                    f"{country_code}_{tech}_dashboard.png"
                )

            except Exception as exc:
                logger.error(
                    "[%s] Dashboard failed: %s",
                    tech,
                    exc,
                    exc_info=True
                )

        # ══════════════════════════════════════════════════════════════════
        # SA-5: Sobol GHG (runs once, cross-technology)
        # ══════════════════════════════════════════════════════════════════
        if run_sa5 and sa5_ghg_function and sa5_base_params:
            logger.info("[SA-5] Sobol global sensitivity (GHG abatement)...")
            try:
                df5 = sa5_sobol_ghg(
                    sa5_base_params,
                    sa5_ghg_function,
                    n_samples=1024
                )

                if df5 is not None:
                    df5.to_csv(
                        out_dir / f"{country_code}_sa5_sobol_ghg.csv",
                        index=False,
                    )

                    results_sa["sa5_sobol"] = {
                        "dominant": str(df5.iloc[0]["parameter"]),
                        "_df": df5,
                    }

                    _fig_sa5_sobol(
                        df5,
                        out_dir / f"{country_code}_sa5_sobol_barplot.png"
                    )

                    logger.info(
                        "[SA-5] Sobol complete: dominant=%s (ST=%s)",
                        df5.iloc[0]["parameter"],
                        f"{df5.iloc[0]['ST']:.3f}"
                    )

            except Exception as exc:
                logger.error(
                    "[SA-5] Execution failed: %s",
                    exc,
                    exc_info=True
                )

        elif run_sa5:
            logger.warning(
                "[SA-5] SA-5 enabled but no valid ghg_function "
                "(abatement result empty or Phase 7 not executed)."
            )

        # ── Final report ─────────────────────────────────────────────────
        elapsed_total = time.perf_counter() - t0_total
        report = self._format_report(
            results_sa,
            country_code,
            country_name,
            elapsed_total
        )

        (out_dir / f"{country_code}_sensitivity_report.txt").write_text(
            report,
            encoding="utf-8"
        )

        logger.info(
            "[%s] Phase 8 completed in %ss.",
            country_code,
            f"{elapsed_total:.1f}"
        )
        logger.info(
            "[%s] Outputs: %d CSVs, %d plots",
            country_code,
            len(list(out_dir.glob("*.csv"))),
            len(list(out_dir.glob("*.png")))
        )

        return results_sa

    def _format_report(
        self,
        rs: Dict,
        code: str,
        country_name: str,
        elapsed: float
    ) -> str:
        """
        Generate text report for sensitivity analysis.

        Args:
            rs: Results dictionary.
            code: Country code.
            country_name: Country name.
            elapsed: Total elapsed time (seconds).

        Returns:
            Formatted text report.
        """
        blocks = [
            f"""\
========================================================================
SENSITIVITY ANALYSIS REPORT — {code} [{country_name}]
Generated on: {date.today()}
========================================================================"""
        ]

        for tech, td in rs.items():
            if tech == "sa5_sobol":
                continue

            sa1 = td.get("sa1", {})
            sa2 = td.get("sa2", {})
            sa3 = td.get("sa3", {})
            sa4 = td.get("sa4", {})
            sa6 = td.get("sa6", {})

            block_tech = f"""
{tech.upper()} [{_TECH_LABEL.get(tech, tech)}]
------------------------------------------------------------------------
  SA-1 · OAT Weight Sensitivity
      Robust criteria (ρ >= 0.95)   : {sa1.get('n_robust', '—')}
      Sensitive criteria (ρ < 0.95) : {sa1.get('n_sensitive', '—')}
      Global minimum ρ              : {_sfmt(sa1.get('rho_min_global'), '.4f')}
      Most sensitive criterion      : {sa1.get('most_sensitive', '—')}

  SA-2 · Monte Carlo AHP (Weight Uncertainty)
      Stable pixels (CI90 < 0.10)   : {_sfmt(sa2.get('stable_fraction_90ci', 0) * 100, '.1f', '0.0')}%
      Mean CV Spatial Baseline      : {_sfmt(sa2.get('mean_cv'), '.4f')}
      Mean CI90 width globally      : {_sfmt(sa2.get('mean_ci90_width'), '.4f')}

  SA-3 · Threshold Sweep (Area Elasticity)
      Base threshold (balanced)     : {_sfmt(sa3.get('base_threshold'), '.2f')}

  SA-4 · LCOE Uncertainty (Triangular Monte Carlo)
      LCOE P50 (Median Proxy)       : {_sfmt(sa4.get('lcoe_p50'), '.1f')} USD/MWh
      Confidence Interval (90%)     : {_sfmt(sa4.get('ci90_width'), '.1f')} USD/MWh Spread
      Standard Deviation            : {_sfmt(sa4.get('lcoe_std'), '.1f')} USD/MWh

  SA-6 · Potential Parameter Sensitivity (OAT)
      Base potential evaluated      : {_sfmt(sa6.get('base_gw'), '.1f')} GW
      Base generation estimated     : {_sfmt(sa6.get('base_twh'), '.1f')} TWh/yr
"""
            blocks.append(block_tech)

        if "sa5_sobol" in rs:
            sa5 = rs["sa5_sobol"]
            df5 = sa5.get("_df")

            block_sobol = f"""\
------------------------------------------------------------------------
SA-5 · Sobol Global Sensitivity (GHG Abatement Function)
------------------------------------------------------------------------
Dominant parameter (ST proxy) : {sa5.get('dominant', '—')}

"""
            if df5 is not None:
                block_sobol += (
                    f"  {'Parameter':<35} {'S1':>8}  {'±S1':>8}  "
                    f"{'ST':>8}  {'±ST':>8}\n"
                )
                block_sobol += "  " + "─" * 65 + "\n"

                for _, r5 in df5.iterrows():
                    block_sobol += (
                        f"  {r5['parameter']:<35} "
                        f"{_sfmt(r5['S1'], '.4f'):>8}  "
                        f"{_sfmt(r5['S1_conf'], '.4f'):>8}  "
                        f"{_sfmt(r5['ST'], '.4f'):>8}  "
                        f"{_sfmt(r5['ST_conf'], '.4f'):>8}\n"
                    )

            blocks.append(block_sobol)

        blocks.append(
            f"""\
========================================================================
Total analysis time: {elapsed:.1f}s
========================================================================"""
        )

        return "\n".join(blocks)