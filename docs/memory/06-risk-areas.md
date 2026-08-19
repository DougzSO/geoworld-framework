---
id: mem-06-risk-areas
type: reference
status: active
created: 2026-08-17
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [mem-05-environment, mem-07-configuration]
linked_by: [mem-readme, mem-08-conventions, mem-10-scripts-and-commands, mem-11-onboarding]
scope: "Áreas frágeis/não testadas/hardcoded (dono do fato 'estado da suite de testes'); não é a lista de tarefas para corrigi-las (ver TASKS.md)."
---

# 06 — Risk Areas

Fragile, untested, hardcoded, or hard-to-reproduce points identified during a light exploration pass. Not exhaustive — extend this file as more is learned.

## Test coverage exists but is uneven

`tests/unit/` exists with `pytest` (82 tests as of 2026-08-18 — `test_ahp.py`, `test_topsis.py`, `test_owa.py`, `test_economics.py`, `test_sensitivity_plots.py`, `test_transport_plots.py`, `test_results_writer.py`, `test_data_auditor.py`). See `../DECISIONS.md` D7 for the historical decision entry (superseded by this fact).

Real gaps remain, though: `src/utils/exclusion.py` and `src/utils/normalization.py` — both named in QI-001's original deliverable list — have zero test coverage (confirmed by grep across `tests/`, no `test_exclusion.py`/`test_normalization.py` exist; tracked as `QI-001 (gap)` in `../TASKS.md`). No integration test exercises the full nine-phase pipeline end-to-end (`QI-002` in `../TASKS.md`, not started). A change to either untested module still has no automated guard against silently breaking a downstream phase.

## Missing `requirements.txt` and `LICENSE` at repo root (partially addressed)

- `requirements.txt` did not exist despite the README's Quick Start requiring it — a fresh clone could not `pip install -r requirements.txt`. **Addressed in this pass**: generated from the local `.venv`'s `pip freeze` and committed. It should be re-verified (or curated down) the next time dependencies change, since it currently includes the full environment snapshot rather than a hand-curated direct-dependency list (see `05-environment.md`).
- `LICENSE` file does not exist, though `README.md`, `CITATION.cff`, and `zenodo.json` all claim MIT. Anyone reusing this code under the stated MIT terms currently has to take that on faith from the metadata files rather than a license file. Adding a `LICENSE` file is a licensing decision for the author to make explicitly, not something to infer/generate silently — tracked as `DOC-008` in `../TASKS.md`.

## `settings.yaml` config-separation rule is convention-only

The strict rule that `settings.yaml` must never carry scientific/technology parameters (see `07-configuration.md`, decision `D1`) is enforced only by documentation and by `config_loader.py` simply not reading those keys from `settings.yaml` — there is no schema validation that would reject or warn if someone reintroduces a scientific key there. A silently-ignored stale `lcoe:` block re-added to `settings.yaml` would fail quietly rather than erroring.

## Pipeline skip-flag state

Dono deste fato → [`07-configuration.md`](07-configuration.md) (seção `settings.yaml` — key sections). Resumo: Transport/Phase 9 fica desligado por um `AttributeError` conhecido (ver seção abaixo), não por escolha intencional.

## Manual, non-automatable data dependency (WDPA)

Protected Planet provides no bulk API; each new country requires a manual shapefile download and placement under `raw/protected_areas/{country}/shp_0/` before the pipeline can run for that country. This is a hard reproducibility gap that cannot be scripted away without a different upstream data source.

## Sensitivity-analysis reproducibility

Seed fixo (42) para os 3 componentes Monte Carlo da Fase 8 — dono deste fato → [`05-environment.md`](05-environment.md) (seção "Reproducibility notes"). Efeito prático: resultados de Fase 8 são reprodutíveis run-to-run hoje.

## Large, single-responsibility-strained modules

Several processor modules exceed 1,500–2,200 lines (`sensitivity_analyzer.py` 2150, `transport_decarbonization_calculator.py` 2237, `abatement_plots.py` 1932, `map_styling.py` 1498, `results_writer.py` 1553, `lcoe_calculator.py` 1523, `data_fetcher.py` 1474). These are candidates for further extraction if they need frequent modification — not urgent, but worth knowing before doing a "quick fix" deep inside one of them.

## `src/visualization/` missing `__init__.py`

Every other `src/*` package (`core`, `io`, `processors`, `utils`) has an `__init__.py`, even if empty; `src/visualization/` does not. Unconfirmed whether this is intentional (implicit namespace package) or an oversight — see the ⚠️ note in `02-architecture.md`, tracked as `DOC-002` in `../TASKS.md`.

## `CITATION.cff` version drift

`CITATION.cff` states `version: 1.0.0` / `date-released: 2024-01-15`, while `README.md` and `zenodo.json` describe `v2.0.0`. If this project is cited by external readers going forward, the citation metadata may point to a stale version — see the ⚠️ note in `01-overview.md`, tracked as `DOC-001` in `../TASKS.md`.

## Messy early git history around the transport module

Early commits deleted `src/processors/transport_decarbonization_calculator.py` and `configs/transport_parameters.json` (`dda9a36`, `b7ff617`) and a README section describing them (`85a49cb`), but both files exist in the current tree, apparently reintroduced by the large `52a9a0f "feat: add main.py framework implementation"` commit. Not a functional risk (the files are present and referenced consistently in the current code), but worth knowing if `git blame`/`git log -p` on that area looks confusing.

## Credentials and large binary outputs

`.env` (credentials) and `*.tif`/`outputs/**` (large binaries) are correctly gitignored — verified, not a current risk, but worth stating so a future session doesn't accidentally `git add -A` and commit an 18 GB working tree or leak Terrascope credentials.
