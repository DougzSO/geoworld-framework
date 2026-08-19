---
id: mem-01-overview
type: reference
status: active
created: 2026-08-06
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: []
linked_by: [mem-readme, mem-11-onboarding]
scope: "O que é o GeoWorld Framework, contexto acadêmico e escopo; não descreve arquitetura de código nem decisões de design (ver 02-architecture.md e DECISIONS.md)."
---

# 01 — Overview

## What it is

GeoWorld Framework is an open-source Python pipeline for spatially explicit assessment of renewable energy potential (Solar PV, Onshore Wind, Biomass/Bioenergy). Given a country identifier, it runs nine sequential phases — from raw geospatial data acquisition through GIS-MCDA suitability scoring, technical/economic potential, LCOE, GHG-abatement modeling, sensitivity analysis, to transport-decarbonization scenarios — producing georeferenced outputs (GeoTIFF, PNG maps, CSV/JSON reports) intended for peer-reviewed publication and doctoral thesis chapters.

## Academic context

- Author: Douglas Silva de Oliveira, Federal University of Minas Gerais (UFMG) — ORCID `0009-0003-6857-0122` (from `CITATION.cff` / `zenodo.json`).
- Positioned in the codebase's own words as doctoral research (`DOUTORADO` in the parent directory path).
- Registered on Zenodo as citable software (DOI `10.5281/zenodo.20184266`, v2.0.0 per `README.md`; `CITATION.cff` currently lists `version: 1.0.0` — see ⚠️ note below).
- MIT licensed per `CITATION.cff` and `zenodo.json`, though no `LICENSE` file exists in the repository.

> ⚠️ Point to validate (tracked as `DOC-001` in `../TASKS.md`): `CITATION.cff` (`version: 1.0.0`, `date-released: 2024-01-15`) is out of sync with `README.md` (`Version 2.0`, DOI for `v2.0.0`). Unclear whether `CITATION.cff` was simply never bumped, or whether it intentionally still points at the first citable release.

## Scope

- **In scope**: solar, wind, and biomass siting suitability; installable capacity and generation; LCOE; electricity-sector GHG abatement via thermal substitution; six sensitivity sub-analyses; transport decarbonization (EV/hydrogen) scenarios.
- **Currently configured countries** (in `configs/parameters.json`): Portugal (PRT), Brazil (BRA), Egypt (EGY), China (CHN), Russia (RUS), India (IND), South Africa (ZAF). `configs/net_zero_db.json` carries a broader reference set (adds e.g. Australia, Germany, Spain, France, UK) used for GHG-abatement national baselines.
- **Out of scope / explicitly noted in code**: GHG-abatement figures cover the electricity generation sector only, not economy-wide emissions (see `src/processors/ghg_abatement_calculator.py` and `src/utils/abatement_plots.py` docstrings).

## Why the architecture looks the way it does

The `README.md` documents a v2.0 refactor with these stated goals (see `docs/memory/09-decisions.md` for the decision-log form of each):

- Type-safe Pydantic v2 contracts replacing an earlier `models.py`.
- One unified `PipelineOrchestrator` for phase skip/cache logic instead of per-phase ad hoc logic.
- MCDA math (AHP/TOPSIS/OWA) extracted to `src/utils/` for reuse outside the suitability phase.
- Centralized text reporting (`src/utils/reporting.py`) and map styling (`src/utils/map_styling.py`) to remove duplicated formatting/plotting code across phases.
- Strict separation of *operational* config (`settings.yaml`) from *scientific* config (`parameters.json`).

See [`02-architecture.md`](02-architecture.md) for the resulting package layout and [`03-pipeline.md`](03-pipeline.md) for how it executes.
