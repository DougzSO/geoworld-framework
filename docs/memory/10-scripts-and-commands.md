# 10 — Scripts and Commands

There is exactly one entry point in this repository: `main.py`. There is no separate `scripts/` directory, no Makefile, no notebook, no CI pipeline, and no cluster job submission script as of this writing — everything runs locally and manually.

## Setup (one-time)

```bash
# from the project root
pip install -r requirements.txt

# set the raw-data path (~18 GB) — see docs/memory/05-environment.md
echo "GEOWORLD_RAW_DATA=/your/path/to/data" > .env
```

Local execution during this documentation pass used a project-local virtualenv at `.venv/` (Windows: `.venv/Scripts/python.exe`, `.venv/Scripts/pip.exe`). No activation script requirement is enforced by the code — any Python 3.10+ interpreter with the dependencies installed works.

## Running the pipeline

```bash
# single country, by full name or ISO-3166-alpha-3
python main.py Portugal
python main.py PRT

# batch mode, parallel workers
python main.py --batch country_list.txt --workers 4
```

`country_list.txt` format was not directly inspected during this pass — infer from `main.py`'s `argparse` setup (`--batch`) before relying on a specific format assumption.

> ⚠️ Point to validate: confirm the exact expected format of `--batch`'s input file (one country name/ISO3 per line is the reasonable assumption, but wasn't verified against `main.py`'s parsing code in this pass).

## Controlling which phases run

Edit `configs/settings.yaml`'s `pipeline.skip_*` flags (see `07-configuration.md`). Each phase reads cached output from the previous phase if `skip_*` is true and a cache exists; if `skip_*` is true and no cache exists, that phase does not run and produces no output (it is not silently skipped-with-defaults).

**Current state (verified 2026-08-17)**: all `skip_*` flags are `false` except `skip_transport` — a full eight-phase run is the standing default. Transport (Phase 9) stays disabled by a known `AttributeError` (see `06-risk-areas.md`), not by an intentional skip choice.

## Adding a country

See `07-configuration.md`'s "Adding a new country" section — edit `configs/parameters.json`, manually place the WDPA shapefile, then run `python main.py <CountryName>`.

## Where outputs land

`outputs/{ISO3}/{phase_subdir}/` — see the tree in `02-architecture.md`. Logs go to `outputs/logs/geoworld_{ISO3}_{timestamp}_{pid}.{log,jsonl}`; audit text reports go to `outputs/reports/audit_{ISO3}_{timestamp}.txt`. Both `.log` (human-readable) and `.jsonl` (structured, machine-parseable) are written per run by `src/utils/logging_utils.py`.

## Test and lint commands

**Corrected 2026-08-17** — this section previously stated no `pytest` setup existed. `pytest tests/` now runs the unit suite (77 tests as of BLOCKER-011 — see `08-conventions.md`). `ruff check` is used ad hoc during sessions (no committed `ruff.toml`/`pre-commit` config yet, so it's not enforced automatically). There is still no `tox.ini` or CI pipeline.
