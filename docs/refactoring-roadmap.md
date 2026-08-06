# Refactoring Roadmap

Planning only — no code changed. Synthesizes `docs/arch-misalignments.md`, `docs/code-duplication.md`, and `docs/write-points-inventory.md` into an ordered task list. Every task below is grounded in a specific finding in one of those three documents; none introduce new findings.

**Commit message convention used in the templates below**: per `CLAUDE.md`'s already-established git rule (plain imperative summary — `Fix X` / `Add Y` / `Move Z` — no Conventional Commit prefixes, no AI attribution, matching this repo's mixed-but-mostly-plain `git log` history), not something newly decided in this document. If a stricter convention is wanted going forward, say so before the first commit lands — the templates are easy to reshape, but should be settled once rather than per-task.

Effort scale: **S** = well under a day of focused work, mechanical or narrowly scoped; **M** = 2-4 days, touches multiple call sites or requires careful behavior-preserving design; **L** = a week or more, structural change across a large file or new subsystem. Risk scale: **Low** = mechanical, easy to verify by diffing output; **Med** = changes numeric output or behavior, needs a before/after pipeline run to verify; **High** = touches a load-bearing path with no test coverage and non-obvious edge cases.

---

## BLOCKERS (must fix before web-platform work starts)

These block a clean parameter input/output API because right now, "what does Phase N return" has more than one answer depending on execution mode (live vs. disk-recovered), which is exactly the ambiguity a request-driven web platform will hit on every request.

### BLOCKER-001 — Persist Phase 5's real supply curve to Parquet

**Title**: Add supply-curve Parquet export to `lcoe_calculator.py` so Phase 6 never has to approximate it.

**Current pain**: `lcoe_calculator.py::run()` computes the real, pixel-accurate `supply_curve` (`compute_supply_curve(lcoe_map, cap_map)`, L891) but discards it down to a 4-number summary before persisting (L808-815: `n_points`, `min_lcoe`, `max_lcoe`, `max_capacity_gw` only). `src/utils/data_recovery.py::recover_supply_curve_from_disk` is fully implemented to read `outputs/{ISO3}/lcoe/data/{CODE}_{tech}_supply_curve.parquet`, but that file is never written anywhere — confirmed via `grep -in "parquet\|supply_curve\.csv\|supply_curve\.to_" src/processors/lcoe_calculator.py` (no matches). Every disk-recovered or cross-process invocation of Phase 6 today silently substitutes a **"1 MW per pixel" proxy** (`results_writer.py::_recover_supply_curve_from_tif`, L379-428) whose own docstring admits "absolute GW totals differ from Phase 5." A web platform that computes Phase 5 in one request and renders Phase 6's dashboard in another **cannot avoid this divergence** — it's not an edge case there, it's the normal call pattern. Can't ship a "get supply curve for country X" API endpoint that sometimes returns real data and sometimes returns a proxy with no way for the caller to tell which.

**Depends on**: none.

