# GeoWorld Framework — README.md (Atualizado para v2.0)

```markdown
# GeoWorld Framework

## Geospatial Renewable Energy Assessment Pipeline

**Version 2.0** · **Python 3.10+** · **Open Source**

---

## 1. Overview

GeoWorld is an open-source Python framework for spatially explicit assessment of renewable energy potential (Solar PV, Onshore Wind, and Biomass/Bioenergy). Given a country identifier, it orchestrates nine sequential analysis phases — from raw data acquisition through GHG abatement estimation and transport decarbonization — producing georeferenced outputs suitable for peer-reviewed publication.

> **Note on Pipeline Execution:**  
> The pipeline is idempotent: each phase reads artefacts written to disk by the previous phase and can be individually skipped via `settings.yaml` when cached outputs are already valid.

**Architecture Highlights (v2.0):**
- **Type-safe contracts** via Pydantic v2 models in `src/core/schemas.py` (replaces legacy `models.py`).
- **Unified phase orchestration** via `PipelineOrchestrator` with consistent skip logic and artifact management.
- **Modular MCDA library** — AHP, TOPSIS, and OWA extracted to `src/utils/` for reusability across processors.
- **Centralised reporting** via `src/utils/reporting.py` — eliminates duplicated text-formatting logic across modules.
- **Unified raster plotting** via `GeoWorldStyler.render_raster_map()` — consistent map styling across all phases.
- **Strict parameter separation:** `settings.yaml` now governs only infrastructure and pipeline flags; all scientific parameters (CF, density, thresholds, LCOE economics) come exclusively from `parameters.json`.

---

## 2. Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Set raw data path in .env (~18 GB required)
echo "GEOWORLD_RAW_DATA=/your/path/to/data" > .env

# Run for a single country (full name or ISO-3166-alpha-3 code)
python main.py Portugal
python main.py PRT

# Batch mode with parallel workers
python main.py --batch country_list.txt --workers 4
```

---

## 3. Pipeline Phases

| Phase | Module | Description | Time (PRT) | Skip flag |
| --- | --- | --- | --- | --- |
| **0** | `main.py` / `DataOrchestrator` | Orchestration, downloads, boundary resolution | ~30 s | — |
| **1** | `DataAuditor` | Raw data quality audit | ~90 s | `skip_audit` |
| **2a** | `GridAligner` | Reprojection to common grid (configurable resolution, EPSG:4326) | ~60 s | `skip_align` |
| **2b** | `CriteriaBuilder` | Individual suitability criteria normalized 0–1; maps + numeric reports | ~60 s | `skip_criteria` |
| **3** | `SuitabilityBuilder` | MCDA via AHP + TOPSIS + OWA; 3 scenarios × 3 technologies | ~120 s | `skip_suitability` |
| **4** | `PotentialCalculator` | Installable capacity (GW) and annual generation (GWh/yr) per pixel | ~60 s | `skip_potential` |
| **5** | `LCOECalculator` | Levelized cost maps (USD/MWh) + technology dominance raster | ~60 s | `skip_lcoe` |
| **6** | `ResultsWriter` | Synthesis: dominance maps, summary dashboard, aggregated GeoTIFFs | ~45 s | `skip_results` |
| **7** | `GHGAbatementCalculator` | Thermal substitution, CO₂ abatement potential, MAC curves | ~45 s | `skip_abatement` |
| **8** | `SensitivityAnalyzer` | SA1–SA6: OAT, Monte Carlo AHP, threshold sweep, LCOE uncertainty, GHG sensitivity, Sobol global SA | ~300 s | `skip_sensitivity` |
| **9** | `TransportDecarbonizationCalculator` | EV / hydrogen penetration scenarios, transport energy demand, charging hub siting | ~60 s | `skip_transport` |

---

## 4. Project Structure

