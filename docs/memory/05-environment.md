---
id: mem-05-environment
type: reference
status: active
created: 2026-08-06
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: []
linked_by: [mem-readme, mem-06-risk-areas]
scope: "Runtime, dependências, dados brutos e reprodutibilidade (dono do fato 'seed=42'); não cobre estado de flags de pipeline (ver 07-configuration.md)."
---

# 05 — External Dependencies and Execution Environment

## Runtime

- Python **3.10+** per `README.md`; the local `.venv` observed during this documentation pass runs **Python 3.14.3** — both should work per the stated floor, but the gap hasn't been tested by this session.
- No containerization (no `Dockerfile`), no CI config (no `.github/workflows/`), no cluster/HPC job scripts found. This appears to be a **local, manually-executed** research pipeline.

> ⚠️ Point to validate (tracked as `DOC-003` in `../TASKS.md`): confirm there truly is no CI/cluster execution path elsewhere (e.g. outside this repo, or not yet committed) — a doctoral pipeline processing ~18 GB of raster data per run may benefit from one, but none exists in-repo as of this writing.

## Dependency management

- `requirements.txt` did not exist in the repository at the start of this documentation pass despite `README.md`'s Quick Start instructing `pip install -r requirements.txt`. It has been generated from the local `.venv` (`python -m pip freeze`) and committed as a snapshot — see root [`requirements.txt`](../../requirements.txt).
- No `pyproject.toml`, `setup.py`, `Pipfile`, or `environment.yml` — dependency management is `pip` + `requirements.txt` only.
- Key direct dependencies (per `README.md` §11, with observed installed versions from the local `.venv` in parentheses):

| Package | Stated min | Observed | Purpose |
| --- | --- | --- | --- |
| `geopandas` | 0.14 | 1.1.3 | Vector I/O, spatial ops |
| `rasterio` | 1.3 | 1.5.0 | Raster I/O, reprojection, masking |
| `numpy` | 1.24 | 2.4.3 | Array computation |
| `scipy` | 1.11 | 1.17.1 | Gaussian smoothing, statistics |
| `pandas` | 2.0 | 3.0.1 | Tabular data |
| `matplotlib` | 3.7 | 3.10.8 | Maps, charts |
| `shapely` | 2.0 | 2.1.2 | Geometry ops |
| `dem_stitcher` | 2.3 | 2.5.13 | DEM tile stitching |
| `SALib` | 1.4 | 1.5.2 | Sobol sensitivity |
| `PyYAML` | 6.0 | 6.0.3 | `settings.yaml` parsing |
| `python-dotenv` | 1.0 | 1.2.2 | `.env` loading |
| `pydantic` | 2.0 | 2.13.4 | Data contracts |
| `terracatalogueclient` | — | 0.1.19 | Optional ESA WorldCover auto-download |

> ⚠️ Point to validate (tracked as `DOC-004` in `../TASKS.md`): `requirements.txt` was generated from a full `pip freeze` of the local `.venv`, which also includes packages whose direct use in `src/` wasn't confirmed during this pass (e.g. `boto3`/`botocore`/`s3transfer`, `pydeps`, `requests-auth`, `tqdm`, `colorama`, `humanfriendly`). Treat it as a reproducibility snapshot, not a curated "these and only these are imported" list — a stricter curation would need either static import analysis across all of `src/` or a clean-room install-and-run.

## Raw data

- ~18 GB of raw geospatial inputs, resolved via `GEOWORLD_RAW_DATA` in `.env` (or a symlink at `data/raw/`).
- Most datasets download automatically via `src/io/data_fetcher.py` (GADM 4.1, ESA WorldCover 10 m, Copernicus GLO-30/90 DEM, Global Solar Atlas, Global Wind Atlas, Global Power Plant Database).
- **One dataset requires a manual step**: WDPA (protected areas) has no public bulk API. Per country: download the shapefile from `https://www.protectedplanet.net/country/{ISO3}` and extract to `raw/protected_areas/{country_name}/shp_0/`. This is a standing reproducibility gap — a fresh clone cannot run end-to-end without this manual action per new country.

## Credentials (`.env`)

```
GEOWORLD_RAW_DATA=<path>        # required
TERRASCOPE_USERNAME=<optional>  # ESA WorldCover auto-download
TERRASCOPE_PASSWORD=<optional>
```

`.env` is gitignored. Do not print its values into documentation, logs, or commit messages.

## Reproducibility notes

- Pipeline phases are individually idempotent/skippable via `settings.yaml`'s `skip_*` flags with on-disk caching — see `03-pipeline.md`. This means a "successful run" can silently be a mix of freshly computed and long-stale cached outputs if `skip_*` flags are left on across parameter changes; there's no cache-invalidation-on-config-change mechanism observed.
- The Monte Carlo components (SA-2, SA-4, SA-6) do seed the RNG: `sensitivity_analyzer.py`'s `sa2_monte_carlo_weights()`, `sa4_lcoe_uncertainty()`, and `sa6_potential_sensitivity()` each default to `seed: int = 42` (hardcoded, not read from `settings.yaml`), applied via `np.random.default_rng(seed)`. No caller overrides the default, so SA-2/SA-4/SA-6 results are reproducible run-to-run. See `06-risk-areas.md`.
