---
id: analysis-arch-misalignments
type: frozen
status: frozen
created: 2026-08-06
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: []
linked_by: [docs-tasks, archive-backlog-full]
scope: "Auditoria original nº1 (estrutura/responsabilidades de módulos), congelada no estado do código de 2026-08-06; NÃO reflete correções aplicadas depois — status atual vive em TASKS.md/archive/backlog-full-2026-08.md."
---

# Architecture Misalignments — Analysis

Analysis only. No code changed in this pass. Scope: `results_writer.py` (Phase 6) read in full; the other six modules flagged in `SUMMARY.md` as >1000 LOC were analyzed structurally (function/class signatures + line positions via `grep`, not full-body reads) — marked per section. Line ranges for `results_writer.py` are exact; line ranges elsewhere are derived from consecutive `def`/`class` start lines and are therefore approximate (±a few lines) until someone reads the body directly.

---

## 1. `src/processors/results_writer.py` (1553 LOC, Phase 6) — full read

### Current state

The module docstring claims Phase 6 "retains only: Raster I/O (load, validate, dominance compute, GeoTIFF export) · Map rendering · LCOE stats enrichment · Supply curve TIF reconstruction · Orchestration." That description undersells what's actually in the file. In practice it mixes four distinct responsibilities:

1. **Genuine I/O and orchestration** — TIF discovery (`_find_suitability_tif` L113–153, `_find_lcoe_tif` L159–190, `_find_suitable_tif` L196–212), array loading (`_load_suitability_arrays` L717–740, `_load_lcoe_arrays` L742–780), `run()` (L487–651), artifact persistence (`_persist_artifacts` L819–868). This is legitimately "write final outputs."
2. **Cross-technology aggregation that only Phase 6 can do** — `_build_suitability_dominance` (L874–899) and `_build_lcoe_dominance` (L901–935) compare the three technologies pixel-by-pixel to determine which one "wins." No earlier phase does this (each of Phases 3/5 works per-technology). This is legitimately new computation, correctly placed here — but it is computation, not I/O, and the docstring should say so.
3. **Map/dashboard rendering** — `_build_rgba` (L941–990), `_plot_dominance_map` (L996–1091), `_plot_executive_dashboard` (L1097–1193), `_build_dashboard_layout` (L1195–1252), `_draw_dominance_on_ax` (L1258–1321). Legitimately "map rendering" per the docstring, but see "Extract to" below — this duplicates the extraction pattern already applied to 5 of 7 dashboard panels.
4. **Statistical/scientific recomputation that does not belong in a synthesis phase** (see §1a and §2 below) — this is the actual misalignment.

### 1a. Functions that don't fit "write final outputs"

| Function | Lines | What it actually does | Why it's misplaced |
| --- | --- | --- | --- |
| `_enrich_lcoe_stats` | L294–354 | Computes p25/median/p75 percentiles from raw LCOE pixel values (reads the Phase-5 TIF back off disk and calls `np.percentile`), OR falls back to linearly interpolating them from p10/p90 if the TIF is unavailable. | This is a **statistics computation**, not report formatting. The docstring even says why it exists: "the calculator does not compute quartiles" — i.e., Phase 5 (`lcoe_calculator.py`) has an incomplete output contract, and Phase 6 patches around it by recomputing. |
| `_recover_supply_curve_from_tif` | L379–428 | Reconstructs a supply-curve `DataFrame` from raw LCOE pixel values using a **"unit capacity proxy" (1 MW/pixel)** — the function's own docstring admits "absolute GW totals differ from Phase 5." | This is not recovery, it's an **approximated re-derivation** that can diverge numerically from what Phase 5 actually computed. A synthesis phase silently substituting an approximation for a real prior-phase result is a correctness risk, not just an architecture one. |
| `_compute_integrated_area` | L434–481 | Recomputes total suitable area (km²) per technology by re-reading Phase 4's suitable-pixel mask TIF and re-running `build_pixel_area_array()` — the in-code comment says this must "always" match Phase 4's own calculation exactly. | If Phase 4 already computes this area internally (it must, to report capacity/generation), Phase 6 is **redoing a calculation Phase 4 already did**, using Phase 4's raw pixel mask as a substitute for a number Phase 4 should simply persist and Phase 6 should read. |
| `_get_scenario_data` (class + module-level duplicate, L1484–1492 and L1499–1523) | L1484–1523 | Defensively reaches into `potential_results` under two different possible dict shapes (`{"techs": {tech: {...}}}` vs legacy `{tech: {...}}`) because the caller doesn't know whether it received a live in-memory result or a disk-recovered one. | Symptom of Phase 4's output not having one canonical shape. This "try shape A, fall back to shape B" pattern recurs elsewhere in the codebase (see `_PotentialView`/`_LCOEView` in `transport_decarbonization_calculator.py`, §3 below) — three independent reimplementations of the same defensive normalization. |