```text
geoworld/
├── main.py                                  ← pipeline entry point (uses orchestrator)
├── configs/
│   ├── settings.yaml                        ← operational configuration (infrastructure only)
│   ├── parameters.json                      ← per-country scientific parameters
│   └── transport_parameters.json            ← transport module parameters
├── .env                                     ← GEOWORLD_RAW_DATA (do not commit)
├── src/
│   ├── core/
│   │   ├── config_loader.py                 ← settings.yaml + parameters.json reader
│   │   ├── constants.py                     ← fixed scientific constants (baselines)
│   │   ├── schemas.py                       ← ALL Pydantic v2 models (CountryParams, results contracts)
│   │   ├── pipeline_orchestrator.py         ← unified phase execution with skip/cache logic
│   │   ├── validators.py                    ← raster shape/CRS/value validation utilities
│   │   └── models.py                        ← legacy state models (PipelineState)
│   ├── io/
│   │   ├── data_manager.py                  ← raw data locator
│   │   ├── data_fetcher.py                  ← dataset downloaders (GADM, ESA, DEM…)
│   │   ├── data_orchestrator.py             ← parallel acquisition coordinator
│   │   └── artifact_manager.py              ← result persistence (pickle), manifest generation
│   ├── processors/
│   │   ├── data_auditor.py                  ← Phase 1
│   │   ├── raster_processor.py              ← slope / TRI derivation
│   │   ├── grid_aligner.py                  ← Phase 2a
│   │   ├── criteria_builder.py              ← Phase 2b
│   │   ├── suitability_builder.py           ← Phase 3 (refactored: orchestration only)
│   │   ├── potential_calculator.py          ← Phase 4
│   │   ├── lcoe_calculator.py               ← Phase 5
│   │   ├── results_writer.py                ← Phase 6
│   │   ├── ghg_abatement_calculator.py      ← Phase 7
│   │   ├── sensitivity_analyzer.py          ← Phase 8
│   │   └── transport_decarbonization_calculator.py  ← Phase 9
│   └── utils/
│       ├── ahp.py                           ← NEW: AHP matrix construction, weight computation, CR validation
│       ├── owa.py                           ← NEW: OWA weight preparation and spatial aggregation
│       ├── topsis.py                        ← NEW: TOPSIS spatial MCDA implementation
│       ├── exclusion.py                     ← NEW: Hard exclusion logic (slope, land cover, protected areas)
│       ├── raster_io.py                     ← NEW: Shared raster I/O (load_reference_meta, load_all_criteria, load_aux_raster)
│       ├── params_helpers.py                ← NEW: Parameter extraction (extract_params_dict)
│       ├── logging_utils.py                 ← logging setup & context management
│       ├── map_styling.py                   ← unified GeoWorldStyler (render_raster_map, decorators, PIL composites)
│       ├── reporting.py                     ← universal text report builder (build_phase_report, ReportSection)
│       ├── timing.py                        ← context-manager timer
│       └── utils.py                         ← generic helpers (safe raster I/O, CRS, area computation, collect_directory_files)
├── data/
│   ├── raw/  → GEOWORLD_RAW_DATA (env var or symlink)
│   └── processed/{country_code}/            ← aligned rasters (auto-generated)
└── outputs/{country_code}/
    ├── audit/                               ← Phase 1 reports
    ├── criteria_builder/{tif,png,reports}/  ← Phase 2b
    ├── suitability/{tif,figures,reports}/   ← Phase 3 (AHP weights JSON + TOPSIS + OWA TIFFs per scenario)
    ├── potential/                           ← Phase 4
    ├── lcoe/                                ← Phase 5
    ├── results/                             ← Phase 6 (dominance maps, dashboard)
    ├── abatement/                           ← Phase 7
    ├── sensitivity/                         ← Phase 8 (SA-1 to SA-6 CSVs + plots)
    ├── transport/                           ← Phase 9
    └── pipeline_state.json                  ← execution state manifest (auto-generated)
```

---

## 5. Required Datasets (~18 GB)

| Dataset | Source | Raw path | Acquisition |
| --- | --- | --- | --- |
| **Administrative boundaries** | GADM 4.1 | `raw/countries_borders/{country}/` | Automatic |
| **Land cover (ESA WorldCover 10 m)** | Terrascope | `raw/land_cover/{country}/` | Automatic |
| **Digital elevation model** | GLO-30 / GLO-90 | `raw/elevation/{country}/` | Automatic |
| **Solar potential (PVOUT)** | Global Solar Atlas 2.0 | `raw/solar_potential/` | Automatic |
| **Wind potential (50/100/200 m)** | Global Wind Atlas 3.0 | `raw/wind_potential/` | Automatic |
| **Existing power plants** | Global Power Plant Database | `raw/global_power_plant_database.csv` | Automatic |
| **Protected areas (WDPA)** | Protected Planet | `raw/protected_areas/{country}/shp_0/` | Manual — see §5.1 |

