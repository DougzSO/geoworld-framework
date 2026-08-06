"""
sensitivity_analyzer.py — Phase 8: Sensitivity Analysis
==========================================================
Sensitivity analysis for the GeoWorld pipeline targeting Q1 publications.

SA-1 · OAT Weight Sensitivity: Perturbs AHP weights (±10 to 30%). Spearman ρ.
SA-2 · Monte Carlo AHP: Dirichlet distributions on weights (spatial robustness).
SA-3 · Threshold Sweep: Area elasticity vs. spatial suitability constraints.
SA-4 · LCOE Uncertainty: Triangular Monte Carlo for CAPEX, OPEX, CF.
SA-5 · Potential Sensitivity: Parameter elasticities (Power Density, CF).
SA-6 · Sobol Global Sensitivity: GHG Abatement indices (S1 and ST).

References:
  - Saltelli et al. (2008). Global Sensitivity Analysis.
  - Malczewski (1999). GIS and Multicriteria Decision Analysis.

Changelog
---------
  BUG_02 (fix) : Matching de tecnologias em _load_suitability_from_disk e
                 _load_weights_from_disk usava `in` (substring), o que tornava
                 o matching ambíguo se um nome de tecnologia fosse substring de
                 outro (ex: "wind" dentro de "windoffshore"). Corrigido para
                 comparação exata por stem do ficheiro.

  BUG_05 (fix) : _lcoe_params_for_tech acessava country_params.lcoe, atributo
                 inexistente em CountryParams. Corrigido para aceder
                 country_params.lcoe_solar / lcoe_wind / lcoe_biomass
                 diretamente, com fallback para cfg dict.

  IMM_09 (fix) : Ambos os métodos de carregamento de pesos já usavam rglob
                 (parcialmente corrigido em sessão anterior). O matching exato
                 por stem completa a correção.

  DUP_21 (ref) : Refatorado _format_report para usar reporting.build_phase_report.
                 Substitui formatação manual (~80 linhas) por estrutura
                 hierárquica com ReportSection e subsections.

  BUG_06 (fix) : _load_suitability_from_disk localizava o TIF de suitability
                 via rglob(f"*{tech}_suitability*.tif"), wildcard que também
                 casa com os GeoTIFFs OWA ({code}_{tech}_suitability_owa_
                 {scenario}.tif). Em produção (ver PRT), next() sobre esse
                 glob pode devolver um raster OWA em vez do TOPSIS esperado,
                 dependendo da ordem de iteração do filesystem. Corrigido
                 para nome exato esperado, com fallback por stem exato.
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

from src.core.schemas import CountryParams
from src.utils.raster_io import find_suitability_tif
from src.utils.reporting import ReportSection, build_phase_report

logger = logging.getLogger("geoworld.processors.SensitivityAnalyzer")

# ── Visual palette ──────────────────────────────────────────────────────────
_TECH_COLOR = {
    "solar":   "#F9A825",
    "wind":    "#1565C0",
    "biomass": "#2E7D32",
}
_TECH_LABEL = {
    "solar":   "Solar PV",
    "wind":    "Wind Onshore",
    "biomass": "Biomass / Bioenergy",
}
_FIG_BG = "#FAFAFA"
_GRID_C = "#E5E7EB"
_TEXT_C = "#111827"
_MUTE_C = "#6B7280"

# ── SA-4 fallback defaults (used only when CountryParams has no LCOE config) ─
_SA4_DEFAULTS: Dict[str, Dict[str, float]] = {
    "solar":   {"capex": 850,  "opex": 15,  "life": 25, "dr": 0.06, "cf": 0.18},
    "wind":    {"capex": 1400, "opex": 40,  "life": 25, "dr": 0.06, "cf": 0.28},
    "biomass": {"capex": 2500, "opex": 100, "life": 30, "dr": 0.07, "cf": 0.75},
}

# ── Mapa canônico: tech → atributo LCOETechParams em CountryParams ──────────
# Evita strings hardcoded em _lcoe_params_for_tech (BUG_05).
_LCOE_ATTR: Dict[str, str] = {
    "solar":   "lcoe_solar",
    "wind":    "lcoe_wind",
    "biomass": "lcoe_biomass",
}


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
    n_samples: int = 1000,
    concentration: float = 20.0,
    max_pixels: int = 300_000,
    seed: int = 42,
) -> Dict[str, Any]:
    """
    SA-2: Monte Carlo weight sampling (Dirichlet) with spatial uncertainty.

    Args:
        criteria_dir:   Path to normalized criteria TIFs.
        criteria_names: List of criterion names.
        base_weights:   Dictionary of base AHP weights.
        height:         Grid height.
        width:          Grid width.
        n_samples:      Number of Monte Carlo weight samples.
        concentration:  Dirichlet concentration parameter.
        max_pixels:     Maximum pixels to sample.
        seed:           Random seed for reproducibility.

    Returns:
        Dictionary with keys: cv, ci_width, stable_fraction, weight_samples,
        n_samples, concentration, is_pixel_sampled, n_pixels_used.
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
    ws = rng.dirichlet(concentration * w0, size=n_samples)

    all_s = np.zeros((n_samples, mat_s.shape[0]), dtype=np.float32)
    for i, w in enumerate(ws):
        all_s[i] = _topsis_flat(mat_s, w.astype(np.float32))

    p05, p50, p95 = np.percentile(all_s, [5, 50, 95], axis=0)
    std = all_s.std(axis=0)
    cv  = np.where(p50 > 0.01, std / p50, 0.0)

    # T8_19: stable_fraction reflete pixels com CI90 < 0.10.
    # Valor baixo (e.g., 2%) não indica erro — mostra que a maioria dos pixels
    # tem incerteza moderada a alta, típico em regiões com grande variação
    # espacial dos critérios (ex: Portugal, com montanha + litoral + planície).
    stable_fraction = float(((p95 - p05) < 0.10).mean())

    return {
        "cv":              cv,
        "ci_width":        p95 - p05,
        "stable_fraction": stable_fraction,
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
        height:               Grid height.
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
        height:               Grid height.
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
        "base_gw":   round(base_gw,  3),
        "base_twh":  round(base_twh, 3),
        "base_area": round(area,     1),
    })
    return df


# ─────────────────────────────────────────────────────────────────────────────
# PLOTS AND DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────


def _watermark(fig: plt.Figure) -> None:
    """Add GeoWorld watermark to figure."""
    fig.text(
        0.99, 0.005,
        f"GeoWorld Framework · {date.today()}",
        ha="right", fontsize=6.5, color=_MUTE_C, style="italic",
    )


def _draw_kpis(ax: plt.Axes, kpis: List[Tuple[str, str]]) -> None:
    """Draw KPI summary panel."""
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.add_patch(mpatches.FancyBboxPatch(
        (0.01, 0.01), 0.98, 0.97,
        boxstyle="round,pad=0.02",
        facecolor="white", edgecolor="#D1D5DB", lw=1,
    ))
    y = 0.92
    for lbl, val in kpis:
        ax.text(0.04, y, lbl + ":", fontsize=7.5, va="top", color=_MUTE_C)
        ax.text(0.96, y, val, fontsize=7.5, va="top", ha="right", color=_TEXT_C, fontweight="bold")
        y -= 0.11
        if y < 0.04:
            break


