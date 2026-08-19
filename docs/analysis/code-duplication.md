---
id: analysis-code-duplication
type: frozen
status: frozen
created: 2026-08-06
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [analysis-arch-misalignments]
linked_by: [docs-tasks, archive-backlog-full]
scope: "Auditoria original nº2 (duplicação de código), congelada no estado do código de 2026-08-06; NÃO reflete correções aplicadas depois — status atual vive em TASKS.md."
---

# Code Duplication — Analysis

Analysis only, follow-up to `docs/arch-misalignments.md`. No code changed in this pass. All line numbers below were verified by direct reads (not structural/grep-only estimates) except where marked. Grep commands are given so the next session can re-locate each finding without re-deriving it.

---

## 1. Duplicated dict-shape adapters

**Root cause common to both:** `potential_results`/`lcoe_results` can arrive at a consumer in three different shapes — a Pydantic model (`PotentialResult`/`LCOEResult` from `src/core/schemas.py`, live-run path), a plain dict with a `"techs"` key (orchestrator-cached path), or a plain dict without it / a differently-nested legacy shape (disk-recovery path via `src/utils/data_recovery.py`). Every consumer that needs a single scalar out of these results re-derives its own "try shape A, catch, try shape B" logic instead of the shape being normalized once at the boundary.

### 1a. `results_writer.py::_get_scenario_data`

- **Purpose**: extract `potential_results["techs"][tech]["scenarios"][scenario]` (primary shape) or `potential_results[tech]["scenarios"][scenario]` (legacy shape), returning `{}` on total miss.
- **Locations**:
  - `src/processors/results_writer.py:1484-1492` — class-level `@staticmethod` wrapper, pure pass-through to the module-level function below (dead indirection).
  - `src/processors/results_writer.py:1499-1523` — the actual module-level implementation.
- **Grep**: `grep -n "_get_scenario_data" src/processors/results_writer.py`

```python
# src/processors/results_writer.py:1499-1523
def _get_scenario_data(
    potential_results: Dict, tech: str, scenario: str
) -> Dict:
    sc = (
        potential_results
        .get("techs", {})
        .get(tech, {})
        .get("scenarios", {})
        .get(scenario, {})
    )
    if sc:
        return sc
    return (
        potential_results
        .get(tech, {})
        .get("scenarios", {})
        .get(scenario, {})
    ) or {}
```

### 1b. `transport_decarbonization_calculator.py::_PotentialView` / `_LCOEView`

