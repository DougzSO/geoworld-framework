# GeoWorld Framework — README.md

```markdown
# GeoWorld Framework

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.20184266.svg)](https://doi.org/10.5281/zenodo.20184266)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

**Automated renewable energy potential assessment pipeline (Solar, Wind, Biomass)
for any country in the world.**

Produces georeferenced suitability maps, technical potential (GWh/yr) and LCOE
(USD/MWh) from globally available open-access datasets. Runs with a single command.

---

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set the path to raw data (~18 GB) in .env
echo "GEOWORLD_RAW_DATA=/your/path/to/data" > .env

# Run analysis for Portugal
python main.py Portugal

# Or using the ISO code
python main.py PRT
```

---

## Project Structure

```
geoworld_framework/
├── main.py                                    ← pipeline entry point
├── configs/
│   ├── settings.yaml                          ← operational configuration
│   └── parameters.json                        ← scientific parameters by country
├── .env                                       ← data paths (do not version)
├── src/
│   ├── core/
│   │   ├── constants.py                       ← fixed numerical constants
│   │   └── config_loader.py                   ← loads settings.yaml + parameters.json
│   ├── io/
│   │   ├── data_manager.py                    ← locates raw data files on disk
│   │   ├── data_fetcher.py                    ← downloads (GADM, ESA, DEM, etc.)
│   │   └── data_orchestrator.py               ← coordinates parallel/sequential acquisition
│   ├── processors/
│   │   ├── data_auditor.py                    ← Phase 1: raw data quality audit
│   │   ├── grid_aligner.py                    ← Phase 2a: common spatial grid
│   │   ├── criteria_builder.py                ← Phase 2b: normalized criteria [0–1]
│   │   ├── suitability_builder.py             ← Phase 3: OWA + TOPSIS MCDA
│   │   ├── potential_calculator.py            ← Phase 4: installable capacity & generation
│   │   ├── lcoe_calculator.py                 ← Phase 5: levelized cost of energy
│   │   ├── results_writer.py                  ← Phase 6: visual & technical synthesis
│   │   ├── ghg_abatement_calculator.py        ← Phase 7: GHG abatement
│   │   ├── sensitivity_analyzer.py            ← Phase 8: sensitivity analysis
│   └── utils/
│       ├── utils.py                           ← shared geospatial utilities
│       ├── logging_utils.py                   ← centralized logging configuration
│       ├── timing.py                          ← shared timing context manager
│       └── map_styling.py                     ← central map layout & styling engine
├── data/
│   ├── raw/  → GEOWORLD_RAW_DATA (symlink or env var)
│   └── processed/{country_code}/             ← aligned grid (auto-generated)
└── outputs/{country_code}/
    ├── criteria/tif/                          ← normalized 0–1 rasters
    ├── criteria/png/                          ← standardized maps
    ├── criteria/reports/                      ← numerical summaries
    ├── suitability/                           ← MCDA suitability maps (Phase 3)
    ├── potential/                             ← GWh/yr per pixel (Phase 4)
    ├── lcoe/                                  ← LCOE per pixel (Phase 5)
    ├── ghg_abatement/                         ← GHG abatement results (Phase 7)
    ├── sensitivity/                           ← sensitivity analysis outputs (Phase 8)
    └── reports/                               ← audit and synthesis reports
```

---

## Required Data (~18 GB total)

| Dataset | Source | Local Path |
|---|---|---|
| Administrative boundaries | GADM 4.1 (auto-download) | `raw/countries_borders/{country}/` |
| Land Cover (ESA WorldCover 10m) | Terrascope (auto-download) | `raw/land_cover/{country}/` |
| Digital Elevation Model | GLO-30 / GLO-90 (auto-download) | `raw/elevation/{country}/` |
| Solar potential (PVOUT) | Global Solar Atlas | `raw/solar_potential/` |
| Wind potential (50/100/200m) | Global Wind Atlas | `raw/wind_potential/` |
| Existing power plants | Global Power Plant Database | `raw/global_power_plant_database.csv` |
| Protected areas (WDPA) | Protected Planet (manual) | `raw/protected_areas/{country}/shp_0/` |
| Population density | WorldPop (auto-download) | `raw/population/{country}/` |

> **WDPA Note:** Download manually from
> https://www.protectedplanet.net/country/{ISO3}
> and extract to `raw/protected_areas/{country_name}/shp_0/`.

