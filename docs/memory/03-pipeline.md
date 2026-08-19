---
id: mem-03-pipeline
type: reference
status: active
created: 2026-08-06
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [mem-02-architecture]
linked_by: [mem-readme, mem-04-algorithms, docs-decisions]
scope: "As 9 fases do pipeline e seus contratos de dados; a matemática de cada método vive em 04-algorithms.md, não aqui."
---

# 03 — Pipeline / Data Flow

## The nine phases

Each phase is idempotent: it reads artifacts written by the previous phase from `outputs/{ISO3}/` or `data/processed/{ISO3}/`, and can be individually skipped via `configs/settings.yaml` (`pipeline.skip_*: true`) if a valid cached result already exists — see the orchestration contract in [`02-architecture.md`](02-architecture.md).

| Phase | Module | Class | Produces |
| --- | --- | --- | --- |
| 0 | `main.py` / `src/io/data_orchestrator.py` | `DataOrchestrator` | Boundary resolution (GADM), parallel raw-data acquisition |
| 1 | `src/processors/data_auditor.py` | `DataAuditor` | Raw-data quality audit report (read-only, polygon-masked) |
| 2a | `src/processors/grid_aligner.py` | `GridAligner` | Common-grid, common-CRS aligned rasters in `data/processed/{ISO3}/` |
| 2b | `src/processors/criteria_builder.py` | `CriteriaBuilder` | Normalized [0,1] suitability criteria (maps + numeric reports) |
| 3 | `src/processors/suitability_builder.py` | `SuitabilityBuilder` | TOPSIS + OWA suitability rasters (3 technologies × 3 OWA scenarios) |
| 4 | `src/processors/potential_calculator.py` | `PotentialCalculator` | Installable capacity (GW) and generation (GWh/yr) per pixel |
| 5 | `src/processors/lcoe_calculator.py` | `LCOECalculator` | LCOE maps (USD/MWh) + technology-dominance raster |
| 6 | `src/processors/results_writer.py` | `ResultsWriter` | Dominance maps, executive dashboard, aggregated GeoTIFFs |
| 7 | `src/processors/ghg_abatement_calculator.py` | `GHGAbatementCalculator` | Thermal substitution, CO₂ abatement, MAC curves |
| 8 | `src/processors/sensitivity_analyzer.py` | `SensitivityAnalyzer` | SA-1…SA-6 outputs (CSV + plots) |
| 9 | `src/processors/transport_decarbonization_calculator.py` | `TransportDecarbonizationCalculator` | EV/H₂ penetration scenarios, charging-hub siting |

## Entry point / invocation

```bash
python main.py Portugal          # full country name
python main.py PRT               # ISO-3166-alpha-3
python main.py --batch country_list.txt --workers 4   # batch, parallel via ProcessPoolExecutor
```

See [`10-scripts-and-commands.md`](10-scripts-and-commands.md) for the full command reference.

## Data contract boundary

Every phase's output that gets **persisted to disk** is validated against a Pydantic v2 model in `src/core/schemas.py` (`CriteriaResult`, `SuitabilityResult`, `PotentialResult`, `LCOEResult`, etc.) — not the in-memory intermediate dict, which may hold `ndarray`/`GeoDataFrame`/`Affine` objects outside Pydantic's boundary. This is stated explicitly in `schemas.py`'s module docstring as a deliberate design principle.

## Phase 3 output detail (two suitability surfaces)

Phase 3 produces two families of suitability rasters per technology, and downstream phases pick one deliberately:

- **TOPSIS** (`{ISO3}_{tech}_suitability.tif`) — single scalar closeness-to-ideal score; the **primary** surface used by Phase 4 onward.
- **OWA**, three scenario variants (`_owa_optimistic` / `_owa_balanced` / `_owa_conservative`) — secondary, scenario-based; available for scenario analysis but Phase 4's `use_owa=True` path is implemented and **not yet wired into the orchestrator** (per `potential_calculator.py`'s own docstring — "reserved for future scenario-specific runs").

This non-integration is a deliberate decision, not an implementation gap — full rationale and status → [`../DECISIONS.md`](../DECISIONS.md) D4.

Every read call site that needs to locate a Phase 3 suitability TIF goes through `src/utils/raster_io.py::find_suitability_tif()` (BLOCKER-006), which always tries TOPSIS first — centralizing what used to be duplicated, independently-implemented lookup logic per phase (one copy, in Phase 6, had inverted the precedence and tried OWA-balanced first).

## Hard exclusions applied before MCDA (Phase 3)

- Slope threshold + per-technology offset (solar +5°, wind +10°, biomass +20°) from `src/utils/exclusion.py`.
- ESA WorldCover classes: built-up (50), snow/ice (70), water (80), wetland (90), mangroves (95); forest (10) optionally, per `parameters.json`'s `forest_as_exclusion`.
- Protected areas with IUCN score < 0.01 (categories Ia/Ib).
- Lakes (binary HydroLAKES mask).

## Criteria produced (Phase 2b) — reference

`solar_resource`, `wind_resource`, `terrain_score`, `slope_degrees`, `lc_solar`/`lc_wind`/`lc_biomass`, `biomass_resource`, `proximity_plants`, `protected_areas`, `river_solar`/`river_wind`/`river_biomass`, `lakes_exclusion`, `seismic_suitability`, `grid_suitability`, `pop_suitability`, `road_suitability`. Each has a corresponding entry in `configs/settings.yaml`'s `visualization.criteria_meta` (title, unit label, colormap) — adding a new criterion means adding both the computation in `criteria_builder.py` and a `criteria_meta` entry.
