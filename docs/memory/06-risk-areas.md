# 06 — Risk Areas

Fragile, untested, hardcoded, or hard-to-reproduce points identified during a light exploration pass. Not exhaustive — extend this file as more is learned.

## Test coverage exists but is uneven

**Corrected 2026-08-17** — this section previously claimed no `tests/` directory existed anywhere in the repo. That's no longer true: `tests/unit/` exists with `pytest` (77 tests as of BLOCKER-011 — `test_ahp.py`, `test_topsis.py`, `test_owa.py`, `test_economics.py`, `test_sensitivity_plots.py`, `test_transport_plots.py`, `test_results_writer.py`). See D7 in `09-decisions.md`'s new location (`docs/BACKLOG.md`'s Appendix) for the same correction on the decision-log side.

Real gaps remain, though: `src/utils/exclusion.py` and `src/utils/normalization.py` — both named in QI-001's original deliverable list (`docs/BACKLOG.md`) — have zero test coverage (confirmed by grep across `tests/`, no `test_exclusion.py`/`test_normalization.py` exist). No integration test exercises the full nine-phase pipeline end-to-end (QI-002, not started). A change to either untested module still has no automated guard against silently breaking a downstream phase.

## Missing `requirements.txt` and `LICENSE` at repo root (partially addressed)

- `requirements.txt` did not exist despite the README's Quick Start requiring it — a fresh clone could not `pip install -r requirements.txt`. **Addressed in this pass**: generated from the local `.venv`'s `pip freeze` and committed. It should be re-verified (or curated down) the next time dependencies change, since it currently includes the full environment snapshot rather than a hand-curated direct-dependency list (see `05-environment.md`).
- `LICENSE` file does not exist, though `README.md`, `CITATION.cff`, and `zenodo.json` all claim MIT. Anyone reusing this code under the stated MIT terms currently has to take that on faith from the metadata files rather than a license file. **Not addressed in this pass** — adding a `LICENSE` file is a licensing decision for the author to make explicitly, not something to infer/generate silently.

## `settings.yaml` config-separation rule is convention-only

The strict rule that `settings.yaml` must never carry scientific/technology parameters (see `07-configuration.md`, decision `D1`) is enforced only by documentation and by `config_loader.py` simply not reading those keys from `settings.yaml` — there is no schema validation that would reject or warn if someone reintroduces a scientific key there. A silently-ignored stale `lcoe:` block re-added to `settings.yaml` would fail quietly rather than erroring.

## Pipeline skip-flag state — corrected

**Corrected 2026-08-17** — this section previously described `settings.yaml` as having most `pipeline.skip_*` flags `true`, with only `skip_potential`/`skip_lcoe`/`skip_results` `false` (i.e., only phases 4–6 active). That was accurate at documentation time but is stale now: `configs/settings.yaml` currently has all `skip_*` flags `false` except `skip_transport: true` — a full eight-phase run is the standing default (Transport/Phase 9 stays off because of the known `AttributeError` two sections below, not by an intentional narrowing to phases 4–6). This is a mutable runtime setting, not a code guarantee — re-check `settings.yaml` directly rather than trusting this note indefinitely.

## Manual, non-automatable data dependency (WDPA)

Protected Planet provides no bulk API; each new country requires a manual shapefile download and placement under `raw/protected_areas/{country}/shp_0/` before the pipeline can run for that country. This is a hard reproducibility gap that cannot be scripted away without a different upstream data source.

## Sensitivity-analysis reproducibility

All three Monte Carlo components of Phase 8 (SA-2 Dirichlet sampling, SA-4 triangular MC, SA-6 Sobol) use a fixed RNG seed: `sensitivity_analyzer.py`'s `sa2_monte_carlo_weights()`, `sa4_lcoe_uncertainty()`, and `sa6_potential_sensitivity()` each default to `seed: int = 42`, applied via `np.random.default_rng(seed)` (with a fallback to global `np.random.seed(seed)` for SA-6 when the installed SALib version's `saltelli.sample`/`sobol.analyze` don't accept a `seed=` kwarg directly). None of the three call sites in `SensitivityAnalyzer.run()` override this default, and the seed is not read from `settings.yaml` — it is a hardcoded literal, not a configurable parameter. Practical effect: Phase 8's Monte Carlo results **are** reproducible run-to-run today.

## Large, single-responsibility-strained modules

Several processor modules exceed 1,500–2,200 lines (`sensitivity_analyzer.py` 2150, `transport_decarbonization_calculator.py` 2237, `abatement_plots.py` 1932, `map_styling.py` 1498, `results_writer.py` 1553, `lcoe_calculator.py` 1523, `data_fetcher.py` 1474). These are candidates for further extraction if they need frequent modification — not urgent, but worth knowing before doing a "quick fix" deep inside one of them.

## `src/visualization/` missing `__init__.py`

Every other `src/*` package (`core`, `io`, `processors`, `utils`) has an `__init__.py`, even if empty; `src/visualization/` does not. Unconfirmed whether this is intentional (implicit namespace package) or an oversight — see the ⚠️ note in `02-architecture.md`.

## `CITATION.cff` version drift

`CITATION.cff` states `version: 1.0.0` / `date-released: 2024-01-15`, while `README.md` and `zenodo.json` describe `v2.0.0`. If this project is cited by external readers going forward, the citation metadata may point to a stale version — see the ⚠️ note in `01-overview.md`.

## Messy early git history around the transport module

Early commits deleted `src/processors/transport_decarbonization_calculator.py` and `configs/transport_parameters.json` (`dda9a36`, `b7ff617`) and a README section describing them (`85a49cb`), but both files exist in the current tree, apparently reintroduced by the large `52a9a0f "feat: add main.py framework implementation"` commit. Not a functional risk (the files are present and referenced consistently in the current code), but worth knowing if `git blame`/`git log -p` on that area looks confusing.

## Credentials and large binary outputs

`.env` (credentials) and `*.tif`/`outputs/**` (large binaries) are correctly gitignored — verified, not a current risk, but worth stating so a future session doesn't accidentally `git add -A` and commit an 18 GB working tree or leak Terrascope credentials.
