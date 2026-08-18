# SUMMARY

Technical inventory of the GeoWorld Framework codebase. One entry per module: purpose, size, responsibilities, and relationships. For narrative documentation (architecture rationale, pipeline flow, decisions, risks), see [`docs/memory/README.md`](docs/memory/README.md).

Format per entry: **description** · **LOC** · **responsibilities** · **depends on / used by**.

---

## Entry point

### `main.py` (1001 lines)
CLI entry point and pipeline driver. Parses country name/ISO3 or `--batch <file>` with `--workers N` for parallel execution via `ProcessPoolExecutor`. Resolves country boundaries (GADM via `geopandas`), sets up logging context, and hands off phase execution to `PipelineOrchestrator`.
Depends on: all of `src.core`, `src.io`, `src.processors`, `src.utils.logging_utils`.

---

## `src/core/` — configuration, contracts, orchestration

### `config_loader.py` (620 lines)
Reads `configs/settings.yaml` (infrastructure) and `configs/parameters.json` (per-country science parameters), merges them with `constants.DEFAULT_TECH_PARAMS` fallbacks, and returns validated `CountryParams` (Pydantic). Loads credentials from `.env` via `python-dotenv`.
Depends on: `schemas.py`, `constants.py`, `PyYAML`, `python-dotenv`. Used by: `main.py` and every processor.

### `constants.py` (392 lines)
Scientific constants and `DEFAULT_TECH_PARAMS` (last-resort technology fallback values: capacity factor, power density, land-use factor, thresholds per technology). Tier-2 in the parameter-resolution hierarchy (`parameters.json` is tier 1).
Depends on: none (leaf module).

### `pipeline_orchestrator.py` (319 lines)
`PipelineOrchestrator` class implementing the uniform phase-execution contract: skip-flag check → cache lookup → input assembly → phase `.run()` → output validation → artifact persistence → state update. Central place where `settings.yaml`'s `skip_*` flags take effect.
Depends on: `schemas.py`, `validators.py`, `src.io.artifact_manager`. Used by: `main.py`.

### `schemas.py` (875 lines)
Single source of truth for all Pydantic v2 data contracts: `CountryParams`, `CriteriaResult`, `SuitabilityResult`, `PotentialResult`, `LCOEResult`, pipeline state models. Explicitly documented as the replacement for a removed `models.py` (see `docs/memory/06-risk-areas.md`). Config models are `frozen=True`; result models validate what gets persisted to disk, not in-memory arrays.
Depends on: `pydantic`. Used by: nearly every module in `src/core`, `src/io`, `src/processors`.

### `validators.py` (257 lines)
Path-based validation utilities for raster shape/CRS/value consistency and inter-phase contract checks (`AlignedLayers`). Operates on file paths rather than open dataset handles to avoid descriptor leaks.
Depends on: `schemas.py`, `rasterio`. Used by: `pipeline_orchestrator.py`.

---

## `src/io/` — data discovery, acquisition, persistence

### `data_manager.py` (552 lines)
Locates raw geospatial files on disk for a given country under the expected `raw_path` layout (borders, land cover, elevation, solar/wind potential, population, roads, grid, hydrology, seismic risk, protected areas). Discovery only — no downloading or processing.
Used by: `data_orchestrator.py`, `main.py`.

### `data_fetcher.py` (1474 lines)
Downloads external datasets (GADM boundaries, ESA WorldCover, Copernicus GLO-30/90 DEM, WorldPop, OSM via Overpass API) with resilient HTTP/FTP I/O, exponential backoff, and adaptive tiling for large countries. The largest single module in `src/io`.
Depends on: `requests`, `dem_stitcher`, `terracatalogueclient` (optional). Used by: `data_orchestrator.py`.

### `data_orchestrator.py` (518 lines)
Coordinates discovery + parallel acquisition of raw inputs; applies layer-specific filtering to prevent cross-country data contamination (country-scoped vs. global layers).
Depends on: `data_manager.py`, `data_fetcher.py`. Used by: `main.py` (Phase 0).

### `artifact_manager.py` (251 lines)
Centralized on-disk artifact management: manifest files, result serialization/deserialization (pickle), consistent path resolution under `outputs/{country_code}/`.
Used by: `pipeline_orchestrator.py`.

---

## `src/processors/` — the nine pipeline phases

