# Refactoring Roadmap

Originally: planning only, synthesizing `docs/arch-misalignments.md`, `docs/code-duplication.md`, and `docs/write-points-inventory.md` into an ordered task list. Since expanded into the canonical, single-source-of-truth BLOCKER tracker for this project (BLOCKER-001 through BLOCKER-016, both fixed and open) and the home for the technical-decisions log formerly at `docs/memory/09-decisions.md` (see the Appendix at the end of this file — moved here verbatim, not rewritten, so its historical Context/Decision/Consequences entries stay intact). `docs/memory/09-decisions.md` is now a redirect stub pointing here.

Entries below marked with a `**Status**` line are retrospective (already fixed, describing what actually shipped); entries without one are still prospective plans as originally written and have not been re-verified against what (if anything) has shipped since — check `git log`/the relevant module before trusting a "Deliverable" section at face value.

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

**Update (2026-08-18)**: the raw-dict `country_params` input path this entry was concerned about no longer exists — removed in commit `de87ceb` ("Remove unused raw-dict input path for country_params"). `build_tech_params()` (`constants.py`), `extract_params_dict()` (`params_helpers.py`), and the call sites this deliverable named (plus `suitability_builder.py`'s `get_technology_configs()`/`SuitabilityBuilder.run()`, which turned out to have the identical pattern) now require a validated `CountryParams` instance and raise `TypeError` with the actual type received on anything else. A raw dict can no longer reach these functions at all — repo-wide search confirmed zero call sites in `main.py`, `tests/`, `scripts/`, or `scratchpad/` ever passed one.

This narrows but does **not** close BLOCKER-005. The removal closes the specific *unvalidated-input* vector; it does not add the load-time validator this entry's deliverable actually asks for ("a validation check that every country entry's `thresholds` dict has all three scenario keys"). Checked explicitly against the current committed `src/core/schemas.py`: **no such guarantee exists there, and never has** — `TechParams.threshold` (`schemas.py:151`) is a single required, range-checked scalar (`Field(..., ge=0.0, le=1.0)`) per technology; there is no `thresholds` (plural, three-scenario-key) field or model anywhere in `schemas.py` (`grep -n "thresholds" src/core/schemas.py` → zero matches). The three-key `{optimistic, balanced, conservative}` dict this entry is actually about is assembled procedurally inside `build_tech_params()` (`constants.py:339-388`): it starts from `copy.deepcopy(DEFAULT_TECH_PARAMS)` (all three keys always present, line 343), patches only `thresholds.balanced` from the validated scalar, then unconditionally recomputes `optimistic`/`conservative` from `balanced ± settings.yaml offset` on every call (lines 375-381). So the three-key completeness this entry asks to validate is still guaranteed only by `build_tech_params()`'s own current implementation shape — exactly what QI-003's Fragility note already found — not by any schema. Remaining scope: add the actual output/load-time validator (or a Pydantic model for the resolved three-scenario thresholds), or explicitly accept this as a documented structural invariant to be re-verified if `build_tech_params()` is ever refactored.

(Separately, and not addressed by this update: the constant-consolidation half of this entry's deliverable — replacing the bare `0.60` literals — was already complete going into this session; `FALLBACK_SUITABILITY_THRESHOLD` (`constants.py:241`) is the single named constant in use in both files. This entry's own citation of `potential_calculator.py:439` still matches current code; its citation of `lcoe_calculator.py:592,597` does not — current usage is at lines 545/547/552.)

**Related**: INVAR-007 (Invariant Validation Project, 2026-08-18 audit — see the "Invariant Validation Project" section near the end of this file) tracks the same `potential_calculator.py:439` fallback site from the broader 16-item invariant-gap catalogue. This cross-reference does **not** close BLOCKER-005 — the remaining open scope (the load-time three-scenario-key validator described in the Update above) stands as written.

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

### BLOCKER-008 — `cf_renewable` structurally inert in SA-5 Sobol GHG function

**Status**: Fixed — commit `79b962a`.

**Title**: Make `cf_renewable` actually influence `sensitivity_analyzer.py::_build_ghg_function_from_abatement()`'s `ghg_function()` return value.

**Current pain (as found)**: `ghg_function()` accepted `cf_renewable` as one of four Sobol-perturbed parameters but never read it in the return computation, so its total-order Sobol index was forced to exactly `0.0` regardless of true physical sensitivity — a scientifically invalid result being reported as if it were a real finding. Portugal's SA-5 report showed `penetration_factor` as dominant at `ST=0.471` partly *because* `cf_renewable` was structurally prevented from ever registering any sensitivity, not because it was genuinely less influential.

**Depends on**: none.

**Effort**: S — one function, one added ceiling term.

**Risk**: Med — changes a reported scientific result (SA-5 dominant-parameter ranking), not just code structure; required domain reasoning about how capacity factor should physically constrain substitution in the real Phase 7 model before writing the fix, not just a mechanical patch.

**Root cause**: In the real Phase 7 model (`ghg_abatement_calculator.py`), capacity factor does not scale the substitution target directly — it constrains the *ceiling* of available renewable generation via `renew_total_gwh` (built from Phase 4's `capacity_mw x capacity_factor x hours_year`), which caps `subst_gwh = min(gwh_to_add, total_th_gwh, renew_total_gwh)`. The Sobol proxy function had never reproduced that capping structure.

**Deliverable (as shipped)**: `ghg_function()` now computes an `available_gwh_sub` ceiling that scales linearly with `cf_renewable` relative to the function's baseline reference value (`cf_base = 0.25`), applied via `gwh_sub = min(target_gwh_sub, available_gwh_sub)` — mirroring the same capping logic as the real model. Verified regression-safe at the unperturbed baseline (output unchanged when `cf_renewable = cf_base`).

**Success criteria (as validated)**: Portugal, Phase 8 SA-5 only (rest cached) — Sobol total-order indices (ST), before → after:

| Parameter | Before | After |
| --- | --- | --- |
| `ef_thermal_gco2_kwh` | 0.394 | **0.441 (now dominant)** |
| `penetration_factor` | 0.471 (previously dominant — an artifact of the bug) | 0.242 |
| `ef_lifecycle_gco2_kwh` | 0.142 | 0.159 |
| `cf_renewable` | 0.000 | 0.244 |

**Commit message** (as shipped):
```
Fix cf_renewable being structurally inert in SA-5 Sobol GHG function
```

---

### BLOCKER-009 — LCOE mean/p10/p90/n_pixels also sourced from region-mean CSV (BLOCKER-002 follow-up)

**Status**: Fixed — commit `7926ec3`.

**Title**: Extend BLOCKER-002's pixel-level TIF overlay to `mean`/`p10`/`p90`/`n_pixels`, not just `p25`/`median`/`p75`.

**Current pain (as found)**: BLOCKER-002 fixed `p25`/`median`/`p75` by overlaying pixel-level TIF stats onto CSV-sourced stats whenever the zonal CSV won Priority 1 recovery, but deliberately left `mean`/`p10`/`p90`/`n_pixels` as the CSV provided them, since fixing quartiles didn't require touching those fields at the time. Checking whether `mean` specifically is printed anywhere citable (the same check BLOCKER-002 did for the quartiles) found that unlike the quartiles — which only ever feed an IQR box-and-whisker plot — LCOE `mean` **is** printed as text: `results_writer.py`'s "BALANCED SCENARIO — INTEGRATED SUMMARY" table in the Phase 6 text report prints `stats["mean"]` directly in its $/MWh column. Portugal's report showed solar at 48.7 USD/MWh (the region-mean-of-means value) where Phase 5's live pixel-level computation gives 43.8 — a real, previously-unnoticed discrepancy in a citable, published-looking figure.

**Depends on**: BLOCKER-002 (extends the same overlay mechanism in the same function).

**Effort**: S — same overlay mechanism already built for BLOCKER-002, extended to replace the complete stats dict instead of three fields.

**Risk**: Med — changes reported LCOE figures for every technology/country whenever the CSV-recovery path is exercised — i.e., whenever Phase 6 runs against disk-recovered rather than live in-process LCOE results, which per BLOCKER-010's investigation is *every* run in current production usage.

**Deliverable (as shipped)**: `data_recovery.py::recover_lcoe_from_disk()`'s overlay block, when the CSV won Priority 1 recovery, now replaces the **complete** stats dict (`mean`, `p10`, `p25`, `median`, `p75`, `p90`, `n_pixels`) with TIF-derived pixel-level values, rather than leaving a dict with some pixel-level and some region-level fields. The overlay marker was renamed from `quartile_source` to `stats_source` to reflect the wider scope.

**Success criteria (as validated)**: Portugal, Phase 6 only (rest cached) — report's $/MWh column, before → after:

| Tech | Before | After | Δ |
| --- | --- | --- | --- |
| Solar | 48.7 | 43.8 | −10.1% |
| Wind | 59.2 | 57.7 | −2.5% |
| Biomass | 61.4 | 61.2 | −0.3% |

Full stats dict, region-mean (old) vs. pixel-level (new): solar mean 48.70→43.84, p10 41.93→39.53, p90 57.44→48.84; wind mean 59.17→57.68, p10 54.80→53.02, p90 66.13→65.94; biomass mean 61.36→61.24, p10 61.02→60.20, p90 62.00→64.31. `results/result.pkl` unchanged (0 diffs) — same as BLOCKER-002, these values are not persisted there, only printed in the text report and rendered in the executive dashboard.

**Commit message** (as shipped):
```
Fix LCOE mean/p10/p90/n_pixels also sourced from region-mean CSV
```

---

### BLOCKER-010 — Phase 6 always reconstructs Potential/LCOE results from disk, live object never used

**Status**: Not fixed. Scoping investigation complete (2026-08-18, see Update below) — root cause, blast radius, and fix classification confirmed by direct code/git inspection. Still requires a separate, dedicated session to implement; no code changed yet.

**Title**: `main.py` always resolves `potential_dir`/`lcoe_dir` to a disk `Path` before calling `ResultsWriter.run()`, so its already-correct "use the live object if given one" branch is permanently dead code in production.

**Current pain**: Discovered during the CHECKPOINT-2 module analysis (`docs/analysis-results_writer.md` §1a). `main.py:806-809` carries a `# BUG_07 (fix)` comment documenting that `lcoe_result` (a live, validated `LCOEResult` Pydantic model) used to be passed where a directory `Path` was expected, causing a type-mismatch failure; the fix applied was to always resolve to the canonical output directory instead, regardless of whether the live object is available. The same pattern applies to `potential_dir`. `ResultsWriter._normalize_potential`/`_normalize_lcoe` already contain the correct dual-mode logic (`if isinstance(data, dict) and "techs" in data: return data`) — but since `main.py` never passes anything but a `Path`, that branch never executes; every Phase 6 run reconstructs both Potential and LCOE results from disk via `recover_potential_from_disk`/`recover_lcoe_from_disk`, unconditionally.

This is not a theoretical risk: `data_recovery.py`'s own comments document a real historical numeric divergence caused by exactly this pattern (LCOE mean 48.7 vs. 43.8 USD/MWh for solar — see BLOCKER-009 above), patched at the specific symptom rather than the architectural cause.

**Depends on**: none technically, but touches the same `main.py` Phase 4-6 handoff and `ResultsWriter._normalize_*` methods that BLOCKER-007 (dict-shape adapters) also touches — sequence together to avoid re-touching the same lines twice.

**Effort**: M — `main.py` needs to pass `pot_result.model_dump()`/`lcoe_result.model_dump()` when the live object is available (not the bare Pydantic model — confirmed via `src/core/validators.py::validate_result_object()` that `pipeline_orchestrator.run_phase()` returns an actual model instance, which would still fail `_normalize_*`'s `isinstance(data, dict)` check as-is). Needs care around what "available" means given the orchestrator's skip/cache semantics (skip=True+cache → cached object still available and usable; skip=True+no cache → `None`, genuine fallback to disk needed).

**Risk**: Med-High — changes which code path computes the LCOE/Potential numbers Phase 6 reports for every run; needs full CHECKPOINT-style before/after validation, not just a mechanical change.

**Deliverable** (when picked up):
- `main.py`: pass `pot_result.model_dump()` / `lcoe_result.model_dump()` to `ResultsWriter.run()`'s `potential_dir`/`lcoe_dir` parameters when non-`None`, falling back to the directory `Path` only when the live object is genuinely unavailable (skip+no-cache case).
- `src/processors/results_writer.py::_normalize_potential`/`_normalize_lcoe`: no change needed — the dual-mode logic already exists and is correct, it just needs to actually be exercised.
- Full pipeline validation comparing live-object-path output against the current always-disk-recovery output, expecting the live path to be *more* accurate where they diverge (per BLOCKER-009's finding), not just numerically identical.

**Success criteria**: a live pipeline run's Phase 6 report reads pixel-level LCOE stats directly from the in-memory `LCOEResult`, verifiable by a log line or debug marker distinguishing "used live object" from "recovered from disk"; `recover_potential_from_disk`/`recover_lcoe_from_disk` are only invoked when `skip=True` and no live object exists.

**Update (2026-08-18)** — isolated scoping investigation, no code changed. All items below confirmed by direct code/git reading, not inference:

- **`main.py` wiring**: confirmed the only omission point. `pot_result`/`lcoe_result` (`main.py` ~750/~774) stay in scope and unconsumed all the way to the Phase 6 `input_getter` (~812-826), which resolves `potential_dir`/`lcoe_dir` to `Path` unconditionally — no branch exists. The same live objects are passed directly to Phases 8/9 (`main.py` ~866-867, ~899-900) a few lines later, proving this is a Phase-6-only wiring gap, not a Phase 4/5 exposure limitation.
- **BUG_07 root cause — git archaeology**: `git log -p --all -S "BUG_07" -- main.py` matches exactly one commit, `153a1cc` ("Snapshot working tree as a restore point before cleanup"), a squash of ~3 months of uncommitted work that also introduced `pipeline_orchestrator.py`/`schemas.py`/`orchestrator.run_phase()` themselves. The immediately preceding commit (`52a9a0f`) shows Phase 6 called *without* an orchestrator at all, already with `Path` args (`potential_dir_p`/`lcoe_dir_p`) — i.e. the actual broken intermediate state the comment describes (a live object passed where `ResultsWriter` expected a `Path`) was never captured as a discrete commit; it lived and died inside the squashed window. The comment's own text is the only available description of the failure mode — not independently git-verifiable.
- **No other Path-typed usage exists to reintroduce the bug through**: `potential_dir`/`lcoe_dir`/`abatement_dir` are each read exactly once in `results_writer.py`, funneled solely through `_normalize_potential`/`_normalize_lcoe`/`_normalize_abatement` (lines 296-298). Single funnel point — passing `.model_dump()` through it carries no risk of resurfacing BUG_07 via a side channel.
- **`_normalize_potential`/`_normalize_lcoe` confirmed to need zero code changes**: their existing `isinstance(data, dict) and "techs" in data: return data` branch was already written for exactly this case (docstring: "from PotentialCalculator.run() directly") and structurally matches `PotentialResult.model_dump()`/`LCOEResult.model_dump()`'s shape (`"techs"` is the top-level key in both schemas). `dashboard_panels.py::_get_scenario_data`'s docstring independently confirms the same intent ("Handles both live run format and disk recovery format"). The only change needed is in `main.py`: pass `.model_dump()` when the live object is non-`None`, `Path` only in the genuine resume-without-cache case.
- **Serialization safety confirmed empirically**, not just by schema reading: loaded the real `outputs/PRT/{potential,lcoe}/result.pkl`, called `.model_dump()` on both live objects, `json.dumps()` succeeded on both with no error. `LCOETechResult.transform` (`rasterio.Affine` originally) is already coerced to a plain 6-float tuple by a `mode="before"` field_validator (`schemas.py:835-848`) at construction time — no raster/Path/datetime/numpy type survives into a live object's `model_dump()` output anywhere in the `PotentialResult`/`LCOEResult` schema tree.
- **Shape asymmetry confirmed safe, not just assumed**: the live object's `ScenarioResult` carries 5 fields (`threshold`, `n_pixels`, `area_eff_km2`, `capacity_mw`, `generation_gwh`) that `recover_potential_from_disk()`'s 3-field dict doesn't. Grepped every actual read site in `results_writer.py` and `src/visualization/*.py` — zero references to any of the 5 extra fields; all reads are `.get()`-based against the 3 fields present in both shapes. `scripts/validate_run_checksum.py` is structurally blind to this too: `potential_results`/`lcoe_results` never enter the dict Phase 6 persists as its own `result.pkl` (only `country`/`timestamp`/`timings`/`exported_tifs`/`dominance_*_counts`/`elapsed_total` do — `results_writer.py:334-422`), and their only two consumers (PNG dashboard, TXT report) are exactly the two file types the script's docstring excludes from hashing.
- **Full disk/live parity for those 5 fields — open, unresolved decision**: `capacity_mw`/`generation_gwh` are cheaply backfillable (`_extract_capacity`/`_extract_generation` in `data_recovery.py` already recognize `capacity_mw_sum`/`capacity_mw` as candidate CSV columns, just don't keep the value). `threshold`/`n_pixels`/`area_eff_km2` are **not** recoverable from the zonal CSV at all — `threshold` only exists as a GeoTIFF tag (`potential_calculator.py:510`), `n_pixels`/`area_eff_km2` are never persisted anywhere in tabular form (computed transiently from the raster mask, `potential_calculator.py:544-546`). Two options on the table, neither chosen: (i) extend `recover_potential_from_disk()` for full parity (partial — 2 of 5 fields cheap, 3 need new raster-tag/pixel-recount logic), or (ii) accept the asymmetry as documented degraded-resume behavior and add a `"_source": "disk_recovery"|"live_object"` marker for traceability.
- **Test coverage confirmed zero**: `tests/unit/test_results_writer.py`'s integration test explicitly `monkeypatch`es around `_normalize_potential`/`_normalize_lcoe`, with a comment naming BLOCKER-010 by number as deliberately out of scope for that test.
- **Classification**: closer to **(a)** than (b) — confined to `main.py`'s Phase 6 `input_getter` + (confirmed) zero changes needed in `results_writer.py`'s `_normalize_*` methods, `potential_calculator.py`, `lcoe_calculator.py`, or `dashboard_panels.py`. Not a pure one-liner: must preserve the `None`/resume-without-cache fallback deliberately (it's a legitimate use case, not just legacy), and needs a new test (none exists today covering this path). Ready for direct implementation in a future dedicated session — no further investigation/design round needed. The shape-parity decision (option (i) vs (ii) above) is the one open call Douglas still needs to make before or during that implementation.

**Related**: INVAR-009/INVAR-010/INVAR-011 (Phase 6 — Invariant Validation Project, 2026-08-18 audit — see the "Invariant Validation Project" section near the end of this file) are gated on this entry: their `_normalize_potential`/`_normalize_lcoe`/`_normalize_abatement` and disk-recovery findings can't be exercised end-to-end via the live-object branch until BLOCKER-010 lands. Not duplicated there — that section links back here for the full architecture discussion.

---

### BLOCKER-011 — Phase 6 double persistence silently discarded dominance pixel counts

**Status**: Fixed — commit `cb12583`, with a follow-up full-path integration test in commit `7fdb0c8`.

**Title**: Remove `ResultsWriter`'s manual `ArtifactManager` persistence; fold `dominance_suitability_counts`/`dominance_lcoe_counts` into the dict `run()` returns.

**Current pain (as found)**: `ResultsWriter._persist_artifacts()` manually saved a dict containing `dominance_suitability_counts`/`dominance_lcoe_counts` via its own `ArtifactManager` call, but `PipelineOrchestrator.run_phase()`'s automatic persistence step ran afterward and overwrote the same `result.pkl` with the plainer dict `run()` returned, which never had those fields. Confirmed empirically (not just by code inspection): `outputs/PRT/results/result.pkl` was missing both fields after every real run.

**Depends on**: none.

**Effort**: S — mechanical, once the right approach was chosen.

**Risk**: Low — behavior-preserving for every field except the two that were previously lost.

**Decision made**: surveyed all 9 phases' persistence patterns before fixing. 5 of 9 (Audit, Grid Align, Criteria, Potential, LCOE) already rely solely on the orchestrator's automatic persistence — the majority, and safer, pattern. Chose to remove `ResultsWriter`'s manual persistence entirely (matching that majority) rather than making the orchestrator "smarter" about not overwriting (would add complexity to a shared component touching all 9 phases for a narrowly-scoped bug). Discovered in the process that Transport (Phase 9) has the identical bug, currently dormant — see BLOCKER-012.

**Deliverable (as shipped)**: removed `ResultsWriter._persist_artifacts()` and the `ArtifactManager` import entirely; `dominance_suitability_counts`/`dominance_lcoe_counts` now get set directly on the `results` dict `run()` returns.

**Tests added**: `tests/unit/test_results_writer.py` — a structural guard (no manual persistence), a behavioral round-trip test (real dominance-computation statics + real `ArtifactManager` save/load), and a full-path integration test (real `ResultsWriter.run()` driven by the real `PipelineOrchestrator.run_phase()`, with only rendering and Phase 4/5 disk recovery stubbed).

**Success criteria (as validated)**: selective pipeline run (Phase 6 live, all else cached) against the CHECKPOINT-2 state confirmed the dominance-count fields now present with correct values, zero other fields/pixels/TIF content changed, full test suite passing throughout.

**Commit message** (as shipped):
```
Fix BLOCKER-011: Phase 6 double persistence silently discarded dominance counts
```

---

### BLOCKER-012 — Transport (Phase 9) has the same double-persistence bug BLOCKER-011 fixed in Phase 6

**Status**: not fixed — documented only. Currently dormant because `skip_transport: true` in `configs/settings.yaml` (Phase 9 has an unrelated `AttributeError` that keeps it disabled — now tracked as **BLOCKER-019**). Discovered while surveying all 9 phases' persistence patterns during BLOCKER-011.

**Title**: Remove `TransportDecarbonizationCalculator`'s manual `ArtifactManager` persistence, same fix shape as BLOCKER-011.

**Current pain**: `src/processors/transport_decarbonization_calculator.py::run()` manually persists via its own `ArtifactManager` call (L570–608) — `serializable_result = {"country_code", "country_name", "generated_at", "summary", "n_hubs", "elapsed"}` (L579–586) — then `run()` returns a **completely different-shaped dict** at L611–617: `{"timeseries_df", "fleet_df", "hubs_gdf", "summary", "report_path"}`. The only overlapping key between the two is `"summary"`. `PipelineOrchestrator.run_phase()`'s automatic `_persist()` step runs after `run()` returns (`pipeline_orchestrator.py:165`) and saves *that* returned dict to `outputs/{ISO3}/transport/result.pkl`, overwriting whatever the manual call just wrote — this is the identical mechanism that caused BLOCKER-011 (`results_writer.py`'s `dominance_suitability_counts`/`dominance_lcoe_counts` being silently discarded), confirmed by direct code inspection, not just structural similarity.

Unlike BLOCKER-011, this has not been confirmed empirically against a real `result.pkl` on disk, because `skip_transport: true` means Phase 9 has not actually completed a full run in this engagement (it hits an unrelated `AttributeError` — `country_params.solar_capacity_factor`, flat vs. nested `CountryParams.solar.capacity_factor` — before ever reaching the persistence step; see `docs/memory/06-risk-areas.md`). The bug is real by inspection of the code path, just not yet observable in a live artifact the way BLOCKER-011's was.

**Depends on**: soft — should be sequenced after (or alongside) whatever eventually fixes Phase 9's `AttributeError` crash, since that crash currently makes this bug unreachable in practice. Not a hard blocker on that fix; the double-persistence code can be removed independently at any time, it just can't be *validated end-to-end* until Phase 9 runs to completion.

**Effort**: S — mechanical, same shape as the BLOCKER-011 fix: delete the manual `ArtifactManager` block, fold whatever fields matter from `serializable_result` (`n_hubs`, `generated_at`) into the dict `run()` already returns, if they're worth keeping; `summary` already appears in both so it isn't lost either way.

**Risk**: Low — behavior-preserving once done the same way BLOCKER-011 was (remove the redundant writer, let the orchestrator's automatic persist be the single source of truth). The only real risk is deciding whether `country_code`/`generated_at`/`n_hubs`/`elapsed` (present only in the manually-persisted dict today) are worth adding to `run()`'s return before deleting the manual call — otherwise those specific fields would be lost the same way `dominance_*_counts` was in BLOCKER-011, rather than fixed.

**Deliverable** (when picked up):
- `src/processors/transport_decarbonization_calculator.py`: remove the manual `ArtifactManager` persistence block (L570–608); add `n_hubs`, `generated_at` (and `country_code` if useful downstream) to the dict returned at L611–617, alongside the existing `timeseries_df`/`fleet_df`/`hubs_gdf`/`summary`/`report_path`.
- A test mirroring `tests/unit/test_results_writer.py`'s structural guard (no manual `ArtifactManager` call) — the full-path integration-style guard is lower priority here since Phase 9 needs its own fixture work regardless (real fleet/hub data), independent of this specific bug.

**Success criteria**: `grep -n "ArtifactManager" src/processors/transport_decarbonization_calculator.py` returns nothing; after Phase 9's `AttributeError` is separately fixed and a real run completes, `outputs/{ISO3}/transport/result.pkl` contains every field the current manual `serializable_result` computes, confirmed empirically the same way BLOCKER-011 was.

**Commit message template** (for when this is picked up):
```
Fix BLOCKER-012: Transport (Phase 9) double persistence, same shape as BLOCKER-011

TransportDecarbonizationCalculator manually persisted a dict via its
own ArtifactManager call, then run() returned a completely different-
shaped dict that PipelineOrchestrator's automatic persistence silently
overwrote it with. Same root cause and same fix as BLOCKER-011
(results_writer.py) -- removed the manual persistence, folded the
fields worth keeping into run()'s return.
```

---

### BLOCKER-013 — Suitability map rendering crashed the whole pipeline for any country above ~1200px

**Status**: Fixed — commit `4f90faf`.

**Severity**: higher than every other numbered BLOCKER in this file. BLOCKER-001 through 012 are numeric-correctness bugs (a wrong value gets computed, persisted, or discarded) — the pipeline still completes. This one is a hard crash: Phase 3 (Suitability) raises an unhandled `IndexError` mid-render and the process exits. **The whole country run fails from Phase 3 onward, not just one map.**

**Title**: `GeoWorldStyler.render_raster_map()` downsampled `score` for display but never resized `exclude_mask` to match, causing a boolean-index shape mismatch for any country whose grid exceeds the display-downsample threshold.

**Current pain (as found)**: reported via `python main.py Brazil` — crashed in `_plot_suitability` → `render_raster_map()` (`src/utils/map_styling.py:1278`, `excl_rgba[exclude_mask, :3] = ...`) with `IndexError: boolean index did not match indexed array along axis 0; size of axis is 1194 but size of corresponding boolean axis is 3902`. Suitability's TOPSIS/OWA math and GeoTIFFs had already computed and saved correctly before the crash — this was a rendering-only bug, not a numeric one, but it aborted the process regardless.

**Root cause**: `render_raster_map()` downsamples `score` via `PIL.Image.resize(..., NEAREST)` whenever `max(h, w) > max_display_px` (`self.layout.get("max_raster_display_px", 1200)` — no override exists in `settings.yaml`, so the fallback of 1200 always applies). `exclude_mask` — the boolean array driving the grey exclusion overlay — was never resized alongside it, so `excl_rgba = np.zeros((*score.shape, 4))` (built from the now-*smaller* `score`) got indexed with the still-full-resolution `exclude_mask`. Confirmed with a standalone reproduction (`scratchpad/repro_bug013.py`, no pipeline run needed): a synthetic 3902×3920 array reproduces the identical `IndexError` message, byte-for-byte; a 170×600 (Portugal-scale) array never enters the downsample branch and never crashes.

**Why Portugal never triggered this and Brazil did**: purely a function of grid size, not geometry/islands — confirmed by reading `mainland_gdf.total_bounds` is only used to set the plot's geographic axis extent, never to crop `score` or `exclude_mask`. Checked the actual grid dimensions (0.01°-resolution reference grid, same logic `grid_aligner.py::build_reference_grid()` uses) for every country currently in `parameters.json`, using already-persisted aligned/suitability rasters rather than re-deriving bounds from scratch:

| Country | Height × Width (px) | Max dim | > 1200px? |
|---|---|---|---|
| PRT | 518 × 333 | 518 | No |
| EGY | 995 × 1111 | 1111 | No (closest to the threshold) |
| ZAF | 1272 × 1645 | 1645 | **Yes** |
| IND | 2519 × 2870 | 2870 | **Yes** |
| CHN | 3335 × 6123 | 6123 | **Yes** |
| BRA | 3902 × 3920 | 3920 | **Yes** (confirmed crash) |
| RUS | 3654 × 15270 | 15270 | **Yes** |

**5 of the 7 countries currently configured are at risk, not just Brazil.** Only PRT and EGY sit under the threshold. CHN/RUS/IND/ZAF have older successful suitability PNGs on disk (April/May 2026), but those predate this version of `map_styling.py` (last substantively modified 2026-08-06) — they are not evidence current code would succeed for them; re-run today without this fix, all four would hit the identical crash BRA did.

**Impact — this is a full pipeline failure, not a cosmetic one**: only `suitability_builder.py`'s `_plot_suitability()` ever passes a real `exclude_mask` into `render_raster_map()` (confirmed by checking every call site in the repo — `lcoe_calculator.py`'s only other caller explicitly passes `exclude_mask=None`). `TECH_ORDER = ["solar", "wind", "biomass"]` processes solar first, so the crash on solar's map halted the phase before wind or biomass were ever attempted — even though wind and biomass's TOPSIS/OWA math would have hit the identical bug on the identical grid. For any at-risk country, Phase 3 fails entirely, and every phase downstream of it (4 through 8) never runs.

**Depends on**: none.

**Effort**: S — root-cause fix was an 8-line addition (mirrors the existing `score` resize).

**Risk**: Low — the fix only activates inside the pre-existing downsample branch (`max(h, w) > max_display_px`), and only when `exclude_mask is not None`; behavior for every call site/country under the threshold is unchanged, confirmed empirically (see Validation below).

**Fix (as shipped)**: inside the existing downsample block, resize `exclude_mask` to the same `new_size` as `score`, using `PIL.Image.NEAREST` explicitly rather than inheriting any particular method by coincidence — `exclude_mask` is boolean, and a continuous/interpolated resize would blend 0/1 into fractional values at exclusion boundaries, which is wrong even when the resulting shape happens to be correct.

**Validation (as performed)**:
- Full PRT regression against the pre-existing CHECKPOINT-2 output: every Suitability/Criteria/Potential/LCOE TIF byte-identical; the two Results dominance TIFs pixel-identical (only an embedded `CREATED` timestamp tag differs, same known pattern as BLOCKER-011's validation); every phase's `result.pkl` identical except wall-clock `timings`/`elapsed_s` fields. One real (but pre-existing and unrelated) difference was found in Abatement's NDC-coverage fields, traced to `_fetch_owid_total_co2_mt()` making a **live network fetch** of an external, time-varying CSV dataset — confirmed unrelated to this fix (`ghg_abatement_calculator.py` doesn't import `map_styling.py`, and its own last modification predates the CHECKPOINT-2 baseline). Root cause tracked separately, see BLOCKER-014.
- BRA Phase 3 run in isolation (`skip_audit`/`skip_land_cover`/`skip_align`/`skip_criteria: true`, reusing already-cached Phase 1/2a/2b outputs; everything downstream of Phase 3 also skipped to keep the run isolated): completed for solar, wind, **and** biomass — all 3 × (1 TOPSIS + 3 OWA) GeoTIFFs and all 4 PNGs (3 per-tech maps + comparison) generated, "Result: COMPLETED".
- `pytest tests/` — 77/77 passing throughout.

**Commit message** (as shipped):
```
Fix BLOCKER-013: exclude_mask shape mismatch crashed Suitability rendering for large countries
```

---

### BLOCKER-014 — Abatement's live OWID network fetch breaks bit-identical validation determinism

**Status**: not fixed — documented only. Discovered as a byproduct of BLOCKER-013's PRT regression validation.

**Severity**: lower than the numeric-correctness/crash BLOCKERs — doesn't make any single run wrong. It undermines the bit-for-bit regression methodology this entire engagement has relied on (CHECKPOINT-1, CHECKPOINT-2, BLOCKER-011's and BLOCKER-013's validations) whenever Abatement is included in a diffed run.

**Title**: `GHGAbatementCalculator._fetch_owid_total_co2_mt()` makes a live HTTP GET of an external, time-varying CSV on every run where the value isn't already cached in-process — no local snapshot, no version pin, no logged provenance.

**Current pain**: `src/processors/ghg_abatement_calculator.py:1137-1159` fetches `https://raw.githubusercontent.com/owid/co2-data/master/owid-co2-data.csv` (the `master` branch — a moving target, not a pinned commit/release) to help populate `total_co2_mt_2022`/net-zero context feeding `calc_net_zero()`. Confirmed empirically: two full PRT runs of the *identical* code (`ghg_abatement_calculator.py` last modified 2026-06-22, `net_zero_db.json` last modified 2026-03-25 — neither changed between the two runs), six days apart, produced different `ndc_coverage_pct` (93.77 vs. `None`), `national_contribution_pct` (22.53 vs. 0.0), and `owid_scope_warning` (`False` vs. `True`) for the same country. Nothing else in either run differed. This is the only non-timing discrepancy either regression check found.

**Depends on**: none.

**Effort**: S — either cache/vendor a pinned snapshot of the OWID CSV under `data/raw` or `configs/`, or (lighter touch) log the fetched CSV's commit SHA / `Last-Modified` header / row date actually used per run, so a future "this run doesn't match the baseline" question is diagnosable in seconds instead of requiring the kind of tracing this entry took.

**Risk**: Low — additive (caching or logging), no computation logic changes.

**Suggested fix (not applied)**: pin the fetch to a specific commit SHA in the URL (or vendor a snapshot file and update it deliberately), and/or add the resolved data date/hash to the persisted `abatement` result so it's visible in `result.pkl`/the text report rather than only in a debug-level log line.

**Success criteria**: two consecutive full-pipeline runs of the same code on different days produce bit-identical Abatement output for the same country, or — if intentionally kept live — the persisted result records exactly which upstream data snapshot was used.

---

### BLOCKER-015 — Phase 1 (Audit) skip-check looks for its report in the wrong directory

**Status**: not fixed — documented only. Low priority. Discovered while configuring BLOCKER-013's isolated BRA Phase 3 validation run (`skip_audit: true`).

**Title**: `main.py`'s Phase 1 skip check globs a directory `DataAuditor` doesn't write to, so `skip_audit: true` never finds a cached report even when one exists.

**Current pain**: `main.py::_run_phase_1_audit()`'s skip branch checks `(outputs_dir / code / "audit").glob("*audit*.txt")` — but `DataAuditor` actually writes its report to `outputs/reports/audit_{ISO3}_{timestamp}.txt`, a different directory entirely. Confirmed directly: `outputs/BRA/audit/` doesn't exist at all, while `outputs/reports/audit_BRA_20260817_111914.txt` does. With `skip_audit: true`, the check always falls through to the "no prior report found — data-quality metadata will be unavailable" warning branch instead of the intended "skipped — prior report on disk" path — harmless (Phase 1 still correctly doesn't re-run, no crash, no wrong data downstream), just a misleading log line and a warning that shouldn't fire.

**Depends on**: none.

**Effort**: S — one-line glob path fix, or match the actual filename pattern `audit_{code}_*.txt` under `outputs/reports/`.

**Risk**: Low — the check only gates a log message and a warning; correcting the path doesn't change what Phase 1 actually does either way.

**Suggested fix (not applied)**: point the glob at `outputs_dir / "reports"` with pattern `f"audit_{code}_*.txt"` instead of `outputs_dir / code / "audit"`.

**Success criteria**: with `skip_audit: true` and a prior audit report on disk, the log shows "skipped — prior report on disk" instead of the "no prior report found" warning.

---

### BLOCKER-016 — SA-2's `stable_fraction` replaced with a threshold-crossing decision-robustness metric

**Status**: Fixed — commit `478caca`. Methodology change, not a bug fix — flagged here for visibility since it changes what a persisted/reported number means, the same reason BLOCKER-008/009 are tracked here rather than left as a silent diff.

**Severity**: not comparable to the crash/correctness BLOCKERs above — nothing was broken, and no downstream computation (Potential/LCOE/Abatement) depends on SA-2's output. This is a decision to replace a metric with a more informative one, validated by a standalone prototype before touching production code.

**Title**: `sa2_monte_carlo_weights()` reported `stable_fraction` (fraction of pixels whose 90% CI band on the raw continuous TOPSIS score was narrower than 0.10) — an absolute-score-stability metric. Replaced with a threshold-crossing metric that measures robustness of the framework's actual apt/not-apt decision instead.

**Why**: a standalone prototype (`scratchpad/threshold_crossing_prototype.py`, fidelity-checked by reproducing the real pipeline's own logged `stable_fraction` numbers exactly before trusting any new metric) compared both approaches on PRT and BRA, all 3 techs:

| Country | Tech | Old: `stable_fraction` | New: decisive / boundary / moderate (among apt-by-base pixels) |
|---|---|---|---|
| PRT | solar | 2.0% | 17.2% / 49.0% / 33.8% |
| PRT | wind | 4.4% | **2.3% / 72.1%** / 25.5% |
| PRT | biomass | 0.4% | 20.4% / 32.6% / 47.0% |
| BRA | solar | 2.5% | 28.1% / 27.7% / 44.2% |
| BRA | wind | 4.3% | **53.1% / 19.9%** / 27.0% |
| BRA | biomass | 0.2% | 24.0% / 44.4% / 31.6% |

`stable_fraction` could not distinguish PRT from BRA on wind (4.4% vs. 4.3%, nearly identical) — the new metric shows they are opposites: PRT's wind classification is almost entirely in the ambiguous boundary zone (72.1%, barely 2.3% decisive either way), while BRA's is mostly decisive (53.1%). `stable_fraction` measures whether the continuous score is numerically stable; the framework's real output is a threshold-based classification (0.75 for the balanced scenario), and score jitter far from that threshold doesn't matter for the actual decision — this is exactly the gap the new metric closes.

**Caveat, not resolved by this change**: `concentration` (the Dirichlet concentration parameter controlling how tightly the 1000 sampled weight vectors cluster around the AHP base weights) was already hardcoded at 20 before this change and remains a plain function parameter now, not wired to `settings.yaml`/`parameters.json`. The same prototype swept `concentration` in `{10, 20, 40}` for BRA/solar and found the reported "decisive" fraction ranges from **17.9% (concentration=10) to 45.4% (concentration=40)** — roughly a 2.5x swing. The qualitative PRT-vs-BRA comparison above held up across that range, but absolute percentages from this metric should always be reported alongside the `concentration` value that produced them, not treated as a fixed ground truth. Making `concentration` configurable, or reporting it as a swept range instead of a point estimate, is future work — not done here.

**Deliverable (as shipped)**:
- `src/processors/sensitivity_analyzer.py`: `sa2_monte_carlo_weights()` now takes a required `threshold` parameter and returns `crossing_fraction`/`apt_base_mask`/`n_apt_base`/`decisive_fraction`/`boundary_fraction`/`moderate_fraction` instead of `stable_fraction`; docstring documents the metric change and the `concentration` sensitivity finding inline (`METRIC_013` changelog tag). New helper `_balanced_threshold()` reads the real balanced-scenario threshold from Phase 4's persisted `pot_results` — deliberately not through `_resolve_tech_params()`'s existing `base_thr`, which returns a hardcoded 0.60 in practice due to a separate, pre-existing bug (`CountryParams` has no `suitability_threshold` attribute, so its `getattr` fallback always wins) — untouched here, out of scope.
- `run()`'s SA-2 block, `_format_report()`'s SA-2 rows, and `sensitivity_plots.py::plot_dashboard()`'s SA-2 KPI panel all updated to the new fields. `plot_sa2_cv()` (the CV/CI90 histogram PNG) was not touched — it never referenced `stable_fraction`.
- `docs/memory/04-algorithms.md`'s SA-2 row and a new explanatory note.

**Validation (as performed)**:
- Full PRT and BRA runs (Phase 8 at minimum, other phases reusing cache) confirmed the new metric's live numbers match the prototype's Item 2 table above, per technology, both countries.
- `pytest tests/` — no test referenced `stable_fraction` (confirmed by repo-wide grep before making any change), so nothing needed updating; full suite still green after the change.
- `configs/settings.yaml` untouched.

**Commit message** (as shipped):
```
Replace SA-2's stable_fraction with a threshold-crossing metric
```

**Update (2026-08-17)**: the `_resolve_tech_params()` bug referenced above as "untouched here, out of scope" has since been fixed — see BLOCKER-017.

**Update (2026-08-18)**: the prototype cited above (`scratchpad/threshold_crossing_prototype.py`) as the source of the "17.9% → 45.4%, ~2.5x" `concentration` swing was never committed — confirmed gone from both the working tree and the full git history (`git log --all` for its filename returns nothing across any branch/commit) — and is unrecoverable. A new, independent, committed sweep (`scratchpad/oat_sa2_concentration_sensitivity.py`, commit `2204109`) reproduces 17.9%/45.4% for BRA/solar exactly, but covering all 3 techs × PRT/BRA found the swing is **not** a universal ~2.5x — PRT/wind swings ~16.3x over the identical `concentration ∈ {10, 20, 40}` range. See Campaign table row 1 and its "Row 1" write-up below.

### BLOCKER-017 — `_resolve_tech_params()`'s threshold bug contaminated SA-3's plot/label and SA-6's absolute potential figures (2.6x–4.3x overstated)

**Status**: Fixed — commit `dcf956e`.

**Severity**: real contamination of already-generated, already-persisted numbers for every country run this session (PRT, BRA) — not a low-impact technicality. Investigated as impact-first (measure before fixing) per explicit instruction, given BLOCKER-016 flagged this same bug as "out of scope."

**Title**: `SensitivityAnalyzer._resolve_tech_params()` resolved `threshold` via `getattr(country_params, "suitability_threshold", thr)` — `CountryParams` has no `suitability_threshold` attribute (it's a per-country object, not per-technology), so `getattr`'s hardcoded default (0.60) silently won on every call, and the early `return` in that branch meant the function's own correct fallback logic (reading `pot_results.techs[tech].params["thresholds"]["balanced"]`, a few lines further down) was never reached. `power_density`/`land_use_factor`/`capacity_factor` resolution in the same function was unaffected — confirmed via `country_params.{tech}.power_density_mw_km2`/`land_use_factor`/`capacity_factor` being independently correct attributes that do exist.

**Call site**: `run()`'s per-technology loop, `src/processors/sensitivity_analyzer.py` — `pd_mw, luf, cf, base_thr = self._resolve_tech_params(tech, country_params, pot_results)`, once per tech, before SA-1 runs.

**Downstream consumers of `base_thr`** (traced every reference):

| Consumer | How it uses `base_thr` | Actually affected? |
|---|---|---|
| SA-3 sweep computation (`sa3_threshold_sweep()`) | not passed in at all — sweeps a fixed range `np.arange(0.30, 0.85, 0.05)` independent of any base threshold | **No** — CSV data intact across the full range, including the real 0.75 row |
| SA-3 report/metadata field (`results_sa[tech]["sa3"]["base_threshold"]`) | label only | Yes, but not currently visible — see BLOCKER-018 (report text wasn't rendering this field at all before that fix) |
| SA-3 plot (`plot_sa3_threshold()`) | vertical reference line position on the PNG | **Yes** — every already-generated SA-3 PNG for PRT/BRA marks the wrong point (0.60) on an otherwise-correct curve |
| SA-6 (`sa6_potential_sensitivity()`) | direct computational input: `area = pxa[score >= base_threshold].sum()`, feeding `base_gw`/`base_twh`/every OAT row's absolute potential/generation | **Yes** — absolute values wrong; `elasticity_gw`/`elasticity_twh` are algebraically threshold-invariant (the `area` term cancels in the ratio), so the sensitivity *conclusions* were not wrong, only the absolute numbers alongside them |

**Quantified impact** (real SA-3 CSV data already on disk this session, threshold=0.60 vs. 0.75 — identical to what SA-6 computes at those thresholds, same formula/area):

| Country/Tech | GW @0.60 (bug) | GW @0.75 (real) | Ratio | Elasticity @0.60 | Elasticity @0.75 |
|---|---|---|---|---|---|
| PRT solar | 69.9 | 26.1 | 2.7x | -2.20 | -9.21 |
| PRT wind | 1.85 | 0.46 | 4.0x | -4.47 | -10.66 |
| PRT biomass | 1.70 | 0.39 | 4.3x | -5.61 | -8.45 |
| BRA solar | 2770 | 1081 | 2.6x | -3.12 | -7.14 |
| BRA wind | 135 | 50.6 | 2.7x | -3.57 | -5.21 |
| BRA biomass | 242 | 66.4 | 3.6x | -2.39 | -10.54 |

Every affected number differs by a factor of 2.6x-4.3x, not a marginal rounding difference — real contamination, not a low-impact technicality.

**Depends on**: none. Related to BLOCKER-016 (same root cause category — a broken `country_params` threshold read — BLOCKER-016 worked around it with an isolated helper rather than fixing it in place).

**Fix — consolidation trade-off considered before choosing**:
- *Isolated duplicate* (mirror `_balanced_threshold()`'s read logic inline inside `_resolve_tech_params()`, independently): zero coupling, but leaves two independent implementations of "read the real balanced threshold" that could silently drift apart in a future refactor of either data path — the same class of bug this entry is about.
- *Shared helper* (chosen): confirmed first that `_balanced_threshold()`'s read path (`pot_results.techs[tech].scenarios["balanced"].threshold`) and `_resolve_tech_params()`'s already-existing-but-unreachable pot_results branch (`pot_results.techs[tech].params["thresholds"]["balanced"]`) resolve to the *same* value — `potential_calculator.py` computes `threshold` once per scenario and stores it into both locations. Made `_resolve_tech_params()` call `_balanced_threshold(tech, pot_results)` directly for the threshold component at both of its return points, leaving `power_density`/`land_use_factor`/`capacity_factor` resolution untouched. `_balanced_threshold()`'s docstring updated (previously described itself as deliberately *not* reused by this function — now the opposite is true).

**Risk**: Low — the only behavior change is what `threshold` resolves to (0.60 hardcoded fallback → the real per-country/tech balanced threshold, 0.75 for both PRT and BRA today); `pd_mw`/`luf`/`cf` resolution paths are byte-identical to before.

**Validation (as performed)**:
- Full Phase 8 runs (PRT, BRA; Phases 1-7 reused from cache) after the fix: SA-2/SA-3/SA-6 all now log `thr=0.75`; SA-6's `base_gw` values match this entry's "@0.75 (real)" column exactly (PRT solar 26.1, wind 0.5, biomass 0.4; BRA solar 1080.7, wind 50.6, biomass 66.4).
- Re-extracted the SA-3 sweep CSVs post-fix: byte-identical to the pre-fix values in the table above at both threshold=0.60 and 0.75 rows, for all 3 techs, both countries — confirms the sweep computation itself was never affected, only the "base" label/reference line, exactly as predicted.
- `pytest tests/` — 78/78 passing.
- `configs/settings.yaml` reverted to the standing configuration, confirmed via empty `git diff`.

**Commit message** (as shipped):
```
Fix BLOCKER-017/018: resolve real threshold in _resolve_tech_params(), render report subsections
```

---

### BLOCKER-018 — Sensitivity report text rendered empty SOLAR/WIND/BIOMASS sections for every country

**Status**: Fixed — commit `dcf956e`. Independent bug from BLOCKER-017, fixed in the same session/commit because both surfaced during the same investigation, tracked separately since they have unrelated root causes.

**Severity**: moderate — no wrong numbers were ever computed or persisted (SA-1 through SA-6 all ran and saved correctly to CSV/pickle/PNG); the text report (`{ISO3}_sensitivity_report.txt`), one of the phase's primary human-facing deliverables, was simply blank where its content should have been. Discovered as a side effect of BLOCKER-017's Task 1 investigation (checking whether `base_thr`'s report-text field was currently visible).

**Title**: `build_phase_report()` (`src/utils/reporting.py`) never rendered `ReportSection.subsections` — only top-level `rows`. `sensitivity_analyzer.py::_format_report()` builds one `ReportSection` per technology with all SA-1...SA-6 content nested inside `subsections` (never in the top-level `rows`), so every tech section rendered its header/separator only, with zero content rows, for both PRT and BRA (confirmed via direct inspection of the persisted `.txt` files).

**Root cause**: a fully-correct recursive helper, `_append_section()`, already existed in the same module (docstring: "Recursively append a section and its subsections to lines") — but was never called from anywhere, including `build_phase_report()` itself, which has its own separate, non-recursive inline loop that iterates `section.rows` only. Classic orphaned-refactor: the recursive helper was written (presumably to replace the inline loop) but never wired in.

**Scope check**: grepped every use of `ReportSection`/`subsections`/`build_phase_report` repo-wide — `sensitivity_analyzer.py` is the *only* caller that populates `subsections` (suitability_builder/lcoe_calculator/potential_calculator/results_writer all only use top-level `rows`). This bug was therefore invisible everywhere except the sensitivity report.

**Depends on**: none.

**Effort**: S.

**Risk**: Low — fix is additive for every other phase's report (their sections never populate `subsections`, so the new recursive branch never fires for them; top-level row formatting reproduced byte-for-byte at depth 0). Confirmed no test in the repo asserts report-text formatting.

**Fix (as shipped)**: replaced `build_phase_report()`'s non-recursive section loop with a recursive `render_section()`/`render_rows()` pair that reproduces the exact prior top-level formatting at depth 0 and additionally recurses into `subsections` at increasing indent. Deleted the now-truly-dead `_append_section()` rather than leaving two parallel implementations — its own formatting (right-aligned values) differed from the inline loop's (left-padded label, compact value) and was never used, so keeping it around post-fix would just be a second, subtly different, unused implementation of the same concept.

**Validation (as performed)**:
- Full Phase 8 runs (PRT, BRA) after the fix: `{ISO3}_sensitivity_report.txt` now shows all 5 subsections (SA-1, SA-2, SA-3, SA-4, SA-6) with real content under SOLAR/WIND/BIOMASS, for both countries — grepped `Base threshold (balanced)` and confirmed 3 hits per country (one per tech), value `0.75`.
- `pytest tests/` — 78/78 passing (shared run with BLOCKER-017's validation, no test exercises `build_phase_report`/`ReportSection` directly).

**Commit message** (as shipped):
```
Fix BLOCKER-017/018: resolve real threshold in _resolve_tech_params(), render report subsections
```

---

## HIGH-VALUE REFACTORS (improve maintainability, enable testing)

### BLOCKER-019 — Transport (Phase 9) crashes on `country_params.solar_capacity_factor` — flat attribute access on a nested schema

**Status**: not fixed — documented only. Low priority while Transport stays dormant — `skip_transport: true` is a deliberate choice (this bug is *why* it was first set, not a symptom of neglect since). Formalizes a bug that has only ever existed in prose (`configs/settings.yaml:160`'s `skip_transport` comment, `docs/memory/06-risk-areas.md`, and BLOCKER-012's "Depends on" note) since it was first discovered, without its own tracking number until now.

**Title**: `TransportDecarbonizationCalculator.run()` and `_log_parameter_dashboard()` both read `country_params.solar_capacity_factor` / `.wind_capacity_factor` / `.biomass_capacity_factor` as flat attributes, but `CountryParams` (`src/core/schemas.py:363-365`) stores these nested — `country_params.solar.capacity_factor`, `.wind.capacity_factor`, `.biomass.capacity_factor`. The flat names exist only as `_FLAT_KEY_MAP` dict-style aliases (`schemas.py:416,427,435` — `country_params.get("solar_capacity_factor")`), not as real attributes, so any real (non-`None`) `CountryParams` passed to Phase 9 raises `AttributeError` immediately.

**Current pain**: confirmed by direct inspection. `src/processors/transport_decarbonization_calculator.py:402-404`, inside `run()`'s "Log CountryParams traceability" block — one of the first things `run()` does after input validation:
```python
logger.info(
    "  [CountryParams] %s — solar CF=%.3f | wind CF=%.3f | biomass CF=%.3f | threshold=%.2f",
    country_params.country_code,
    country_params.solar_capacity_factor,   # AttributeError
    country_params.wind_capacity_factor,    # AttributeError
    country_params.biomass_capacity_factor, # AttributeError
    suitability_threshold,
)
```
A second, currently-unreachable instance of the identical pattern exists at `_log_parameter_dashboard()` (L1461-1462) — unreachable today only because `run()` already crashes at L402 before that method is ever called with a real `country_params`. This is the concrete cause behind `configs/settings.yaml`'s `skip_transport: true` (L160, inline comment) — Phase 9 was not deprioritized, it cannot complete a run at all while `country_params` is passed in, which `main.py` always does.

**Depends on**: none technically. Referenced as a soft dependency by BLOCKER-012 ("should be sequenced after ... whatever eventually fixes Phase 9's `AttributeError` crash") and REFACTOR-009 (hub-siting extraction), since both need Phase 9 to actually complete a run before their own success criteria can be validated end-to-end rather than just by code inspection.

**Effort**: S — two call sites, same fix shape at each: read the nested attributes (`country_params.solar.capacity_factor` etc.) or use the dict-style flat accessor the schema already supports (`country_params.get("solar_capacity_factor")`, per the docstring at `schemas.py:354`).

**Risk**: Low in isolation (the fix itself is mechanical), but **fixing this unblocks Phase 9 running for the first time in this engagement** — once it can complete, it will surface whatever else in the ~1758-line `transport_decarbonization_calculator.py` has never been exercised end-to-end (BLOCKER-012's double-persistence bug, the `hubs_gdf.to_csv()` spatial-fidelity gap from `docs/00-project-state-and-reorg-plan.md` Part 4d, and any other currently-cold code path in that module). Do not fix this in isolation without budgeting for a full Phase 9 validation pass — BLOCKER-012 itself notes its own bug "has not been confirmed empirically... because `skip_transport: true` means Phase 9 has not actually completed a full run."

**Suggested fix (not applied)**: replace the three flat attribute reads at each of the two sites with either the nested path (`country_params.solar.capacity_factor`, `.wind.capacity_factor`, `.biomass.capacity_factor`) or `country_params.get("solar_capacity_factor")` etc. — whichever convention the rest of the file already favors elsewhere (not audited in this pass) — applied consistently at both sites.

**Success criteria**: `python main.py PRT` with `skip_transport: false` completes Phase 9 without an `AttributeError`; `grep -n "country_params\.\(solar\|wind\|biomass\)_capacity_factor" src/processors/transport_decarbonization_calculator.py` returns zero matches.

**Not fixed here** — this entry only assigns a tracking number and records the exact evidence already known informally. Per standing direction, Transport stays dormant by decision; this is not the dedicated Phase 9 reactivation session that would also need to budget for BLOCKER-012 and the Part 4d gap alongside this fix.

---

### BLOCKER-020 — `criteria_builder.py`'s `ParamsLike` keeps a raw-`dict` input path alive for `country_params`, same class of gap as BLOCKER-005/QI-003

**Status**: Closed (2026-08-18) — absorbed by **INVAR-004** (Invariant Validation Project, 2026-08-18 audit — see the "Invariant Validation Project" section near the end of this file). The finding and its ~12-call-site inventory below remain the canonical detail for this item; INVAR-004 does not repeat it, it links back here. Originally: not fixed — documented only. Found while scoping the BLOCKER-005 "validation half" removal (raw-dict `country_params` input to `build_tech_params()`/`PotentialCalculator.run()`); out of scope for that pass because it lives in a different subsystem (Phase 2b, not the Phase 4/5 threshold chain) and touches ~12 call sites on its own, so it gets its own tracking number instead of being folded in or left as a loose comment.

**Title**: `src/processors/criteria_builder.py:70` defines `ParamsLike = Union[CountryParams, Dict[str, Any]]`, with the inline comment `# Type alias: accepts both new typed contract and legacy dict during migration.` The private helper `_param(params: ParamsLike, key: str, default: Any = None) -> Any` (line 115-117, body: `return params.get(key, default)`) duck-types across both shapes — it works today only because `CountryParams` itself implements a dict-style `.get()` for legacy-compatibility (`schemas.py`'s "Compatibility" design principle), not because `_param()` validates anything. A raw, unvalidated `dict` standing in for `country_params` is therefore still a live, typed-and-documented code path here, same as the one BLOCKER-005/QI-003 closed for `build_tech_params()`.

**Call sites using `ParamsLike`** (11 function parameters + 1 class attribute, ~12 total):
- `_param(params: ParamsLike, ...)` — line 115 (the duck-typed accessor itself)
- `compute_solar_resource(solar_path, params: ParamsLike)` — line 125-127
- `compute_wind_resource(wind_path, params: ParamsLike)` — line 148-150
- `compute_terrain_score(slope_path, elev_path, params: ParamsLike)` — line 167-170 (reads `_param(params, "slope_threshold_deg", 7.0)` at line 176)
- `compute_linear_proximity_suitability(dist_path, params: ParamsLike, max_dist_key, default_max_km)` — line 234-238 (reads `_param(params, max_dist_key, default_max_km)` at line 248)
- `compute_road_suitability(roads_path, params: ParamsLike)` — line 263-264
- `compute_grid_suitability(grid_path, params: ParamsLike)` — line 272-273
- `compute_biomass_resource(land_cover_path, yield_raw, params: ParamsLike)` — line 388-391
- `compute_population_suitability(pop_path, params: ParamsLike)` — line 518-519
- `compute_river_suitability(rivers_path, params: ParamsLike, tech="solar")` — line 550-551
- `CriteriaBuilder.run(..., country_params: ParamsLike, ...)` — line 831 (the public entry point that receives whatever `main.py`/callers pass in and threads it through all of the above)

**Overlap with Campaign #9/#10/#11** (`docs/BACKLOG.md`'s Sensitivity & Config Migration Campaign table, row 9/10/11 — all `criteria_builder.py`): partial, and about different things living in the same lines, not the same defect.
- **Row 9** (`compute_terrain_score` weights `0.6×slope + 0.4×TRI`, line 216) — **touches the same function** as this entry (`compute_terrain_score`, which also reads `_param(params, "slope_threshold_deg", ...)` at line 176), but Row 9's concern is the hardcoded `0.6`/`0.4` blend weights (a scientific-parameter migration question, per the Campaign's own scope), not the `ParamsLike`/`_param()` type-safety gap this entry tracks. Two different fixes in the same function; neither blocks the other.
- **Row 11** (hardcoded percentiles, `criteria_builder.py:258,349,589`) — **partial overlap only**. Line 258 (`compute_linear_proximity_suitability`, roads/grid path) is inside a `ParamsLike`-typed function and does overlap. Lines 349 (`compute_proximity_plants`) and 589 (`compute_seismic_suitability`) do **not** — neither function takes a `params` argument at all, so that portion of Row 11 is unrelated to this entry.
- **Row 10** (`TRI_THRESHOLD=50.0`, `constants.py:200`) — no overlap; it's a module-level constant in a different file, not read via `_param()`/`ParamsLike` anywhere.

**Depends on**: none technically. Conceptually the same category of fix as BLOCKER-005's validation half / QI-003 (removing an unvalidated raw-dict stand-in for a Pydantic-validated config object) — should probably be scheduled alongside or immediately after that work for consistency, but does not block or get blocked by it code-wise (disjoint files, disjoint call graphs — `criteria_builder.py` does not import `build_tech_params` or call `PotentialCalculator.run`).

**Effort**: M — mechanical but wide: delete `ParamsLike`, retype ~11 function signatures plus `CriteriaBuilder.run` from `ParamsLike` to `CountryParams`, decide whether `_param()` becomes a thin `getattr`-based accessor or is removed in favor of direct attribute/`.get()` access on a guaranteed-`CountryParams` instance, and add the same `TypeError`-on-wrong-type guard used elsewhere once this pattern is settled.

**Risk**: Low — per the BLOCKER-005 diff precedent, no test in `tests/` references `criteria_builder.py`'s `ParamsLike`-typed functions or `country_params` at all (`grep -rn "country_params" tests/` returns zero matches repo-wide), and `main.py` already always passes a validated `CountryParams` in production. The risk is coverage blindness (nothing would catch a regression), not behavioral risk from the change itself.

**Success criteria**: `grep -n "ParamsLike" src/processors/criteria_builder.py` returns zero matches; `CriteriaBuilder.run()` and every `compute_*` helper it calls type-hint `country_params`/`params` as `CountryParams` (or `Optional[CountryParams]`), not a `Union` with `Dict`; passing a raw `dict` to `CriteriaBuilder.run()` raises a clear `TypeError` instead of being silently accepted.

**Not fixed here** — this entry only registers the finding and its scope; no code in `criteria_builder.py` was changed in this pass.

---

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

**Status**: **Partial.** The extraction half is done — commit `350c80c` (part of the 2026-08-18 `sensitivity_analyzer.py` enxugamento, see `docs/BACKLOG.md`'s Sensitivity & Config Migration Campaign section) moved 11 pure functions (`_topsis_flat`, `_load_criteria_arrays`, `_balanced_threshold`, `_build_ghg_function_from_abatement`, `sa1_oat_weight_sensitivity` through `sa6_potential_sensitivity`, `_sfmt`) to `src/utils/sensitivity_math.py` — not `sensitivity_methods.py` as originally named below, functionally equivalent. The `run()`-split half is **not done**: `grep -n "_run_sa[0-9]" src/processors/sensitivity_analyzer.py` returns zero matches — `run()` (currently starting at L446) remains one method, not six `_run_sa1()`...`_run_sa6()` orchestrators. Do not mark this item "done" on the strength of the extraction alone.

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

**Status update (verified 2026-08-17, `docs/00-project-state-and-reorg-plan.md` Part 4a follow-up)**: `results_writer.py` is fixed — see **BLOCKER-011** (direction (a): manual `ArtifactManager` calls removed, the orchestrator's automatic `_persist()` is now sole authority). The other three phases named in this task's original deliverable were investigated for whether their second write actually discards data — none do, so they stay REFACTOR-tier (harmless duplicate I/O), not promoted to a BLOCKER:

- **`suitability_builder.py`** (L492-549): the manually-persisted dict is built by filtering `results` — the same dict `run()` returns — down to 17 known-serializable keys per tech. What the orchestrator actually persists (`results`) is a strict superset. No data loss.
- **`ghg_abatement_calculator.py`** (L871-931): `run()` returns the exact same `serializable_result` object that gets manually persisted — not a derived subset, the identical dict pickled twice. The most harmless of the three; the two copies can never diverge.
- **`sensitivity_analyzer.py`** (L1490-1545): same shape as Suitability — `run()` returns `results_sa`, the rich dict `serializable` was filtered from (scalars only; `_`-prefixed keys and non-primitive values, including the SA-5 Sobol `_df`, stripped). `results_sa` is a superset, so no data is lost. **This one should be prioritized above the other two**, though: `main.py` passes `output_model=None` for Phase 8, and no `SensitivityResult` schema exists anywhere in `schemas.py` — so the richer, unfiltered `results_sa` (including the raw `pandas.DataFrame` inside `sa5_sobol._df`) is what the orchestrator's automatic persist actually writes to `result.pkl` today, defeating the scalar-only filter `serializable` was clearly written to enforce. Phase 8 is the one phase among 2b/3/4/5/6/7 whose persisted artifact has no Pydantic validation boundary at all, which runs against `docs/memory/03-pipeline.md`'s stated principle that "every phase's output that gets persisted to disk is validated against a Pydantic v2 model" — that principle holds for Phases 2b/3/4/5 only; Phases 6-9 are `output_model=None` by `main.py`'s own documented design (L790-797 comment), a real exception the narrative doc doesn't currently call out. Not fixed here — investigation and documentation only, per this session's scope.
- **`transport_decarbonization_calculator.py`** — not re-investigated here; already tracked separately as **BLOCKER-012**, which does lose data (manual dict and `run()`'s return share only the `"summary"` key).

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

**Fragility note (added 2026-08-17, not part of the original entry)**: the specific protection BLOCKER-005's "validation half" was meant to add — rejecting a country config whose per-scenario `thresholds` dict is incomplete — already holds today, but only by implementation accident, not by any validator. `build_tech_params()` (`src/core/constants.py:344`) always starts from `copy.deepcopy(DEFAULT_TECH_PARAMS)`, which hardcodes all three scenario keys (`optimistic`/`balanced`/`conservative`); a country override only ever patches `thresholds.balanced` (`constants.py:356,361,366`), never replaces the dict wholesale. So the missing-key scenario BLOCKER-005 worried about can't currently happen — but nothing in `schemas.py`/`config_loader.py` would catch it if a future refactor of `build_tech_params()` changed that assumption (e.g., building `thresholds` fresh per-country instead of patching a hardcoded default). When this script is built, it should include the explicit check BLOCKER-005 originally specified rather than relying on `build_tech_params()`'s current internal structure to keep protecting it by accident. Not fixed — documented only.

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

### QI-005 — Generate `SUMMARY.md`'s LOC table from a script instead of hand-editing

**Status**: not started — registered only, per explicit instruction not to implement yet.

**Title**: `SUMMARY.md`'s per-module LOC figures are typed by hand and have now gone stale twice after a structural refactor (once before the Fase 0 documentation consolidation, again after the 2026-08-18 `sensitivity_analyzer.py` enxugamento — REFACTOR-001/002/004 alone moved ~2500 lines out of `sensitivity_analyzer.py`/`transport_decarbonization_calculator.py` into three new files with no `SUMMARY.md` entry at all until manually corrected in this pass). This is a recurring pattern, not a one-off oversight — the underlying cause is that LOC counts live in prose, disconnected from the files they describe.

**Current pain**: every module split/extraction (already routine in this codebase — `ahp.py`/`topsis.py`/`owa.py`/`exclusion.py`, `abatement_plots.py`, `dashboard_panels.py`, `sensitivity_math.py`/`sensitivity_plots.py`/`transport_plots.py`, and whatever REFACTOR-003/008/009 eventually produce) silently invalidates `SUMMARY.md`'s numbers and can leave the new file with zero entry, and nothing catches it — the drift is only found by whoever happens to `wc -l` by hand, as this pass did.

**Depends on**: none.

**Effort**: S — a script that walks `src/`, `main.py`, and `configs/*.{yaml,json}`, runs `wc -l` per file, and either regenerates `SUMMARY.md`'s LOC parentheticals in place or fails CI/pre-commit if they've drifted. Does not need to auto-generate the prose (responsibilities/dependencies) — only the numbers, and ideally flags files present in `src/` with no `SUMMARY.md` entry at all (the second failure mode this pass found, not just wrong numbers).

**Risk**: Low — read-only tooling, no behavior change to the pipeline itself.

**Deliverable (not built in this pass)**: `scripts/update_summary_loc.py` (or similar, alongside the existing `scripts/session_lock.py`/`scripts/validate_run_checksum.py` campaign tooling) that either rewrites the `(N lines)` figures in `SUMMARY.md` in place, or runs as a check that exits non-zero on drift.

**Success criteria**: a future module split (e.g. whichever of REFACTOR-003/008/009 lands next) cannot leave `SUMMARY.md` stale without an explicit, visible failure.

**Commit message template**:
```
Add SUMMARY.md LOC drift check/generator

SUMMARY.md's per-module line counts have gone stale twice after
structural refactors, both times caught by hand rather than
tooling. Automates the wc -l bookkeeping so the next module split
can't silently invalidate it again.
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

---

## Appendix: Technical Decisions

Moved verbatim from `docs/memory/09-decisions.md` (content unchanged, only relocated) — `09-decisions.md` is now a redirect stub pointing here. Format: **Context**, **Decision**, **Consequences**, **Related files**, **Status** (`Active` | `Legacy` | `In migration` | `Uncertain`). Entries marked "inferred" were deduced from project structure/docstrings, not stated as an explicit standalone design document — none was found in the repository.

### D1 — Strict separation of operational config (`settings.yaml`) from scientific config (`parameters.json`)

**Context.** Earlier versions apparently kept LCOE financial parameters (CAPEX, OPEX, discount rate) inside `settings.yaml` alongside infrastructure settings, per the explicit callout in `README.md` §6.1 ("`settings.yaml` no longer contains LCOE financial parameters... Any legacy `lcoe` block in `settings.yaml` is ignored").
**Decision.** All scientific/technology parameters live exclusively in `parameters.json`; `settings.yaml` governs only infrastructure, paths, resolutions, visualization, and phase skip flags.
**Consequences.** A researcher changing a scientific assumption (e.g. a capacity factor) only ever needs to touch one file and cannot accidentally leave a stale value in the "wrong" config. The tradeoff: this separation is enforced only by convention/documentation, not a runtime check that rejects scientific keys if they reappear in `settings.yaml` — see `06-risk-areas.md`.
**Related files.** `configs/settings.yaml`, `configs/parameters.json`, `src/core/config_loader.py`, `src/core/constants.py`.
**Status.** Active. (Inferred from docstrings and README; no standalone ADR document exists.)

---

### D2 — Pydantic v2 schemas (`schemas.py`) replace a legacy `models.py`

**Context.** `schemas.py`'s own docstring states it is the "Autoridade única de modelos de dados — substitui models.py, que foi removido" (single authority for data models — replaces models.py, which was removed).
**Decision.** All phase input/output/config contracts consolidated into one Pydantic v2 module.
**Consequences.** Type safety and validation at persistence boundaries; a single place to look up any contract. However, `pipeline_orchestrator.py`'s own docstring still says "AlignedLayers is imported from schemas, not models" (present tense, defensive phrasing) and `src/core/validators.py` repeats the same note — suggesting the migration left residual references worth double-checking whenever `models.py` is mentioned anywhere in code or comments.
**Related files.** `src/core/schemas.py`, `src/core/validators.py`, `src/core/pipeline_orchestrator.py`.
**Status.** Active. `models.py` itself does not exist in the current tree (confirmed) — only its removal is referenced.

---

### D3 — MCDA math (AHP/TOPSIS/OWA/exclusion) extracted from `suitability_builder.py` into `src/utils/`

**Context.** `suitability_builder.py`'s docstring notes it was "refactored: orchestration only" as of v2.0.
**Decision.** AHP, TOPSIS, OWA, and hard-exclusion logic each became a standalone, technology-agnostic module in `src/utils/`, callable independently of the suitability phase.
**Consequences.** These primitives are reusable for any future MCDA application in the codebase (stated goal in README §4), and independently testable/reviewable — though no tests currently exist (`06-risk-areas.md`). `grid_aligner.py` (Phase 2a) already reuses `src/utils/ahp.py` for multi-height wind resource aggregation, outside the suitability phase.
**Related files.** `src/utils/{ahp,topsis,owa,exclusion}.py`, `src/processors/suitability_builder.py`, `src/processors/grid_aligner.py`.
**Status.** Active.

---

### D4 — TOPSIS as primary suitability surface; OWA as secondary/scenario surface

**Context.** Phase 3 produces both; Phase 4 needs exactly one apt-pixel mask per run.
**Decision.** TOPSIS output is the default input to Phase 4 (`potential_calculator.py`). OWA outputs exist per scenario but selecting them (`use_owa=True`) is implemented and explicitly reserved for future use, not wired into the orchestrator.

The non-integration of OWA into potential/LCOE calculation is deliberate, not an implementation gap. Formal weight-uncertainty analysis is already covered by Phase 8 (SA-1: OAT perturbation; SA-2: Monte Carlo Dirichlet sampling, fixed seed=42, reproducible), which captures weight sensitivity continuously and with statistical grounding. Running OWA with 3 fixed scenarios through the full pipeline would duplicate that analysis with a cruder method, at ~4x processing time and disk cost, without clear scientific gain. TOPSIS remains the primary score because it is an established method in renewable energy siting literature (Hwang & Yoon, 1981) and is already in use for every country processed to date. OWA remains available for comparative visual inspection, preserving the information without making it part of the "official" result.

**Consequences.** Current pipeline runs only reflect the TOPSIS-based apt-pixel definition end-to-end; OWA scenario outputs on disk are informational/comparative only unless someone wires the flag through. Anyone changing this should update this decision entry and `03-pipeline.md`.

BLOCKER-006 (fixed) hardened this decision at the implementation level: TOPSIS-vs-OWA raster discovery across all read call sites is now centralized in `src/utils/raster_io.py::find_suitability_tif()`, with TOPSIS tried first, always, explicitly. Previously `results_writer.py` (Phase 6) had its own, independently-implemented lookup that tried the OWA-balanced file *before* TOPSIS — since Phase 3 always writes the OWA-balanced GeoTIFF unconditionally, that candidate matched on every run, meaning Phase 6's suitability-dominance map (and its printed pixel-count summary) had been silently built from OWA, not TOPSIS, contradicting this decision in practice despite the code/docs stating otherwise. See validation results in the BLOCKER-006 entry above for the before/after numeric impact.

**Related files.** `src/processors/potential_calculator.py`, `src/processors/suitability_builder.py`, `src/processors/sensitivity_analyzer.py`, `src/processors/lcoe_calculator.py`, `src/processors/results_writer.py`, `src/utils/raster_io.py`.
**Status.** Active (TOPSIS path); the OWA-driven alternative is **In migration** / not yet activated. Raster-discovery precedence bug fixed (BLOCKER-006).

---

### D5 — Centralized map styling and text reporting instead of per-phase implementations

**Context.** README §"Architecture Highlights" and multiple module docstrings (`results_writer.py`, `reporting.py`) describe eliminating duplicated `_plot_*` and text-formatting logic that previously existed independently across phases.
**Decision.** All raster maps render through `GeoWorldStyler.render_raster_map()` (`src/utils/map_styling.py`); all phase text reports build through `build_phase_report()` (`src/utils/reporting.py`).
**Consequences.** Visual and textual consistency across all nine phases' outputs — important for a document meant to read as one coherent thesis/publication figure set. New phases must use these rather than writing bespoke plotting/formatting code.
**Related files.** `src/utils/map_styling.py`, `src/utils/reporting.py`, all `src/processors/*.py`.
**Status.** Active.

---

### D6 — GHG abatement scope limited to the electricity generation sector

**Context.** Explicitly stated in `ghg_abatement_calculator.py` (`SCOPE: ELECTRICITY TRANSITION`) and `abatement_plots.py` docstrings.
**Decision.** All Phase 7 figures and calculations model electricity-sector substitution only; any "total national CO₂" figure shown for context includes all sectors and is clearly a different, larger denominator.
**Consequences.** Prevents the common analytical error of implying economy-wide decarbonization from an electricity-only substitution model. Anyone extending Phase 7 to other sectors (transport is instead handled separately in Phase 9) needs a new, explicitly-scoped module rather than expanding this one's scope silently.
**Related files.** `src/processors/ghg_abatement_calculator.py`, `src/utils/abatement_plots.py`, `configs/net_zero_db.json`.
**Status.** Active.

---

### D7 — No automated test suite

**Context.** No `tests/` directory, no `pytest`/`unittest` files found anywhere in the repository at documentation time.
**Decision (inferred, not stated).** Correctness is currently verified manually/visually (inspecting output maps, reports, and comparing to published benchmarks like IRENA LCOE figures) rather than through an automated suite.
**Consequences.** Refactors and dependency bumps carry higher regression risk, especially in numerically sensitive modules (AHP/TOPSIS math, LCOE formulas, GHG substitution logic). See `06-risk-areas.md`.
**Related files.** N/A (absence of files).
**Status.** Uncertain — it is not documented whether this is a deliberate choice for a single-researcher pipeline or simply not yet done.

---

## Sensitivity & Config Migration Campaign

**Purpose.** Following BLOCKER-016 (SA-2's `stable_fraction` → threshold-crossing replacement), a broader question surfaced: `concentration=20` was one hardcoded, undocumented-provenance parameter among several, and it alone moved a reported metric by ~2.5x. This campaign inventories every such parameter found across `suitability_builder.py`, `criteria_builder.py`, `exclusion.py`, `normalization.py`, `economics.py`, `lcoe_calculator.py`, `constants.py` and `sensitivity_analyzer.py`, and for each one: (1) tests sensitivity via OAT, (2) records a human decision on the final value (never an automated "optimal" choice), (3) migrates it from a hardcoded literal to `parameters.json` (scientific/geospatial parameters) or `settings.yaml`'s `pipeline.sensitivity` block (statistical-method meta-parameters), always with a Pydantic schema validating format/range in the same commit as the migration.

**Binding design rule.** A parameter sweep produces a **sensitivity report** (the range of results under different values) — it never makes the pipeline auto-select an "optimal" value and use it as the official result. The official result always comes from the base parameters chosen by a human, informed by the sensitivity report. This rule applies to every row below, no exceptions.

**Scope note.** `_MIN_TECH_SUBSTITUTION` (`ghg_abatement_calculator.py`) was flagged in the originating investigation as a related but out-of-scope candidate (outside the five modules originally audited); not included in the 13 rows below. Rows 1, 12 and 13 live in `sensitivity_analyzer.py` — BLOCKER-017/018 landed (commits `dcf956e`/`7499c61`), so these three rows are unblocked.

| # | Parâmetro | Status | Valor(es) testado(s) | Veredito | Destino final (config file + chave) | Commit |
|---|---|---|---|---|---|---|
| 1 | `concentration` (Dirichlet, SA-2) — `src/utils/sensitivity_math.py:276` | Done (OAT sweep only — no final-value decision) | conc ∈ {10, 20, 40} × {PRT, BRA} × {solar, wind, biomass} (18 cells) — see "Row 1" write-up below | Highly heterogeneous, **not** a uniform ~2.5x — new open finding, needs triage (see below) | Not migrated — `concentration=20` remains the sole hardcoded default (`sensitivity_analyzer.py:675-679`) for every country/tech pair; value decision pending Douglas's triage | `2204109` |
| 2 | `common_exclusions` (lakes_exclusion / protected_areas / proximity_plants) — `suitability_builder.py:167-171` | **Bloco 1 — Task 2 (migration) done** | See "Bloco 1 results" below | lakes_exclusion: keep hardcoded (mathematically inert, no value to tune). protected_areas: 0.99, migrated as-is — **value pending Bloco 6 (IUCN_SCORES)**, not final. proximity_plants: 0.01, migrated as-is — **no documented external justification, review in a literature-comparison pass**, not final. | `parameters.json`'s new `exclusion_thresholds.protected_areas` / `.proximity_plants` (`CountryParams.protected_areas_threshold`/`.proximity_plants_threshold`) | `e6b7da4` |
| 3 | `_SLOPE_OFFSET_DEG` (solar/wind/biomass) — `suitability_builder.py:93-97` | **Bloco 1 — Task 2 (migration) done** | See "Bloco 1 results" below | Current literals (5.0/10.0/20.0°) kept as-is — low priority, no material numeric effect found (<0.3% apt_area swing at ±50%), migrated for config-traceability only, not because a new value was chosen. | `parameters.json`'s new `exclusion_thresholds.slope_offset_deg` (`CountryParams.slope_offset_{solar,wind,biomass}_deg`) | `e6b7da4` |
| 4 | `normalize_percentile` default `p_low=5.0, p_high=95.0` (solar/wind/biomass resource) — `normalization.py:26-27` | Pending | — | — | — | — |
| 5 | `IUCN_SCORES` mapping — `constants.py:177-194` | Pending | — | — | — | — |
| 6 | `_MIN_RESOURCE_COVERAGE=0.05` — `lcoe_calculator.py:112` | Pending | — | — | — | — |
| 7 | `src_cv < 0.01` (biomass constant-resource fallback) — `lcoe_calculator.py:1021` | Pending | — | — | — | — |
| 8 | Scenario deltas `±0.10` (optimistic/conservative) — `configs/settings.yaml` `potential.scenarios` | Pending | — | — | — | — |
| 9 | `compute_terrain_score` weights `0.6×slope + 0.4×TRI` — `criteria_builder.py:216` | Pending | — | — | — | — |
| 10 | `TRI_THRESHOLD=50.0` — `constants.py:200` | Pending | — | — | — | — |
| 11 | Hardcoded percentiles w/ no config path: roads/grid `p_low=5.0,p_high=95.0`; proximity_plants `p_low=5.0,p_high=95.0`; seismic `p_low=2.0,p_high=98.0` — `criteria_builder.py:258,349,589` | Pending | — | — | — | — |
| 12 | `sa4_lcoe_uncertainty` variations `capex=±15%, opex=±15%, cf=±10%` — `sensitivity_analyzer.py:573-575` | Pending (blocked on BLOCKER-017/018) | — | — | — | — |
| 13 | SA-1 "robust" cutoff `rho >= 0.95` — `sensitivity_analyzer.py:362` | Pending (blocked on BLOCKER-017/018) | — | — | — | — |

### Row 1 — `concentration` (Dirichlet, SA-2) OAT sweep

**Status**: OAT sensitivity test complete. The "Done" in the table above refers to this step only — per the campaign's own binding design rule, a sweep produces a report, not an auto-selected value; no final-value decision or config migration has been made.

**Why this row was still "Pending" despite BLOCKER-016 already citing a `concentration` sweep**: BLOCKER-016's cited prototype (`scratchpad/threshold_crossing_prototype.py`) covered BRA/solar only and was never committed — confirmed gone from both the working tree and the full git history (`git log --all` for its filename returns nothing). Its 17.9%/45.4% figures could not be re-derived from that source. The sweep below is an independent implementation against the current `sa2_monte_carlo_weights()` signature, not a replay of the lost code.

**Method**: `scratchpad/oat_sa2_concentration_sensitivity.py` (committed, commit `2204109`) calls `sa2_monte_carlo_weights()` (`src/utils/sensitivity_math.py`) directly — standalone, does not go through `main.py`/`PipelineOrchestrator`. Reuses already-cached Phase 2b criteria TIFs, Phase 3 AHP weight JSONs, and Phase 4's persisted balanced-scenario threshold (read from the `THRESHOLD` tag on `outputs/{code}/potential/tifs/{code}_{tech}_suitable_balanced.tif` — `recover_potential_from_disk()`'s reconstructed dict does not carry a `"threshold"` field, so the production `_balanced_threshold()` fallback path would silently return 0.60 instead of the real 0.75; this harness reads the tag directly to avoid that gap rather than reproducing it). `n_samples=1000`, `seed=42` — both match production defaults.

**Results** (decisive_fraction / boundary_fraction / moderate_fraction; full data in `scratchpad/oat_sa2_concentration_sensitivity_results.json`):

| Country | Tech | conc=10 | conc=20 (production default) | conc=40 | conc=40 / conc=10 ratio |
|---|---|---|---|---|---|
| PRT | solar | 9.7% / 56.2% / 34.1% | 17.2% / 49.0% / 33.8% | 26.7% / 37.7% / 35.6% | 2.76x |
| PRT | wind | 0.4% / 84.5% / 15.1% | 2.3% / 72.1% / 25.5% | 7.2% / 58.3% / 34.5% | **16.3x** |
| PRT | biomass | 6.3% / 38.3% / 55.5% | 20.4% / 32.6% / 47.0% | 40.8% / 27.2% / 32.0% | 6.52x |
| BRA | solar | 17.9% / 36.7% / 45.4% | 28.1% / 27.7% / 44.2% | 45.4% / 19.5% / 35.1% | 2.54x |
| BRA | wind | 31.8% / 27.0% / 41.2% | 53.1% / 19.9% / 27.0% | 66.8% / 14.8% / 18.4% | 2.10x |
| BRA | biomass | 14.7% / 52.1% / 33.2% | 24.0% / 44.4% / 31.6% | 34.5% / 35.5% / 30.0% | 2.35x |

BRA/solar's ratio (2.54x) matches BLOCKER-016's old "~2.5x" claim almost exactly — confirming that figure was numerically correct and is reproducible from the current code/data given the same seed/threshold/weights, **not** independent corroboration from separately-built logic (the lost script's own inputs/assumptions can no longer be checked, only that today's deterministic computation lands on the same number). It does not generalize: PRT/wind swings 16.3x over the identical `concentration` range — more than six times BRA/solar's swing. The heterogeneity itself, not any single point estimate, is this row's real finding.

**Validation**: the `concentration=20` column above is byte-identical to the `Decisive`/`Boundary`/`Moderate` figures already persisted in `outputs/{PRT,BRA}/sensitivity/*_sensitivity_report.txt` (the 2026-08-18 enxugamento validation run), confirming the harness reads the same production inputs (AHP weights, criteria rasters, balanced-scenario threshold=0.75) rather than synthetic data.

**Open finding (new, unassigned — needs Douglas's triage), not closed by this row**: `concentration=20` is the sole hardcoded production default for every country/tech pair (`sensitivity_analyzer.py:675-679`, no per-tech/per-country override exists anywhere). Given PRT/wind's 16.3x swing vs. BRA/solar's 2.5x and BRA/wind's 2.1x over the same tested range, the open question is whether `concentration=20` is appropriate uniformly, or whether high-variance pairs like PRT/wind (and potentially others not yet swept, for countries beyond PRT/BRA) need per-tech/per-country calibration — or at minimum a documented caveat in thesis methodology acknowledging this sensitivity. No priority assigned yet. Tracked in `docs/CURRENT-SPRINT.md` as "Campaign #1 (follow-up)".

**Commit**: `2204109` (harness + results JSON).

### Bloco 1 — `common_exclusions` and `_SLOPE_OFFSET_DEG` (Phase 3 binary gates)

**Status**: Task 1 (OAT sensitivity) complete for PRT, confirmed in BRA. Task 2 (migration) not started — waiting on user confirmation of final values. No production code changed in this pass.

**Method**: standalone harness `scratchpad/oat_phase3_gate_sensitivity.py` (analysis-only, not committed to the module tree). Reuses cached Phase 2b criteria TIFs (`outputs/{ISO3}/criteria_builder/tif/`) and Phase 2a aligned land-cover/slope rasters (`data/processed/{ISO3}/`) directly from disk — does **not** go through `main.py`/`PipelineOrchestrator`, so it never reads or depends on `configs/settings.yaml`'s pipeline skip flags (which BLOCKER-017/018's parallel session currently has mid-edit for its own Phase-8 isolation testing) and never imports `sensitivity_analyzer.py`. For each tested value it replays `get_technology_configs()` → `apply_hard_exclusions()` → `compute_ahp_weights()` → `topsis_spatial()` exactly as `SuitabilityBuilder` does, via `dataclasses.replace()` copies of the real `TechnologyConfig` objects — no monkeypatching of production code. Two figures reported per test: **area_valid** (km², pixels surviving Phase 3's hard-exclusion stage — the population that enters AHP/TOPSIS) and **apt_area** (km², subset of that with TOPSIS score ≥ the country's balanced-scenario threshold, 0.75 for both PRT and BRA — computed directly from Phase 3's own math, not by running Phase 4).

**`common_exclusions` results** (each key varied alone, other two held at base: lakes=0.5, protected_areas=0.99, proximity_plants=0.01):

| Key tested | Values | PRT effect | BRA effect (confirmation run) |
|---|---|---|---|
| `lakes_exclusion` | 0.3 / 0.5 / 0.7 | **Zero effect** — n_excluded, area_valid, apt_area byte-identical across all 3 values, all 3 techs | **Zero effect**, confirmed — identical across all 3 values |
| `protected_areas` | 0.90 / 0.99 | **Zero effect** in this range — identical n_excluded/area/apt across both values, all 3 techs | **Zero effect**, confirmed — identical across both values |
| `proximity_plants` | 0.001 / 0.01 / 0.05 | **Real, monotonic swing.** apt_area: solar −1.76%, wind −6.05%, biomass −1.87% (0.001→0.05); area_valid ≈ −1.9% | **Real, monotonic swing, same direction, smaller magnitude.** apt_area: solar −0.52%, wind −0.60%, biomass −0.62% (0.001→0.05); area_valid ≈ −0.25% |

Explanation for the two zero-effect results (not a harness bug — verified against the underlying score distributions):
- `lakes_exclusion`: `compute_lakes_exclusion()` only ever emits `{0.0, 1.0}` (binary lake/land mask). Any threshold strictly inside `(0.0, 1.0)` — including the full 0.3–0.7 range tested — excludes exactly the same pixels (`score < threshold` catches only `score == 0.0` either way). This parameter is provably inert anywhere in `(0, 1)`, not just at the 3 points tested.
- `protected_areas`: `compute_protected_areas()` emits a **discrete** score ladder from `IUCN_SCORES` — `{0.0, 0.10, 0.25, 0.30, 0.45, 0.55, 1.0}` (candidate row 5 in this same table). Both 0.90 and 0.99 fall inside the same gap, `(0.55, 1.0)`, so no category boundary is crossed between them and the result is identical. This is **not** evidence the parameter has no effect in general — a value crossing into `(0.45, 0.55]` (e.g. 0.50) would additionally exclude IUCN category VI pixels. The as-tested range (0.90/0.99, both requested by the user) just happens to sit in one flat step of that ladder.

`proximity_plants`'s effect direction is consistent between countries (higher threshold → more excluded → less eligible/apt area) but its magnitude is markedly country-dependent: ~3–8x larger relative swing in PRT (small territory, denser plant registry relative to area) than in continental-scale BRA.

**`_SLOPE_OFFSET_DEG` results** (each tech's offset varied alone at 0.5×/1.0×/1.5× its current value, other two techs held at base):

| Tech | Offsets tested (deg) | PRT: raw `slope_excluded` swing | PRT: apt_area swing | BRA: raw `slope_excluded` swing | BRA: apt_area swing |
|---|---|---|---|---|---|
| solar | 2.5 / 5.0 / 7.5 | 1,356 → 366 → 80 (−94% top-to-bottom) | 10,438.1 → 10,447.8 → 10,446.8 km² (**+0.08%**) | 7,620 → 2,791 → 864 (−89%) | 480,304.9 → 480,301.3 → 480,298.9 km² (**−0.001%**) |
| wind | 5.0 / 10.0 / 15.0 | 366 → 13 → 0 | 2,811.5 → 2,804.8 → 2,804.8 km² (**−0.24%**) | 2,791 → 216 → 12 | 281,129.7 → 281,140.0 → 281,140.0 km² (**+0.004%**) |
| biomass | 10.0 / 20.0 / 30.0 | 13 → 0 → 0 | 2,620.6 km² unchanged (**0%**) | 216 → 0 → 0 | 442,873.5 → 442,874.6 → 442,874.6 km² (**~0%**) |

Both countries show the same pattern: halving or increasing the slope offset by 50% swings the **raw** slope-exclusion pixel count by up to ~90%, but the **final eligible/apt area** barely moves (well under 0.3% in every cell, both countries) — because the pixels the slope gate would additionally exclude or admit are, almost entirely, pixels *already* excluded by `proximity_plants`/`protected_areas`/other criteria or already outside the country mask. Not confirmed as a "material" finding under this pass's own quantification (see below), so no BRA-vs-PRT generalization claim beyond what's shown in both columns above — both countries were run in full regardless, since the harness computes all countries in one pass.

**Materiality call for triggering the BRA confirmation run** (per instructions, this is a reporting judgment only — not a value decision): swings under ~0.3% on final apt_area were treated as not material and not singled out for separate discussion; `proximity_plants`'s 0.5–6% apt_area swing was treated as material and is the only row confirmed in BRA before being written up above as a relevant finding. `lakes_exclusion`/`protected_areas` were run in BRA anyway (same script pass) and both confirmed zero effect, consistent with the structural explanation above rather than needing a materiality judgment call.

**No verdict recorded** (as of the OAT sensitivity pass above). Task 2 (migration) followed, per the user's explicit per-item decisions:

### Bloco 1 — Task 2 (migration, completed)

**Decisions** (user-directed, not automated):
1. `lakes_exclusion` — confirmed mathematically inert in all of `(0, 1)` (see OAT results above). **Not migrated** — `parameters.json`/Pydantic would imply a real tunable value that doesn't exist. `suitability_builder.py`'s `common_exclusions` dict keeps the hardcoded `0.5` literal with a comment explaining why (binary criterion, `crit_arr < threshold` always selects exactly the `0.0` pixels regardless of the exact threshold value in `(0, 1)`).
2. `protected_areas` — migrated at its current value (0.99), **explicitly marked as a pending value decision**, not final — depends on Bloco 6's `IUCN_SCORES` mapping work (row 5 in the table above), since 0.99 currently sits in the same flat step of the IUCN score ladder as several other candidate values and the "right" threshold can't be chosen independently of that ladder's own review.
3. `proximity_plants` — kept at its current operational value (0.01), migrated with Pydantic validation, **flagged as having no documented external justification** — a real, monotonic, country-dependent effect was found (PRT apt_area swings up to −6.05% for wind across the tested range; BRA up to −0.62%), so this is not a "safe to ignore" parameter — it should be revisited against literature/regulatory siting-distance standards in a future pass, not treated as settled just because it's now in a config file.
4. `_SLOPE_OFFSET_DEG` — migrated at current literals (5.0/10.0/20.0°), no value change — the OAT pass found under 0.3% apt_area effect at ±50% swings for every tech/country cell, so this migration is purely for config-traceability (removing a hardcoded-and-therefore-invisible parameter), not a response to a material sensitivity finding.

**Implementation**: new `parameters.json` top-level key `exclusion_thresholds` (global, not per-country — matches the pre-migration behavior, which was a single hardcoded dict shared by all countries):
```json
"exclusion_thresholds": {
  "protected_areas": 0.99,
  "proximity_plants": 0.01,
  "slope_offset_deg": { "solar": 5.0, "wind": 10.0, "biomass": 20.0 }
}
```
Note: the user's original instruction said "parameters.json/land_suitability" for the slope offsets — `land_suitability` was already a top-level key with an unrelated meaning (ESA WorldCover class → tech suitability score), so a new `exclusion_thresholds` key was used instead to avoid a semantic collision; flagged for the user to correct if a different placement was intended. Five new `CountryParams` fields (`protected_areas_threshold`, `proximity_plants_threshold`, `slope_offset_{solar,wind,biomass}_deg`), all `Field(..., ge=..., le=...)`-validated, populated via a new `ConfigLoader.exclusion_thresholds` property (mirrors the existing `land_suitability` property pattern) with fallback defaults identical to the pre-migration literals. `suitability_builder.py`'s `get_technology_configs()` now reads these from `CountryParams` instead of the module-level `_SLOPE_OFFSET_DEG` constant and the inline `common_exclusions` literals (which remain only as fallback defaults for legacy dict-style `country_params`).

**Validation (as performed)**:
- Confirmed via direct instantiation (`ConfigLoader.get_country()` + `get_technology_configs()`) that resolved values are byte-identical to the pre-migration hardcoded literals for both PRT and BRA (`hard_exclusions={'lakes_exclusion': 0.5, 'protected_areas': 0.99, 'proximity_plants': 0.01}`, `slope_max_deg` = 15.0/20.0/30.0 for PRT, 17.0/22.0/32.0 for BRA).
- Full Phase 3 (Suitability) run for PRT and BRA (Phases 1-2b reused from cache): all suitability/OWA GeoTIFFs and AHP weights JSONs confirmed **byte-identical** (`md5sum` diff empty) against a pre-migration baseline copy of the same outputs.
- `pytest tests/` — 78/78 passing.
- `configs/settings.yaml` reverted to the standing configuration, confirmed via empty `git diff`.

**Commit message** (as shipped):
```
Migrate Bloco 1 exclusion-gate parameters to parameters.json
```

---

## Invariant Validation Project (2026-08-18 audit)

**Purpose.** A repo-wide audit dated 2026-08-18, run after BLOCKER-005's narrowing and BLOCKER-020's registration both surfaced the same underlying shape of gap in two different subsystems (Phase 4 and Phase 2b respectively) — an input's *type* is accepted, or a lookup silently falls back, without validating its *content*. This project catalogues every phase-scoped instance of that gap found across all 9 pipeline phases: 16 items, INVAR-001 through INVAR-016. **Documentation only** — no file under `src/` was modified to register this project; nothing listed below has been fixed yet.

**Pattern definitions.**
- **Pattern 1 (P1) — silent fallback**: a missing or malformed value is quietly replaced via `.get(default)` or a bare `except: pass`, instead of failing loudly at the point of the mistake.
- **Pattern 2 (P2) — unrestricted input type**: a function or parameter accepts `Union[Model, Dict]` — a validated model or a raw, unvalidated dict standing in for it.
- **Pattern 3 (P3) — multiple accepted input formats, no content-level validation**: a consumer branches on shape (`isinstance(...)`, `"key" in data`) to accept several input formats, without validating the values once a shape is matched.

**Active vs. latent.** "Active" = the gap is reachable through a real call path in current production usage (`main.py`). "Latent" = the code path exists but nothing in current production (`main.py`, `tests/`, `scripts/`, `scratchpad/`) exercises the vulnerable branch today — same distinction BLOCKER-005 and BLOCKER-020 already used after their own narrowing/scoping passes.

### The 16 items

| # | Location | Phase | Pattern | Status | Notes |
|---|---|---|---|---|---|
| INVAR-001 | `data_auditor.py:1023-1036` | Phase 1 | P1 | Active | |
| INVAR-002 | `data_auditor.py:877` | Phase 1 | P1 | Latent (low severity) | |
| INVAR-003 | `grid_aligner.py:910,960,968-969` | Phase 2a | P1 | Latent (low severity) | |
| INVAR-004 | `criteria_builder.py` — `ParamsLike` + `_param()` + 12 call sites | Phase 2b | P1+P2 | Latent | Absorbs and closes **BLOCKER-020** — see that entry (marked Closed, not deleted) for the full ~12-call-site inventory |
| INVAR-005 | `suitability_builder.py:140,144,162,167-169,190-191` | Phase 3 | P1 | Latent | |
| INVAR-006 | `suitability_builder.py:339` | Phase 3 | P1 | Latent (low severity) | |
| INVAR-007 | `potential_calculator.py:439` | Phase 4 | P1 | Active | Relates to **BLOCKER-005**'s remaining (not-closed) scope — same site; see that entry's "Related" note |
| INVAR-008 | `lcoe_calculator.py:527-560` (`_resolve_threshold`) | Phase 5 | P1 | Active | |
| INVAR-009 | `results_writer.py:162-211` (`_normalize_potential`/`_normalize_lcoe`) | Phase 6 | P3 | Active | Gated on **BLOCKER-010** — see that entry, not duplicated here |
| INVAR-010 | `results_writer.py:213-229+` (`_normalize_abatement`) | Phase 6 | P3 | Active | Gated on **BLOCKER-010** |
| INVAR-011 | `data_recovery.py` (`recover_potential_from_disk`/`recover_lcoe_from_disk`/`recover_abatement_from_disk`) | Phase 6 | P3 | Active | Gated on **BLOCKER-010** |
| INVAR-012 | `ghg_abatement_calculator.py:946` | Phase 7 | P3 | Active | |
| INVAR-013 | `ghg_abatement_calculator.py:1031-1054` | Phase 7 | P1 | Active | |
| INVAR-014 | `sensitivity_analyzer.py:230-278` (`_resolve_tech_params`) | Phase 8 | P1+P3 | Active | |
| INVAR-015 | `sensitivity_analyzer.py:484-496` | Phase 8 | P1 | Active | |
| INVAR-016 | `sensitivity_analyzer.py:418` | Phase 8 | P1 | Latent (minor) | |

**Cross-references, both directions:**
- **INVAR-004 ↔ BLOCKER-020**: INVAR-004 absorbs BLOCKER-020's finding (`criteria_builder.py`'s `ParamsLike`/`_param()` gap, ~12 call sites). BLOCKER-020's entry is marked `Status: Closed — absorbed by INVAR-004`, not deleted — its full call-site inventory and the Row 9/10/11 overlap analysis it already contains remain the canonical detail for this item; this section does not repeat that inventory.
- **INVAR-007 ↔ BLOCKER-005**: INVAR-007 tracks the same `potential_calculator.py:439` fallback site BLOCKER-005 (narrowed, 2026-08-18) already covers. BLOCKER-005 is **not** closed by this cross-reference — its remaining open scope (the load-time three-scenario-key validator described in its Update) stands as written; BLOCKER-005's entry now carries a "Related: INVAR-007" note pointing here.
- **INVAR-009/010/011 ↔ BLOCKER-010**: all three Phase 6 items are gated on BLOCKER-010 landing — until Phase 6 actually receives the live in-memory object instead of always reconstructing from disk, the live-object branch of `_normalize_potential`/`_normalize_lcoe`/`_normalize_abatement` and the equivalent `data_recovery.py` functions can't be exercised end-to-end to validate a fix. See the existing BLOCKER-010 entry for the full architecture discussion — not duplicated here. BLOCKER-010's entry now carries a "Related: INVAR-009/INVAR-010/INVAR-011" note pointing here.

### Phase 9 (dormant, unnumbered)

Four additional findings were noted in `transport_decarbonization_calculator.py` during the same audit pass:
- `transport_decarbonization_calculator.py:106-156`
- `transport_decarbonization_calculator.py:163-211`
- `transport_decarbonization_calculator.py:217-269+`
- `transport_decarbonization_calculator.py:1509` / `:1635`

These are **explicitly not part of the 16-item INVAR-001–016 count**. Phase 9 is dormant (`skip_transport: true`, per BLOCKER-019's crash and BLOCKER-012's double-persistence bug) — auditing invariant gaps in a phase that cannot currently complete a run is lower priority than the 16 items above, all of which sit on active phases. Revisit these four only if/when Phase 9 is reactivated (i.e., after BLOCKER-019 is fixed), at which point they should either be folded into a Phase-9 batch of their own INVAR numbers or reassessed against whatever Phase 9 looks like post-fix.

---

**Editorial note (added at move time, not part of the original entry)**: D7 is now stale — `tests/unit/` exists (`pytest`, 77 tests as of BLOCKER-011) since QI-001. Left unchanged above per the "move, don't rewrite" rule for this appendix; flagging here rather than editing the historical entry itself.

---

### sensitivity_analyzer.py enxugamento (2026-08-18) — complete

**Status**: Done and fully validated. 6 commits, each independently validated and containing exactly one category of change (2a extraction vs. 2b redundancy-removal vs. new-output-addition, never mixed), per the standing instruction for this task.

**Context**: `sensitivity_analyzer.py` had grown to ~1800 lines mixing pure computation (SA-1 through SA-6, TOPSIS core) with orchestration (I/O, caching, report formatting). This pass reduced it to orchestration-only, fixed two small, independently-discovered redundancy/hygiene issues, and closed the one real output-consistency gap (SA-2 was the only sub-analysis with no persisted CSV).

**Commits**:
1. `fa0684c` — `_load_suitability_from_disk()` now reuses `_load_weights_from_disk()` instead of duplicating its rglob/parsing logic, keeping its own equal-weights fallback on top. Investigated first (per explicit instruction) whether "always reread weights from disk" was the BUG_07/BLOCKER-010 pattern (preferring disk over a live in-memory object) — confirmed it is **not**: `SuitabilityStats` (the Pydantic schema for a successful Phase 3 tech result) never carries the actual weight values in memory, only a `weights_json: Path` pointer (confirmed both by reading the schema and by inspecting a real persisted `result.pkl`), so disk is the only real source of the weights either way.
2. `350c80c` (2a, extraction) — moved 11 pure functions (`_topsis_flat`, `_load_criteria_arrays`, `_balanced_threshold`, `_build_ghg_function_from_abatement`, `sa1_oat_weight_sensitivity` through `sa6_potential_sensitivity`, `_sfmt`) verbatim to new `src/utils/sensitivity_math.py`, mirroring the existing `sensitivity_plots.py`/`abatement_plots.py` split (REFACTOR-001/002). Zero logic change — verified via a full Phase 8 PRT run producing byte-identical CSVs and report text against a pre-extraction baseline. Unused `numpy`/`pandas`/`warnings` imports dropped from `sensitivity_analyzer.py` after the move (pyflakes-clean).
3. `a163f36` (2b) — removed the silent `try/except ImportError` fallback around the `sensitivity_plots` import (previously set every `plot_*` function to `None` on failure, which would have surfaced a broken import as a confusing per-SA `TypeError: NoneType not callable` instead of a clear startup error — `sensitivity_plots` is a first-party sibling module, not an optional dependency). Also fixed the module docstring's SA-5/SA-6 swap (labeled backwards relative to the actual function names and `docs/memory/04-algorithms.md`).
4. `5010176` (2b) — removed the orphaned `base_area` field from SA-6's output (`sa6_potential_sensitivity()`'s `df.attrs` and `results_sa[tech]["sa6"]`): computed and persisted but never read anywhere (absent from `_format_report()`, absent from `sensitivity_plots.py`), and redundant with SA-3's own `area_apt_km2`, already persisted per-threshold.
5. `d7a4729` (new output) — added `sa2_distribution_summary()` (`sensitivity_math.py`) and its CSV output `{ISO3}_{tech}_sa2_distribution_summary.csv`, closing the one real output-consistency gap: SA-2 was the only sub-analysis with no persisted CSV (its per-pixel `cv`/`ci_width`/`crossing_fraction` arrays only fed a PNG). Format proposed and approved before coding: 3 rows (one per metric), columns `n_pixels, mean, std, p05, p25, p50, p75, p95` — not a raw per-pixel dump. `crossing_fraction`'s percentiles are restricted to `apt_base_mask`, the same population `decisive_fraction`/`boundary_fraction`/`moderate_fraction` are already computed over.
6. `1150ad1` — interim handoff note (superseded by this entry).

**Final validation (2026-08-18, full Phase 8 pipeline, PRT and BRA, cache 1-7 reused)**:
- Every pre-existing CSV and the report `.txt` for both countries confirmed **byte-identical** against a baseline captured immediately before this run — the only difference found was the report's embedded `date.today()` stamp (day rolled over from 08-17 to 08-18 between baseline capture and this run; not a regression).
- All 6 new `{ISO3}_{tech}_sa2_distribution_summary.csv` files generated (solar/wind/biomass × PRT/BRA) with correct row counts matching already-known values from this session's logs (e.g. PRT wind: `cv`/`ci_width` at `n_pixels=92402` matching the tech's total territorial pixel count, `crossing_fraction` at `n_pixels=5462` matching the already-logged `n_apt_base`).
- `pytest tests/` — 78/78 passing.
- `configs/settings.yaml` reverted to the standing configuration, confirmed via empty `git diff`.

**Not touched, per standing instruction**: BLOCKER-010.