### 5.1 WDPA — Manual Download

> **CRITICAL:** Protected Planet does not provide a public bulk API. For each country, download the shapefile at `https://www.protectedplanet.net/country/{ISO3}` and extract to `raw/protected_areas/{country_name}/shp_0/`. No code changes are needed after placement.

---

## 6. Configuration

### 6.1 `configs/settings.yaml` — Operational

Controls **infrastructure paths, spatial resolution, visualization layout, and pipeline execution flags only**. All `skip_*` keys default to `false` (full run). Key parameters:

| Key path in settings.yaml | Default | Purpose |
| --- | --- | --- | --- |
| `geospatial.resolutions.suitability` | 0.01 ° | Common grid resolution (~1 km) |
| `geospatial.resolutions.dem_slope` | 0.005 ° | Slope / TRI resolution (~500 m) |
| `pipeline.use_mainland_only` | true | Exclude island territories from analysis |
| `pipeline.skip_*` | false | Skip individual phases (audit, land_cover, align, criteria, suitability, potential, lcoe, results, abatement, sensitivity, transport) |
| `pipeline.sensitivity.n_mc_samples` | 1000 | Monte Carlo sample count (SA2 and SA6) |
| `pipeline.sensitivity.run_sa1 … run_sa6` | true | Enable individual sensitivity sub-analyses |
| `pipeline.transport.primary_scenario` | reference | EV penetration scenario for primary reports (`reference` \| `accelerated` \| `conservative`) |
| `visualization.layout.dpi` | 150 | Map export DPI (use 300 for print-ready output) |
| `visualization.show_lat_lon_grid` | false | Enable lat/lon grid lines for spatial debugging |

> **Important:** `settings.yaml` **no longer contains** LCOE financial parameters (CAPEX, OPEX, discount_rate). These are now defined exclusively in `parameters.json` (see §6.2). Any legacy `lcoe` block in `settings.yaml` is ignored.

### 6.2 `configs/parameters.json` — Scientific

**Single source of truth for all per-country scientific parameters.** Top-level keys: `countries` (indexed by ISO-3166-alpha-3), `land_suitability` (ESA WorldCover class scores), `abatement_defaults`. Each country entry contains:

* **solar** — `land_use_factor`, `threshold`, `power_density_mw_km2`, `capacity_factor`, `pvout_weight`, `ghi_weight`, `dni_weight`, `cf_ceiling`, `cf_floor`
* **wind** — `land_use_factor`, `threshold`, `capacity_density_mw_km2`, `capacity_factor`, `cf_ceiling`, `cf_floor`
* **biomass** — `collection_factor`, `threshold`, `power_density_mw_km2`, `yield_by_land_cover` (ESA class → t/ha/yr), `capacity_factor`, `cf_ceiling`, `cf_floor`
* **owa** — `default_scenario`, `optimistic`/`balanced`/`conservative` weight arrays (must sum to 1.0, non-increasing)
* **lcoe** — per-technology `capex_usd_kw`, `opex_usd_kw_yr`, `lifetime_years`, `discount_rate`
* **abatement** — `carbon_price_usd_tco2e`, `penetration_factor`, `thermal_types`, `emission_factors`
* `slope_threshold_deg`, `protected_as_exclusion`, `forest_as_exclusion`, `use_mainland_only`

> **Parameter Precedence (Highest → Lowest):**
> 1. Country entry in `parameters.json`
> 2. `constants.DEFAULT_TECH_PARAMS` (hardcoded fallbacks in `src/core/constants.py`)
> 
> `settings.yaml` is **never** consulted for technology parameters — only for infrastructure and skip flags (T2_09).

### 6.3 `.env`

```ini
GEOWORLD_RAW_DATA=/path/to/18gb/raw/data
TERRASCOPE_USERNAME=your_username   # optional: ESA WorldCover auto-download
TERRASCOPE_PASSWORD=your_password
```

