# 11 — Onboarding Guide for Future AI Sessions

Read this after [`README.md`](../../README.md) and the rest of `/docs/memory` in the order given in [`README.md`](README.md) of this folder — this file is a condensed checklist, not a substitute for those.

## What you're working on, in one sentence

A Python geospatial research pipeline (doctoral work, UFMG) that scores renewable-energy siting suitability and downstream economics/emissions per country, through nine cache-aware, individually-skippable phases.

## Before touching anything

1. Read `/docs/memory/README.md` and follow its order.
2. Identify which phase(s) your task touches using the table in `03-pipeline.md` and the file inventory in `/SUMMARY.md`.
3. Check `06-risk-areas.md` for known fragility in that area before changing it.
4. If the task involves a scientific parameter, confirm you're editing `configs/parameters.json`, not `configs/settings.yaml` — see `07-configuration.md`.

## Fast orientation checks (cheap, do these first)

- `git log --oneline -20` — see recent activity and match commit style (also required by `CLAUDE.md` before any commit).
- Confirm current `pipeline.skip_*` state in `configs/settings.yaml` before assuming a "normal" run — see the ⚠️ note in `06-risk-areas.md`; it was non-default (only phases 4–6 active) at last documentation time and may have changed since.
- If reproducing a bug, check `outputs/logs/*.jsonl` for the relevant country/timestamp before re-running the whole pipeline — logs are structured and searchable.

## What NOT to do

- Don't add scientific parameters to `settings.yaml` — they belong in `parameters.json` only (`07-configuration.md`, decision `D1`).
- Don't write a bespoke `_plot_*` function in a processor — use `GeoWorldStyler.render_raster_map()` (`src/utils/map_styling.py`).
- Don't write a bespoke text-report formatter — use `build_phase_report()` (`src/utils/reporting.py`).
- Don't use the stateful `pyplot` interface (`plt.plot`, etc.) in library code — OO `Figure`/`Axes` only, for thread safety (`08-conventions.md`).
- Don't assume a test suite exists to catch regressions — none does. Manually reason through the numerical impact of any change to `src/utils/{ahp,topsis,owa,economics}.py` or any `*_calculator.py`.
- Don't mention Claude/AI authorship anywhere — not in commits, not in code comments, not in documentation. See `CLAUDE.md`'s git and comment rules.

## When you finish a task

- If you changed architecture, a formula, a data structure, a config's meaning, or a dependency: update the relevant `/docs/memory` file(s) **and** this folder's `README.md` index if you added/removed a file. This is a hard requirement, not a suggestion — see `CLAUDE.md`'s "Mandatory documentation update rule."
- If you found something during the task that you couldn't confirm with certainty, add a `> ⚠️ Point to validate:` note in the relevant file rather than guessing silently.
- If the task didn't touch anything structural (a narrow fix with no documented-flow impact), documentation updates are not required.

## Quick file-to-topic map

| I need to understand... | Read |
| --- | --- |
| What this project is for | `01-overview.md` |
| How packages relate | `02-architecture.md`, `/SUMMARY.md` |
| The 9-phase execution flow | `03-pipeline.md` |
| AHP/TOPSIS/OWA/LCOE/GHG/sensitivity math | `04-algorithms.md` |
| `settings.yaml` vs `parameters.json` | `07-configuration.md` |
| Dependencies, `.venv`, raw data, reproducibility | `05-environment.md` |
| How to actually run something | `10-scripts-and-commands.md` |
| Code style already in use | `08-conventions.md` |
| Why something was built a certain way | `09-decisions.md` |
| What's fragile / untested / hardcoded | `06-risk-areas.md` |