### 1b. Answer to "does Phase 6 recalculate metrics already computed upstream, or does it purely read + format?"

**It recalculates.** It is not a pure read-and-format synthesis stage. Three concrete recalculations were found (table above): LCOE quartiles, supply curve, and integrated suitable area. All three are metrics that conceptually belong to an earlier phase's output contract (Phase 5 for LCOE stats/supply curve, Phase 4 for area) and are being **reconstructed from raw pixel arrays inside Phase 6** rather than read from a persisted value. This contradicts the idempotent "reads artifacts written by the previous phase" pipeline contract described in `docs/memory/03-pipeline.md` — the artifacts being read are raw rasters, not the prior phase's *computed results*, so Phase 6 has to re-derive results itself, sometimes approximately.

The dominance computation (`_build_suitability_dominance`/`_build_lcoe_dominance`) is the one exception that's correctly scoped here — it's genuinely new cross-technology comparison, not a recalculation of an existing per-technology metric.

### Should be

Phase 6 should only: (a) read already-computed per-technology results from Phases 3–5 (via `data_recovery.py`, unmodified), (b) compute the cross-technology dominance comparison (the one thing only Phase 6 can do), and (c) render maps/dashboard and format the report. It should never need to re-open a Phase 4/5 raster and recompute a statistic that phase already calculated.

### Extract to

- **`_enrich_lcoe_stats`** → move the percentile computation into `lcoe_calculator.py`'s own stats block (Phase 5 should just always compute p25/median/p75 alongside p10/p90/mean — same TIF, same pass, zero extra I/O) or into `src/utils/economics.py` as a shared `raster_percentile_stats()` helper called once, at source, not reconstructed downstream. Phase 6's `_enrich_lcoe_stats` becomes unnecessary.
- **`_recover_supply_curve_from_tif`** → either (preferred) make `lcoe_calculator.py` always persist the real supply curve (Parquet/CSV) so the Tier-2 fallback is never needed, or, if the fallback must exist, move it into `src/utils/data_recovery.py` next to `recover_supply_curve_from_disk` and label it explicitly as an approximation in its public name (e.g. `reconstruct_approximate_supply_curve_from_tif`) so callers can't mistake it for the real thing.
- **`_compute_integrated_area`** → move into `potential_calculator.py` (Phase 4) as a value computed once and persisted in its stats dict (`PotentialResult` in `schemas.py` would need a new field), OR promote to a shared `src/utils/geo_stats.py` function (`integrated_suitable_area_km2(tif_path, transform)`) called identically by both Phase 4 and Phase 6 instead of only Phase 6 having a copy.
- **`_get_scenario_data`** (and its module-level duplicate) → delete the class-level wrapper (L1484–1492 is a pure pass-through to the module-level function — dead indirection); move the module-level version into `src/utils/data_recovery.py` as a shared `normalize_scenario_dict()`, and reuse it from `transport_decarbonization_calculator.py`'s `_PotentialView`/`_LCOEView` instead of each processor reimplementing the same dual-shape defense (see §3).
- **`_build_rgba`** (L941–990) → move to `src/utils/map_styling.py` as a `GeoWorldStyler` method (e.g. `build_dominance_overlay_rgba()`). Color/alpha-blending logic for a raster overlay is squarely "map rendering," which is exactly what `map_styling.py` is supposed to centralize per `docs/memory/02-architecture.md`'s "Cross-cutting concerns" table.
- **`_plot_executive_dashboard`, `_build_dashboard_layout`, `_draw_dominance_on_ax`** (L1097–1321, ~225 lines) → move to `src/visualization/dashboard_panels.py`. `DashboardPanels` already holds 5 of the dashboard's 7 panel-drawers (`draw_potential_bars`, `draw_lcoe_distribution`, `draw_supply_curves`, `draw_summary_table`, `draw_abatement_summary` per `SUMMARY.md`); the dominance-map panel and the overall layout/assembly logic are the two panels that were never actually moved when `DashboardPanels` was created, despite `results_writer.py`'s own docstring claiming "Dashboard panels → src.visualization.dashboard_panels" as a completed refactor.
- **`_build_integrated_summary_section`** (L1526–1554) — bespoke fixed-width ASCII table building, duplicating formatting concerns that `src/utils/reporting.py`'s `ReportSection`/`build_phase_report` presumably already generalizes (not confirmed in this pass — reporting.py wasn't read in full; worth checking whether this hand-rolled table is avoidable before deciding where it should live).