### `data_auditor.py` (1357 lines) — Phase 1
Raw data quality audit prior to any processing. Masks datasets to the actual country polygon (not bounding box) via `rasterio.mask()` to avoid overcounting neighboring territory/ocean. Read-only — does not modify data.
Depends on: `src.utils.geo_stats`, `rasterio`, `geopandas`.

### `raster_processor.py` (94 lines)
Small utility processor: derives slope from DEM rasters, reading in 512-row blocks to bound memory use on global-extent DEMs.
Used by: `grid_aligner.py` (Phase 2a).

### `grid_aligner.py` (1212 lines) — Phase 2a
Reprojects all raw heterogeneous rasters/vectors onto a unified reference grid (shared CRS, affine transform, dimensions). Performs AHP aggregation across wind hub heights (50/100/200 m) as part of harmonization.
Depends on: `src.utils.ahp`, `raster_processor.py`, `src.utils.utils`.

### `criteria_builder.py` (1086 lines) — Phase 2b
Converts physical units (slope degrees, distance km, irradiance kWh/m²) into normalized [0,1] fuzzy suitability scores per criterion. Two-stage internally: vectorized NumPy algebra, then async Matplotlib (Agg backend, OO Figure/Axes for thread safety) for map rendering via `ThreadPoolExecutor`.
Depends on: `src.utils.normalization`, `src.utils.map_styling`, `src.utils.timing`.

### `suitability_builder.py` (1009 lines) — Phase 3
MCDA suitability scoring for Solar/Wind/Biomass: AHP-derived criterion weights (Saaty pairwise matrix, geometric-mean method, CR ≤ 0.10) aggregated via TOPSIS (primary output, single 0–1 score) and OWA (3 scenarios: optimistic/balanced/conservative). Refactored to orchestration-only; math lives in `src/utils/{ahp,topsis,owa,exclusion}.py`.
Depends on: `src.utils.{ahp,topsis,owa,exclusion,raster_io,params_helpers}`.

### `potential_calculator.py` (1024 lines) — Phase 4
Installable capacity (GW) and annual generation (GWh/yr) per pixel. Uses Phase 3's TOPSIS suitability as the primary surface, applies per-scenario threshold offsets from `settings.yaml`. OWA-based scenario selection is implemented but not yet wired into the orchestrator (`use_owa=True` reserved for future use).
Depends on: `src.utils.economics`, `src.utils.raster_io`.

### `lcoe_calculator.py` (1493 lines) — Phase 5
Spatial LCOE (USD/MWh) per pixel and technology-dominance raster. Strictly reads scientific parameters from `parameters.json` only (not `settings.yaml`). Calculates LCOE only for the apt-pixel mask produced by Phase 4, ensuring consistent pixel population downstream.
Depends on: `src.utils.economics`, `src.utils.data_recovery`, `src.utils.raster_io`, `src.utils.reporting`.

### `results_writer.py` (1071 lines) — Phase 6
Final synthesis: technology suitability/LCOE dominance maps (GeoTIFF + PNG), executive dashboard, text report. Delegates data recovery to `src.utils.data_recovery`, report text to `src.utils.reporting`, dashboard panel drawing to `src.visualization.dashboard_panels`; retains only raster I/O and dominance-map plotting.
Depends on: `src.utils.data_recovery`, `src.utils.reporting`, `src.visualization.dashboard_panels`.

### `ghg_abatement_calculator.py` (1456 lines) — Phase 7
GHG abatement and carbon intensity for the **electricity generation sector only**. Models substitution of fossil thermal generation (coal/gas/oil) by renewables against a target penetration factor vs. current renewable share, with a minimum technical-substitution floor and legacy fallback when national grid totals aren't configured. Produces MAC curves.
Depends on: `configs/net_zero_db.json`, `src.utils.economics`, `src.utils.abatement_plots`.

### `sensitivity_analyzer.py` (1094 lines) — Phase 8
Six sensitivity sub-analyses (SA-1 to SA-6): OAT weight perturbation (Spearman ρ), Monte Carlo AHP (Dirichlet), threshold sweep, LCOE uncertainty (triangular MC), potential-parameter elasticity, and Sobol global sensitivity (via `SALib`) on GHG abatement indices. Each sub-analysis independently toggleable via `settings.yaml`. Orchestration only as of the 2026-08-18 enxugamento — pure math lives in `sensitivity_math.py` (REFACTOR-004, partial — see `docs/BACKLOG.md`).
Depends on: `SALib`, `src.utils.data_recovery`, `src.utils.economics`, `src.utils.sensitivity_math`, `src.utils.sensitivity_plots`.