**Effort**: M — new Parquet write path, needs to work from both the live in-memory `supply_curve` DataFrame and be verified against what `recover_supply_curve_from_disk` expects (column names `lcoe_usd_mwh`, `cum_capacity_gw`, already documented in that function's docstring).

**Risk**: Med — doesn't change what Phase 5 *computes*, only what it *persists*; but any first run against a country whose supply curve was previously only ever seen as the Phase 6 proxy will show a different (correct) shape post-fix, worth flagging to whoever reviews prior published figures.

**Deliverable**:
- `src/processors/lcoe_calculator.py`: add a write step (near the existing zonal CSV export, L1177) writing `supply_curve.to_parquet(outputs_dir/country_code/"lcoe"/"data"/f"{code}_{tech}_supply_curve.parquet")`.
- `src/processors/results_writer.py`: delete `_recover_supply_curve_from_tif` (L379-428) and its "1 MW per pixel" assumption once the Parquet file is reliably present; keep `_recover_supply_curve` (L360-377)'s Tier-1 path (`recover_supply_curve_from_disk`) as the sole path, or retain Tier-2 only as an explicit `reconstruct_approximate_supply_curve_from_tif()` in `data_recovery.py` per `docs/code-duplication.md`'s naming recommendation, clearly labeled as approximate for legacy runs predating this fix.
- Tests: none exist yet for this module (see QI-004) — at minimum, manually verify `outputs/PRT/lcoe/data/PRT_solar_supply_curve.parquet` exists and round-trips through `recover_supply_curve_from_disk` after a fresh Phase 5 run.

**Success criteria**: `recover_supply_curve_from_disk(outputs_dir/"PRT"/"lcoe", "PRT", "solar")` returns a non-`None` DataFrame after a fresh Phase 5 run without ever touching a TIF; `grep -n "_recover_supply_curve_from_tif" src/processors/results_writer.py` returns nothing (function deleted) or returns only inside a clearly-labeled legacy/approximate path.

**Commit message template**:
```
Persist real supply curve from Phase 5 to Parquet

Phase 5 computed the pixel-accurate merit-order curve but only
persisted a 4-number summary, forcing Phase 6 to reconstruct an
approximate curve (1 MW/pixel proxy) whenever it ran against
disk-recovered LCOE results instead of a live in-process handoff.
```

---

### BLOCKER-002 — Persist full LCOE percentile stats from Phase 5

**Title**: Compute and persist p25/median/p75 in `lcoe_calculator.py` alongside the existing p10/p90/mean.

**Current pain**: `results_writer.py::_enrich_lcoe_stats` (L294-354) re-opens `outputs/{ISO3}/lcoe/tif/{CODE}_{tech}_lcoe_usd_mwh.tif` and calls `np.percentile` to backfill quartiles Phase 5 never computed. Confirmed this isn't a rare fallback: the disk-recovery path (`data_recovery.py::_parse_lcoe_csv`, L345-372) extracts only a single **mean** value from the zonal CSV's `lcoe_mean` column — no percentiles survive that path at all — so the "fallback" TIF-read in `_enrich_lcoe_stats` is, in practice, always exercised. A web API returning "LCOE distribution for country X" needs one authoritative stats object; today that object is assembled by whichever consumer happens to ask for it, from whichever partial data happens to be reachable.

**Depends on**: none (independent of BLOCKER-001, but touches the same `lcoe_calculator.py` stats-computation step — sequencing them in the same work session avoids re-touching the file twice).

**Effort**: S — same in-memory `lcoe_map` array already used for p10/p90/mean; add three more `np.percentile` calls in the same place.

**Risk**: Low — additive, doesn't change existing values, only fills gaps.

**Deliverable**:
- `src/processors/lcoe_calculator.py::_compute_cf_and_lcoe` (per `docs/arch-misalignments.md` §6's line range, L1021-1146): compute `p25`, `median`, `p75` alongside existing `p10`/`p90`/`mean`.
- Persist all six stats as columns in the zonal CSV (L1177 write) and/or the pickled result dict.
- `src/processors/results_writer.py`: delete `_enrich_lcoe_stats` (L294-354) once the stats are reliably complete from both live and disk-recovered paths; remove its two call sites (`run()` L533, and anywhere else it's invoked).

**Success criteria**: `outputs/{ISO3}/lcoe/data/{CODE}_{tech}_lcoe_zonal.csv` has `lcoe_p25`, `lcoe_median`, `lcoe_p75` columns after a fresh run; `grep -n "_enrich_lcoe_stats" src/processors/results_writer.py` returns nothing.

**Commit message template**:
```
Compute full LCOE percentile stats in Phase 5

p25/median/p75 were only ever backfilled downstream by re-reading
the LCOE raster in Phase 6, on every run, live or recovered. Compute
them once where the pixel array already lives.
```

---

### BLOCKER-003 — Persist integrated suitable area from Phase 4

**Title**: Add `area_km2` (and capacity/generation totals) to Phase 4's zonal CSV so nobody re-derives it from a raster mask.

**Current pain**: `potential_calculator.py::_run_technology` already computes `area_km2` at **L462/533** using `build_pixel_area_array()` over the apt-pixel mask — the exact canonical calculation. It is currently re-derived **twice more**, independently: `src/utils/data_recovery.py::_extract_area_from_suitable_tif` (L212-266, used in disk-recovery) and `results_writer.py::_compute_integrated_area` (L434-481, used unconditionally in Phase 6's report, live or recovered — it never checks whether the value is already sitting in the `potential_results` dict it received). This is the highest-multiplicity duplication found across all three analysis passes: **one number, three independent implementations of the same geodesic calculation.** A web platform's "potential" API response needs exactly one area figure per tech/scenario, computed once, with one place to fix if the area methodology ever changes.

**Depends on**: none.

**Effort**: S-M — add a column to an existing CSV write (S); updating `data_recovery.py::recover_potential_from_disk` to prefer the CSV column over `_extract_area_from_suitable_tif` and updating `results_writer.py` to read `potential_results[...]["area_km2"]` before recomputing pushes this to M.

**Risk**: Med — touches three files' worth of call sites for the same logical value; get the precedence order wrong (CSV vs. TIF-recompute) and area could silently diverge between a fresh run and a recovered one during the transition.

**Deliverable**:
- `src/processors/potential_calculator.py`: add `area_km2` (and ideally `capacity_gw`, `generation_twh` totals — currently only per-zone sums exist) as a column/row in the zonal CSV write (L518-527), or a small per-tech/scenario JSON sidecar.
- `src/utils/data_recovery.py::recover_potential_from_disk` (L48-163): read the new column instead of calling `_extract_area_from_suitable_tif`; delete that function (L212-266) once unused.
- `src/processors/results_writer.py::_format_report`: read `area` from `potential_results[...]["area_km2"]` (via the BLOCKER-007 accessor once that lands) instead of calling `_compute_integrated_area`; delete `_compute_integrated_area` (L434-481).

**Success criteria**: `grep -rn "_extract_area_from_suitable_tif\|_compute_integrated_area" src/` returns zero matches; the zonal CSV has an `area_km2` column; a disk-recovered Phase 6 run produces byte-identical area figures to a live-run Phase 6 for the same country.

**Commit message template**:
```
Persist integrated suitable area from Phase 4

Area was independently recomputed from the suitable-pixel mask TIF
in both data_recovery and results_writer despite Phase 4 already
having the number. Write it once, read it everywhere else.
```

---

### BLOCKER-004 — Fix divergent capacity-factor fallback table

**Title**: Delete `lcoe_calculator.py`'s inline `_irena_defaults` and route its emergency fallback through `constants.DEFAULT_TECH_PARAMS`.

**Current pain**: `lcoe_calculator.py:563` has `_irena_defaults = {"solar": 0.20, "wind": 0.30, "biomass": 0.73}`, used when `build_tech_params()` returns `capacity_factor=0`. `constants.py`'s `DEFAULT_TECH_PARAMS` — the codebase's own documented Tier-2 fallback (`docs/memory/07-configuration.md`) — has `{"solar": 0.195, "wind": 0.274, "biomass": 0.750}` (L247/262/277). These are **two independently-maintained tables with different values**, both claiming to be "the" emergency default. This is the single highest-severity finding in `docs/code-duplication.md`: not just duplication, a **silent numeric divergence** in a scientific fallback path. A web platform exposing "what capacity factor did we use for this calculation, and why" cannot answer that question correctly while two tables disagree about the answer.

**Depends on**: none.

**Effort**: S — delete one dict literal, redirect one lookup.

**Risk**: Med — if this emergency path has ever actually been exercised for a currently-configured country (unlikely per the code's own comment "should never be reached if parameters.json is properly populated," but not verified in this pass), fixing it changes that country's LCOE output. Verify with a targeted re-run.

**Deliverable**:
- `src/processors/lcoe_calculator.py::_resolve_base_cf` (L547-572): replace `_irena_defaults` lookup with `constants.DEFAULT_TECH_PARAMS[tech]["capacity_factor"]`.
- `src/core/constants.py`: no change needed (already correct) — this task only removes the second table, doesn't create anything new.

**Success criteria**: `grep -n "_irena_defaults" src/processors/lcoe_calculator.py` returns nothing; the emergency-fallback capacity factor for every technology matches `constants.DEFAULT_TECH_PARAMS` exactly (verifiable by a one-line assertion or a QI-004 unit test).

**Commit message template**:
```
Fix emergency LCOE capacity-factor fallback divergence

lcoe_calculator carried its own IRENA default table that had
drifted from constants.DEFAULT_TECH_PARAMS (0.20/0.30/0.73 vs
0.195/0.274/0.750). Route through the one canonical table.
```

---

### BLOCKER-005 — Consolidate suitability-threshold fallback and add config validation

**Title**: Replace the three independent `0.60` literals with one named constant, and validate `parameters.json`/`build_tech_params()` output at load time so the fallback is provably unreachable in normal operation.

**Current pain**: `potential_calculator.py:439` and `lcoe_calculator.py:592,597` each independently fall back to a bare `0.60` suitability threshold if a scenario key is missing — three literals, two files, no shared source (`docs/code-duplication.md` §3, items #3/#4). None of this is validated at config-load time, so a malformed `parameters.json` entry (missing `thresholds.balanced`, say) is silently patched over with a guessed number instead of failing loudly where the mistake was made. A web platform accepting user-editable country parameters (a very likely feature — "let a researcher tune their own country's assumptions") needs config validation *before* a bad value reaches a calculation, not a silent numeric substitution three layers downstream.

**Depends on**: none.

**Effort**: S-M — the constant consolidation is S; adding real schema validation (Pydantic validators on `CountryParams`/`build_tech_params()` output, or a startup check) is M and overlaps with QI-003's broader validation script.

**Risk**: Low — the constant move is behavior-preserving; validation *additions* only reject configs that were already producing silently-wrong output, which is the point.

**Deliverable**:
- `src/core/constants.py`: add `DEFAULT_SUITABILITY_THRESHOLD_FALLBACK = 0.60` (or fold into `DEFAULT_TECH_PARAMS`).
- `src/processors/potential_calculator.py:439`, `src/processors/lcoe_calculator.py:592,597`: reference the new constant instead of the bare literal.
- `src/core/schemas.py` or `src/core/config_loader.py`: add a validation check (Pydantic field validator or explicit assertion) that every country entry's `thresholds` dict has all three scenario keys before `build_tech_params()` is trusted to have populated them — this is the "with validation" half of the ask, and is the narrow, single-value seed of the broader QI-003 validation script.

**Success criteria**: `grep -rn "0\.60" src/processors/potential_calculator.py src/processors/lcoe_calculator.py` returns zero matches (all references now go through the named constant); a `parameters.json` entry with a missing `thresholds.balanced` key fails config load with a clear error instead of silently defaulting.

**Commit message template**:
```
Consolidate suitability-threshold fallback, validate on load

Three independent 0.60 literals across two files replaced with one
named constant; missing threshold keys in parameters.json now fail
at config load instead of silently substituting a guessed value.
```

---

### BLOCKER-006 — Centralize TIF-discovery logic in `raster_io.py`

**Title**: Replace six near-identical "find upstream TIF by filename variants" methods (`results_writer.py` ×3, `lcoe_calculator.py` ×3) with one `find_raster_by_base_name()` in `src/utils/raster_io.py`, and resolve the TOPSIS/OWA precedence divergence between them.

**Current pain**: `docs/code-duplication.md` §2 confirmed `raster_io.py`'s existing functions don't already solve this (they glob a whole directory as a category, none take a `(tech, country_code)` pair and try ordered candidate filenames). Worse: `results_writer._find_suitability_tif` (L129-134) and `lcoe_calculator._find_suitability_tif` (L401-410) try **opposite candidate orders** (OWA-first vs. TOPSIS-first) for the *same lookup* — confirmed via `lcoe_calculator.py`'s own comment at L386 ("✅ FIX (Grupo C): Priority order changed to prefer TOPSIS over OWA... to match Phase 4 behavior") that patched only its own copy. `write-points-inventory.md` found a fourth call site doing the equivalent lookup inline (`sensitivity_analyzer.py:1781,1868`). A web platform's "get the suitability map for tech X" endpoint cannot have two different answers depending on which internal module happens to serve the request.

**Depends on**: BLOCKER-001, BLOCKER-002, BLOCKER-003 — those tasks delete or shrink the two `results_writer.py` functions (`_enrich_lcoe_stats`, `_recover_supply_curve_from_tif`, `_compute_integrated_area`) that are among the current callers of `_find_lcoe_tif`/`_find_suitable_tif`. Centralizing before that cleanup means editing call sites twice.

**Effort**: M — one new function, ~7 call-site rewrites across 3 files, plus a real decision (not just a code move) on which precedence order (TOPSIS-first or OWA-first) is correct going forward.

**Risk**: Med-High — this is the one blocker task that can change *which raster gets read* for a given tech/country if the TOPSIS/OWA precedence decision changes behavior in either `results_writer.py` or `lcoe_calculator.py` (whichever one's current order turns out to be wrong). Needs domain sign-off (which family — TOPSIS or OWA-balanced — is *supposed* to be authoritative for Phase 6's dominance maps), not just a mechanical merge.

**Deliverable**:
- `src/utils/raster_io.py`: add `find_raster_by_base_name(directory, country_code, tech, patterns, glob_patterns=None) -> Optional[Path]` per the signature proposed in `docs/code-duplication.md` §2.
- `src/processors/results_writer.py`: replace `_find_suitability_tif` (L113-153), `_find_lcoe_tif` (L159-190), `_find_suitable_tif` (L196-212) with calls passing this module's candidate order explicitly.
- `src/processors/lcoe_calculator.py`: replace `_find_potential_suitable_tif` (L262-299), `_find_suitability_tif` (L377-423), `_find_resource_tif` (L425-465) likewise.
- `src/processors/sensitivity_analyzer.py`: replace the inline path construction at L1781, L1868 with the same shared function.
- A written decision (one paragraph in the PR description, not a new doc) on the TOPSIS/OWA precedence question, applied consistently to both former call sites.

**Success criteria**: `grep -rn "def _find.*tif\|glob(f\"\*.*suitab" src/processors/` returns zero matches (all six-plus original implementations gone); `results_writer.py` and `lcoe_calculator.py` select the same suitability TIF for the same tech/country/scenario input, verified by a QI-004 test asserting this explicitly.

**Commit message template**:
```
Centralize upstream-TIF discovery in raster_io

Six independent implementations across results_writer and
lcoe_calculator (plus inline lookups in sensitivity_analyzer) are
replaced with one shared finder. Also resolves a real divergence:
results_writer and lcoe_calculator disagreed on TOPSIS vs. OWA
filename precedence for the same lookup.
```

---

### BLOCKER-007 — Centralize dict-shape adapters in `params_helpers.py`

**Title**: Add `normalize_phase_result()`/`get_scenario_data()`/`get_capacity_gw()`/`get_mean_lcoe()` to `src/utils/params_helpers.py`; delete the three independent "which dict shape did I get" implementations.

**Current pain**: `potential_results`/`lcoe_results` arrive at consumers in at least three shapes (Pydantic model, `{"techs": {...}}` dict, legacy flat dict), and three separate call sites each re-derive their own defensive branching: `results_writer._get_scenario_data` (L1484-1523, two dict shapes only), and `transport_decarbonization_calculator._PotentialView`/`_LCOEView` (L167-297, which additionally handles the Pydantic-model path and, in `_LCOEView.mean_lcoe` alone, **four** distinct dict layouts). This is evidence the upstream shape is genuinely inconsistent, not just inconsistently read. A web platform's Pydantic response models need one predictable input shape to serialize from — not three call sites each guessing independently what they were handed.

**Depends on**: none.

**Effort**: S-M — the new `params_helpers.py` functions are small; the two consumer files need their internals swapped without changing their public method signatures (`_PotentialView.capacity_gw()` etc. can stay as thin wrappers).

**Risk**: Low — this is a behavior-preserving consolidation (same inputs recognized, same outputs returned), not a change to what data means.

**Deliverable**:
- `src/utils/params_helpers.py`: add `normalize_phase_result(result: Any) -> Dict[str, Any]`, `get_scenario_data(result, tech, scenario) -> Dict`, `get_capacity_gw(result, tech, scenario="balanced") -> float`, `get_mean_lcoe(result, tech, fallback=60.0) -> float` per the signatures proposed in `docs/code-duplication.md` §1.
- `src/processors/results_writer.py`: delete `_get_scenario_data` class staticmethod (L1484-1492, pure dead-indirection pass-through) and the module-level function (L1499-1523); replace both call sites with `params_helpers.get_scenario_data()`.
- `src/processors/transport_decarbonization_calculator.py`: `_PotentialView`/`_LCOEView` keep their public API (`capacity_gw()`, `mean_lcoe()`, `normalised_score()`, `as_re_dict()`, `available()`) but delegate the shape-branching internals to the new `params_helpers` functions.

**Success criteria**: `grep -n "_get_scenario_data" src/processors/results_writer.py` returns nothing; `grep -n "isinstance(self._raw, dict)" src/processors/transport_decarbonization_calculator.py` returns nothing (branching moved to `params_helpers.py`); `results_writer.py` and `transport_decarbonization_calculator.py` no longer contain any dict-shape `isinstance`/`hasattr` branching on phase-result objects — **"Phase 6 and Phase 9 no longer contain their own dict-shape detection logic for Phase 4/5 results."**

**Commit message template**:
```
Centralize phase-result shape adapters in params_helpers

results_writer and transport_decarbonization_calculator each
re-derived their own "which dict shape did I get" logic for
potential_results/lcoe_results. Consolidated into one normalizer
plus typed accessors.
```

---

## HIGH-VALUE REFACTORS (improve maintainability, enable testing)

### REFACTOR-001 — Extract `sensitivity_analyzer.py` plotting to `sensitivity_plots.py`

**Title**: Move all `_fig_*`/`_watermark`/`_draw_kpis`/`_sfmt` module-level plotting functions (L692-1196, ~500 lines) out of `sensitivity_analyzer.py` into a new `src/utils/sensitivity_plots.py`, mirroring the existing `abatement_plots.py` pattern.

**Depends on**: none (independent of blockers; coordinate with REFACTOR-004 since both touch `sensitivity_analyzer.py`, but neither blocks the other — they extract disjoint line ranges).

**Effort**: M. **Risk**: Low — mechanical move, functions are already module-level with no `self` dependency.

**Deliverable**: new `src/utils/sensitivity_plots.py` with all 11 plotting functions; `sensitivity_analyzer.py` imports and calls them instead of defining them inline.

**Success criteria**: `sensitivity_analyzer.py` contains zero `def _fig_` definitions; all 8 PNG outputs are byte-identical before/after for a re-run of an already-processed country.

**Commit message template**:
```
Extract sensitivity analysis plotting to sensitivity_plots

Mirrors the abatement_plots.py precedent already established for
Phase 7 — Phase 8's ~500 lines of matplotlib figure code move out
of the processor into a sibling utils module.
```

---

### REFACTOR-002 — Extract `transport_decarbonization_calculator.py` plotting to `transport_plots.py`

**Title**: Move the four `_plot_*` methods (L1528-2005, ~477 lines) to a new `src/utils/transport_plots.py`.

**Depends on**: none.

**Effort**: M. **Risk**: Low.

**Deliverable**: new `src/utils/transport_plots.py`; `transport_decarbonization_calculator.py` calls into it instead of defining the plots as instance methods (functions will need `styler`/data passed explicitly instead of `self`, matching the `abatement_plots.py` "no global state" convention).

**Success criteria**: `transport_decarbonization_calculator.py` contains zero `def _plot_` definitions; all 4 PNG outputs unchanged for a re-run.

**Commit message template**:
```
Extract transport decarbonization plotting to transport_plots

Same extraction pattern as abatement_plots.py / sensitivity_plots.py
— Phase 9's plotting code moves out of the processor class.
```

---

### REFACTOR-003 — Split `data_fetcher.py` by dataset

**Title**: Split the 1474-line `data_fetcher.py` into generic HTTP/retry infra plus per-dataset acquisition modules, largest clusters first.

**Depends on**: none.

**Effort**: L — three new files, careful import-path updates in `data_orchestrator.py` and anywhere else that imports `DataFetcher` methods; network-code changes are inherently harder to verify without live downloads.

**Risk**: Med — this is the one module in this section where "harder to verify without live download tests" is a real constraint (per `docs/arch-misalignments.md` §7), since there's no test fixture standing in for GADM/Copernicus/Overpass endpoints.

**Deliverable**:
- `src/io/http_utils.py`: `_check_endpoint_reachable`, `_check_dns_resolution`, `_tile_bbox`, `_request_with_retry`, `_get_with_retry`, `_safe_extract`.
- `src/io/elevation_fetcher.py`: `download_elevation`, `_download_elevation_copernicus`, `_download_elevation_opentopo`, `_mosaic_and_save` (the largest single cluster, ~457 lines).
- `src/io/osm_fetcher.py`: `_parse_osm_elements`, `_overpass_query`, `_download_osm_features`, `download_osm_grid`, `download_osm_roads` (~268 lines).
- `data_fetcher.py` retains `download_gadm`, `download_land_cover`, `download_worldpop` and re-exports or delegates to the split modules so `data_orchestrator.py`'s existing import surface doesn't need to change in the same commit (stage the internal split before any caller-facing rename).

**Success criteria**: `data_fetcher.py` under 700 lines; a full Phase 0 run for a not-yet-cached country (e.g. a country not in `outputs/`) produces the same raw file tree as before the split.

**Commit message template**:
```
Split data_fetcher into per-dataset acquisition modules

Elevation (DEM) and OSM infrastructure acquisition, plus generic
HTTP retry/DNS/tiling infra, extracted into their own files. GADM,
land cover, and WorldPop remain in data_fetcher.py.
```

---

### REFACTOR-004 — Extract sensitivity SA1-6 methods and split `run()`

**Title**: Move `sa1_oat_weight_sensitivity`...`sa6_potential_sensitivity` (L212-691, ~480 lines) to `src/utils/sensitivity_methods.py`; split `SensitivityAnalyzer.run()` (L1524-2041, ~517 lines) into six `_run_sa1()`...`_run_sa6()` orchestration methods.

**Depends on**: none strictly, but do **after** REFACTOR-001 (plotting extraction) in the same file to avoid two large simultaneous diffs to `sensitivity_analyzer.py` colliding.

**Effort**: L — the `run()` split in particular requires understanding shared setup/state across all six sub-analyses well enough to factor it out correctly, not just cut-and-paste.

**Risk**: Med — six statistically distinct methods, each iterating countries/technologies; a mis-split could silently drop a code path only exercised by one specific `run_sa*` toggle combination in `settings.yaml`.

**Deliverable**: new `src/utils/sensitivity_methods.py` (mirrors the `ahp.py`/`topsis.py`/`owa.py` extraction precedent); `SensitivityAnalyzer.run()` reduced to a sequencer calling `_run_sa1()` through `_run_sa6()`, each of which assembles inputs, calls the extracted method, calls the (already-extracted, per REFACTOR-001) plot function, and collects results.

**Success criteria**: `run()` is under 100 lines; each `_run_sa{N}()` method is independently callable/testable; all `settings.yaml` `run_sa1`...`run_sa6` toggle combinations produce identical output to pre-refactor.

**Commit message template**:
```
Extract SA1-6 methods, split SensitivityAnalyzer.run() by sub-analysis

Statistical methods move to sensitivity_methods.py, following the
ahp/topsis/owa precedent. run() becomes a sequencer over six
independently orchestrated sub-analyses instead of one 517-line method.
```

---

### REFACTOR-005 — Migrate Phase 4/5/6 raw raster writes to `safe_raster_write()`

**Title**: Replace the three raw `rasterio.open(path, "w", **profile)` call sites with the existing `safe_raster_write()` context manager.

**Depends on**: BLOCKER-001, BLOCKER-002, BLOCKER-003 (soft — those tasks edit the same functions in `lcoe_calculator.py`/`potential_calculator.py`/`results_writer.py`; sequence after to avoid rebasing the same lines twice).

**Effort**: S. **Risk**: Low — `safe_raster_write` already defaults to the same `compress="lzw"` these call sites set explicitly; only `tiled`, `blockxsize`/`blockysize`, and `predictor` settings need reconciling across the three (`docs/code-duplication.md` §4a documents each site's current kwargs).

**Deliverable**: `potential_calculator.py:495-511`, `lcoe_calculator.py:1209-1224`, `results_writer.py:1353-1365` (`_write_uint8`) rewritten to use `with safe_raster_write(path, **profile) as dst:`, matching the pattern already used in `grid_aligner.py`/`criteria_builder.py`/`suitability_builder.py`.

**Success criteria**: `grep -rn "rasterio.open(" src/processors | grep -v safe_raster` returns zero matches in these three files (Phase 0's `data_fetcher.py` is explicitly out of scope for this task, lower priority per `docs/code-duplication.md`).

**Commit message template**:
```
Migrate Phase 4/5/6 raster writes to safe_raster_write

Three independent inline rasterio.open("w", **profile) blocks
replaced with the shared context manager already used by Phases
2a/2b/3, normalizing compression/tiling settings across phases.
```

---

### REFACTOR-006 — Resolve `ArtifactManager` double-persistence

**Title**: Decide whether phase-level `ArtifactManager` calls or the orchestrator's automatic `_persist()` is authoritative; remove the redundant one from the 5 phases that currently do both, and delete `lcoe_calculator.py`'s dead import.

**Depends on**: soft dependency on BLOCKER-001/002/003 for `results_writer.py`/`lcoe_calculator.py` (same files, avoid conflicting edits); otherwise independent.

**Effort**: S-M — mostly deletion, but requires actually reading `pipeline_orchestrator.py::run_phase()`'s `_persist()` call (L165, unconditional for every phase) closely enough to confirm nothing phase-specific is lost by removing the internal calls.

**Risk**: Med — touches the one mechanism every phase's caching/skip-resume behavior depends on; a mistake here could silently break `skip_*: true` resume logic for whichever phase is edited.

**Deliverable**: pick one of two directions and apply it uniformly — (a) remove the phase-level `ArtifactManager` calls in `results_writer.py` (L833/850/858), `suitability_builder.py` (L529/537), `sensitivity_analyzer.py` (L2006/2011), `ghg_abatement_calculator.py` (L912/920), `transport_decarbonization_calculator.py` (L583/596), relying solely on the orchestrator; or (b) make the orchestrator's `_persist()` conditional on the phase not having already persisted. Either way, delete the unused `from src.io.artifact_manager import ArtifactManager` in `lcoe_calculator.py:59`.

**Success criteria**: every phase's result is persisted exactly once per run (verifiable by a temporary log line counting `ArtifactManager.save_result` calls during a full pipeline run, removed after verification); `grep -n "ArtifactManager" src/processors/lcoe_calculator.py` returns nothing.

**Commit message template**:
```
Resolve duplicate phase-result persistence

Five phases called ArtifactManager themselves in addition to the
orchestrator's automatic persistence step, doing the same pickle/
JSON write twice per run. Also removes lcoe_calculator's dead
ArtifactManager import.
```

---

### REFACTOR-007 — Complete the `results_writer.py` → `dashboard_panels.py`/`map_styling.py` extraction

**Title**: Finish the dashboard-panel extraction that `DashboardPanels` only partially completed — move `_plot_executive_dashboard`, `_build_dashboard_layout`, `_draw_dominance_on_ax` (L1097-1321, ~225 lines) to `src/visualization/dashboard_panels.py`, and `_build_rgba` (L941-990) to `src/utils/map_styling.py` as a `GeoWorldStyler` method.

**Depends on**: BLOCKER-001, BLOCKER-002, BLOCKER-003 (those remove `_enrich_lcoe_stats`/`_recover_supply_curve_from_tif`/`_compute_integrated_area` from the same file first, so this task's diff isn't fighting three other simultaneous edits to `results_writer.py`).

**Effort**: M. **Risk**: Low-Med — mechanical move, but the dashboard layout logic has several interdependent axes/gridspec calls that need to move as a coherent unit, not line-by-line.

**Deliverable**: `DashboardPanels` gains the dominance-map panel and the overall layout/assembly method, matching what its own module docstring already claims was extracted; `GeoWorldStyler` gains `build_dominance_overlay_rgba()`; `results_writer.py` shrinks by ~275 lines, calling into both instead of implementing them inline.

**Success criteria**: `results_writer.py` contains no `plt.figure`/`gridspec` calls of its own; the executive dashboard PNG is unchanged for a re-run.

**Commit message template**:
```
Move dashboard assembly and RGBA overlay out of results_writer

Completes the DashboardPanels extraction — the dominance-map panel
and overall dashboard layout were the two panels left behind when
DashboardPanels was created. RGBA overlay building moves to
GeoWorldStyler, matching where map-rendering logic belongs.
```

---

### REFACTOR-008 — Split `map_styling.py`'s PIL compositing and decoration helpers

**Title**: Extract `create_comparison_via_pil()` (L1334-1480, ~146 lines) to `src/utils/map_composite.py`, and `_draw_compass_rose()`/`_draw_segmented_scalebar()` (L823-1010, ~187 lines) to `src/utils/map_decorations.py`.

**Depends on**: coordinate with REFACTOR-007 (both touch `map_styling.py` — REFACTOR-007 adds a method, this task splits the file; doing REFACTOR-007 first means the split accounts for the new method's placement).

**Effort**: M. **Risk**: Low — `create_comparison_via_pil` uses a genuinely different rendering technology (PIL, not matplotlib) from the rest of the class, and the compass rose/scalebar functions don't depend on instance state beyond styling config passed as parameters — both are clean seams.

**Deliverable**: two new files; `GeoWorldStyler` shrinks to figure lifecycle + basemap + colormap + the two high-level entry points (`render_raster_map`, and a thin call into the new `map_composite.py` for `create_comparison_via_pil`).

**Success criteria**: `map_styling.py` under 1200 lines; every phase currently calling `styler.create_comparison_via_pil(...)` continues to work unchanged (thin delegating method kept on `GeoWorldStyler` for API compatibility, or all call sites updated in the same commit — pick one and note which in the PR).

**Commit message template**:
```
Split PIL compositing and map decorations out of map_styling

create_comparison_via_pil (PIL-based, distinct rendering path) and
the compass rose/scale bar drawers (self-contained, no instance
state) move to their own modules.
```

---

### REFACTOR-009 — Extract `transport_decarbonization_calculator.py`'s hub-siting logic

**Title**: Move `_place_charging_hubs` (L1083-1352, ~269 lines) to its own module.

**Depends on**: none.

**Effort**: M. **Risk**: Low — self-contained spatial-siting logic, distinct in kind from the time-series fleet/emissions/cost math around it.

**Deliverable**: new `src/utils/hub_siting.py` (or `src/processors/transport_hub_siting.py` if it's judged closer to a sub-phase than a utility); `TransportDecarbonizationCalculator` calls into it instead of implementing siting inline.

**Success criteria**: hub CSV output unchanged for a re-run; `_place_charging_hubs` no longer appears in `transport_decarbonization_calculator.py`.

**Commit message template**:
```
Extract charging-hub siting from transport calculator

Spatial suitability-threshold siting logic is a distinct concern
from the time-series fleet/emissions/cost modeling that dominates
the rest of the module.
```

---

### REFACTOR-010 — Move remaining Phase 3-6 hardcoded config-adjacent values

**Title**: Sweep the remaining lower-severity hardcoded values identified in `docs/code-duplication.md` §3 (items #1, #6, #7, #8, #9, #10, #11, #12, #13 — everything except the two promoted to BLOCKER-004/005) into `settings.yaml`/`parameters.json`.

**Depends on**: coordinate with BLOCKER-005 — both touch `constants.py`/`settings.yaml` for different constants; sequence BLOCKER-005 first since it also adds the validation hook this task's new config keys should be checked by.

**Effort**: S — each individual item is a literal-to-config-key move; bundled into one task because they're mechanically identical in shape, not because they're related in meaning.

**Risk**: Low — all additive/config-driven, no computation logic changes.

**Deliverable**, one bullet per item:
- `suitability_builder.py:93-97` `_SLOPE_OFFSET_DEG` → `parameters.json` top-level `slope_offset_deg` block.
- `lcoe_calculator.py:113` `_MIN_RESOURCE_COVERAGE` → `settings.yaml` (data-quality gate category, alongside `audit.resolution_tolerance`).
- `lcoe_calculator.py:1067,1069` biomass CV threshold (`0.01`, currently unnamed) → named constant, then `settings.yaml` alongside the item above.
- `results_writer.py:878,879,905` dominance/competition thresholds (`competition_delta`, `min_score`, `competition_delta_usd`) → `settings.yaml` under a new `visualization.dominance` block.
- `results_writer.py:1056` — rebuild the `"Competition Zone (ΔTOPSIS < 0.10)"` legend label from the same config value instead of a separately-typed string.
- `results_writer.py:417` supply-curve proxy assumption → named constant `_SUPPLY_CURVE_PROXY_MW_PER_PIXEL` (moot if BLOCKER-001 removes this function entirely first — check before doing this line item).
- `results_writer.py:424-426` supply-curve downsampling cap (`5000`) → `settings.yaml` `pipeline.supply_curve_max_points` (optional, lowest priority in this already-low-priority task).

**Success criteria**: every value in `docs/code-duplication.md` §3's table (minus #3/#4/#5, covered by BLOCKER-004/005) is either a named constant or a config key, none are bare literals in the processor files.

**Commit message template**:
```
Move remaining Phase 3-6 hardcoded values to config

Slope offsets, data-quality gates, and dominance-map thresholds
promoted from bare literals to parameters.json/settings.yaml.
```

---

## QUALITY INFRASTRUCTURE (reduce regression risk)

No test suite exists anywhere in this repository (`docs/memory/06-risk-areas.md`) — every task above is currently verified only by manual before/after diffing of `outputs/{ISO3}/`. This section is what makes that verification repeatable instead of ad hoc.

### QI-001 — `tests/utils/` pytest suite for the pure-math utilities

**Title**: Unit tests for `ahp.py`, `topsis.py`, `owa.py`, `exclusion.py`, `economics.py`, `normalization.py`.

**Depends on**: none — these six modules are untouched by every blocker/refactor above, so this can start immediately and in parallel with everything else.

**Effort**: M-L (six modules, each needs several cases: normal input, edge cases like all-zero weights or single-criterion AHP matrices, known-answer tests against the cited papers' worked examples where feasible).

**Risk**: Low.

**Deliverable**: `tests/utils/test_ahp.py`, `test_topsis.py`, `test_owa.py`, `test_exclusion.py`, `test_economics.py`, `test_normalization.py`, plus a `tests/utils/__init__.py` and a root `pytest.ini`/`pyproject.toml` `[tool.pytest]` section (none exists yet). Each file covers: normal-case correctness, at least one edge case, and — for `ahp.py` — a consistency-ratio check against Saaty's published example matrices if practical.

**Success criteria**: `pytest tests/utils/` runs and passes; each of the six modules has at least 70% line coverage (informal target, no coverage tool currently configured — adding one, e.g. `pytest-cov`, is in scope for this task).

**Commit message template**:
```
Add unit tests for MCDA and economics utilities

First test coverage in the repository. Covers ahp, topsis, owa,
exclusion, economics, and normalization — the pure-math layer with
no I/O dependencies, chosen as the highest-value/lowest-effort
starting point.
```

---

### QI-002 — `tests/integration/` synthetic-country end-to-end test

**Title**: Build a minimal synthetic country (small fabricated GeoTIFFs, a tiny GADM boundary) and run the full nine-phase pipeline against it, asserting known-correct output values.

**Depends on**: BLOCKER-001, BLOCKER-002, BLOCKER-003 — building "known correct values" now, then re-baselining them immediately after those three land, would be wasted work; do this after so the baseline reflects the corrected data flow.

**Effort**: L — the hard part isn't the test runner, it's constructing synthetic inputs small enough to run in CI/locally in seconds but realistic enough to exercise every phase's code paths (nodata handling, multi-technology exclusion overlap, non-trivial AHP weight matrices).

**Risk**: Med — a synthetic fixture that's *too* simple (e.g., uniform rasters) can pass while missing real bugs (e.g., anything that only manifests with actual spatial heterogeneity); needs deliberate design, not just "make it small."

**Deliverable**: `tests/integration/fixtures/` (synthetic raw data for one fake country, small enough to commit), `tests/integration/test_full_pipeline.py` running `main.py`'s equivalent entry point against it end-to-end and asserting specific numeric outputs (capacity GW, LCOE range, dominance-map pixel counts) within a tolerance.

**Success criteria**: `pytest tests/integration/` completes in under a few minutes on a normal laptop; a deliberately-introduced regression (e.g., reverting BLOCKER-004's fix) causes a test failure.

**Commit message template**:
```
Add synthetic-country integration test

First end-to-end test covering all nine phases against fabricated
minimal inputs, with known-correct output assertions.
```

---

### QI-003 — Startup config/schema validation script

**Title**: A validation pass that runs before `main.py` starts the pipeline, checking `parameters.json`/`settings.yaml`/`transport_parameters.json`/`net_zero_db.json` for structural completeness and cross-file consistency.

**Depends on**: BLOCKER-004 (one canonical capacity-factor table to validate against, not two disagreeing ones), BLOCKER-005 (the narrow per-country-threshold validator this task generalizes).

**Effort**: M. **Risk**: Low.

**Deliverable**: `src/core/config_validator.py` (or extend `config_loader.py`) with checks: every configured country has all required `parameters.json` sub-keys (`solar`, `wind`, `biomass`, `owa`, `lcoe`, `abatement`, `slope_threshold_deg` per `README.md` §10's own documented requirement list); OWA weight vectors sum to 1.0 and are non-increasing; `settings.yaml` contains no leftover scientific-parameter keys (a runtime guard for the `07-configuration.md`-documented separation rule, currently convention-only per `docs/memory/06-risk-areas.md`). Wired into `main.py` to run and fail fast before Phase 0 starts.

**Success criteria**: running the pipeline against a deliberately-malformed `parameters.json` (missing a required sub-key, or an OWA vector summing to 0.9) fails immediately with a specific, actionable error instead of failing deep inside some later phase or silently producing wrong numbers.

**Commit message template**:
```
Add startup config validation

Checks parameters.json/settings.yaml structural completeness and
cross-file consistency before the pipeline starts, instead of
failing deep inside a phase or silently defaulting.
```

---

### QI-004 — Unit tests for the shared utilities introduced by BLOCKER-003/006/007

**Title**: Test coverage for the new `raster_io.find_raster_by_base_name()`, `params_helpers.normalize_phase_result()`/accessors, and `data_recovery.py`'s updated area-recovery path.

**Depends on**: BLOCKER-003, BLOCKER-006, BLOCKER-007 (tests the code those tasks create — can't exist before them).

**Effort**: S-M. **Risk**: Low.

**Deliverable**: `tests/utils/test_raster_io.py` (candidate-order precedence, glob fallback, not-found case), `tests/utils/test_params_helpers.py` (all three input shapes — Pydantic model, `{"techs": ...}` dict, legacy dict — resolve to the same accessor output).

**Success criteria**: the exact TOPSIS/OWA precedence decision made in BLOCKER-006 is pinned by a test, so a future edit can't silently reintroduce the divergence that motivated that task.

**Commit message template**:
```
Add tests for centralized TIF-finder and result-shape adapters

Pins the TOPSIS/OWA precedence decision from the raster_io
consolidation and verifies params_helpers resolves all three known
result shapes identically.
```

---

## QUICK WINS 🎯

S effort, Low risk, high value relative to effort. All reference tasks already defined above — this is a curated subset, not new work.

- 🎯 **BLOCKER-004** — fix the divergent `_irena_defaults` capacity-factor table. One dict deleted, one lookup redirected; closes a silent correctness bug with the highest severity-to-effort ratio in the entire roadmap.
- 🎯 **BLOCKER-002** — persist LCOE percentile stats. Three more `np.percentile` calls next to three that already exist.
- 🎯 **BLOCKER-007** — centralize dict-shape adapters into `params_helpers.py`. Removes ~90 duplicated lines across two files.
- 🎯 **REFACTOR-005** — migrate Phase 4/5/6's three raw `rasterio.open()` writes to `safe_raster_write()`. Mechanical, and the target utility already exists.
- 🎯 **REFACTOR-008** (PIL-compositing half only) — extract the ~146-line `create_comparison_via_pil()` from `map_styling.py` into `map_composite.py`. Clean seam, distinct rendering technology (PIL vs. matplotlib) from the rest of the class.
- 🎯 Delete `results_writer.py`'s dead `_get_scenario_data` `@staticmethod` wrapper (L1484-1492) — a 9-line pure pass-through to the module-level function two lines away. Folded into BLOCKER-007's deliverable but worth doing standalone first if BLOCKER-007 is delayed — it's zero-risk on its own.
- 🎯 Remove `lcoe_calculator.py`'s dead `ArtifactManager` import (L59) — one line, zero behavior change, currently just confusing dead code. Folded into REFACTOR-006 but trivial enough to do immediately.
- 🎯 **BLOCKER-005** (constant consolidation half only, without the validation addition) — replace the three `0.60` literals with one named constant. The validation-hook half of BLOCKER-005 is genuinely M effort; the literal consolidation alone is S.

---

## Dependency graph

```
BLOCKER-001 ─┐
BLOCKER-002 ─┼──► BLOCKER-006 (TIF-discovery centralization)
BLOCKER-003 ─┤    │
             │    └──► QI-004 (tests for BLOCKER-006's output)
             │
             ├──► REFACTOR-005 (safe_raster_write migration)
             ├──► REFACTOR-006 (double-persistence fix)   [also independent]
             ├──► REFACTOR-007 (results_writer → dashboard_panels/map_styling)
             │         │
             │         └──► REFACTOR-008 (map_styling split)
             │
             └──► QI-002 (synthetic-country integration test)

BLOCKER-004 ─┬──► QI-003 (startup validation)
BLOCKER-005 ─┘         │
    │                  └── (also feeds REFACTOR-010, soft)
    └──► REFACTOR-010 (remaining hardcoded values)

BLOCKER-007 ──► QI-004
            └─► REFACTOR-006 (soft, same files)

REFACTOR-001 (sensitivity plotting) ─┐
                                      ├─ same file, coordinate, no hard order
REFACTOR-004 (sensitivity SA-split) ─┘

REFACTOR-002 (transport plotting)     — independent
REFACTOR-003 (data_fetcher split)     — independent
REFACTOR-009 (hub siting extraction)  — independent

QI-001 (pure-math unit tests)         — independent, start anytime
```

Everything with no incoming arrow (BLOCKER-004, BLOCKER-005, BLOCKER-007, REFACTOR-001/002/003/004/009, QI-001) can start on day one in parallel if more than one person/session is available. Everything downstream of BLOCKER-001/002/003 should wait for those three, in the order given, before starting.

---

## Effort summary

Rough T-shirt-size counts, converted to day-ranges purely for planning-order intuition (S ≈ 0.5-1 day, M ≈ 2-4 days, L ≈ 5-8 days) — **not a committed estimate**, since none of this codebase has test coverage yet to make "done" a fast, confident checkpoint.

| Group | S | M | L | Rough total |
| --- | --- | --- | --- | --- |
| BLOCKERS (7 tasks) | 5 | 2 | 0 | ~10 days |
| HIGH-VALUE REFACTORS (10 tasks) | 3 | 5 | 2 | ~30 days |
| QUALITY INFRASTRUCTURE (4 tasks) | 1 | 2 | 1 | ~13 days |
| **Total** | **9** | **9** | **3** | **~53 days** (~10-11 weeks solo) |

---

## Recommended execution order, with checkpoints

1. **Week 1 — Blockers, parallelizable**: BLOCKER-004, BLOCKER-005, BLOCKER-007 can start immediately and in parallel (no shared files, no dependencies). Start QI-001 (pure-math tests) in parallel too — it touches none of the files the blockers touch.
2. **Week 1-2 — Blockers, sequenced**: BLOCKER-001 → BLOCKER-002 → BLOCKER-003 (same-area work in `lcoe_calculator.py`/`potential_calculator.py`, sequence to avoid rebasing).
3. **✅ CHECKPOINT 1 — before touching `results_writer.py`'s remaining structure or running any new country**: all seven BLOCKER tasks complete. Re-run the full pipeline for at least one already-processed country (PRT, per the existing `outputs/PRT/` baseline) and diff every output file against the pre-refactor version. This is the gate `write-points-inventory.md` already flagged as necessary — do not skip it. Do not start processing a genuinely new country, and do not start web-platform integration work, before this checkpoint passes.
4. **Week 2-3 — TIF-discovery and dependent cleanup**: BLOCKER-006 (now that 001-003 have shrunk its call sites), then QI-004 (tests pinning BLOCKER-006's precedence decision), then REFACTOR-005/006/007 (raster-write migration, double-persistence fix, dashboard extraction — all touch the same now-stabilized files).
5. **Week 3-4 — Remaining structural refactors, parallelizable**: REFACTOR-001/002/003/004/008/009/010 have no interdependencies with each other beyond the soft same-file coordination notes above — split across however many sessions/people are available.
6. **✅ CHECKPOINT 2 — before declaring the codebase "web-platform ready"**: QI-002 (synthetic-country integration test) passing, QI-003 (startup validation) wired into `main.py` and rejecting a deliberately-malformed config. At this point every BLOCKER item is resolved, the highest-value REFACTOR items are done, and there is for the first time an automated way to catch a regression instead of relying on manual `outputs/` diffing.
7. **Ongoing**: the remaining REFACTOR/QI items not yet done by Checkpoint 2 (likely `abatement_plots.py`'s optional subpackage split, mentioned in `docs/arch-misalignments.md` as explicitly low-priority, and any QI test-coverage gaps) are backlog, not blockers to shipping.

No code was changed in this pass — this document is planning only, per the task's instruction.
