# 04 — Algorithms and Methods

## MCDA (Phase 3 — `src/processors/suitability_builder.py`, orchestration only; math in `src/utils/`)

### AHP — `src/utils/ahp.py`
Analytic Hierarchy Process (Saaty, 1980). Builds a pairwise comparison matrix on the Saaty scale, derives criterion weights via the geometric-mean method, and validates the consistency ratio (CR) against `AHP_RANDOM_INDEX` — required CR ≤ 0.10. Reference: Al Garni & Awasthi (2017), *Renew. Sustain. Energy Rev.* 76.

### TOPSIS — `src/utils/topsis.py`
Technique for Order of Preference by Similarity to Ideal Solution (Hwang & Yoon, 1981). Computes, per pixel, Euclidean distance to the positive and negative ideal solutions in AHP-weighted criterion space; outputs a single [0,1] closeness score. This is the **primary** suitability surface used downstream (see `03-pipeline.md`).

### OWA — `src/utils/owa.py`
Ordered Weighted Averaging (Yager, 1988). Normalizes scenario-specific weight vectors (optimistic / balanced / conservative, from `parameters.json`'s `owa` block — must sum to 1.0 and be non-increasing) and performs spatial aggregation. Secondary output; see the Phase 4 wiring note in `03-pipeline.md`.

### Exclusion — `src/utils/exclusion.py`
Deterministic hard-exclusion mask applied before any of the above (slope, land cover, protected areas, water) — not itself an MCDA method, but a gate on it. `TechnologyConfig`/`ExclusionResult` dataclasses.

## Economics (Phase 4/5 — `src/utils/economics.py`)

- **Capital Recovery Factor (CRF)** and **LCOE** formula (standard discounted CAPEX/OPEX annuity form).
- **Supply curve** (merit-order) generation across the apt-pixel population.
- Capacity-factor clamping between country-specific `cf_floor`/`cf_ceiling`.

## GHG Abatement (Phase 7 — `src/processors/ghg_abatement_calculator.py`)

Scope: **electricity generation sector only**. Core substitution logic (v2.4 semantics, per module docstring):

```
renewable_gap = penetration_factor - existing_renewable_share      # target vs. current
gwh_to_add    = grid_total_gwh × max(0, renewable_gap)
subst_gwh     = min(gwh_to_add, total_th_gwh, renew_total_gwh)
```

If `existing_renewable_share >= penetration_factor`, the country already meets its target and a minimum technical substitution floor (`_MIN_TECH_SUBSTITUTION`) applies instead. If `grid_total_gwh` is not configured for a country, the module falls back to legacy behavior: `subst_gwh = min(total_th_gwh, renew_total_gwh) × penetration`. `configs/net_zero_db.json` supplies the national baseline figures this phase reads. Produces MAC (marginal abatement cost) curves.

## Sensitivity Analysis (Phase 8 — `src/processors/sensitivity_analyzer.py`)

Six independently toggleable sub-analyses (`settings.yaml` → `pipeline.sensitivity.run_sa1`…`run_sa6`):

| ID | Method | Target |
| --- | --- | --- |
| SA-1 | OAT (one-at-a-time) AHP weight perturbation ±10–30% | Spearman ρ on suitability ranking |
| SA-2 | Monte Carlo AHP via Dirichlet-distributed weights | Spatial suitability robustness |
| SA-3 | Threshold sweep | Area elasticity vs. suitability cutoff |
| SA-4 | Triangular Monte Carlo on CAPEX/OPEX/CF | LCOE uncertainty |
| SA-5 | Parameter elasticity (power density, CF) | Potential-calculation sensitivity |
| SA-6 | Sobol global sensitivity (`SALib`), first-order (S1) and total-order (ST) indices | GHG abatement indices |

References cited in-module: Saltelli et al. (2008, 2010), Malczewski (1999).

## Transport Decarbonization (Phase 9)

EV/hydrogen penetration scenarios, transport energy-demand modeling, and charging-hub siting (threshold-based on suitability rasters, `settings.yaml`'s `pipeline.transport.hub_suitability_threshold`). Parameters in `configs/transport_parameters.json` (`global_defaults` + per-country overrides).

> ⚠️ Point to validate: the exact hydrogen/EV demand model equations were not read in full during this pass (module is 2237 lines, the largest in the codebase) — treat this section as a locator, not a full method description, until someone reads `transport_decarbonization_calculator.py` in depth.

## Methodological references (from `README.md` §12)

- Saaty, T.L. (1980). *The Analytic Hierarchy Process*. McGraw-Hill.
- Hwang, C.L., & Yoon, K. (1981). *Multiple Attribute Decision Making*. Springer.
- Yager, R.R. (1988). On ordered weighted averaging aggregation operators. *IEEE Trans. SMC* 18(1).
- Zanaga et al. (2022). ESA WorldCover 10 m 2021 v200. *Zenodo*. doi:10.5281/zenodo.7254221.
- Global Solar Atlas 2.0 (World Bank / Solargis, 2023); Global Wind Atlas 3.0 (DTU / World Bank, 2023).
- UNEP-WCMC & IUCN (2024). Protected Planet.
- IRENA (2024). *Renewable Power Generation Costs in 2023*.
- Saltelli et al. (2010). Variance based sensitivity analysis of model output. *Computer Physics Communications* 181(2).
