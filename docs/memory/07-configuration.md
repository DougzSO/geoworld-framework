---
id: mem-07-configuration
type: reference
status: active
created: 2026-08-17
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: []
linked_by: [mem-readme, mem-06-risk-areas, mem-10-scripts-and-commands, mem-11-onboarding, docs-decisions]
scope: "settings.yaml vs. parameters.json (dono dos fatos 'estado das flags skip_*' e 'como adicionar país'); não é o log da decisão D1 em si (ver DECISIONS.md)."
---

# 07 — Configuration

## Files

| File | Lines | Governs |
| --- | --- | --- |
| `configs/settings.yaml` | 173 | Infrastructure, paths, spatial resolutions, visualization layout, per-phase `skip_*` flags, scenario offsets. **Never** technology/scientific parameters. |
| `configs/parameters.json` | 768 | Per-country scientific parameters (solar/wind/biomass tech params, OWA weights, LCOE economics, abatement defaults, land-suitability class scores). The single source of truth for anything scientific. |
| `configs/net_zero_db.json` | 270 | National net-zero/GHG baseline reference figures (broader country coverage than `parameters.json`), consumed by Phase 7. |
| `configs/transport_parameters.json` | 555 | Global defaults + per-country parameters for Phase 9 (transport decarbonization). |
| `.env` | — | `GEOWORLD_RAW_DATA` path, optional Terrascope credentials. Not committed. |

## Why the settings.yaml / parameters.json split exists

This is the central configuration decision in the codebase (see `09-decisions.md` for the formal decision-log entry). In short: `settings.yaml` used to also hold LCOE financial parameters; the v2.0 refactor moved all scientific/technology parameters exclusively into `parameters.json` so that changing where the pipeline *runs* (paths, resolution, which phases to skip) never risks accidentally changing *what it computes*. `lcoe_calculator.py`'s module docstring states explicitly: "`settings.yaml` NÃO é consultado para parâmetros tecnológicos científicos" (settings.yaml is NOT consulted for scientific technology parameters).

### Parameter precedence (highest → lowest)

1. Country entry in `parameters.json` (`countries.{ISO3}`)
2. `parameters.json`'s `abatement_defaults.default` (abatement only)
3. `settings.yaml`'s `potential.technologies` (documented as a tech fallback layer in `config_loader.py`, though the primary/authoritative source is always #1)
4. `constants.DEFAULT_TECH_PARAMS` in `src/core/constants.py` — last resort, hardcoded

## `settings.yaml` — key sections

- `paths` — `raw_data`, `processed_data`, `outputs`, `transport_params`.
- `geospatial` — CRS (`EPSG:4326`), resolutions per purpose: `land_cover` (~100 m), `suitability` (~1 km), `dem_slope` (~500 m).
- `criteria_defaults` — fallback spatial-processing parameters (road max distance, population density threshold, river buffers) used when a country-specific value is absent. These are process-control values, not scientific ones.
- `potential.scenarios` — per-scenario offsets applied on top of the country's base threshold/land-use factor from `parameters.json` (`optimistic`/`balanced`/`conservative`).
- `visualization` — DPI, figure sizing, color palette per criterion (`criteria_meta`), plant-type colors for context layers.
- `pipeline.skip_*` — one flag per phase (`skip_audit`, `skip_land_cover`, `skip_align`, `skip_criteria`, `skip_suitability`, `skip_potential`, `skip_lcoe`, `skip_results`, `skip_abatement`, `skip_sensitivity`, `skip_transport`).
- `pipeline.sensitivity` — per-sub-analysis toggles (`run_sa1`…`run_sa6`) and `n_mc_samples`.
- `pipeline.transport` — `primary_scenario`, `hub_suitability_threshold`, `run_all_scenarios`.

> **Verified 2026-08-17**: `settings.yaml` now has all `skip_*` flags `false` except `skip_transport: true` (Phase 9 disabled by a known bug, not an intentional skip — see `06-risk-areas.md`). The "only phases 4–6 active" state this note originally described is no longer current — BLOCKER-series sessions since have run all eight active phases end-to-end. Still re-check `settings.yaml` directly before relying on this — it's a runtime setting, not a code guarantee.

## `parameters.json` — structure

Top-level keys: `_meta`, `abatement_defaults`, `countries` (indexed by ISO3), `fallback_logic`, `land_suitability`, `exclusion_thresholds`. Each country entry under `countries` has: `solar`, `wind`, `biomass` (technology blocks — land use factor, threshold, power density, capacity factor, cf_ceiling/floor, plus resource-specific weights), `owa` (three named scenario weight vectors, must sum to 1.0 and be non-increasing), `lcoe` (per-technology CAPEX, OPEX, lifetime, discount rate), `abatement` (carbon price, penetration factor, thermal types, emission factors), and top-level `slope_threshold_deg`, `protected_as_exclusion`, `forest_as_exclusion`, `use_mainland_only`.

`exclusion_thresholds` (added Bloco 1, see `archive/backlog-full-2026-08.md`) is **global**, not per-country: `protected_areas` (0.99) and `proximity_plants` (0.01) are Phase 3 hard-exclusion gate thresholds, and `slope_offset_deg` (`{solar: 5.0, wind: 10.0, biomass: 20.0}`) is added on top of each country's `slope_threshold_deg`. Loaded via `ConfigLoader.exclusion_thresholds` into `CountryParams.protected_areas_threshold`/`.proximity_plants_threshold`/`.slope_offset_{solar,wind,biomass}_deg`. `protected_areas`'s value is pending `Campaign-05` (`IUCN_SCORES`); `proximity_plants`'s has no documented external justification yet (`Campaign-02`) — see `../TASKS.md` before treating either as settled science. `lakes_exclusion` (0.5) stays hardcoded in `suitability_builder.py`, not here — it's mathematically inert for any value in `(0, 1)` (binary criterion), so there is no real parameter to migrate.

## Adding a new country

Per `README.md` §10 (verified structurally consistent with the code, not independently re-derived):

1. Add a `countries.{ISO3}` entry to `parameters.json` with all required sub-keys.
2. Manually download the WDPA shapefile for that country (see `05-environment.md`) to `raw/protected_areas/{country_name}/shp_0/`.
3. Run `python main.py <CountryName>`. No `.py` changes are required.