### `transport_decarbonization_calculator.py` (1758 lines) — Phase 9
EV/hydrogen penetration scenarios, transport energy demand, and charging-hub siting (threshold-based on suitability rasters). Was removed and re-added earlier in git history (see `docs/memory/06-risk-areas.md`). Plotting split out to `transport_plots.py` (REFACTOR-002).
Depends on: `configs/transport_parameters.json`, `rasterio`, `src.utils.transport_plots`.

---

## `src/utils/` — shared, technology-agnostic libraries

### `ahp.py` (166 lines)
Analytic Hierarchy Process: pairwise comparison matrix construction, geometric-mean weight computation, consistency-ratio (CR) validation against `AHP_RANDOM_INDEX`/`AHP_SCALE_TABLE` (from `constants.py`).
Used by: `grid_aligner.py`, `suitability_builder.py`.

### `topsis.py` (139 lines)
TOPSIS spatial MCDA: Euclidean distance to positive/negative ideal solutions in AHP-weighted criterion space, per-pixel.
Used by: `suitability_builder.py`.

### `owa.py` (131 lines)
Ordered Weighted Averaging: weight-vector normalization and spatial aggregation for the three scenario weight sets (optimistic/balanced/conservative).
Used by: `suitability_builder.py`.

### `exclusion.py` (157 lines)
Hard-exclusion logic (slope limits, land-cover class exclusions) applied before MCDA. Defines `TechnologyConfig` and `ExclusionResult` dataclasses.
Used by: `suitability_builder.py`.

### `normalization.py` (114 lines)
Percentile-based array normalization (`normalize_percentile`) shared across criteria/LCOE computation.
Used by: `criteria_builder.py`, `lcoe_calculator.py`.

### `economics.py` (358 lines)
Financial modeling: Capital Recovery Factor, LCOE formula, supply-curve (merit-order) generation, capacity-factor clamping.
Used by: `lcoe_calculator.py`, `potential_calculator.py`, `sensitivity_analyzer.py`.

### `geo_stats.py` (170 lines)
Generic geospatial array statistics: per-pixel area (WGS84 Helmert ellipsoidal series), scalar pixel area, zonal aggregation by admin polygon.
Used by: `data_auditor.py` and area-dependent processors.

### `raster_io.py` (183 lines)
Shared raster I/O helpers: `load_reference_meta`, `load_all_criteria`, `load_aux_raster`, `get_raster_meta`.
Used by: `suitability_builder.py`, `lcoe_calculator.py`, `potential_calculator.py`.

### `data_recovery.py` (551 lines)
Reconstructs Pydantic result models from on-disk CSV/TIF/JSON artifacts (priority fallback CSV → TIF → JSON → estimation) when results come from cache, a resumed pipeline, or Phase 6 aggregating Phases 3–5.
Used by: `results_writer.py`, `sensitivity_analyzer.py`.

### `params_helpers.py` (60 lines)
`extract_params_dict()` — duck-typed flattening of `CountryParams` (Pydantic model or dict) into a plain dict.
Used by: `suitability_builder.py` and other processors.

### `map_styling.py` (1510 lines)
`GeoWorldStyler` — the unified map-rendering engine: figure/axes creation with cosine-corrected aspect ratio, basemap/context-country rendering, titles/footers/legends/colorbars, compass rose, scale bar, multi-panel PIL compositing. `render_raster_map()` is the single entry point used by every phase that produces a map.
Depends on: `matplotlib`, `PIL`. Used by: nearly every `src/processors/*` module and `dashboard_panels.py`.

### `dashboard_panels.py` (see `src/visualization/`, 781 lines) — cross-referenced here as it consumes `map_styling`.

### `abatement_plots.py` (1932 lines)
All Phase 7 (GHG abatement) figures, split out of `ghg_abatement_calculator.py` to keep that module lean. Each function takes a `GeoWorldStyler` and data explicitly — no global state. Scope note: figures cover the electricity sector only; total national CO₂ figures used for context include all sectors.
Depends on: `map_styling.GeoWorldStyler`. Used by: `ghg_abatement_calculator.py`.