---

## 7. Criteria Produced (Phase 2b)

| Criterion key | Output file (`*.tif` / `*.png`) | Description |
| --- | --- | --- |
| `solar_resource` | `solar_resource` | PVOUT normalized P5–P95 |
| `wind_resource` | `wind_resource` | AHP-combined wind power density across hub heights (50/100/200 m) |
| `terrain_score` | `terrain_score` | Slope (60 %) + TRI (40 %) combined terrain suitability |
| `slope_degrees` | `slope_degrees` | Raw slope in degrees (not normalized; diagnostic use) |
| `lc_solar` / `lc_wind` / `lc_biomass` | `lc_{tech}` | Land-cover suitability score per technology (ESA class mapping) |
| `biomass_resource` | `biomass_resource` | LC-weighted yield + Gaussian spatial smoothing |
| `proximity_plants` | `proximity_plants` | Gaussian score + concentric rings to existing power plants |
| `protected_areas` | `protected_areas` | IUCN-category-scored WDPA mask / penalty layer |
| `river_solar` / `river_wind` / `river_biomass` | `river_{tech}` | Riparian safety setback + water-access proximity score |
| `lakes_exclusion` | `lakes_exclusion` | HydroLAKES binary exclusion mask (0 = water, 1 = suitable) |
| `seismic_suitability` | `seismic_suitability` | Seismic hazard penalty (GAR / USGS source) |
| `grid_suitability` | `grid_suitability` | Proximity-to-grid score (OSM power lines) |
| `pop_suitability` | `pop_suitability` | Population density suitability (exclusion above threshold) |
| `road_suitability` | `road_suitability` | Road proximity score (accessibility proxy) |

---

## 8. Phase 3: Suitability Builder — MCDA Outputs

**Phase 3 generates two types of suitability maps per technology:**

1. **TOPSIS suitability** — Primary output, named `{country}_{tech}_suitability.tif`
   - Euclidean distance to Positive/Negative Ideal Solutions in AHP-weighted criterion space
   - Single scalar score per pixel: 0 (worst) to 1 (best)
   - Used for comparative analysis and visualization

2. **OWA suitability (3 scenarios)** — Secondary outputs for scenario-based analysis
   - `{country}_{tech}_suitability_owa_optimistic.tif`
   - `{country}_{tech}_suitability_owa_balanced.tif`
   - `{country}_{tech}_suitability_owa_conservative.tif`
   - Ordered Weighted Averaging with scenario-specific weight vectors
   - Used in Phase 4 (PotentialCalculator) when scenario-based thresholds are applied

**Additional outputs:**
- `{country}_{tech}_weights.json` — AHP-derived criterion weights with consistency metrics (CR, λmax)
- Comparison maps (`{country}_suitability_comparison.png`) — Side-by-side TOPSIS results for all technologies
- Text report with pixel statistics, exclusion counts, and high-suitability percentages

**Hard exclusions applied before MCDA:**
- Slope > `slope_threshold_deg` + technology offset (solar: +5°, wind: +10°, biomass: +20°)
- ESA WorldCover classes: 50 (built-up), 70 (snow/ice), 80 (water), 90 (wetland), 95 (mangroves), optionally 10 (forest)
- Protected areas with IUCN scores < 0.01 (Ia, Ib categories)
- Lakes (binary mask from HydroLAKES)

> **Modularity (v2.0):** AHP matrix construction, TOPSIS aggregation, and OWA spatial operators are now implemented in `src/utils/{ahp,topsis,owa}.py`, making them reusable across other MCDA applications.

---

## 9. Map Visual Standards

All output maps follow a consistent publication-quality template aligned with Q1-journal and doctoral thesis conventions:

* **Ocean background:** `#D6EAF8` (neutral light blue)
* **Neighboring countries:** `#EEEEEE` fill / `#CCCCCC` edge — when `context_gdf` is available
* **Cartographic scale bar:** bottom-left corner on all maps
* **North arrow:** top-left / top-right corner on all maps
* **Lat/lon grid:** disabled by default (`show_lat_lon_grid: false` in `settings.yaml`)
* **Colorbars:** uniform position and dimensions across the full output set
* **Geometry clipping:** all buffers are clipped to the country polygon — no bleed over coastlines