---

## Pipeline Phases

| Phase | Module | Description | Typical time (PRT) |
|---|---|---|---|
| 0 | `main.py` | Orchestration, downloads, pre-flight checks | ~30s |
| 1 | `DataAuditor` | Raw data quality audit | ~90s |
| 2a | `GridAligner` | Reprojection to common grid (0.01°, EPSG:4326) | ~60s |
| 2b | `CriteriaBuilder` | Normalized individual criteria [0–1] + maps | ~60s |
| 3 | `SuitabilityBuilder` | OWA + TOPSIS MCDA, 4 scenarios × 3 technologies | ~120s |
| 4 | `PotentialCalculator` | Technical potential GWh/yr per pixel | ~60s |
| 5 | `LCOECalculator` | LCOE USD/MWh + dominance map | ~60s |
| 6 | `ResultsWriter` | Visual and technical synthesis | ~90s |
| 7 | `GHGAbatementCalculator` | GHG abatement, MACC, carbon intensity | ~60s |
| 8 | `SensitivityAnalyzer` | SA-1 to SA-6 sensitivity suite | ~300s |

### Development Flags (in `main.py`)

```python
SKIP_AUDIT      = False   # True skips Phase 1 (~90s)
SKIP_ALIGN      = False   # True uses existing processed/ grid
SKIP_CRITERIA   = False   # True skips Phase 2b
SKIP_SUITABILITY= False   # True skips Phase 3
SKIP_POTENTIAL  = False   # True skips Phase 4
SKIP_LCOE       = False   # True skips Phase 5
SKIP_RESULTS    = False   # True skips Phase 6
SKIP_GHG        = False   # True skips Phase 7
SKIP_SENSITIVITY= False   # True skips Phase 8
```

---

## Criteria Produced (Phase 2b)

| Criterion | File | Description |
|---|---|---|
| `solar_resource` | `solar_resource.tif/.png` | PVOUT normalized P5–P95 |
| `wind_resource` | `wind_resource.tif/.png` | AHP-combined power density |
| `terrain_score` | `terrain_score.tif/.png` | Slope (60%) + TRI (40%) |
| `slope_degrees` | `slope_degrees.tif/.png` | Slope in degrees (unnormalized) |
| `lc_solar` | `lc_solar.tif/.png` | Land cover suitability for solar |
| `lc_wind` | `lc_wind.tif/.png` | Land cover suitability for wind |
| `lc_biomass` | `lc_biomass.tif/.png` | Land cover suitability for biomass |
| `biomass_resource` | `biomass_resource.tif/.png` | Biomass yield + Gaussian smooth |
| `proximity_plants` | `proximity_plants.tif/.png` | Gaussian score + concentric rings |
| `protected_areas` | `protected_areas.tif/.png` | WDPA mask / penalty |
| *(extra)* | `power_plants.png` | Power plants by type + clipped buffers |

---

## Sensitivity Analysis Modules (Phase 8)

| Module | Description |
|---|---|
| SA-1 | OAT weight sensitivity — Spearman ρ per criterion |
| SA-2 | Monte Carlo AHP — Dirichlet weight sampling |
| SA-3 | Threshold sweep — area elasticity |
| SA-4 | LCOE uncertainty — triangular Monte Carlo |
| SA-5 | Sobol global sensitivity — GHG abatement function |
| SA-6 | Potential parameter sensitivity — OAT elasticity |

---

## Map Visual Standard

All maps follow a consistent publication-quality template
(Q1 journal / doctoral thesis style):

- **Ocean background:** `#D6EAF8` (neutral light blue)
- **Neighboring countries:** `#EEEEEE` (light gray) — when `context_gdf` is provided
- **Scale bar:** bottom-left corner on **all** maps
- **North arrow:** top-left corner on **all** maps
- **Lat/lon grid:** off by default (`show_lat_lon_grid: false` in `settings.yaml`)
- **Colorbars:** identical position and size across all maps
- **Buffers:** always clipped to country polygon — never extend beyond coastline

Color palettes by criterion type:

| Type | Palette |
|---|---|
| Solar (warm) | `YlOrRd` |
| Wind (cool) | `Blues` |
| Biomass (vegetation) | `Greens` |
| Suitability / aptitude | `RdYlGn` (0 = red = poor, 1 = green = good) |
| Restrictions | `RdYlGn` reversed (0 = red = restricted) |