- **Purpose**: same problem, more thoroughly handled — two small adapter *classes* wrapping `potential_results`/`lcoe_results` and exposing typed accessor methods (`capacity_gw()`, `as_re_dict()`, `mean_lcoe()`, `normalised_score()`) instead of raw dict digging. They additionally handle the **Pydantic-model path** (`hasattr(self._raw, "techs")`) that `results_writer._get_scenario_data` does not (that one only handles two dict shapes, not a live Pydantic object — worth checking whether `results_writer.py` ever actually receives a live Pydantic model here or whether that's a latent gap).
- **Locations**:
  - `src/processors/transport_decarbonization_calculator.py:167-215` — `_PotentialView` (constructor L180-181, `capacity_gw()` L183-205, `as_re_dict()` L207-212, `available()` L214-215).
  - `src/processors/transport_decarbonization_calculator.py:222-297` — `_LCOEView` (constructor L234-235, `mean_lcoe()` L237-279, `normalised_score()` L281-294, `available()` L296-297).
- **Grep**: `grep -n "class _PotentialView\|class _LCOEView" src/processors/transport_decarbonization_calculator.py`

```python
# src/processors/transport_decarbonization_calculator.py:183-205 (excerpt)
def capacity_gw(self, tech: str, scenario: str = "balanced") -> float:
    if self._raw is None:
        return 0.0
    try:
        if hasattr(self._raw, "techs"):                 # Pydantic model path
            tech_obj = self._raw.techs.get(tech)
            if tech_obj is None:
                return 0.0
            sc_obj = tech_obj.scenarios.get(scenario)
            if sc_obj is None:
                return 0.0
            return float(sc_obj.capacity_gw)
        if isinstance(self._raw, dict):                  # Plain dict path
            return float(
                self._raw["techs"][tech]["scenarios"][scenario]["capacity_gw"]
            )
    except (KeyError, TypeError, AttributeError):
        pass
    return 0.0
```

```python
# src/processors/transport_decarbonization_calculator.py:255-274 (excerpt — _LCOEView.mean_lcoe dict-shape branch)
if isinstance(self._raw, dict):
    # Structure 1: {tech: {"lcoe_usd_mwh": ...}}
    if tech in self._raw:
        v = self._raw[tech]
        if isinstance(v, dict):
            for key in ("lcoe_usd_mwh", "mean", "stats"):
                if key in v:
                    raw = v[key]
                    if isinstance(raw, dict):
                        return float(raw.get("mean", fallback))
                    return float(raw)
    # Structure 2: {"techs": {tech: {"stats": {"mean": ...}}}}
    techs = self._raw.get("techs", {})
    if tech in techs:
        t = techs[tech]
        stats_d = t.get("stats", {})
        if "mean" in stats_d:
            return float(stats_d["mean"])
```

Note `_LCOEView.mean_lcoe` alone tolerates **four** distinct dict layouts (`{tech: {"lcoe_usd_mwh": ...}}`, `{tech: {"mean": ...}}`, `{tech: {"stats": {"mean": ...}}}`, `{"techs": {tech: {"stats": {"mean": ...}}}}`) plus the Pydantic-model path — this is the most defensive/uncertain of the three implementations, which itself is evidence the upstream shape is genuinely inconsistent across code paths, not just inconsistently *read*.

### Proposed shared utility — `src/utils/params_helpers.py`

`params_helpers.py` already exists (currently 29 LOC, one function: `extract_params_dict`) and is exactly the right home — it already owns "safely resolve any parameters instance down to a flat structure regardless of whether it's Pydantic or dict." Extend it with the phase-result equivalent:

```python
# proposed addition to src/utils/params_helpers.py

def normalize_phase_result(result: Any) -> Dict[str, Any]:
    """
    Normalize a PotentialResult/LCOEResult — Pydantic model, plain dict
    (any of the shapes currently handled ad hoc by _PotentialView,
    _LCOEView, and results_writer._get_scenario_data), or None — down to
    one canonical shape: {"techs": {tech: {"scenarios": {...}, "stats": {...}}}}.
    Returns {"techs": {}} if result is None or unrecognized.
    """

def get_scenario_data(result: Any, tech: str, scenario: str) -> Dict[str, Any]:
    """result["techs"][tech]["scenarios"][scenario], normalizing first. {} on miss."""
    return normalize_phase_result(result).get("techs", {}).get(tech, {}).get("scenarios", {}).get(scenario, {})

def get_capacity_gw(result: Any, tech: str, scenario: str = "balanced") -> float:
    """Shared replacement for _PotentialView.capacity_gw."""
    return float(get_scenario_data(result, tech, scenario).get("capacity_gw", 0.0))

def get_mean_lcoe(result: Any, tech: str, fallback: float = 60.0) -> float:
    """Shared replacement for _LCOEView.mean_lcoe. Consolidates all 4 known dict shapes."""
```

`_PotentialView`/`_LCOEView` in `transport_decarbonization_calculator.py` become thin wrappers calling these (keep the classes if `normalised_score()`/`as_re_dict()`'s convenience API is still wanted, but delete the duplicated shape-branching inside them); `results_writer._get_scenario_data` (both the class staticmethod and the module-level function, L1484-1523) is deleted outright and replaced with a call to `get_scenario_data()`.

---

## 2. Duplicated "find upstream TIF by filename variants" logic

**Confirmed**: `src/utils/raster_io.py` (108 LOC: `get_raster_meta`, `load_reference_meta`, `load_all_criteria`, `load_aux_raster`) does **not** already solve this. Its functions glob an entire *directory* for "the criteria rasters" as a category; none of them take a `(tech, country_code)` pair and try a specific ordered list of candidate filenames the way the functions below do. A new function is needed, not a re-use of an existing one.

### 2a. `results_writer.py` — three near-identical finder methods

| Method | Lines | Finds |
| --- | --- | --- |
| `_find_suitability_tif` (`@staticmethod`) | `src/processors/results_writer.py:113-153` | Phase 3 suitability TIF |
| `_find_lcoe_tif` | `src/processors/results_writer.py:159-190` | Phase 5 LCOE TIF |
| `_find_suitable_tif` | `src/processors/results_writer.py:196-212` | Phase 4 suitable-pixel mask TIF |

```python
# src/processors/results_writer.py:113-138 (excerpt, pattern shared by all three)
candidates: List[Path] = [
    suitability_dir / f"{country_code}_{tech}_suitability_owa_{scenario}.tif",
    suitability_dir / f"{country_code}_{tech}_suitability.tif",
    suitability_dir / f"{country_code.lower()}_{tech}_suitability_owa_{scenario}.tif",
    suitability_dir / f"{country_code.lower()}_{tech}_suitability.tif",
]
for path in candidates:
    if path.exists():
        return path
matches = sorted(suitability_dir.glob(f"*{tech}*suitability*{scenario}*.tif"))
if not matches:
    matches = sorted(suitability_dir.glob(f"*{tech}*suitability*.tif"))
if matches:
    return matches[0]
return None
```

- **Grep**: `grep -n "_find_suitability_tif\|_find_lcoe_tif\|_find_suitable_tif" src/processors/results_writer.py`

### 2b. `lcoe_calculator.py` — three more, same shape

| Method | Lines | Finds |
| --- | --- | --- |
| `_find_potential_suitable_tif` | `src/processors/lcoe_calculator.py:262-299` | Phase 4 suitable-pixel mask TIF (**same target as `results_writer._find_suitable_tif` above — two independent implementations of the identical lookup**) |
| `_find_suitability_tif` | `src/processors/lcoe_calculator.py:377-423` | Phase 3 suitability TIF (**same target as `results_writer._find_suitability_tif`, different candidate order — see divergence note below**) |
| `_find_resource_tif` | `src/processors/lcoe_calculator.py:425-465` | Phase 2b criteria/resource TIF |

```python
# src/processors/lcoe_calculator.py:284-299 (excerpt)
tif_dir = self.outputs_dir / country_code / "potential" / "tifs"
candidates = [
    tif_dir / f"{country_code}_{tech}_suitable_{scenario}.tif",
    tif_dir / f"{country_code.lower()}_{tech}_suitable_{scenario}.tif",
]
for path in candidates:
    if path.exists():
        return path
matches = sorted(tif_dir.glob(f"*{tech}*suitable*{scenario}*.tif"))
if matches:
    return matches[0]
return None
```

- **Grep**: `grep -n "_find_potential_suitable_tif\|_find_suitability_tif\|_find_resource_tif" src/processors/lcoe_calculator.py`

**⚠️ Divergence worth flagging, not just duplication**: `results_writer._find_suitability_tif` (L129-134) tries the **OWA-scenario filename first**, TOPSIS second. `lcoe_calculator._find_suitability_tif` (L401-410) does the **opposite** — TOPSIS first (its own comment at L386 says "✅ FIX (Grupo C): Priority order changed to prefer TOPSIS over OWA... We prefer TOPSIS to match Phase 4 behavior"). So Phase 5 and Phase 6 can silently pick *different* suitability rasters as their reference when both a TOPSIS and an OWA-balanced file exist for the same tech/country, because the fallback order was fixed in one copy of this logic and not the other. This is exactly the kind of bug duplicated logic invites: the fix at L386 only patched Phase 5's copy.

### Proposed shared function — `src/utils/raster_io.py`

```python
# proposed addition to src/utils/raster_io.py

def find_raster_by_base_name(
    directory: Path,
    country_code: str,
    tech: str,
    patterns: List[str],
    glob_patterns: Optional[List[str]] = None,
) -> Optional[Path]:
    """
    Locate a phase-output TIF by trying an ordered list of filename
    templates, then falling back to glob.

    Args:
        directory: directory to search.
        country_code: ISO3 code, tried both as-is and lower-cased.
        tech: technology key ("solar" | "wind" | "biomass").
        patterns: filename templates, checked in order, each formatted
            with .format(code=country_code, tech=tech) — caller supplies
            e.g. ["{code}_{tech}_suitability_owa_balanced.tif",
                  "{code}_{tech}_suitability.tif"]. Caller controls the
            order (this is the fix for the TOPSIS/OWA divergence above —
            the order becomes an explicit, single-sourced argument
            instead of being silently re-decided per call site).
        glob_patterns: glob fallback(s) if no exact candidate exists,
            e.g. ["*{tech}*suitability*.tif"].

    Returns:
        First matching Path, or None.
    """
```

Replace all six call sites above (`results_writer.py` ×3, `lcoe_calculator.py` ×3) with calls to this one function, each passing its own `patterns=[...]` list — this preserves each phase's exact current filename precedence as an explicit argument (so behavior doesn't change silently) while removing the ~250 combined lines of duplicated glob/candidate-loop logic down to ~6 short call sites. As a second step (not in this pass), the TOPSIS/OWA precedence divergence between `results_writer.py` and `lcoe_calculator.py` should be resolved to one intentional order.

**Not investigated in this pass** (flagged, not confirmed): `docs/arch-misalignments.md` also names `sensitivity_analyzer.py` and `data_recovery.py` as likely having their own copies of this pattern, since both load prior-phase TIFs. Confirm with:
```
grep -n "def _find.*tif\|def _find.*suitab\|\.glob(f\"\*" src/processors/sensitivity_analyzer.py src/utils/data_recovery.py
```

---

## 3. Hardcoded values in Phase 3–6 processors that look like they belong in config

Scope: `suitability_builder.py` (Phase 3), `potential_calculator.py` (Phase 4), `lcoe_calculator.py` (Phase 5), `results_writer.py` (Phase 6). Excludes loop counters, array indices, NumPy percentile/axis arguments, and matplotlib cosmetic values (`alpha`, `linewidth`, `fontsize`, `zorder`, figure spacing) — those were grepped and reviewed but are not listed below as they don't affect scientific output.

| # | Module | Location | Current value | What it controls | Proposed config key |
| --- | --- | --- | --- | --- | --- |
| 1 | `suitability_builder.py` | `_SLOPE_OFFSET_DEG`, module-level dict, **L93-97** | `{"solar": 5.0, "wind": 10.0, "biomass": 20.0}` | Per-technology slope-exclusion offset added on top of each country's `slope_threshold_deg` (Phase 3 hard exclusion — see `docs/memory/03-pipeline.md`) | `parameters.json` — currently country-scoped params have no slope-offset field; add a top-level (technology-only, not country-varying, unless intentionally meant to vary) `slope_offset_deg: {solar: 5.0, wind: 10.0, biomass: 20.0}` block, analogous to the existing top-level `land_suitability` block |
| 2 | `suitability_builder.py` | `base_slope_deg` fallback, **L156** | `float(cp.get("slope_threshold_deg", 10.0))` | Fallback base slope threshold (degrees) if a country's `parameters.json` entry is missing `slope_threshold_deg` | Route through `constants.DEFAULT_TECH_PARAMS` (tier-2 fallback location, per `docs/memory/07-configuration.md`) instead of a bare literal, so there is one documented fallback table, not scattered literals |
| 3 | `potential_calculator.py` | `threshold` fallback, **L439** | `params["thresholds"].get(scenario, 0.60)` | Fallback suitability threshold if a scenario key is missing from `build_tech_params()` output | Same as #2 — should read from `constants.DEFAULT_TECH_PARAMS`/`HIGH_SUITABILITY_THRESHOLD`-style named constant, not a bare `0.60` |
| 4 | `lcoe_calculator.py` | `_resolve_threshold`, **L592 and L597** | `0.60` (two separate literals in the same method: one when `thresholds` dict is empty, one when the requested scenario key is missing) | Same fallback threshold as #3, third independent occurrence of the same magic number in two files | Consolidate #3 and #4 into one named constant (e.g. `DEFAULT_SUITABILITY_THRESHOLD_FALLBACK = 0.60` in `constants.py`) — currently `0.60` is typed out **three times** across two files with no shared source |
| 5 | `lcoe_calculator.py` | `_irena_defaults`, **L563** | `{"solar": 0.20, "wind": 0.30, "biomass": 0.73}` | "Emergency fallback" capacity factor if `build_tech_params()` returns `capacity_factor=0` | **High priority — see divergence note below.** Should not exist as a second table; use `constants.DEFAULT_TECH_PARAMS[tech]["capacity_factor"]` instead |
| 6 | `lcoe_calculator.py` | `_MIN_RESOURCE_COVERAGE`, **L113** | `0.05` (module-level named constant) | Minimum fraction of finite pixels a resource TIF must have before it's trusted (else falls back to suitability-based modulation) | `settings.yaml` — this is a data-quality gate, same category as the existing `audit.resolution_tolerance` entry, not a scientific parameter, so `settings.yaml` fits better than `parameters.json` |
| 7 | `lcoe_calculator.py` | biomass CV check, **L1067, L1069** | `src_cv < 0.01` (inline literal, not even a named constant) | Coefficient-of-variation threshold below which a biomass resource TIF is treated as "flat/unreliable" | Promote to a named constant at minimum (e.g. `_BIOMASS_RESOURCE_CV_MIN`); consider `settings.yaml` alongside #6 (same "data-quality gate" category) |
| 8 | `results_writer.py` | `_build_suitability_dominance` default arg, **L878** | `competition_delta: float = 0.10` | TOPSIS-score gap below which two technologies are considered "competing" for a pixel in the dominance map | `settings.yaml` — analytical/visualization threshold, not a physical constant; candidate key `visualization.dominance.competition_delta_topsis` |
| 9 | `results_writer.py` | `_build_suitability_dominance` default arg, **L879** | `min_score: float = 0.30` | Minimum TOPSIS score for a pixel to be assigned to any technology at all (below this: "no technology") | `settings.yaml` — candidate key `visualization.dominance.min_suitability_score` |
| 10 | `results_writer.py` | `_build_lcoe_dominance` default arg, **L905** | `competition_delta_usd: float = 10.0` | USD/MWh gap below which two technologies are considered "competing" in the LCOE dominance map | `settings.yaml` — candidate key `visualization.dominance.competition_delta_lcoe_usd` |
| 11 | `results_writer.py` | legend label string, **L1056** | `"Competition Zone (ΔTOPSIS < 0.10)"` | Duplicates the value from #8 as display text | Not a config candidate by itself, but a **duplication risk**: if #8's default changes, this string silently goes stale. Should be built with an f-string referencing the same constant/parameter, not typed out separately |
| 12 | `results_writer.py` | `_recover_supply_curve_from_tif`, **L417** | `/ 1000.0` (unit conversion, no named constant) embodies a **"1 MW per pixel" assumption** (stated only in the function's docstring, not in code) | The proxy capacity-per-pixel assumption used only when Phase 5's real supply curve wasn't persisted (see `docs/arch-misalignments.md` §1a for the correctness risk this creates) | If the Tier-2 fallback is kept at all (per `arch-misalignments.md`'s recommendation to prefer always persisting the real curve), name the assumption explicitly, e.g. `_SUPPLY_CURVE_PROXY_MW_PER_PIXEL = 1.0`, rather than a bare `/1000.0` |
| 13 | `results_writer.py` | `_recover_supply_curve_from_tif`, **L424-426** | `if len(sc) > 5000: ... 5000 ...` | Downsampling cap for the reconstructed supply-curve DataFrame | Lower priority (pipeline-behavior tuning, not scientific) — `settings.yaml` candidate key `pipeline.supply_curve_max_points` if it's ever worth exposing |

**⚠️ Priority finding — #5 diverges numerically from the codebase's own designated fallback table.** `constants.py`'s `DEFAULT_TECH_PARAMS` (the documented Tier-2 fallback per `docs/memory/07-configuration.md`) has:

```
# src/core/constants.py:238-278 (excerpt)
"solar":   {"capacity_factor": 0.195, ...}   # L247
"wind":    {"capacity_factor": 0.274, ...}   # L262
"biomass": {"capacity_factor": 0.750, ...}   # L277
```

`lcoe_calculator.py`'s inline `_irena_defaults` (L563) has `{"solar": 0.20, "wind": 0.30, "biomass": 0.73}` — close but **not identical** to any of the three. This means there are currently two independently-maintained "IRENA emergency default capacity factor" tables in the codebase, and they have drifted apart. If `lcoe_calculator.py`'s emergency path is ever actually reached (only "should never be reached if parameters.json is properly populated" per its own comment at L561-562), it silently uses different numbers than `constants.py` claims to be the canonical fallback.

**Grep to re-locate all of the above**:
```
grep -n "_SLOPE_OFFSET_DEG\|slope_threshold_deg.*10.0" src/processors/suitability_builder.py
grep -n "0\.60" src/processors/potential_calculator.py src/processors/lcoe_calculator.py
grep -n "_irena_defaults\|_MIN_RESOURCE_COVERAGE\|src_cv < 0.01" src/processors/lcoe_calculator.py
grep -n "competition_delta\|min_score\|ΔTOPSIS" src/processors/results_writer.py
grep -n "capacity_factor" src/core/constants.py
```

---

## 4. Repeated "write to disk" boilerplate

### 4a. GeoTIFF writes

A shared context manager already exists — `safe_raster_write()` in `src/utils/utils.py:83-108` (sets `compress="lzw"`/`tiled=True` defaults, creates the parent directory, guarantees `dst.close()` in a `finally` block). Adoption is inconsistent:

| File | Line(s) | Uses `safe_raster_write`? |
| --- | --- | --- |
| `src/processors/grid_aligner.py` | 259, 453, 541, 674, 738, 799, 877 (7 call sites) | **Yes** |
| `src/processors/criteria_builder.py` | 111 | **Yes** |
| `src/processors/suitability_builder.py` | 269 | **Yes** |
| `src/processors/raster_processor.py` | 56 | **Yes** |
| `src/processors/potential_calculator.py` | 495-511 | **No** — raw `rasterio.open(tif_out, 'w', driver='GTiff', ...)`, manually specifies `compress='lzw'` again inline (would be a no-op default under `safe_raster_write`) |
| `src/processors/lcoe_calculator.py` | 1209-1224 | **No** — builds its own `profile = dict(driver="GTiff", ..., compress="lzw", tiled=True, blockxsize=256, blockysize=256)` then raw `rasterio.open(str(tif_path), "w", **profile)` |
| `src/processors/results_writer.py` | 1353-1365 (`_write_uint8` helper) | **No** — raw `rasterio.open(str(path), "w", driver="GTiff", ..., compress="lzw", predictor=1, nodata=0)`. Note the extra `predictor=1` kwarg not present in the other two non-compliant sites — a third slightly different inline profile |
| `src/io/data_fetcher.py` | 942-944 (block write), 1125-1134 (mosaic write) | **No** — raw `rasterio.open(..., "w", **prof)` / `**meta)`. Lower priority: this is Phase 0 raw-data acquisition/mosaicking, not pipeline-output writing, and predates the `src/utils/` split — still inconsistent, but a different category from the Phase 4-6 non-compliance above |

**Pattern**: Phases 2a/2b/3 (the earlier-refactored phases, per `docs/memory/09-decisions.md`) consistently use the shared wrapper. Phases 4/5/6 (and Phase 0) each reimplement their own `profile`/`meta` dict with slightly different kwargs (`compress='lzw'` vs `compress="lzw"`, presence/absence of `tiled`, `blockxsize`/`blockysize`, `predictor`). This is the same "extraction pattern applied inconsistently across phases" observation already made in `docs/arch-misalignments.md` (re: `abatement_plots.py` vs. inline plotting in Phases 8/9), now confirmed for raster I/O too.

**Grep**: `grep -rn "rasterio.open(" src/processors src/io | grep -v safe_raster` (raw calls) vs. `grep -rn "safe_raster_write(" src/processors` (compliant calls)

### 4b. CSV writes

No shared wrapper exists; every site calls pandas' `DataFrame.to_csv()` directly. Given `to_csv()` is already a one-line stdlib-style call with no meaningful boilerplate around it (unlike the ~15-20 line GeoTIFF profile dicts above), this is **not** flagged as a duplication problem — listed here only for completeness per the task scope:

- `src/processors/potential_calculator.py:526` — zonal stats CSV
- `src/processors/lcoe_calculator.py:1177` — zonal LCOE CSV
- `src/processors/sensitivity_analyzer.py:1717, 1801, 1841, 1886, 1928` — five separate SA-result CSVs
- `src/processors/transport_decarbonization_calculator.py:517-518, 520` — timeseries/fleet/hubs CSVs

**Grep**: `grep -rn "\.to_csv(" src/processors`

### 4c. Text report writes

Also no shared wrapper — every processor calls `path.write_text(report, encoding="utf-8")` on the string returned by its own `_format_report()` (which itself does correctly call the shared `src/utils/reporting.py::build_phase_report()` in the phases that use it, per `SUMMARY.md`). One-line boilerplate, same "not worth wrapping" conclusion as CSVs:

- `src/processors/data_auditor.py:1357`, `src/processors/suitability_builder.py:477, 597`, `src/processors/potential_calculator.py:319`, `src/processors/lcoe_calculator.py:765`, `src/processors/results_writer.py:637`, `src/processors/ghg_abatement_calculator.py:836`, `src/processors/sensitivity_analyzer.py:1955`, `src/processors/transport_decarbonization_calculator.py:523`, `src/processors/grid_aligner.py:1171` (JSON, not text, but same one-line-write pattern), `src/utils/reporting.py:376` (the shared builder itself also has a `write_text` — presumably an optional direct-write convenience path; worth checking whether processors could call that instead of duplicating the last `write_text` line themselves — **not confirmed in this pass**)

**Grep**: `grep -rn "\.write_text(" src/processors src/utils`

### 4d. Pickle/JSON artifact persistence — the one already-correct case

`src/io/artifact_manager.py`'s `ArtifactManager.save_result()` (**L135**) and `.save_manifest()` (**L92**) are the shared, consistently-used wrapper for phase-result serialization (pickle for result objects, JSON for manifests/pipeline state) — every processor's end-of-`run()` persistence step goes through these, confirmed via `docs/memory/02-architecture.md`'s orchestration contract and `SUMMARY.md`'s dependency listings. Included here as the positive baseline: this is what "GeoTIFF writes" (§4a) should look like once consolidated.

---

## Summary for the next refactoring pass

| Category | Severity | Effort to fix | Where |
| --- | --- | --- | --- |
| Dict-shape adapters (§1) | Medium — correctness-neutral but triplicated logic | Small — one new module-level function pair in `params_helpers.py` + delete ~90 combined lines | `results_writer.py`, `transport_decarbonization_calculator.py` |
| TIF-finding duplication (§2) | **High** — confirmed behavioral divergence (TOPSIS/OWA precedence), not just duplication | Medium — one new function in `raster_io.py` + 6 call-site rewrites | `results_writer.py`, `lcoe_calculator.py` |
| Hardcoded values (§3) | **High** for #5 (silently divergent fallback table); Medium for #1, #3/#4 (repeated `0.60`); Low for the rest | Small per item — mostly moving literals into `constants.py`/`settings.yaml` | `suitability_builder.py`, `potential_calculator.py`, `lcoe_calculator.py`, `results_writer.py` |
| GeoTIFF write boilerplate (§4a) | Medium — inconsistent compression/tiling settings across phases could cause subtly different output file characteristics | Small — swap 3 call sites (Phase 4/5/6) to `safe_raster_write()`; Phase 0 (`data_fetcher.py`) lower priority | `potential_calculator.py`, `lcoe_calculator.py`, `results_writer.py`, (`data_fetcher.py`) |

No code was changed in this pass. No test suite exists (`docs/memory/06-risk-areas.md`) — any fix here, especially §2's TOPSIS/OWA precedence unification and §3's `_irena_defaults` consolidation, should be verified by re-running the pipeline for at least one already-processed country (e.g. PRT) and diffing the resulting rasters/reports against the current `outputs/PRT/` before/after, since both have a real chance of changing numeric output, not just code shape.
