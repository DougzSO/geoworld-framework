---
id: mem-11-onboarding
type: index
status: active
created: 2026-08-06
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [docs-readme, docs-tasks]
linked_by: [mem-readme]
scope: "Checklist condensado de onboarding para nova sessão de IA; não substitui a ordem de leitura completa (ver docs/README.md) nem repete fatos já donos de outros arquivos."
---

# 11 — Onboarding Guide for Future AI Sessions

Read this after following the reading order in [`../README.md`](../README.md) (the one canonical reading order for all of `docs/`) — this file is a condensed checklist, not a substitute for it.

## What you're working on, in one sentence

A Python geospatial research pipeline (doctoral work, UFMG) that scores renewable-energy siting suitability and downstream economics/emissions per country, through nine cache-aware, individually-skippable phases.

## Before touching anything

1. Read [`../README.md`](../README.md) and follow its order.
2. Check `../TASKS.md` — is this already a tracked item? If not, it will need one once you're done (see `../CLAUDE-DOCS-RULES.md`).
3. Identify which phase(s) your task touches using the table in `03-pipeline.md` and the file inventory in `/SUMMARY.md`.
4. Check `06-risk-areas.md` for known fragility in that area before changing it.
5. If the task involves a scientific parameter, confirm you're editing `configs/parameters.json`, not `configs/settings.yaml` — see `07-configuration.md`.

## Fast orientation checks (cheap, do these first)

- `git log --oneline -20` — see recent activity and match commit style (also required by `CLAUDE.md` before any commit).
- Confirm current `pipeline.skip_*` state in `configs/settings.yaml` before assuming a "normal" run — dono deste fato: [`07-configuration.md`](07-configuration.md).
- If reproducing a bug, check `outputs/logs/*.jsonl` for the relevant country/timestamp before re-running the whole pipeline — logs are structured and searchable.

## What NOT to do

- Don't add scientific parameters to `settings.yaml` — they belong in `parameters.json` only (`07-configuration.md`, decision `D1`).
- Don't write a bespoke `_plot_*` function in a processor — use `GeoWorldStyler.render_raster_map()` (`src/utils/map_styling.py`).
- Don't write a bespoke text-report formatter — use `build_phase_report()` (`src/utils/reporting.py`).
- Don't use the stateful `pyplot` interface (`plt.plot`, etc.) in library code — OO `Figure`/`Axes` only, for thread safety (`08-conventions.md`).
- Don't assume every module is covered by the test suite — a suite exists (`pytest tests/`) but coverage is uneven, see [`06-risk-areas.md`](06-risk-areas.md) for what's tested and what isn't. For untested modules, still manually reason through the numerical impact of any change to `src/utils/{ahp,topsis,owa,economics}.py` or any `*_calculator.py`.
- Don't mention Claude/AI authorship anywhere — not in commits, not in code comments, not in documentation. See `CLAUDE.md`'s git and comment rules.

## When you finish a task

- Mark the task done in `../TASKS.md` (move it to the ✅ section) — see `../CLAUDE-DOCS-RULES.md`, mandatory after every task.
- If you made an architectural decision, append (never overwrite) an entry to `../DECISIONS.md`.
- If you changed architecture, a formula, a data structure, a config's meaning, or a dependency: update the relevant `/docs/memory` file(s) **and** this folder's `README.md` index if you added/removed a file. This is a hard requirement, not a suggestion — see `CLAUDE.md`'s "Mandatory documentation update rule."
- If you found something during the task that you couldn't confirm with certainty, add a `> ⚠️ Point to validate:` note in the relevant file rather than guessing silently — and register it as a `DOC-00X` item in `../TASKS.md` so it isn't lost.
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
| Why something was built a certain way | `../DECISIONS.md` |
| What's fragile / untested / hardcoded | `06-risk-areas.md` |