---

## Adding a New Country

1. Add the country entry to `configs/parameters.json` (or `Countries` sheet if
   using the legacy Excel format).
2. Download WDPA from https://www.protectedplanet.net/country/{ISO3}.
3. Run: `python main.py <Country_Name>`

No `.py` files need to be modified.

---

## Configuration

### `configs/settings.yaml` — operational

```yaml
visualization:
  show_lat_lon_grid: false   # true for spatial debugging
  output_dpi: 150            # 300 for print quality
geospatial:
  resolutions:
    suitability: 0.01        # ~1 km — common grid resolution
logging:
  log_level: INFO
  pid_suffix: false          # true for parallel batch runs
```

### `configs/parameters.json` — scientific

Contains per-country and per-technology parameters:
- `Tech_Params`: power density, capacity factors, CAPEX/OPEX
- `Economic_Params`: discount rate, lifetime, carbon price
- `Land_Suitability`: land cover class scores per technology

### `.env`

```
GEOWORLD_RAW_DATA=/path/to/18gb/of/data
```

---

## Logging

GeoWorld uses a three-handler logging architecture:

| Handler | Format | Purpose |
|---|---|---|
| Console (stdout) | `HH:MM:SS \| LEVEL \| message` | Real-time monitoring |
| File (`.log`) | Full timestamp + logger name | Persistent human-readable log |
| File (`.jsonl`) | JSON Lines | Automated analysis (pandas, ELK, BigQuery) |

Log files are written to `outputs/{country_code}/logs/` and include
`country` and `phase` context fields injected automatically.

Use `gdal_quiet()` for surgical GDAL warning suppression within a specific
operation block:

```python
from src.utils.logging_utils import gdal_quiet

with gdal_quiet():
    reproject(source=rasterio.band(src, 1), destination=arr, ...)
```

---

## Key Dependencies

```
geopandas    >= 0.14
rasterio     >= 1.3
numpy        >= 1.24
scipy        >= 1.11
pandas       >= 2.0
matplotlib   >= 3.7
shapely      >= 2.0
SALib        >= 1.4       # Phase 8: Sobol sensitivity analysis
dem-stitcher >= 2.3       # DEM auto-download
Pillow       >= 9.0       # Multi-panel map compositing
terracatalogueclient      # optional: ESA WorldCover download
```

---

## Methodological References

- **AHP:** Saaty, T. L. (1980). *The Analytic Hierarchy Process*. McGraw-Hill.
- **OWA:** Yager, R. R. (1988). On ordered weighted averaging aggregation
  operators. *IEEE Transactions on Systems, Man, and Cybernetics*, 18(1),
  183–190.
- **TOPSIS:** Hwang, C. L., & Yoon, K. (1981). *Multiple Attribute Decision
  Making*. Springer.
- **Sobol:** Saltelli, A. et al. (2008). *Global Sensitivity Analysis: The
  Primer*. Wiley.
- **LCOE:** IRENA (2023). *Renewable Power Generation Costs in 2022*. Abu
  Dhabi.
- **ESA WorldCover:** Zanaga, D. et al. (2022). ESA WorldCover 10m 2021 v200.
  Zenodo.
- **PVOUT:** Global Solar Atlas 2.0, World Bank Group / Solargis.
- **Wind:** Global Wind Atlas 3.0, Technical University of Denmark /
  World Bank.
- **WDPA:** UNEP-WCMC and IUCN (2024). *Protected Planet*. Cambridge /
  Gland.
- **GHG abatement:** IPCC AR5 WG3, Annex II — Lifecycle emission factors.

---

## 📖 Citation

If you use GeoWorld in your research, please cite:

**Software:**
```bibtex
@software{silva2024geoworld,
  author       = {Douglas Silva},
  title        = {GeoWorld Framework: Automated Renewable Energy 
                  Potential Assessment Pipeline},
  month        = jan,
  year         = 2024,
  publisher    = {Zenodo},
  version      = {v1.0.0},
  doi          = {10.5281/zenodo.20184266},
  url          = {https://doi.org/10.5281/zenodo.20184266}
}

## License

MIT License — see `LICENSE` for details.
```

---
