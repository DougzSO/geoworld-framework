"""
sensitivity_analyzer.py — Phase 8: Sensitivity Analysis
==========================================================
Sensitivity analysis for the GeoWorld pipeline targeting Q1 publications.

SA-1 · OAT Weight Sensitivity: Perturbs AHP weights (±10 to 30%). Spearman ρ.
SA-2 · Monte Carlo AHP: Dirichlet distributions on weights (decision robustness
       under the framework's real scenario threshold -- see METRIC_013 below).
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

  METRIC_013 (change): SA-2's stable_fraction (fraction of pixels whose 90%
                 CI band on the raw TOPSIS score is narrower than 0.10)
                 replaced with a threshold-crossing metric: among pixels apt
                 under the base AHP weights, what fraction of the 1000
                 Dirichlet weight samples keep that pixel above the real
                 scenario threshold. A prototype (scratchpad/
                 threshold_crossing_prototype.py, validated against the
                 real pipeline's own logged stable_fraction numbers before
                 trusting the new metric) showed stable_fraction couldn't
                 distinguish PRT from BRA on wind (4.4% vs. 4.3%) while the
                 threshold-crossing metric revealed a real inversion (PRT
                 wind: 72.1% of apt pixels in the 25-75% boundary zone,
                 barely 2.3% decisive; BRA wind: 53.1% decisive). The old
                 metric answered "is the score numerically stable"; the
                 framework's actual output is a threshold-based apt/not-apt
                 decision, which is what this metric now measures directly.
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
import numpy as np
import pandas as pd
import rasterio

from src.core.schemas import CountryParams
from src.utils.map_styling import GeoWorldStyler
from src.utils.raster_io import find_suitability_tif
from src.utils.reporting import ReportSection, build_phase_report

try:
    from src.utils.sensitivity_plots import (
        TECH_LABEL,
        plot_sa1_tornado,
        plot_sa1_heatmap,
        plot_sa2_cv,
        plot_sa3_threshold,
        plot_sa4_lcoe,
        plot_sa5_sobol,
        plot_sa6_potential,
        plot_dashboard,
    )
    _SENSITIVITY_PLOTS_AVAILABLE = True
except ImportError:
    _SENSITIVITY_PLOTS_AVAILABLE = False
    TECH_LABEL: Dict[str, str] = {
        "solar":   "Solar PV",
        "wind":    "Wind Onshore",
        "biomass": "Biomass / Bioenergy",
    }
    plot_sa1_tornado = plot_sa1_heatmap = plot_sa2_cv = None
    plot_sa3_threshold = plot_sa4_lcoe = plot_sa5_sobol = None
    plot_sa6_potential = plot_dashboard = None

logger = logging.getLogger("geoworld.processors.SensitivityAnalyzer")

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


def _balanced_threshold(tech: str, pot_results: Any) -> float:
    """
    Resolve the real balanced-scenario TOPSIS threshold from Phase 4's
    persisted result, for SA-2's threshold-crossing metric (METRIC_013).

    Deliberately does not reuse SensitivityAnalyzer._resolve_tech_params()'s
    `base_thr` -- that helper's `country_params is not None` branch always
    returns a hardcoded 0.60 fallback in practice (CountryParams has no
    `suitability_threshold` attribute, so getattr's default silently wins,
    and the branch that would read the real value from `pot_results` is
    never reached). That is a separate, pre-existing issue, out of scope
    here; this function reads Phase 4's actual persisted threshold
    directly instead, independent of that other code path.
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
        self.styler = GeoWorldStyler(
            cfg.system.get("visualization", {}),
            global_dpi=cfg.system.get("pipeline", {}).get("map_dpi_export", 150),
        )

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
                    plot_sa1_tornado(
                        self.styler, df1, tech,
                        out_dir / f"{country_code}_{tech}_sa1_tornado.png",
                    )
                    plot_sa1_heatmap(
                        self.styler, df1, tech,
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
                    sa2_threshold = _balanced_threshold(tech, pot_results)
                    logger.info(
                        "[%s] SA-2: Monte Carlo AHP (%d samples, thr=%.2f)...",
                        tech, n_mc_samples, sa2_threshold,
                    )
                    mc = sa2_monte_carlo_weights(
                        criteria_dir, list(weights.keys()), weights,
                        height, width, threshold=sa2_threshold,
                        n_samples=n_mc_samples,
                    )
                    results_sa[tech]["sa2"] = {
                        "threshold":          mc["threshold"],
                        "concentration":      mc["concentration"],
                        "n_apt_base":         mc["n_apt_base"],
                        "decisive_fraction":  mc["decisive_fraction"],
                        "boundary_fraction":  mc["boundary_fraction"],
                        "moderate_fraction":  mc["moderate_fraction"],
                        "mean_cv":            float(mc["cv"].mean()),
                        "mean_ci90_width":    float(mc["ci_width"].mean()),
                        "elapsed_s":          round(time.perf_counter() - t0, 1),
                        "_cv":                mc["cv"],
                    }
                    plot_sa2_cv(
                        self.styler, mc["cv"], mc["ci_width"], tech,
                        out_dir / f"{country_code}_{tech}_sa2_cv_dist.png",
                    )
                    logger.info(
                        "[%s] SA-2 complete: decisive=%.1f%% boundary=%.1f%% "
                        "moderate=%.1f%% (thr=%.2f, n_apt=%d)",
                        tech,
                        mc["decisive_fraction"] * 100,
                        mc["boundary_fraction"] * 100,
                        mc["moderate_fraction"] * 100,
                        mc["threshold"], mc["n_apt_base"],
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
                        plot_sa3_threshold(
                            self.styler, df3, tech, base_thr,
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
                    plot_sa4_lcoe(
                        self.styler, df4, tech,
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
                        plot_sa6_potential(
                            self.styler, df6, tech,
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
                plot_dashboard(
                    self.styler,
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
                    plot_sa5_sobol(
                        self.styler, df5, out_dir / f"{country_code}_sa5_sobol_barplot.png"
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
                title=f"{tech.upper()} [{TECH_LABEL.get(tech, tech)}]"
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
            decisive_pct = sa2.get('decisive_fraction', 0) * 100
            boundary_pct = sa2.get('boundary_fraction', 0) * 100
            moderate_pct = sa2.get('moderate_fraction', 0) * 100
            rows2 = [
                ("Threshold used", _sfmt(sa2.get('threshold'), '.2f')),
                ("Apt pixels (base weights)", str(sa2.get('n_apt_base', '—'))),
                ("Decisive (>95% or <5% crossing)", f"{_sfmt(decisive_pct, '.1f', '0.0')}%"),
                ("Boundary (25-75% crossing)", f"{_sfmt(boundary_pct, '.1f', '0.0')}%"),
                ("Moderate (rest)", f"{_sfmt(moderate_pct, '.1f', '0.0')}%"),
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