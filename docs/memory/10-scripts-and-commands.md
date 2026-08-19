---
id: mem-10-scripts-and-commands
type: reference
status: active
created: 2026-08-17
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [mem-07-configuration]
linked_by: [mem-readme, mem-11-onboarding]
scope: "Como rodar o pipeline (setup, comandos, onde ficam outputs); não é dono do estado das flags skip_* nem da contagem de testes (ver 07-configuration.md/06-risk-areas.md)."
---

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

> ⚠️ Point to validate (tracked as `DOC-005` in `../TASKS.md`): confirm the exact expected format of `--batch`'s input file (one country name/ISO3 per line is the reasonable assumption, but wasn't verified against `main.py`'s parsing code in this pass).

## Controlling which phases run

Edit `configs/settings.yaml`'s `pipeline.skip_*` flags. Each phase reads cached output from the previous phase if `skip_*` is true and a cache exists; if `skip_*` is true and no cache exists, that phase does not run and produces no output (it is not silently skipped-with-defaults).

Estado atual das flags → dono deste fato: [`07-configuration.md`](07-configuration.md).

## Adding a country

See `07-configuration.md`'s "Adding a new country" section — edit `configs/parameters.json`, manually place the WDPA shapefile, then run `python main.py <CountryName>`.

## Where outputs land

`outputs/{ISO3}/{phase_subdir}/` — see the tree in `02-architecture.md`. Logs go to `outputs/logs/geoworld_{ISO3}_{timestamp}_{pid}.{log,jsonl}`; audit text reports go to `outputs/reports/audit_{ISO3}_{timestamp}.txt`. Both `.log` (human-readable) and `.jsonl` (structured, machine-parseable) are written per run by `src/utils/logging_utils.py`.

## Test and lint commands

`pytest tests/` runs the unit suite — coverage/count → dono deste fato: [`06-risk-areas.md`](06-risk-areas.md). `ruff check` is used ad hoc during sessions (no committed `ruff.toml`/`pre-commit` config yet, so it's not enforced automatically). There is still no `tox.ini` or CI pipeline.
