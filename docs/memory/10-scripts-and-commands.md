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

**Current state at documentation time**: most phases are skipped except potential/LCOE/results (phases 4–6) — see `06-risk-areas.md`. Reset the relevant `skip_*` flags to `false` for a full nine-phase run.

## Adding a country

See `07-configuration.md`'s "Adding a new country" section — edit `configs/parameters.json`, manually place the WDPA shapefile, then run `python main.py <CountryName>`.

## Where outputs land

`outputs/{ISO3}/{phase_subdir}/` — see the tree in `02-architecture.md`. Logs go to `outputs/logs/geoworld_{ISO3}_{timestamp}_{pid}.{log,jsonl}`; audit text reports go to `outputs/reports/audit_{ISO3}_{timestamp}.txt`. Both `.log` (human-readable) and `.jsonl` (structured, machine-parseable) are written per run by `src/utils/logging_utils.py`.

## No test or lint commands found

There is no `pytest`, `tox.ini`, `.flake8`, `ruff.toml`, or `pre-commit` config in the repository. There is currently nothing to "run before committing" beyond manually exercising the pipeline against a known country and checking outputs.