**Color palettes by criterion type:**

* **Solar (warm gradient):** `YlOrRd`
* **Wind (cool gradient):** `Blues`
* **Biomass (vegetation):** `YlGn`
* **Suitability / scores:** `RdYlGn` (0 = red = poor; 1 = green = good)
* **Restrictions:** `RdYlGn` reversed (0 = red = restricted)

> **Unified Plotting (v2.0):** All raster maps are now rendered via `GeoWorldStyler.render_raster_map()`, ensuring identical layout, colorbars, and footer styling across phases (DUP_22). This replaces scattered `_plot_*` implementations in individual modules.

---

## 10. Adding a New Country

1. Add an entry under `countries` in `configs/parameters.json` with all required sub-keys (`solar`, `wind`, `biomass`, `owa`, `lcoe`, `abatement`, `slope_threshold_deg`).
2. Download the WDPA shapefile for the target country at `https://www.protectedplanet.net/country/{ISO3}` and extract to `raw/protected_areas/{country_name}/shp_0/`.
3. Run the pipeline: `python main.py <CountryName>`

*Note: No modifications to any `.py` file are required.*

---

## 11. Key Dependencies

| Package | Min version | Purpose |
| --- | --- | --- | --- |
| `geopandas` | 0.14 | Vector data I/O and spatial operations |
| `rasterio` | 1.3 | Raster I/O, reprojection, masking |
| `numpy` | 1.24 | Array computation |
| `scipy` | 1.11 | Gaussian smoothing, statistics |
| `pandas` | 2.0 | Tabular data processing |
| `matplotlib` | 3.7 | Map and chart rendering |
| `shapely` | 2.0 | Geometry operations |
| `dem-stitcher` | 2.3 | DEM tile stitching (GLO-30 / GLO-90) |
| `SALib` | 1.4 | Sobol sensitivity analysis (Phase 8) |
| `PyYAML` | 6.0 | `settings.yaml` parsing |
| `python-dotenv` | 1.0 | Environment variable loading |
| `terracatalogueclient` | — | Optional: automated ESA WorldCover download |
| `pydantic` | 2.0 | Type-safe data contracts (`src/core/schemas.py`) |

---

## 12. Methodological References

* **AHP:** Saaty, T. L. (1980). *The Analytic Hierarchy Process*. McGraw-Hill.
* **TOPSIS:** Hwang, C. L., & Yoon, K. (1981). *Multiple Attribute Decision Making: Methods and Applications*. Springer.
* **OWA:** Yager, R. R. (1988). On ordered weighted averaging aggregation operators. *IEEE Transactions on Systems, Man, and Cybernetics*, 18(1), 183–190.
* **ESA WorldCover:** Zanaga, D. et al. (2022). ESA WorldCover 10 m 2021 v200. *Zenodo*. doi:10.5281/zenodo.7254221.
* **Solar:** Global Solar Atlas 2.0, World Bank Group / Solargis (2023).
* **Wind:** Global Wind Atlas 3.0, Technical University of Denmark / World Bank (2023).
* **Protected areas:** UNEP-WCMC and IUCN (2024). Protected Planet. Cambridge UK / Gland Switzerland.
* **LCOE benchmarks:** IRENA (2024). *Renewable Power Generation Costs in 2023*. Abu Dhabi.
* **Sensitivity analysis:** Saltelli, A. et al. (2010). Variance based sensitivity analysis of model output. *Computer Physics Communications*, 181(2), 259–270.

---

## 📖 Citation

If you use GeoWorld in your research, please cite:

**Software:**
```bibtex
@software{silva2025geoworld,
  author       = {Douglas Silva},
  title        = {GeoWorld Framework: Automated Renewable Energy 
                  Potential Assessment Pipeline},
  month        = jan,
  year         = 2025,
  publisher    = {Zenodo},
  version      = {v2.0.0},
  doi          = {10.5281/zenodo.20184266},
  url          = {https://doi.org/10.5281/zenodo.20184266}
}
```

## License

MIT License — see `LICENSE` for details.
```

---