---

## 2. `src/processors/sensitivity_analyzer.py` (2150 LOC, Phase 8) — structural scan only

### Current state

Three unrelated concerns share one file:

1. **L108–211**: module-level helpers `_topsis_flat`, `_load_criteria_arrays`, `_build_ghg_function_from_abatement` — a flattened/non-spatial re-implementation of TOPSIS logic for fast repeated Monte Carlo evaluation.
2. **L212–691** (~480 lines): the six sensitivity *methods themselves* — `sa1_oat_weight_sensitivity`, `sa2_monte_carlo_weights`, `sa3_threshold_sweep`, `sa4_lcoe_uncertainty`, `sa5_sobol_ghg`, `sa6_potential_sensitivity` — all module-level functions, not class methods. This is the actual statistical content of Phase 8.
3. **L692–1196** (~500 lines): plotting — `_watermark`, `_draw_kpis`, `_fig_sa1_tornado`, `_fig_sa1_heatmap`, `_fig_sa2_cv`, `_fig_sa3_threshold`, `_fig_sa4_lcoe`, `_fig_sa5_sobol`, `_fig_sa6_potential`, `_fig_dashboard`, `_sfmt` — all module-level functions, matplotlib figure construction.
4. **L1197–2150**: the `SensitivityAnalyzer` class — config/param resolution (`_lcoe_params_for_tech`, `_resolve_tech_params`, `_match_tech_from_stem`), disk loading (`_load_suitability_from_disk`, `_load_weights_from_disk`), a single `run()` method spanning **L1524–2041 (~517 lines)**, and `_format_report` (L2042–2150, ~108 lines).

### Should be

`SensitivityAnalyzer` should orchestrate: load inputs → call each SA method → call each plot function → format report. It should not itself contain either the statistical math or the plotting code, mirroring how Phase 7 already separated `ghg_abatement_calculator.py` from `abatement_plots.py`.

### Extract to

