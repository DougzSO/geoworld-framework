# 09 — Technical Decisions

Format: **Context**, **Decision**, **Consequences**, **Related files**, **Status** (`Active` | `Legacy` | `In migration` | `Uncertain`). Entries marked "inferred" were deduced from project structure/docstrings, not stated as an explicit standalone design document — none was found in the repository.

---

## D1 — Strict separation of operational config (`settings.yaml`) from scientific config (`parameters.json`)

**Context.** Earlier versions apparently kept LCOE financial parameters (CAPEX, OPEX, discount rate) inside `settings.yaml` alongside infrastructure settings, per the explicit callout in `README.md` §6.1 ("`settings.yaml` no longer contains LCOE financial parameters... Any legacy `lcoe` block in `settings.yaml` is ignored").
**Decision.** All scientific/technology parameters live exclusively in `parameters.json`; `settings.yaml` governs only infrastructure, paths, resolutions, visualization, and phase skip flags.
**Consequences.** A researcher changing a scientific assumption (e.g. a capacity factor) only ever needs to touch one file and cannot accidentally leave a stale value in the "wrong" config. The tradeoff: this separation is enforced only by convention/documentation, not a runtime check that rejects scientific keys if they reappear in `settings.yaml` — see `06-risk-areas.md`.
**Related files.** `configs/settings.yaml`, `configs/parameters.json`, `src/core/config_loader.py`, `src/core/constants.py`.
**Status.** Active. (Inferred from docstrings and README; no standalone ADR document exists.)

---

## D2 — Pydantic v2 schemas (`schemas.py`) replace a legacy `models.py`

**Context.** `schemas.py`'s own docstring states it is the "Autoridade única de modelos de dados — substitui models.py, que foi removido" (single authority for data models — replaces models.py, which was removed).
**Decision.** All phase input/output/config contracts consolidated into one Pydantic v2 module.
**Consequences.** Type safety and validation at persistence boundaries; a single place to look up any contract. However, `pipeline_orchestrator.py`'s own docstring still says "AlignedLayers is imported from schemas, not models" (present tense, defensive phrasing) and `src/core/validators.py` repeats the same note — suggesting the migration left residual references worth double-checking whenever `models.py` is mentioned anywhere in code or comments.
**Related files.** `src/core/schemas.py`, `src/core/validators.py`, `src/core/pipeline_orchestrator.py`.
**Status.** Active. `models.py` itself does not exist in the current tree (confirmed) — only its removal is referenced.

---

## D3 — MCDA math (AHP/TOPSIS/OWA/exclusion) extracted from `suitability_builder.py` into `src/utils/`

**Context.** `suitability_builder.py`'s docstring notes it was "refactored: orchestration only" as of v2.0.
**Decision.** AHP, TOPSIS, OWA, and hard-exclusion logic each became a standalone, technology-agnostic module in `src/utils/`, callable independently of the suitability phase.
**Consequences.** These primitives are reusable for any future MCDA application in the codebase (stated goal in README §4), and independently testable/reviewable — though no tests currently exist (`06-risk-areas.md`). `grid_aligner.py` (Phase 2a) already reuses `src/utils/ahp.py` for multi-height wind resource aggregation, outside the suitability phase.
**Related files.** `src/utils/{ahp,topsis,owa,exclusion}.py`, `src/processors/suitability_builder.py`, `src/processors/grid_aligner.py`.
**Status.** Active.

---

## D4 — TOPSIS as primary suitability surface; OWA as secondary/scenario surface

**Context.** Phase 3 produces both; Phase 4 needs exactly one apt-pixel mask per run.
**Decision.** TOPSIS output is the default input to Phase 4 (`potential_calculator.py`). OWA outputs exist per scenario but selecting them (`use_owa=True`) is implemented and explicitly reserved for future use, not wired into the orchestrator.
**Consequences.** Current pipeline runs only reflect the TOPSIS-based apt-pixel definition end-to-end; OWA scenario outputs on disk are informational/comparative only unless someone wires the flag through. Anyone changing this should update this decision entry and `03-pipeline.md`.
**Related files.** `src/processors/potential_calculator.py`, `src/processors/suitability_builder.py`.
**Status.** Active (TOPSIS path); the OWA-driven alternative is **In migration** / not yet activated.

---

## D5 — Centralized map styling and text reporting instead of per-phase implementations

**Context.** README §"Architecture Highlights" and multiple module docstrings (`results_writer.py`, `reporting.py`) describe eliminating duplicated `_plot_*` and text-formatting logic that previously existed independently across phases.
**Decision.** All raster maps render through `GeoWorldStyler.render_raster_map()` (`src/utils/map_styling.py`); all phase text reports build through `build_phase_report()` (`src/utils/reporting.py`).
**Consequences.** Visual and textual consistency across all nine phases' outputs — important for a document meant to read as one coherent thesis/publication figure set. New phases must use these rather than writing bespoke plotting/formatting code.
**Related files.** `src/utils/map_styling.py`, `src/utils/reporting.py`, all `src/processors/*.py`.
**Status.** Active.

---

## D6 — GHG abatement scope limited to the electricity generation sector

**Context.** Explicitly stated in `ghg_abatement_calculator.py` (`SCOPE: ELECTRICITY TRANSITION`) and `abatement_plots.py` docstrings.
**Decision.** All Phase 7 figures and calculations model electricity-sector substitution only; any "total national CO₂" figure shown for context includes all sectors and is clearly a different, larger denominator.
**Consequences.** Prevents the common analytical error of implying economy-wide decarbonization from an electricity-only substitution model. Anyone extending Phase 7 to other sectors (transport is instead handled separately in Phase 9) needs a new, explicitly-scoped module rather than expanding this one's scope silently.
**Related files.** `src/processors/ghg_abatement_calculator.py`, `src/utils/abatement_plots.py`, `configs/net_zero_db.json`.
**Status.** Active.

---

## D7 — No automated test suite

**Context.** No `tests/` directory, no `pytest`/`unittest` files found anywhere in the repository at documentation time.
**Decision (inferred, not stated).** Correctness is currently verified manually/visually (inspecting output maps, reports, and comparing to published benchmarks like IRENA LCOE figures) rather than through an automated suite.
**Consequences.** Refactors and dependency bumps carry higher regression risk, especially in numerically sensitive modules (AHP/TOPSIS math, LCOE formulas, GHG substitution logic). See `06-risk-areas.md`.
**Related files.** N/A (absence of files).
**Status.** Uncertain — it is not documented whether this is a deliberate choice for a single-researcher pipeline or simply not yet done.
