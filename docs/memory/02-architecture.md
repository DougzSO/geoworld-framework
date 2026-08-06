# 02 — Architecture

## Package layout

```
geoworld_framework/
├── main.py                    ← CLI entry point / pipeline driver
├── configs/                   ← settings.yaml, parameters.json, net_zero_db.json, transport_parameters.json
├── .env                       ← GEOWORLD_RAW_DATA, Terrascope credentials (not committed)
├── src/
│   ├── core/                  ← config loading, Pydantic contracts, orchestration, validation
│   ├── io/                    ← raw-data discovery, download, artifact persistence
│   ├── processors/            ← the nine pipeline phases (one class each, roughly)
│   ├── utils/                 ← technology-agnostic math/IO/reporting/styling libraries
│   └── visualization/         ← reusable dashboard panel drawers
├── data/
│   ├── raw/                   ← external, ~18 GB, resolved via GEOWORLD_RAW_DATA or symlink
│   └── processed/{ISO3}/      ← Phase 2a aligned rasters (auto-generated, gitignored)
└── outputs/{ISO3}/            ← per-phase results, reports, figures, pipeline_state.json (gitignored, selective)
```

See [`/SUMMARY.md`](../../SUMMARY.md) for the file-by-file inventory (LOC, exact responsibilities, per-module dependency notes).

## Layering / dependency direction

```
main.py
  → src.io          (data discovery + acquisition)
  → src.core         (config, contracts, orchestration)
      → src.processors  (the 9 phases)
          → src.utils        (math, I/O, reporting, styling — technology-agnostic)
          → src.visualization (dashboard panels, built on src.utils.map_styling)
```

`src/utils/` has no dependency on `src/processors/` — it's the reusable layer, by design (see `docs/memory/09-decisions.md`, "MCDA extraction to utils").

`src/core/schemas.py` is the one file nearly everything else depends on: every phase's inputs/outputs are typed Pydantic v2 models defined there.

## Configuration split (architectural, not incidental)

`src/core/config_loader.py` merges two independent config sources with a strict responsibility boundary:

- `configs/settings.yaml` — infrastructure only: paths, spatial resolutions, visualization layout, per-phase `skip_*` flags, scenario threshold/land-use offsets.
- `configs/parameters.json` — **the only source** of scientific/technology parameters (capacity factor, power density, land-use factor, thresholds, LCOE economics, OWA weights) per country.
- `src/core/constants.py` (`DEFAULT_TECH_PARAMS`) — last-resort fallback if a country entry in `parameters.json` is incomplete.

This split is enforced by convention (documented in module docstrings across `lcoe_calculator.py`, `results_writer.py`, etc.), not by a runtime guard — see `docs/memory/06-risk-areas.md`.

## Orchestration contract

`src/core/pipeline_orchestrator.py`'s `PipelineOrchestrator` defines one uniform contract every phase follows:

1. If `skip_*` is set **and** a cached result exists on disk → return the cached result.
2. If `skip_*` is set **and** no cache exists → do not execute; return `None`.
3. If `skip_*` is `False` → execute the phase.
4. Assemble phase inputs via an `input_getter()` callable.
5. Optionally validate aligned layers (phases 2b, 3) via `src/core/validators.py`.
6. Instantiate the phase class and call `.run(**inputs)`.
7. Optionally validate the result against a Pydantic output model.
8. Persist the result via `src/io/artifact_manager.py` and update `outputs/{ISO3}/pipeline_state.json`.

`main.py` wires each of the nine processor classes into the orchestrator in sequence; it does not call processors directly.

## Cross-cutting concerns, each centralized in one module

| Concern | Module |
| --- | --- |
| Logging (console + `.log` + `.jsonl`, per-country/phase context) | `src/utils/logging_utils.py` |
| Map rendering (all phases) | `src/utils/map_styling.py` (`GeoWorldStyler.render_raster_map()`) |
| Text report formatting (5 phases) | `src/utils/reporting.py` (`build_phase_report`) |
| Step timing | `src/utils/timing.py` (`timer()` context manager) |
| Artifact recovery from disk (cache/resume) | `src/utils/data_recovery.py` |
| Financial math (CRF, LCOE, supply curve) | `src/utils/economics.py` |

> ⚠️ Point to validate: `src/visualization/` has no `__init__.py`, unlike every other `src/*` package (`core`, `io`, `processors`, `utils` all have one, even if empty). It still appears importable (only `dashboard_panels.py` is imported directly), but confirm whether this is an intentional namespace-package choice or an oversight before adding a second file to that package.