- **SA1–SA6 methods (L212–691)** → `src/utils/sensitivity_methods.py`. These are already written as pure module-level functions with no `self` — extraction is close to mechanical. This also follows the same precedent as `ahp.py`/`topsis.py`/`owa.py` being pulled out of `suitability_builder.py`.
- **All `_fig_*`/`_watermark`/`_draw_kpis`/`_sfmt` plotting functions (L692–1196)** → `src/utils/sensitivity_plots.py`, mirroring `abatement_plots.py` exactly (same module-level-function style, same "takes a styler + data, no global state" pattern already used there).
- **`_topsis_flat`/`_load_criteria_arrays` (L108–162)** → co-locate with or call into `src/utils/topsis.py` rather than living as a silent parallel implementation; at minimum cross-reference in both docstrings if a genuinely different (non-spatial, faster) implementation is intentionally needed for Monte Carlo repetition.
- **`run()` (L1524–2041, ~517 lines)** → split into six `_run_sa1()`…`_run_sa6()` private orchestration methods, each doing input assembly + calling the (now-extracted) `sa*_*()` function + calling the plot function + collecting results, with `run()` reduced to a sequencer over the toggled sub-analyses (`settings.yaml`'s `run_sa1`…`run_sa6`).

---

## 3. `src/processors/transport_decarbonization_calculator.py` (2237 LOC, Phase 9) — structural scan only, largest module in the codebase

### Current state

- **L77–166**: module-level trajectory-interpolation helpers (`_safe_anchors`, `_interpolate_trajectory`, `_interpolate_powertrain_shares`) — generic time-series math, not transport-specific.
- **L167–303**: `_PotentialView` and `_LCOEView` — two adapter classes that wrap `potential_results`/`lcoe_results` dicts to tolerate both live-run and disk-recovered shapes. **Same defensive-normalization pattern as `results_writer._get_scenario_data`** (§1a) — a third independent implementation of "figure out which dict shape I got" logic would be a fourth if `sensitivity_analyzer.py` also does this (unconfirmed, not checked in this pass).
- **L304–2237**: `TransportDecarbonizationCalculator` class — `run()` (L362–623, ~261 lines), fleet/emissions/cost modeling (`_build_fleet_trajectory`, `_build_timeseries`, `_compute_emissions`, `_compute_costs`, L624–1082), **`_place_charging_hubs` (L1083–1352, ~269 lines)** — spatial siting logic, conceptually distinct from the time-series fleet/emissions/cost modeling around it, `_build_summary`/`_log_parameter_dashboard` (L1353–1527), four `_plot_*` methods (L1528–2005, ~477 lines total), and **`_format_report` (L2006–2236, ~230 lines)**.

### Should be

Same target shape as Phase 7/8: a processor that orchestrates fleet/emissions/cost modeling and delegates siting, plotting, and report formatting to separate modules.

### Extract to

- **Four `_plot_*` methods (L1528–2005, ~477 lines)** → `src/utils/transport_plots.py`, mirroring the `abatement_plots.py` precedent.
- **`_place_charging_hubs` (L1083–1352, ~269 lines)** → its own module, e.g. `src/utils/hub_siting.py` or `src/processors/transport_hub_siting.py` — this is GIS/spatial-suitability siting logic (per `03-pipeline.md`, threshold-based on suitability rasters), a different kind of computation from the time-series fleet-trajectory/emissions/cost math that dominates the rest of the class.
- **`_format_report` (L2006–2236, ~230 lines)** → verify whether this already calls `src/utils/reporting.py`'s `build_phase_report`/`ReportSection` (unconfirmed — body not read in this pass). If it hand-rolls its own report formatting instead, that's a second concrete instance (alongside `results_writer._build_integrated_summary_section`) of the centralized-reporting convention (`docs/memory/09-decisions.md`, decision D5) not actually being followed everywhere it claims to be, and 230 lines of report-building should collapse close to `lcoe_calculator._format_report`'s ~71 lines once it does.
- **Trajectory interpolation helpers (L77–166)** → `src/utils/` (new `trajectory.py`, or fold into `src/utils/utils.py`) — generic enough to be reusable, not transport-specific.
- **`_PotentialView`/`_LCOEView` (L167–303)** → consolidate with `results_writer._get_scenario_data` into one shared adapter in `src/utils/data_recovery.py` (see §1a's "Extract to").

---

## 4. `src/utils/abatement_plots.py` (1932 LOC) — structural scan only

### Current state

Five module-level functions, each building one complete multi-panel figure: `_build_thermal_geodf` (L54–119, a shared data-prep helper), `plot_geography` (L120–612, ~492 lines), `plot_macc_curve` (L613–986, ~373 lines), `plot_substitution` (L987–1210, ~223 lines), `plot_carbon_intensity` (L1211–1457, ~246 lines), `plot_net_zero` (L1458–~1932, ~474 lines).

### Should be

Unlike the other flagged modules, this one **is not mixing unrelated responsibilities** — every function is "build one report figure," which matches the module's stated purpose exactly. The size comes from each individual figure being genuinely complex (multi-axis composite plots), not from scope creep. This is a materially different kind of "large" than `results_writer.py` or `transport_decarbonization_calculator.py`.

### Extract to

No responsibility-based split is warranted. If this module needs to be smaller purely for navigability, the only defensible split is mechanical: one file per figure function under a `src/utils/abatement_plots/` subpackage (`geography.py`, `macc_curve.py`, `substitution.py`, `carbon_intensity.py`, `net_zero.py`, plus a `_shared.py` for `_build_thermal_geodf`) — but this is a lower-priority cleanup than the other modules in this document, since there's no misplaced logic to relocate, only line count.

---

## 5. `src/utils/map_styling.py` (1498 LOC) — structural scan only

### Current state

One class, `GeoWorldStyler`, with ~19 methods covering five sub-concerns that are all currently flat in one class:

1. Figure/axes lifecycle: `create_figure` (L237–308), `_ax_bounds` (L197–236), `axes_center_x` (L121–141), `save` (L1481–end), `save_to_buffer` (L788–822)
2. Basemap/geography: `draw_basemap` (L309–365), `draw_admin_labels` (L366–449), `load_admin_boundaries` (L450–542)
3. Decorations (map chrome, no data dependency): `_draw_compass_rose` (L823–898), `_draw_segmented_scalebar` (L899–1010), `add_decorations` (L1011–1104), `add_standard_title` (L543–597), `add_standard_footer` (L598–692), `add_standard_legend` (L693–744), `add_stats_strip` (L745–787)
4. Colormap: `make_cmap` (L142–196), `add_colorbar` (L1105–1149)
5. High-level composite entry points: `render_raster_map` (L1150–1333, ~183 lines, the single most-used public API per `SUMMARY.md`), `create_comparison_via_pil` (L1334–1480, ~146 lines — PIL-based multi-panel compositing, architecturally distinct from the rest of the matplotlib-based class)

### Should be

The class itself is legitimately cohesive (one concern: "how GeoWorld maps look"), so this is a lower-severity flag than the processor modules above — but two sub-concerns are large and self-contained enough to be worth separating out.

### Extract to

- **`create_comparison_via_pil` (L1334–1480, ~146 lines)** → `src/utils/map_composite.py`. It uses PIL image compositing rather than matplotlib, which is a genuinely different rendering technology from the rest of the class — the cleanest seam in this file.
- **`_draw_compass_rose`/`_draw_segmented_scalebar` (L823–1010, ~187 lines)** → `src/utils/map_decorations.py` as standalone functions taking `ax` (they don't appear to need instance state beyond styling config, which can be passed as parameters) — would shrink `GeoWorldStyler` to figure lifecycle + basemap + colormap + the two high-level entry points.

---

## 6. `src/processors/lcoe_calculator.py` (1523 LOC, Phase 5) — structural scan only

### Current state

- **L144–610 (~466 lines)**: `__init__` plus a cluster of input-discovery/loading methods — `_load_tech_params`, `_load_pot_params`, `_find_potential_suitable_tif`, `_load_potential_suitable_mask`, `_find_suitability_tif`, `_find_resource_tif`, `_load_resource_arr`, `_resolve_base_cf`, `_resolve_threshold`. This is file-discovery-by-naming-convention logic — "try these candidate filenames, glob as fallback" — structurally identical in *purpose* to `results_writer.py`'s own `_find_suitability_tif`/`_find_lcoe_tif`/`_find_suitable_tif` (§1).
- **L611–925**: `run()` (L611–824, ~213 lines) and `_run_technology()` (L825–925) — orchestration.
- **L926–1146 (~220 lines)**: `_load_input_rasters`, `_prepare_financials`, `_prepare_modulation_source`, `_compute_cf_and_lcoe` — the actual scientific computation (capacity factor + LCOE per pixel via `src/utils/economics.py`). This is correctly scoped to Phase 5.
- **L1147–1230**: `_export_technology_outputs` — I/O.
- **L1231–1451 (~220 lines)**: `_plot_lcoe_map`, `_plot_supply_curve`, `_plot_comparison` — plotting. Not confirmed in this pass whether these are thin wrappers around `GeoWorldStyler.render_raster_map()` (compliant with the documented convention) or bespoke matplotlib code.
- **L1452–1523**: `_format_report` — small, uses `reporting.py` per `SUMMARY.md`'s dependency list; no concern.

### Should be

Phase 5 should focus on the CF/LCOE computation; file discovery should not be reinvented per phase.

### Extract to

- **The "find the upstream TIF by trying naming-convention variants" cluster** (`_find_potential_suitable_tif`, `_find_suitability_tif`, `_find_resource_tif`, and their `results_writer.py` counterparts) → consolidate into `src/utils/raster_io.py` (which already holds `load_reference_meta`/`load_all_criteria`/`load_aux_raster` — a natural sibling) as a shared `find_phase_output_tif(base_dir, tech, country_code, patterns)` used by both `lcoe_calculator.py` and `results_writer.py` (and likely `sensitivity_analyzer.py`/`data_recovery.py`, which load prior-phase TIFs too — not confirmed in this pass). This is the clearest concrete duplication-across-phases in the whole codebase found during this analysis.
- **`_plot_lcoe_map`/`_plot_supply_curve`/`_plot_comparison` (L1231–1451)** → verify against `GeoWorldStyler.render_raster_map()` first; only worth a dedicated extraction if they turn out to contain bespoke rendering logic rather than thin calls into the styler.

---

## 7. `src/io/data_fetcher.py` (1474 LOC, Phase 0) — structural scan only

### Current state

Organized by dataset, which is basically the right shape already — the size comes from five independent, self-contained download pipelines sharing one file plus a small generic-infra cluster:

- **L59–145**: generic infra — `_check_endpoint_reachable`, `_check_dns_resolution`, `_tile_bbox`.
- **L155–307**: `DataFetcher.__init__`, `_resolve_dem_resolution`, `_request_with_retry`, `_get_with_retry`, `_safe_extract` — generic HTTP/retry/extraction infra, dataset-agnostic.
- **L308–430**: `download_gadm`, `_download_naturalearth_fallback`.
- **L431–690 (~260 lines)**: `download_land_cover`.
- **L691–1147 (~457 lines, the largest single cluster)**: `download_elevation`, `_download_elevation_copernicus`, `_download_elevation_opentopo`, `_mosaic_and_save` — DEM acquisition.
- **L1148–1205**: `download_worldpop`.
- **L1206–1474 (~268 lines)**: `_parse_osm_elements`, `_overpass_query`, `_download_osm_features`, `download_osm_grid`, `download_osm_roads` — OSM infrastructure acquisition.

### Should be

Each dataset's acquisition logic is already independent of the others (confirmed by the clean line-range separation above) — this is a "just too many datasets in one file" problem, not a responsibility-mixing one, similar in kind to `abatement_plots.py` (§4) rather than to `results_writer.py`/`transport_decarbonization_calculator.py`.

### Extract to

- **Generic HTTP/retry/DNS/tiling infra** (L59–145, L172–307) → `src/io/http_utils.py`.
- **Elevation/DEM acquisition** (L691–1147, ~457 lines, the biggest cluster) → `src/io/elevation_fetcher.py`.
- **OSM infrastructure acquisition** (L1206–1474, ~268 lines) → `src/io/osm_fetcher.py`.
- GADM, land cover, and WorldPop downloads could stay in a slimmer `data_fetcher.py`, or follow the same per-dataset-file pattern if consistency is preferred over minimizing file count.

---

## Cross-cutting observations

1. **The "defensive dict-shape adapter" pattern is duplicated at least twice** — `results_writer._get_scenario_data` (§1a) and `transport_decarbonization_calculator._PotentialView`/`_LCOEView` (§3) both exist solely to tolerate the fact that `potential_results`/`lcoe_results` can arrive in more than one shape (live run vs. disk-recovered). This should be solved once, in `src/utils/data_recovery.py` or as a `schemas.py` model method, not per-consumer.
2. **The `abatement_plots.py` extraction pattern (Phase 7) was not applied consistently** — Phase 8 (`sensitivity_analyzer.py`) and Phase 9 (`transport_decarbonization_calculator.py`) both still have their plotting code inline in the processor, despite the same "extract plots to a sibling `src/utils/*_plots.py` module" precedent already existing and being documented as a deliberate v2.0 decision (`docs/memory/09-decisions.md`, D5).
3. **The `DashboardPanels` extraction (Phase 6) was only partially completed** — the two largest/most complex panels (dominance map, overall layout/assembly) were left behind in `results_writer.py` despite the module's own docstring claiming the extraction as done.
4. **"Find the upstream TIF by naming convention" is reimplemented per phase** (`results_writer.py`, `lcoe_calculator.py`, and likely others not read in this pass) instead of living once in `src/utils/raster_io.py`.
5. No test suite exists (`docs/memory/06-risk-areas.md`) — any of the extractions proposed here should be done incrementally and verified by manually diffing output rasters/reports before/after, since there is no automated regression net.