### `sensitivity_math.py` (765 lines)
Pure SA-1–SA-6 statistical methods (OAT weight perturbation, Dirichlet Monte Carlo, threshold sweep, LCOE triangular MC, potential elasticity, Sobol GHG) plus the shared TOPSIS/criteria-loading helpers, split out of `sensitivity_analyzer.py` (REFACTOR-004, partial — `run()` itself not yet split, see `docs/BACKLOG.md`).
Used by: `sensitivity_analyzer.py`.

### `sensitivity_plots.py` (545 lines)
All Phase 8 (sensitivity) figures — KPI dashboards, tornado charts, SA-1 heatmaps, SA-2 CV distributions, SA-3 threshold curves, SA-4 LCOE histograms, SA-5 Sobol bar plots, SA-6 potential charts — split out of `sensitivity_analyzer.py` (REFACTOR-001).
Used by: `sensitivity_analyzer.py`.

### `transport_plots.py` (531 lines)
Phase 9 (transport decarbonization) figures — fleet transition, emissions trajectory, renewable-need, hub-siting maps — split out of `transport_decarbonization_calculator.py` (REFACTOR-002).
Used by: `transport_decarbonization_calculator.py`.

### `reporting.py` (412 lines)
`ReportSection` + `build_phase_report()` — universal text-report formatter (width, separators, alignment) eliminating duplicated formatting code across `suitability_builder`, `sensitivity_analyzer`, `lcoe_calculator`, `potential_calculator`, `results_writer`.
Used by: five processor modules.

### `logging_utils.py` (326 lines)
Centralized logging: `ContextFilter` (injects country/phase via `ContextVar`), `StructuredLogHandler` (JSON Lines output), `GDALWarningFilter`, `gdal_quiet()` context manager, `setup_logging()` (console + `.log` + `.jsonl` handlers), `set_logging_context()` for worker processes.
Used by: `main.py` and effectively every module (via `logging.getLogger`).

### `timing.py` (66 lines)
`timer()` context manager for phase step timing, with or without an accumulating dict. Deduplicated ~40 lines that were previously copy-pasted across four modules.
Used by: `suitability_builder.py`, `criteria_builder.py`, `potential_calculator.py`.

### `utils.py` (359 lines)
Generic geospatial helpers: `safe_raster_open`/`safe_raster_write` context managers, `reproject_to_target`, `get_intersecting_files`, `is_geographic_crs`, `get_local_utm_crs`, `get_mainland_bounds`, `compute_pixel_area_geodesic`.
Used by: most `src/processors/*` and `src/io/*` modules.

---

## `src/visualization/`

### `dashboard_panels.py` (781 lines)
`DashboardPanels` class: reusable, individually-testable dashboard panel drawers (potential bars, LCOE box-whisker with IRENA benchmarks, supply curves, summary tables, abatement KPI cards) extracted from `results_writer._plot_executive_dashboard`.
Depends on: `map_styling.GeoWorldStyler`. Used by: `results_writer.py`.

> ⚠️ Point to validate: `src/visualization/` has no `__init__.py` (unlike `core`, `io`, `processors`, `utils`, which all have one, even if empty). Confirm whether this is intentional or an oversight.

---

## `configs/`

| File | Lines | Purpose |
| --- | --- | --- |
| `settings.yaml` | 177 | Infrastructure, paths, resolutions, visualization layout, per-phase `skip_*` flags, scenario offsets. No scientific parameters. |
| `parameters.json` | 774 | Per-country (`PRT`, `BRA`, `EGY`, `CHN`, `RUS`, `IND`, `ZAF`) scientific parameters: solar/wind/biomass technology params, OWA weight sets, LCOE economics, abatement defaults, land-suitability class scores, global `exclusion_thresholds`. |
| `net_zero_db.json` | 270 | Reference database of national net-zero/GHG baseline figures, keyed by ISO3 (broader country coverage than `parameters.json`, includes e.g. `AUS`, `DEU`, `ESP`, `FRA`, `GBR`). Consumed by Phase 7. |
| `transport_parameters.json` | 555 | Global defaults + per-country parameters for Phase 9 (transport decarbonization). |

---

## Not yet covered by automated tooling

No test suite exists in this repository as of this writing (no `tests/` directory, no `pytest`/`unittest` files found). See `docs/memory/06-risk-areas.md`.