def _fig_sa1_tornado(df: pd.DataFrame, tech: str, out_path: Path) -> None:
    """Generate SA-1 tornado plot."""
    summary = (
        df.groupby("criterion")
        .agg(rho_min=("spearman_rho", "min"), weight=("weight_base", "first"))
        .reset_index()
        .sort_values("rho_min")
    )
    fig, ax = plt.subplots(figsize=(10, max(4, len(summary) * 0.48 + 1.6)), dpi=130)
    fig.patch.set_facecolor(_FIG_BG)
    ax.set_facecolor(_FIG_BG)
    for idx, (_, row) in enumerate(summary.iterrows()):
        r     = row["rho_min"]
        color = "#16a34a" if r >= 0.95 else "#d97706" if r >= 0.90 else "#dc2626"
        ax.barh(idx, 1.0 - r, 0.60, color=color, alpha=0.82)
        ax.text(
            1.0 - r + 0.002, idx,
            f"ρ={r:.3f} (w={row['weight']:.3f})",
            va="center", fontsize=8.5, color=_TEXT_C,
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
        fontsize=12, fontweight="bold", pad=10,
    )
    _watermark(fig)
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_sa1_heatmap(df: pd.DataFrame, tech: str, out_path: Path) -> None:
    """Generate SA-1 heatmap."""
    pivot = df.pivot_table(
        index="criterion", columns="perturbation_pct",
        values="spearman_rho", aggfunc="mean",
    )
    pivot = pivot.loc[pivot.min(axis=1).sort_values().index]
    fig, ax = plt.subplots(figsize=(12, max(4, len(pivot) * 0.45 + 1.5)), dpi=130)
    fig.patch.set_facecolor(_FIG_BG)
    im   = ax.imshow(pivot.values, cmap="RdYlGn", vmin=0.85, vmax=1.0, aspect="auto")
    cbar = fig.colorbar(im, ax=ax, shrink=0.75, pad=0.02)
    cbar.set_label("Spearman ρ", fontsize=9)
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels([f"{int(v):+d}%" for v in pivot.columns], fontsize=9)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index, fontsize=9)
    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            v = pivot.values[i, j]
            ax.text(j, i, f"{v:.3f}", ha="center", va="center",
                    fontsize=7, color="black" if v > 0.92 else "white")
    ax.tick_params(top=True, bottom=False, labelbottom=False)
    ax.set_title(
        f"SA-1 · Spearman ρ Heatmap — {_TECH_LABEL.get(tech, tech)}",
        fontsize=12, fontweight="bold", pad=10,
    )
    _watermark(fig)
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_sa2_cv(cv: np.ndarray, ci: np.ndarray, tech: str, out_path: Path) -> None:
    """Generate SA-2 CV distribution plots."""
    color = _TECH_COLOR.get(tech, "#2563EB")
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4.8), dpi=130)
    fig.patch.set_facecolor(_FIG_BG)
    for ax, data, title, xlabel, ref_val, ref_lbl in [
        (ax1, cv[np.isfinite(cv) & (cv >= 0)], "Coefficient of Variation (CV)",
         "CV per pixel",       0.25, "CV=0.25"),
        (ax2, ci[np.isfinite(ci) & (ci >= 0)], "90% CI Width",
         "P95 − P05 per pixel", 0.10, "CI90=0.10"),
    ]:
        ax.set_facecolor(_FIG_BG)
        if data.size > 0:
            ax.hist(data, bins=60, color=color, alpha=0.75, edgecolor="white", lw=0.4)
            med = float(np.median(data))
            p90 = float(np.percentile(data, 90))
            ax.axvline(med,     color="#111827", ls="--", label=f"Median={med:.3f}")
            ax.axvline(p90,     color="#dc2626", ls=":",  label=f"P90={p90:.3f}")
            ax.axvline(ref_val, color="#d97706", ls="-.", alpha=0.7, label=ref_lbl)
            ax.legend(fontsize=8.5)
        ax.set_title(title,  fontsize=10, fontweight="bold")
        ax.set_xlabel(xlabel, fontsize=9)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color=_GRID_C, lw=0.7)
    fig.suptitle(
        f"SA-2 · MC AHP Uncertainty — {_TECH_LABEL.get(tech, tech)}",
        fontsize=12, fontweight="bold", y=1.02,
    )
    _watermark(fig)
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_sa3_threshold(df: pd.DataFrame, tech: str, base_thr: float, out_path: Path) -> None:
    """Generate SA-3 threshold sweep plot."""
    color = _TECH_COLOR.get(tech, "#2563EB")
    fig, ax1 = plt.subplots(figsize=(10, 5.5), dpi=130)
    fig.patch.set_facecolor(_FIG_BG)
    ax1.set_facecolor(_FIG_BG)
    l1, = ax1.plot(
        df["threshold"], df["potential_gw"],
        color=color, lw=2.5, marker="o", ms=5, label="Potential (GW)",
    )
    ax1.fill_between(df["threshold"], 0, df["potential_gw"], color=color, alpha=0.10)
    ax1.axvline(base_thr, color="#374151", ls="--", alpha=0.8, label=f"Base ({base_thr:.2f})")
    ax1.set_xlabel("Suitability Threshold", fontsize=10)
    ax1.set_ylabel("Technical Potential (GW)", fontsize=10, color=color)
    ax2  = ax1.twinx()
    el   = df["elasticity"].dropna()
    l2,  = ax2.plot(
        df.loc[el.index, "threshold"], el,
        color="#7C3AED", lw=1.8, ls="--", marker="s", ms=4, label="Elasticity ε",
    )
    ax2.axhline(-1.0, color="#7C3AED", ls=":", alpha=0.5)
    ax2.set_ylabel("Elasticity ε", fontsize=9, color="#7C3AED")
    ax1.legend([l1, l2], [l1.get_label(), l2.get_label()], fontsize=9, loc="upper right")
    ax1.set_title(
        f"SA-3 · Threshold Sweep — {_TECH_LABEL.get(tech, tech)}",
        fontsize=12, fontweight="bold",
    )
    ax1.grid(color=_GRID_C, lw=0.6, alpha=0.8)
    _watermark(fig)
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_sa4_lcoe(df: pd.DataFrame, tech: str, out_path: Path) -> None:
    """Generate SA-4 LCOE uncertainty plot."""
    stats = df.attrs.get("stats", {})
    color = _TECH_COLOR.get(tech, "#F59E0B")
    fig, (ax_h, ax_s) = plt.subplots(
        1, 2, figsize=(13, 5.5), dpi=130,
        gridspec_kw={"width_ratios": [2.5, 1]},
    )
    fig.patch.set_facecolor(_FIG_BG)
    ax_h.set_facecolor(_FIG_BG)
    lc  = df["lcoe_usd_mwh"].values
    ax_h.hist(lc, bins=80, color=color, alpha=0.75, edgecolor="white", lw=0.3)
    p05 = stats.get("lcoe_p05", np.percentile(lc,  5))
    p50 = stats.get("lcoe_p50", np.percentile(lc, 50))
    p95 = stats.get("lcoe_p95", np.percentile(lc, 95))
    ax_h.axvline(p05, color="#1D4ED8", ls="--", label=f"P5 = {p05:.1f} USD/MWh")
    ax_h.axvline(p50, color="#111827", ls="-",  label=f"P50 = {p50:.1f} USD/MWh")
    ax_h.axvline(p95, color="#DC2626", ls="--", label=f"P95 = {p95:.1f} USD/MWh")
    ax_h.spines[["top", "right"]].set_visible(False)
    ax_h.set_xlabel("LCOE (USD/MWh)", fontsize=10)
    ax_h.legend(fontsize=9)
    ax_s.axis("off")
    ax_s.add_patch(mpatches.FancyBboxPatch(
        (0.01, 0.01), 0.98, 0.98, boxstyle="round,pad=0.02",
        facecolor="white", edgecolor="#D1D5DB",
    ))
    y = 0.97
    for lbl, val in [
        ("Nominal CAPEX", f"${stats.get('capex_nominal', 0):.0f}/kW"),
        ("Nominal OPEX",  f"${stats.get('opex_nominal',  0):.0f}/kW/yr"),
        ("Nominal CF",    f"{stats.get('cf_nominal',     0):.3f}"),
        ("P50",           f"{p50:.1f} USD/MWh"),
        ("CI90",          f"{p95 - p05:.1f} USD/MWh"),
    ]:
        ax_s.text(0.04, y, lbl + ":", fontsize=8.5, color=_MUTE_C)
        ax_s.text(0.96, y, val, fontsize=8.5, ha="right", fontweight="bold")
        y -= 0.08
    fig.suptitle(
        f"SA-4 · LCOE Uncertainty — {_TECH_LABEL.get(tech, tech)}",
        fontsize=12, fontweight="bold",
    )
    _watermark(fig)
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_sa5_sobol(df: pd.DataFrame, out_path: Path) -> None:
    """Generate SA-5 Sobol indices barplot."""
    fig, ax = plt.subplots(figsize=(9, 5), dpi=130)
    fig.patch.set_facecolor(_FIG_BG)
    ax.set_facecolor(_FIG_BG)
    x = np.arange(len(df))
    w = 0.36
    ax.bar(x - w / 2, df["S1"], w, color="#2563EB", label="S1 (1st order)", alpha=0.82)
    ax.bar(x + w / 2, df["ST"], w, color="#7C3AED", label="ST (total)",     alpha=0.82)
    ax.errorbar(x - w / 2, df["S1"], yerr=df["S1_conf"], fmt="none", color="#1D4ED8", capsize=4)
    ax.errorbar(x + w / 2, df["ST"], yerr=df["ST_conf"], fmt="none", color="#5B21B6", capsize=4)
    ax.axhline(0.10, color="#DC2626", ls="--", label="S1=0.10 (dominant)")
    for i, v in enumerate(df["ST"]):
        ax.text(i + w / 2, v + 0.01, f"{v:.3f}", ha="center", fontsize=8)
    ax.set_xticks(x)
    ax.set_xticklabels(df["parameter"], rotation=12, ha="right", fontsize=9)
    ax.set_title("SA-5 · Sobol Indices (GHG Abatement Function)", fontsize=12, fontweight="bold")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(fontsize=9)
    _watermark(fig)
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_sa6_potential(df: pd.DataFrame, tech: str, out_path: Path) -> None:
    """Generate SA-6 potential sensitivity plots."""
    p_colors = {
        "power_density_mw_km2": "#DC2626",
        "land_use_factor":       "#2563EB",
        "capacity_factor":       "#16A34A",
    }
    p_labels = {
        "power_density_mw_km2": "Power Density",
        "land_use_factor":       "Land Use",
        "capacity_factor":       "Capacity Factor",
    }
    fig, (ax_l, ax_b) = plt.subplots(1, 2, figsize=(14, 5.5), dpi=130)
    fig.patch.set_facecolor(_FIG_BG)
    ax_l.set_facecolor(_FIG_BG)
    ax_b.set_facecolor(_FIG_BG)
    for param in df["parameter"].unique():
        sub = df[df["parameter"] == param].sort_values("perturbation_pct")
        c   = p_colors.get(param, "#888")
        lbl = p_labels.get(param, param)
        ax_l.plot(sub["perturbation_pct"], sub["delta_gw_pct"],
                  color=c, lw=2.2, marker="o", label=f"{lbl} (GW)")
        if param == "capacity_factor":
            ax_l.plot(sub["perturbation_pct"], sub["delta_twh_pct"],
                      color=c, ls="--", marker="^", label=f"{lbl} (TWh)")
    ax_l.axhline(0, color="#374151")
    ax_l.axvline(0, color="#D1D5DB", ls="--")
    ax_l.set_title("OAT Curves — ΔGW (and ΔTWH for CF)", fontsize=10, fontweight="bold")
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
        alpha=0.82,
    )
    for bar, v in zip(bars, s30["abs_dgw"]):
        ax_b.text(
            bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2,
            f"{v:.1f}%", va="center", fontsize=9, fontweight="bold",
        )
    ax_b.set_title("Elasticity at +30% perturbation", fontsize=10, fontweight="bold")
    ax_b.spines[["top", "right"]].set_visible(False)
    fig.suptitle(
        f"SA-6 · Potential Sensitivity — {_TECH_LABEL.get(tech, tech)}",
        fontsize=12, fontweight="bold",
    )
    _watermark(fig)
    fig.tight_layout()
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


def _fig_dashboard(
    rs: Dict[str, Any],
    tech: str,
    country_name: str,
    out_path: Path,
) -> None:
    """Generate comprehensive sensitivity dashboard."""
    td    = rs.get(tech, {})
    color = _TECH_COLOR.get(tech, "#2563EB")
    fig   = plt.figure(figsize=(22, 12), dpi=120)
    fig.patch.set_facecolor(_FIG_BG)
    gs    = gridspec.GridSpec(
        2, 4, figure=fig,
        hspace=0.46, wspace=0.38,
        left=0.05, right=0.97, top=0.91, bottom=0.06,
    )
    axes = [fig.add_subplot(gs[r, c]) for r in range(2) for c in range(4)]

    for ax, lt in zip(axes, "abcdefgh"):
        ax.set_facecolor(_FIG_BG)
        ax.text(0.02, 0.97, f"({lt})", transform=ax.transAxes,
                fontsize=9.5, fontweight="bold", va="top", zorder=10)

    # (a) SA-1 Tornado
    ax = axes[0]
    ax.set_title("SA-1 · OAT Sensitivity\n(1−ρ_min per criterion)",
                 fontsize=9, fontweight="bold")
    if "sa1" in td and "_df" in td["sa1"]:
        sm = (
            td["sa1"]["_df"].groupby("criterion")["spearman_rho"].min()
            .reset_index().sort_values("spearman_rho").head(8)
        )
        cl = [
            "#16a34a" if r >= 0.95 else "#d97706" if r >= 0.90 else "#dc2626"
            for r in sm["spearman_rho"]
        ]
        ax.barh(range(len(sm)), 1 - sm["spearman_rho"], color=cl, alpha=0.82)
        ax.set_yticks(range(len(sm)))
        ax.set_yticklabels(sm["criterion"], fontsize=7.5)
        ax.axvline(0.05, color="#dc2626", ls="--", alpha=0.6)
        ax.set_xlabel("1 − ρ_min", fontsize=8)
    else:
        ax.text(0.5, 0.5, "SA-1 not executed", ha="center", color=_MUTE_C, fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)

    # (b) SA-2 CV
    ax = axes[1]
    ax.set_title("SA-2 · MC AHP\nCV Distribution", fontsize=9, fontweight="bold")
    if "sa2" in td and "_cv" in td["sa2"]:
        cv_c = td["sa2"]["_cv"]
        cv_c = cv_c[np.isfinite(cv_c) & (cv_c >= 0)]
        if cv_c.size > 0:
            ax.hist(cv_c, bins=45, color=color, alpha=0.75, edgecolor="white")
            med = float(np.median(cv_c))
            ax.axvline(med, color="#111827", ls="--", label=f"Med={med:.3f}")
            ax.legend(fontsize=7.5)
    else:
        ax.text(0.5, 0.5, "SA-2 not executed", ha="center", color=_MUTE_C, fontsize=9)
    ax.set_xlabel("CV per pixel", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # (c) SA-3 Threshold
    ax = axes[2]
    ax.set_title("SA-3 · Threshold Sweep\nPotential vs Threshold",
                 fontsize=9, fontweight="bold")
    if "sa3" in td and "_df" in td["sa3"]:
        df3 = td["sa3"]["_df"]
        ax.plot(df3["threshold"], df3["potential_gw"], color=color, lw=2.0, marker="o")
        ax.fill_between(df3["threshold"], 0, df3["potential_gw"], color=color, alpha=0.10)
        ax.axvline(td["sa3"].get("base_threshold", 0.60), color="#374151", ls="--", alpha=0.8)
    else:
        ax.text(0.5, 0.5, "SA-3 not executed", ha="center", color=_MUTE_C, fontsize=9)
    ax.set_xlabel("Threshold", fontsize=8)
    ax.set_ylabel("GW", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # (d) SA-4 LCOE
    ax = axes[3]
    ax.set_title("SA-4 · LCOE MC\n$/MWh Histogram", fontsize=9, fontweight="bold")
    if "sa4" in td and "_df" in td["sa4"]:
        lc = td["sa4"]["_df"]["lcoe_usd_mwh"].values
        st = td["sa4"]["_df"].attrs.get("stats", td["sa4"])
        ax.hist(lc, bins=50, color=color, alpha=0.75, edgecolor="white")
        for p, pc, l_lbl in [
            (st.get("lcoe_p05", 0), "#1D4ED8", "P5"),
            (st.get("lcoe_p50", 0), "#111827", "P50"),
            (st.get("lcoe_p95", 0), "#DC2626", "P95"),
        ]:
            ax.axvline(p, color=pc,
                       ls="--" if l_lbl != "P50" else "-",
                       label=f"{l_lbl}={p:.0f}")
        ax.legend(fontsize=7.5)
    else:
        ax.text(0.5, 0.5, "SA-4 not executed", ha="center", color=_MUTE_C, fontsize=9)
    ax.set_xlabel("LCOE ($/MWh)", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # (e) SA-6 OAT
    ax = axes[4]
    ax.set_title("SA-6 · Potential Sensitivity\nΔGW (%) per parameter",
                 fontsize=9, fontweight="bold")
    p_c = {
        "power_density_mw_km2": "#DC2626",
        "land_use_factor":       "#2563EB",
        "capacity_factor":       "#16A34A",
    }
    p_s = {
        "power_density_mw_km2": "PD",
        "land_use_factor":       "LU",
        "capacity_factor":       "CF",
    }
    if "sa6" in td and "_df" in td["sa6"]:
        df6 = td["sa6"]["_df"]
        for param in df6["parameter"].unique():
            sub = df6[df6["parameter"] == param].sort_values("perturbation_pct")
            ax.plot(sub["perturbation_pct"], sub["delta_gw_pct"],
                    color=p_c.get(param, "#888"), label=p_s.get(param, param))
        ax.axhline(0, color="#374151")
        ax.legend(fontsize=7.5)
    else:
        ax.text(0.5, 0.5, "SA-6 not executed", ha="center", color=_MUTE_C, fontsize=9)
    ax.set_xlabel("Perturbation (%)", fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)

    # (f) SA-1 Heatmap
    ax = axes[5]
    ax.set_title("SA-1 · Heatmap ρ\n(Top 6 criteria)", fontsize=9, fontweight="bold")
    if "sa1" in td and "_df" in td["sa1"]:
        df1  = td["sa1"]["_df"]
        top6 = df1.groupby("criterion")["spearman_rho"].min().sort_values().head(6).index
        piv  = (
            df1[df1["criterion"].isin(top6)]
            .pivot_table(
                index="criterion", columns="perturbation_pct",
                values="spearman_rho", aggfunc="mean",
            )
        )
        piv = piv.loc[piv.min(axis=1).sort_values().index]
        if not piv.empty:
            ax.imshow(piv.values, cmap="RdYlGn", vmin=0.85, vmax=1.0, aspect="auto")
            ax.set_xticks(range(len(piv.columns)))
            ax.set_xticklabels([f"{int(v)}%" for v in piv.columns], fontsize=6.5)
            ax.set_yticks(range(len(piv.index)))
            ax.set_yticklabels(piv.index, fontsize=7)
    else:
        ax.text(0.5, 0.5, "SA-1 not executed", ha="center", color=_MUTE_C, fontsize=9)

    # (g) SA-1 + SA-2 KPIs
    ax = axes[6]
    ax.axis("off")
    ax.set_title("SA-1 + SA-2 Summary", fontsize=9, fontweight="bold")
    kpis_g: List[Tuple[str, str]] = []
    if "sa1" in td:
        kpis_g += [
            ("SA-1 Robust criteria",    str(td["sa1"].get("n_robust",       "—"))),
            ("SA-1 Sensitive criteria", str(td["sa1"].get("n_sensitive",    "—"))),
            ("SA-1 Global min ρ",       f"{td['sa1'].get('rho_min_global', 0.0):.4f}"),
        ]
    if "sa2" in td:
        kpis_g += [
            ("SA-2 Stable pixels", f"{td['sa2'].get('stable_fraction_90ci', 0) * 100:.1f}%"),
            ("SA-2 Mean CV",       f"{td['sa2'].get('mean_cv', 0):.4f}"),
        ]
    _draw_kpis(ax, kpis_g)

    # (h) SA-3 + SA-4 + SA-6 KPIs
    ax = axes[7]
    ax.axis("off")
    ax.set_title("SA-3 + SA-4 + SA-6 Summary", fontsize=9, fontweight="bold")
    kpis_h: List[Tuple[str, str]] = []
    if "sa3" in td:
        df3 = td["sa3"].get("_df")
        bt  = td["sa3"].get("base_threshold", 0.6)
        if df3 is not None and not df3[abs(df3["threshold"] - bt) < 0.001].empty:
            kpis_h.append((
                "SA-3 GW @ balanced",
                f"{df3[abs(df3['threshold'] - bt) < 0.001]['potential_gw'].values[0]:.2f}",
            ))
    if "sa4" in td:
        kpis_h += [
            ("SA-4 P50 $/MWh", f"{td['sa4'].get('lcoe_p50', '—')}"),
            ("SA-4 CI90",      f"{td['sa4'].get('ci90_width', '—')} $/MWh"),
        ]
    if "sa6" in td:
        kpis_h.append(("SA-6 Base GW", str(td["sa6"].get("base_gw", "—"))))
    _draw_kpis(ax, kpis_h)

    fig.suptitle(
        f"Sensitivity Dashboard — {_TECH_LABEL.get(tech, tech)} · {country_name}",
        fontsize=14, fontweight="bold", y=0.97,
    )
    _watermark(fig)
    fig.savefig(str(out_path), dpi=120, bbox_inches="tight", facecolor=_FIG_BG)
    plt.close(fig)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────


def _sfmt(val: Any, fmt_str: str, default: str = "—") -> str:
    """Safe formatting helper."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return default
    try:
        return format(float(val), fmt_str)
    except (ValueError, TypeError):
        return str(val)


class SensitivityAnalyzer:
    """Orchestrates Phase 8 evaluations (SA-1 to SA-6) with unified reporting."""

    def __init__(self, cfg: Any, outputs_dir: Path) -> None:
        self.cfg         = cfg
        self.outputs_dir = Path(outputs_dir)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _lcoe_params_for_tech(
        self,
        tech: str,
        country_params: Optional[CountryParams],
    ) -> Dict[str, float]:
        """
        Return LCOE parameters for *tech*, preferring CountryParams.

        BUG_05 (fix): A versão anterior acessava country_params.lcoe (dict),
        que não existe em CountryParams. O acesso correto é via os atributos
        tipados lcoe_solar / lcoe_wind / lcoe_biomass (LCOETechParams).
        """
        fallback = dict(_SA4_DEFAULTS.get(tech, _SA4_DEFAULTS["solar"]))

        if country_params is not None:
            # Acesso direto ao atributo tipado (LCOETechParams)
            lcoe_attr = _LCOE_ATTR.get(tech)
            if lcoe_attr:
                try:
                    tech_lcoe = getattr(country_params, lcoe_attr, None)
                    if tech_lcoe is not None:
                        # capacity_factor vem do TechParams da tecnologia,
                        # não do LCOETechParams (que não tem esse campo).
                        tech_obj = getattr(country_params, tech, None)
                        cf = (
                            float(tech_obj.capacity_factor)
                            if tech_obj is not None
                            else fallback["cf"]
                        )
                        return {
                            "capex": float(tech_lcoe.capex_usd_kw),
                            "opex":  float(tech_lcoe.opex_usd_kw_yr),
                            "life":  int(tech_lcoe.lifetime_years),
                            "dr":    float(tech_lcoe.discount_rate),
                            "cf":    cf,
                        }
                except Exception as exc:
                    logger.debug(
                        "[_lcoe_params_for_tech] CountryParams access failed "
                        "for tech=%s: %s", tech, exc,
                    )

        # Legacy cfg dict (fallback quando CountryParams não disponível)
        try:
            lc = (
                self.cfg.system
                .get("lcoe", {})
                .get("technologies", {})
                .get(tech, {})
            )
            if lc:
                return {
                    "capex": float(lc.get("capex_usd_kw",
                                          lc.get("base_capex_usd_kw", fallback["capex"]))),
                    "opex":  float(lc.get("opex_usd_kw_yr",
                                          lc.get("base_opex_usd_kw_yr", fallback["opex"]))),
                    "life":  int(  lc.get("lifetime_years",   fallback["life"])),
                    "dr":    float(lc.get("discount_rate",    fallback["dr"])),
                    "cf":    float(lc.get("capacity_factor",  fallback["cf"])),
                }
        except Exception:
            pass

        logger.debug(
            "[_lcoe_params_for_tech] Using hardcoded defaults for tech=%s.", tech
        )
        return fallback

    def _resolve_tech_params(
        self,
        tech: str,
        country_params: Optional[CountryParams],
        pot_results: Any,
    ) -> Tuple[float, float, float, float]:
        """Resolve (power_density, land_use_factor, capacity_factor, threshold)."""
        tp    = self.cfg.system.get("potential", {}).get("technologies", {}).get(tech, {})
        pd_mw = float(tp.get("power_density_mw_km2",  30.0))
        luf   = float(tp.get("land_use_factor",        0.20))
        cf    = float(tp.get("capacity_factor_max",    0.22))
        thr   = float(tp.get("base_threshold",         0.60))

        if country_params is not None:
            try:
                tech_obj = getattr(country_params, tech, None)
                if tech_obj is not None:
                    pd_mw = float(getattr(tech_obj, "power_density_mw_km2", pd_mw))
                    luf   = float(getattr(tech_obj, "land_use_factor",       luf))
                    cf    = float(getattr(tech_obj, "capacity_factor",        cf))
                    thr   = float(getattr(country_params, "suitability_threshold", thr))
                    return pd_mw, luf, cf, thr
            except AttributeError:
                pass

        if pot_results is not None:
            try:
                if hasattr(pot_results, "techs"):
                    tech_obj = pot_results.techs.get(tech)
                elif isinstance(pot_results, dict):
                    tech_obj = pot_results.get("techs", {}).get(tech)
                else:
                    tech_obj = None
                if tech_obj is not None:
                    pp = (
                        tech_obj.params if hasattr(tech_obj, "params")
                        else tech_obj.get("params", {})
                    )
                    if pp:
                        pd_mw = float(pp.get("power_density_mw_km2", pd_mw))
                        luf   = float(pp.get("land_use_factor",       luf))
                        cf    = float(pp.get("capacity_factor",        cf))
                        thr_d = pp.get("thresholds", {})
                        if isinstance(thr_d, dict):
                            thr = float(thr_d.get("balanced", thr))
            except Exception:
                pass

        return pd_mw, luf, cf, thr

    @staticmethod
    def _match_tech_from_stem(stem: str, country_code: str) -> Optional[str]:
        """
        Determina a tecnologia a partir do stem do ficheiro de pesos.

        BUG_02 (fix): A versão anterior usava `in` para matching de substring,
        o que tornava o matching ambíguo se um nome de tecnologia fosse
        substring de outro (ex: "wind" dentro de "windoffshore").

        Esta implementação usa igualdade exata no stem completo, tentando
        primeiro o padrão com country_code (ex: "PRT_solar_weights") e depois
        o padrão sem prefixo (ex: "solar_weights"). Retorna None se não
        reconhecer nenhuma tecnologia.

        Args:
            stem:         Stem do ficheiro (nome sem extensão).
            country_code: Código ISO-3 do país (ex: "PRT").

        Returns:
            Nome canônico da tecnologia ("solar", "wind", "biomass") ou None.
        """
        for tech in ("solar", "wind", "biomass"):
            if stem == f"{country_code}_{tech}_weights" or stem == f"{tech}_weights":
                return tech
        return None

    def _load_suitability_from_disk(
        self,
        country_code: str,
        criteria_dir: Path,
    ) -> Dict[str, Any]:
        """
        Load suitability results from disk when Phase 3 was previously executed.

        Uses recursive search (rglob) for both TIFs and JSON files under the
        suitability/ directory, so it works regardless of subfolder layout.

        BUG_02 (fix): matching de tecnologias agora usa _match_tech_from_stem,
        que compara o stem completo do ficheiro — sem ambiguidade de substring.
        IMM_09 (fix): rglob já estava presente; o matching exato completa a correção.
        """
        techs: Dict[str, Any] = {}
        suitability_dir = self.outputs_dir / country_code / "suitability"

        # ── Mapear ficheiros de pesos: tech → Path ───────────────────────
        weights_by_tech: Dict[str, Path] = {}
        for wf in suitability_dir.rglob("*_weights.json"):
            tech = self._match_tech_from_stem(wf.stem, country_code)
            if tech is not None and tech not in weights_by_tech:
                # Primeira ocorrência vence (mais específica, country_code primeiro)
                weights_by_tech[tech] = wf

        for tech in ("solar", "wind", "biomass"):
            # 1. Localizar TIF de suitability — resolver centralizado
            # (raster_io.find_suitability_tif, BLOCKER-006), que tenta o
            # nome exato do TOPSIS primeiro e cai para um rglob por stem
            # exato (nunca um wildcard `*suitability*` ambíguo que também
            # casaria com os GeoTIFFs OWA — mesma classe de bug que BUG_02
            # corrigiu para os ficheiros de pesos). allow_owa_fallback=False
            # porque este carregamento de pesos nunca usou OWA.
            suit_tif = find_suitability_tif(
                suitability_dir, tech, country_code, allow_owa_fallback=False
            )
            if suit_tif is None:
                logger.debug("[%s] Suitability TIF not found.", tech)
                continue

            logger.info(
                "[%s] TIF found: %s", tech,
                suit_tif.relative_to(self.outputs_dir),
            )

            # 2. Carregar pesos
            weights: Dict[str, float] = {}
            wf = weights_by_tech.get(tech)
            if wf:
                try:
                    raw = json.loads(wf.read_text(encoding="utf-8"))
                    if "weights" in raw and isinstance(raw["weights"], dict):
                        weights = {str(k): float(v) for k, v in raw["weights"].items()}
                    else:
                        weights = {
                            str(k): float(v) for k, v in raw.items()
                            if isinstance(v, (int, float))
                        }
                    logger.info(
                        "[%s] %d weights loaded from %s", tech, len(weights), wf.name
                    )
                except Exception as exc:
                    logger.warning("[%s] Failed to read weights JSON: %s", tech, exc)

            # 3. Fallback: pesos iguais a partir dos critérios disponíveis
            if not weights:
                logger.info(
                    "[%s] Weights JSON not found — inferring criteria from %s",
                    tech, criteria_dir,
                )
                crit_names: List[str] = []
                for tif_path in sorted(criteria_dir.glob("*.tif")):
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
                    eq = 1.0 / len(crit_names)
                    weights = {n: eq for n in crit_names}
                    logger.warning(
                        "[%s] Using %d criteria with equal weights (fallback).",
                        tech, len(weights),
                    )

            if weights:
                techs[tech] = {"weights": weights, "error": False}
            else:
                logger.warning("[%s] No weights available — skipping technology.", tech)

        if not techs:
            logger.warning("No technologies found with valid data on disk.")
            return {}

        return {"techs": techs}

    def _load_weights_from_disk(
        self,
        country_code: str,
    ) -> Dict[str, Dict[str, float]]:
        """
        Carrega os pesos AHP do disco para cada tecnologia.

        BUG_02 (fix): matching agora usa _match_tech_from_stem (exato),
        eliminando o risco de ambiguidade por substring entre tecnologias.

        Returns:
            Dict[tech_name, weights_dict]
        """
        weights_by_tech: Dict[str, Dict[str, float]] = {}
        suitability_dir = self.outputs_dir / country_code / "suitability"

        for wf in suitability_dir.rglob("*_weights.json"):
            tech = self._match_tech_from_stem(wf.stem, country_code)
            if tech is None or tech in weights_by_tech:
                # Ignora ficheiros não reconhecidos; primeira ocorrência vence.
                continue
            try:
                raw = json.loads(wf.read_text(encoding="utf-8"))
                if "weights" in raw and isinstance(raw["weights"], dict):
                    weights = {str(k): float(v) for k, v in raw["weights"].items()}
                else:
                    weights = {
                        str(k): float(v) for k, v in raw.items()
                        if isinstance(v, (int, float))
                    }
                if weights:
                    weights_by_tech[tech] = weights
                    logger.info(
                        "[%s] Weights loaded from %s (%d criteria)",
                        tech, wf.name, len(weights),
                    )
                else:
                    logger.warning(
                        "[%s] No valid weights found in %s", tech, wf.name
                    )
            except Exception as exc:
                logger.warning(
                    "[%s] Failed to read weights JSON %s: %s", tech, wf.name, exc
                )

        return weights_by_tech

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        country_code: str,
        suitability_results: Optional[Dict[str, Any]],
        criteria_dir: Path,
        country_name: str = "",
        country_params: Optional[CountryParams] = None,
        pot_results: Any = None,
        abat_result: Optional[Dict[str, Any]] = None,
        run_sa1: bool = True,
        run_sa2: bool = True,
        run_sa3: bool = True,
        run_sa4: bool = True,
        run_sa5: bool = False,
        run_sa6: bool = True,
        n_mc_samples: int = 1000,
        sa5_ghg_function: Optional[Callable] = None,
        sa5_base_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute sensitivity analysis suite (SA-1 to SA-6).

        Args:
            country_code:        ISO-3166 alpha-3 country code.
            suitability_results: Phase 3 output (Pydantic model or dict).
            criteria_dir:        Directory with normalized criteria TIFs.
            country_name:        Human-readable country name for reports.
            country_params:      Typed CountryParams schema instance.
            pot_results:         Phase 4 output (PotentialResult or dict).
            abat_result:         Phase 7 output dict (for SA-5 GHG function).
            run_sa1..run_sa6:    Flags to enable/disable individual SA modules.
            n_mc_samples:        Monte Carlo sample count (SA-2 and SA-4).
            sa5_ghg_function:    Pre-built GHG function (overrides auto-build).
            sa5_base_params:     Parameter bounds for SA-5 Sobol.

        Returns:
            Nested dict: {tech: {sa1: {...}, sa2: {...}, ...}, sa5_sobol: {...}}.
        """
        # ── 0. Normalize suitability_results ─────────────────────────────
        if suitability_results is not None:
            if hasattr(suitability_results, "model_dump"):
                suitability_results = suitability_results.model_dump()
            elif hasattr(suitability_results, "dict"):
                suitability_results = suitability_results.dict()
            elif not isinstance(suitability_results, dict):
                try:
                    suitability_results = dict(suitability_results)
                except Exception:
                    suitability_results = {}
        else:
            suitability_results = {}

        # ── ENRICH: Load weights from disk (always) ──────────────────────
        weights_from_disk = self._load_weights_from_disk(country_code)
        if weights_from_disk:
            techs = suitability_results.get("techs", {})
            for tech, wdict in weights_from_disk.items():
                if tech not in techs:
                    techs[tech] = {}
                techs[tech]["weights"] = wdict
            suitability_results["techs"] = techs
            logger.info(
                "Weights enriched from disk for %d technologies.",
                len(weights_from_disk),
            )
        else:
            logger.warning("No weights found on disk for any technology.")

        # ── 1. Validate inputs ────────────────────────────────────────────
        criteria_dir = Path(criteria_dir)
        if not criteria_dir.exists():
            logger.error("Criteria directory not found: %s", criteria_dir)
            return {}

        tifs = sorted(criteria_dir.glob("*.tif"))
        if tifs:
            logger.info("  Input criteria: %d TIF files found", len(tifs))
            for tif in tifs[:5]:
                logger.info("    - %s", tif.name)
            if len(tifs) > 5:
                logger.info("    ... and %d more", len(tifs) - 5)
        else:
            logger.warning("  No criteria TIF files found in %s", criteria_dir)

        # Log CountryParams origin for traceability
        if country_params is not None:
            try:
                solar_cf = country_params.solar.capacity_factor
                wind_cf  = country_params.wind.capacity_factor
                bio_cf   = country_params.biomass.capacity_factor
            except AttributeError:
                solar_cf = wind_cf = bio_cf = 0.0
            logger.info(
                "  [CountryParams] Loaded for %s — "
                "solar CF=%.3f | wind CF=%.3f | biomass CF=%.3f",
                country_params.country_code, solar_cf, wind_cf, bio_cf,
            )
        else:
            logger.warning(
                "  [CountryParams] Not provided — "
                "falling back to cfg.system dict for all parameters."
            )

        out_dir = self.outputs_dir / country_code / "sensitivity"
        out_dir.mkdir(parents=True, exist_ok=True)

        logger.info("=" * 62)
        logger.info("SENSITIVITY ANALYZER — Phase 8 (%s)", country_code)
        logger.info("Output: %s", out_dir)
        logger.info("=" * 62)

        results_sa: Dict[str, Any] = {}
        t0_total = time.perf_counter()

        # ── 2. Load suitability from disk if not in memory ────────────────
        if not suitability_results or not suitability_results.get("techs"):
            logger.info(
                "Suitability results not available in memory. "
                "Attempting to load from disk..."
            )
            suitability_results = self._load_suitability_from_disk(
                country_code, criteria_dir
            )
            if not suitability_results or not suitability_results.get("techs"):
                logger.error(
                    "No suitability data found. "
                    "Run Phase 3 (SuitabilityBuilder) first."
                )
                return {}

        # ── 3. Reference grid ─────────────────────────────────────────────
        ref_tif = next(criteria_dir.glob("*.tif"), None)
        if not ref_tif:
            logger.error(
                "No criteria TIF found. Run Phase 2b (CriteriaBuilder) first."
            )
            return {}

        with rasterio.open(str(ref_tif)) as src:
            height    = src.height
            width     = src.width
            transform = src.transform

        logger.info("Reference grid: %d×%d pixels", height, width)
        logger.info(
            "Technologies to analyze: %s",
            list(suitability_results.get("techs", {}).keys()),
        )

        # ── 4. SA-5 GHG function (built once) ─────────────────────────────
        if run_sa5 and sa5_ghg_function is None and abat_result:
            sa5_ghg_function, sa5_base_params = _build_ghg_function_from_abatement(
                abat_result
            )
            if sa5_ghg_function:
                logger.info("[SA-5] GHG function built from Phase 7 abatement results.")
            else:
                logger.warning(
                    "[SA-5] Could not build GHG function "
                    "(abatement result empty or CO₂=0)."
                )

        # ══════════════════════════════════════════════════════════════════
        # Main per-technology loop
        # ══════════════════════════════════════════════════════════════════
        for tech, tech_data in suitability_results.get("techs", {}).items():
            if tech_data.get("error"):
                logger.warning("[%s] Marked with error — skipping.", tech)
                continue

            weights: Dict[str, float] = tech_data.get("weights", {})
            if not weights:
                logger.warning("[%s] No AHP weights available — skipping.", tech)
                continue

            logger.info(
                "[%s] Starting SA suite (%d criteria)...", tech, len(weights)
            )
            results_sa[tech] = {}

            # ── Resolve tech params once per technology ──────────────────
            pd_mw, luf, cf, base_thr = self._resolve_tech_params(
                tech, country_params, pot_results
            )

            # ── SA-1 ──────────────────────────────────────────────────────
            if run_sa1:
                t0 = time.perf_counter()
                try:
                    logger.info("[%s] SA-1: OAT weight perturbation...", tech)
                    df1 = sa1_oat_weight_sensitivity(
                        criteria_dir, list(weights.keys()), weights, height, width,
                    )
                    df1.to_csv(
                        out_dir / f"{country_code}_{tech}_sa1_oat.csv", index=False
                    )
                    rb = df1.groupby("criterion")["spearman_rho"].min()
                    results_sa[tech]["sa1"] = {
                        "n_robust":       int((rb >= 0.95).sum()),
                        "n_sensitive":    len(weights) - int((rb >= 0.95).sum()),
                        "most_sensitive": str(rb.idxmin()),
                        "rho_min_global": float(rb.min()),
                        "elapsed_s":      round(time.perf_counter() - t0, 1),
                        "_df":            df1,
                    }
                    _fig_sa1_tornado(
                        df1, tech,
                        out_dir / f"{country_code}_{tech}_sa1_tornado.png",
                    )
                    _fig_sa1_heatmap(
                        df1, tech,
                        out_dir / f"{country_code}_{tech}_sa1_heatmap.png",
                    )
                    logger.info(
                        "[%s] SA-1 complete: rho_min=%.4f, most_sensitive=%s",
                        tech, rb.min(), rb.idxmin(),
                    )
                except Exception as exc:
                    logger.error("[%s] SA-1 failed: %s", tech, exc, exc_info=True)

            # ── SA-2 ──────────────────────────────────────────────────────
            if run_sa2:
                t0 = time.perf_counter()
                try:
                    logger.info(
                        "[%s] SA-2: Monte Carlo AHP (%d samples)...",
                        tech, n_mc_samples,
                    )
                    mc = sa2_monte_carlo_weights(
                        criteria_dir, list(weights.keys()), weights,
                        height, width, n_samples=n_mc_samples,
                    )
                    results_sa[tech]["sa2"] = {
                        "stable_fraction_90ci": mc["stable_fraction"],
                        "mean_cv":              float(mc["cv"].mean()),
                        "mean_ci90_width":      float(mc["ci_width"].mean()),
                        "elapsed_s":            round(time.perf_counter() - t0, 1),
                        "_cv":                  mc["cv"],
                    }
                    _fig_sa2_cv(
                        mc["cv"], mc["ci_width"], tech,
                        out_dir / f"{country_code}_{tech}_sa2_cv_dist.png",
                    )
                    logger.info(
                        "[%s] SA-2 complete: stable_fraction=%.1f%%",
                        tech, mc["stable_fraction"] * 100,
                    )
                except Exception as exc:
                    logger.error("[%s] SA-2 failed: %s", tech, exc, exc_info=True)

            # ── SA-3 ──────────────────────────────────────────────────────
            if run_sa3:
                t0 = time.perf_counter()
                try:
                    from src.utils.utils import compute_pixel_area_geodesic as _paf

                    suit_tif = find_suitability_tif(
                        self.outputs_dir / country_code / "suitability" / "tif",
                        tech, country_code, allow_owa_fallback=False,
                    )
                    if suit_tif is None:
                        logger.warning(
                            "[%s] SA-3: Suitability TIF not found — skipping.",
                            tech,
                        )
                    else:
                        logger.info(
                            "[%s] SA-3: threshold sweep "
                            "(pd=%.1f luf=%.3f cf=%.3f)...",
                            tech, pd_mw, luf, cf,
                        )
                        df3 = sa3_threshold_sweep(
                            suit_tif, _paf, transform, width, height,
                            power_density_mw_km2=pd_mw,
                            land_use_factor=luf,
                            capacity_factor=cf,
                        )
                        df3.to_csv(
                            out_dir / f"{country_code}_{tech}_sa3_threshold_sweep.csv",
                            index=False,
                        )
                        results_sa[tech]["sa3"] = {
                            "base_threshold": base_thr,
                            "elapsed_s":      round(time.perf_counter() - t0, 1),
                            "_df":            df3,
                        }
                        _fig_sa3_threshold(
                            df3, tech, base_thr,
                            out_dir / f"{country_code}_{tech}_sa3_curve.png",
                        )
                        logger.info(
                            "[%s] SA-3 complete: %d thresholds evaluated",
                            tech, len(df3),
                        )
                except Exception as exc:
                    logger.error("[%s] SA-3 failed: %s", tech, exc, exc_info=True)

            # ── SA-4 ──────────────────────────────────────────────────────
            if run_sa4:
                t0 = time.perf_counter()
                try:
                    lc_p = self._lcoe_params_for_tech(tech, country_params)
                    logger.info(
                        "[%s] SA-4: LCOE MC (%d samples) | "
                        "CAPEX=%d OPEX=%d CF=%.3f r=%.1f%% n=%dyr",
                        tech, max(n_mc_samples, 10_000),
                        lc_p["capex"], lc_p["opex"], lc_p["cf"],
                        lc_p["dr"] * 100, lc_p["life"],
                    )
                    df4 = sa4_lcoe_uncertainty(
                        base_capex_usd_kw=lc_p["capex"],
                        base_opex_usd_kw_yr=lc_p["opex"],
                        lifetime=int(lc_p["life"]),
                        discount_rate=lc_p["dr"],
                        capacity_factor=lc_p["cf"],
                        n_samples=max(n_mc_samples, 10_000),
                    )
                    df4.to_csv(
                        out_dir / f"{country_code}_{tech}_sa4_lcoe_mc.csv", index=False
                    )
                    results_sa[tech]["sa4"] = {
                        **df4.attrs.get("stats", {}),
                        "elapsed_s": round(time.perf_counter() - t0, 1),
                        "_df":       df4,
                    }
                    _fig_sa4_lcoe(
                        df4, tech,
                        out_dir / f"{country_code}_{tech}_sa4_lcoe_hist.png",
                    )
                    st = df4.attrs.get("stats", {})
                    logger.info(
                        "[%s] SA-4 complete: P50=$%.1f/MWh CI90=$%.1f",
                        tech, st.get("lcoe_p50", 0), st.get("ci90_width", 0),
                    )
                except Exception as exc:
                    logger.error("[%s] SA-4 failed: %s", tech, exc, exc_info=True)

            # ── SA-6 ──────────────────────────────────────────────────────
            if run_sa6:
                t0 = time.perf_counter()
                try:
                    from src.utils.utils import compute_pixel_area_geodesic as _paf

                    suit_tif = find_suitability_tif(
                        self.outputs_dir / country_code / "suitability" / "tif",
                        tech, country_code, allow_owa_fallback=False,
                    )
                    if suit_tif is None:
                        logger.warning(
                            "[%s] SA-6: Suitability TIF not found — skipping.",
                            tech,
                        )
                    else:
                        logger.info(
                            "[%s] SA-6: potential parameter sensitivity "
                            "(pd=%.1f luf=%.3f cf=%.3f thr=%.2f)...",
                            tech, pd_mw, luf, cf, base_thr,
                        )
                        df6 = sa6_potential_sensitivity(
                            suit_tif, _paf, transform, width, height,
                            pd_mw, luf, cf, base_thr,
                        )
                        df6.to_csv(
                            out_dir / f"{country_code}_{tech}_sa6_potential_oat.csv",
                            index=False,
                        )
                        results_sa[tech]["sa6"] = {
                            "base_gw":   df6.attrs.get("base_gw",   0),
                            "base_twh":  df6.attrs.get("base_twh",  0),
                            "base_area": df6.attrs.get("base_area",  0),
                            "elapsed_s": round(time.perf_counter() - t0, 1),
                            "_df":       df6,
                        }
                        _fig_sa6_potential(
                            df6, tech,
                            out_dir / f"{country_code}_{tech}_sa6_potential.png",
                        )
                        logger.info(
                            "[%s] SA-6 complete: base=%.1f GW",
                            tech, df6.attrs.get("base_gw", 0),
                        )
                except Exception as exc:
                    logger.error("[%s] SA-6 failed: %s", tech, exc, exc_info=True)

            # ── Dashboard ─────────────────────────────────────────────────
            try:
                _fig_dashboard(
                    results_sa,
                    tech,
                    country_name or country_code,
                    out_dir / f"{country_code}_{tech}_dashboard.png",
                )
                logger.info("[%s] Dashboard saved.", tech)
            except Exception as exc:
                logger.error("[%s] Dashboard failed: %s", tech, exc, exc_info=True)

        # ══════════════════════════════════════════════════════════════════
        # SA-5: Sobol GHG (cross-technology, runs once)
        # ══════════════════════════════════════════════════════════════════
        if run_sa5 and sa5_ghg_function and sa5_base_params:
            logger.info("[SA-5] Sobol global sensitivity (GHG abatement)...")
            try:
                df5 = sa5_sobol_ghg(sa5_base_params, sa5_ghg_function, n_samples=1024)
                if df5 is not None:
                    df5.to_csv(
                        out_dir / f"{country_code}_sa5_sobol_ghg.csv", index=False
                    )
                    results_sa["sa5_sobol"] = {
                        "dominant": str(df5.iloc[0]["parameter"]),
                        "_df":      df5,
                    }
                    _fig_sa5_sobol(
                        df5, out_dir / f"{country_code}_sa5_sobol_barplot.png"
                    )
                    logger.info(
                        "[SA-5] Sobol complete: dominant=%s (ST=%.3f)",
                        df5.iloc[0]["parameter"], df5.iloc[0]["ST"],
                    )
            except Exception as exc:
                logger.error("[SA-5] Execution failed: %s", exc, exc_info=True)
        elif run_sa5:
            logger.warning(
                "[SA-5] SA-5 enabled but no valid ghg_function "
                "(abatement result empty or Phase 7 not executed)."
            )

        # ── Final report ──────────────────────────────────────────────────
        elapsed_total = time.perf_counter() - t0_total
        report = self._format_report(
            results_sa, country_code, country_name, elapsed_total
        )
        (out_dir / f"{country_code}_sensitivity_report.txt").write_text(
            report, encoding="utf-8"
        )

        # ── Verify outputs ────────────────────────────────────────────────
        expected_files = [f"{country_code}_sensitivity_report.txt"]
        for tech in [t for t in results_sa if t != "sa5_sobol"]:
            expected_files.extend([
                f"{country_code}_{tech}_sa1_tornado.png",
                f"{country_code}_{tech}_sa1_heatmap.png",
                f"{country_code}_{tech}_sa2_cv_dist.png",
                f"{country_code}_{tech}_sa3_curve.png",
                f"{country_code}_{tech}_sa4_lcoe_hist.png",
                f"{country_code}_{tech}_sa6_potential.png",
                f"{country_code}_{tech}_dashboard.png",
            ])
        missing = [
            f for f in expected_files
            if not (out_dir / f).exists() and f.endswith(".png")
        ]
        if missing:
            logger.warning("  Missing plots: %s", ", ".join(missing[:5]))
        else:
            logger.info("  All expected sensitivity outputs generated.")

        # ── Persist artefacts ─────────────────────────────────────────────
        from src.io.artifact_manager import ArtifactManager
        artifact_mgr = ArtifactManager(self.outputs_dir, country_code)
        phase_dir    = artifact_mgr.phase_dir("sensitivity")

        serializable: Dict[str, Any] = {}
        for tech, td in results_sa.items():
            if tech == "sa5_sobol":
                serializable[tech] = {
                    "dominant":       td.get("dominant", ""),
                    "_df_available":  "_df" in td,
                }
                continue
            serializable[tech] = {}
            for sa_name, sa_data in td.items():
                if sa_name.startswith("_"):
                    continue
                if isinstance(sa_data, dict):
                    serializable[tech][sa_name] = {
                        k: v for k, v in sa_data.items()
                        if not k.startswith("_")
                        and isinstance(v, (str, int, float, bool, type(None)))
                    }
                else:
                    serializable[tech][sa_name] = sa_data

        artifact_mgr.save_result(phase_dir, serializable, serializer="pickle")
        files = {
            str(p.relative_to(phase_dir)): str(p)
            for p in out_dir.rglob("*") if p.is_file()
        }
        artifact_mgr.save_manifest(
            phase_dir,
            "sensitivity",
            files=files,
            parameters={
                "country":                  country_code,
                "run_sa1":                  run_sa1,
                "run_sa2":                  run_sa2,
                "run_sa3":                  run_sa3,
                "run_sa4":                  run_sa4,
                "run_sa5":                  run_sa5,
                "run_sa6":                  run_sa6,
                "n_mc_samples":             n_mc_samples,
                "country_params_available": country_params is not None,
            },
        )

        logger.info("[%s] Phase 8 completed in %.1fs.", country_code, elapsed_total)
        logger.info(
            "[%s] Outputs: %d CSVs, %d plots",
            country_code,
            len(list(out_dir.glob("*.csv"))),
            len(list(out_dir.glob("*.png"))),
        )

        return results_sa

    # ─────────────────────────────────────────────────────────────────────
    # Format report (DUP_21 — refatorado para usar reporting)
    # ─────────────────────────────────────────────────────────────────────

    def _format_report(
        self,
        rs: Dict[str, Any],
        code: str,
        country_name: str,
        elapsed: float,
    ) -> str:
        """
        Gera o relatório de sensibilidade usando o módulo unificado reporting.

        DUP_21: Substitui a formatação manual (~80 linhas) por uma estrutura
        hierárquica com ReportSection e subsections.
        """
        sections: List[ReportSection] = []

        for tech, td in rs.items():
            if tech == "sa5_sobol":
                continue

            sa1 = td.get("sa1", {})
            sa2 = td.get("sa2", {})
            sa3 = td.get("sa3", {})
            sa4 = td.get("sa4", {})
            sa6 = td.get("sa6", {})

            tech_section = ReportSection(
                title=f"{tech.upper()} [{_TECH_LABEL.get(tech, tech)}]"
            )

            # SA-1
            rows1 = [
                ("Robust criteria (ρ >= 0.95)", str(sa1.get('n_robust', '—'))),
                ("Sensitive criteria (ρ < 0.95)", str(sa1.get('n_sensitive', '—'))),
                ("Global minimum ρ", _sfmt(sa1.get('rho_min_global'), '.4f')),
                ("Most sensitive criterion", sa1.get('most_sensitive', '—')),
            ]
            tech_section.subsections.append(
                ReportSection(title="SA-1 · OAT Weight Sensitivity", rows=rows1)
            )

            # SA-2
            stable_pct = sa2.get('stable_fraction_90ci', 0) * 100
            rows2 = [
                ("Stable pixels (CI90 < 0.10)", f"{_sfmt(stable_pct, '.1f', '0.0')}%"),
                ("Mean CV Spatial Baseline", _sfmt(sa2.get('mean_cv'), '.4f')),
                ("Mean CI90 width globally", _sfmt(sa2.get('mean_ci90_width'), '.4f')),
            ]
            tech_section.subsections.append(
                ReportSection(title="SA-2 · Monte Carlo AHP", rows=rows2)
            )

            # SA-3
            rows3 = [
                ("Base threshold (balanced)", _sfmt(sa3.get('base_threshold'), '.2f')),
            ]
            tech_section.subsections.append(
                ReportSection(title="SA-3 · Threshold Sweep", rows=rows3)
            )

            # SA-4
            rows4 = [
                ("LCOE P50 (Median Proxy)", f"{_sfmt(sa4.get('lcoe_p50'), '.1f')} USD/MWh"),
                ("Confidence Interval (90%)", f"{_sfmt(sa4.get('ci90_width'), '.1f')} USD/MWh Spread"),
                ("Standard Deviation", f"{_sfmt(sa4.get('lcoe_std'), '.1f')} USD/MWh"),
            ]
            tech_section.subsections.append(
                ReportSection(title="SA-4 · LCOE Uncertainty", rows=rows4)
            )

            # SA-6
            rows6 = [
                ("Base potential evaluated", f"{_sfmt(sa6.get('base_gw'), '.1f')} GW"),
                ("Base generation estimated", f"{_sfmt(sa6.get('base_twh'), '.1f')} TWh/yr"),
            ]
            tech_section.subsections.append(
                ReportSection(title="SA-6 · Potential Parameter Sensitivity", rows=rows6)
            )

            sections.append(tech_section)

        # ── SA-5 section ──────────────────────────────────────────────────
        if "sa5_sobol" in rs:
            sa5 = rs["sa5_sobol"]
            df5 = sa5.get("_df")
            rows5 = [
                ("Dominant parameter (ST proxy)", sa5.get('dominant', '—')),
            ]
            if df5 is not None and not df5.empty:
                rows5.append(("", ""))  # separador
                for _, r5 in df5.iterrows():
                    rows5.append(
                        (f"{r5['parameter']} (ST)", _sfmt(r5['ST'], '.4f'))
                    )
            sections.append(
                ReportSection(
                    title="SA-5 · Sobol Global Sensitivity (GHG Abatement Function)",
                    rows=rows5
                )
            )

        return build_phase_report(
            title="SENSITIVITY ANALYSIS REPORT",
            country_name=country_name,
            country_code=code,
            timestamp=date.today().isoformat(),
            sections=sections,
            timings={},
            elapsed_total=elapsed,
            width=72,
        )