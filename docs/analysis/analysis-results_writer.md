---
id: analysis-results-writer
type: frozen
status: frozen
created: 2026-08-11
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [analysis-code-duplication, analysis-write-points-inventory]
linked_by: [docs-tasks, archive-backlog-full]
scope: "Leitura pós-fix da Fase 6 (results_writer.py), congelada em 2026-08-11; origem textual do BLOCKER-010/BLOCKER-011 — NÃO reflete mudanças posteriores ao código."
---

# `results_writer.py` (Phase 6) — Analysis

Analysis only. No code changed in this pass. Full line-by-line read of `src/processors/results_writer.py` (1108 LOC, current `main` HEAD `6d59120`) plus supporting reads of `main.py` (Phase 4–6 orchestration block), `src/core/pipeline_orchestrator.py` (`run_phase`/`_persist`), and `src/utils/data_recovery.py` (`recover_potential_from_disk`, `recover_lcoe_from_disk`) to trace the two findings below to their actual root cause rather than stopping at this file's boundary. One finding (§1b) is verified empirically against this session's own CHECKPOINT-2 pipeline output, not just by reading code.

This module is dramatically cleaner than the 1553-LOC version `docs/arch-misalignments.md` describes (now 1108 LOC) — REFACTOR-007 (this engagement, commit `6d59120`) already moved `_plot_executive_dashboard`/`_build_dashboard_layout` out, and the BLOCKER-series commits already removed `_enrich_lcoe_stats`, `_recover_supply_curve_from_tif`, and `_compute_integrated_area` entirely. See §2 for the full fixed-issue ledger. The two findings below (§1) are new to this pass and are the most significant results of all three analyses in this series.

---

## 1. New findings — candidates for a new BLOCKER

### 1a. [HIGH — BLOCKER CANDIDATE] The literal "BUG_07" pattern is still present today, systemically, on every run

The task that produced this document specifically asked whether "Phase 6 always reconstructing from disk instead of using the live object" (referred to as the BUG_07 pattern) has any remaining, unaddressed instance. It does — and it is not a rare edge case, it is the **normal, only path Phase 6 ever takes**, on every single pipeline run, for both Potential and LCOE results.

**The evidence chain:**

1. `main.py:806-809` carries this exact comment, already in the codebase:
   > `# BUG_07 (fix): lcoe_result is a LCOEResult model (or None), not a Path. ResultsWriter expects a Path for lcoe_dir. Resolve to the canonical LCOE output directory regardless of whether lcoe_result was loaded from disk or computed now.`

   This documents that BUG_07 was a real, named bug: `lcoe_result` (the live, validated `LCOEResult` Pydantic model Phase 5 just produced) was being passed where a directory `Path` was expected, causing a type-mismatch failure. The fix applied: **always resolve to the canonical output directory** (`country_out / "lcoe"`, `main.py:810,821`) **regardless of whether the live object is available.** The same pattern applies to `potential_dir` (`main.py:820`, `country_out / "potential"`, always a Path — no bug comment exists for this one, but the effect is identical).

2. `ResultsWriter._normalize_potential`/`_normalize_lcoe` (`results_writer.py` L153–202) already contain the *correct* dual-mode logic: `if isinstance(data, dict) and "techs" in data: return data` — i.e., "use the live result directly if you were handed one." **This branch is dead code in current production usage.** Because `main.py` never passes anything but a `Path`, this `isinstance` check always fails and every single call falls through to `recover_potential_from_disk(...)` / `recover_lcoe_from_disk(...)`.

   (Side note on the fix's own limits: even if `main.py` were changed to pass `pot_result`/`lcoe_result` directly, it would still fail this `isinstance(data, dict)` check, because `pipeline_orchestrator.run_phase()`'s `validate_result_object()` — confirmed by reading `src/core/validators.py:232-258` — returns an actual Pydantic **model instance**, not a dict. The correct fix would need `main.py` to pass `pot_result.model_dump()` / `lcoe_result.model_dump()` when available, not the bare model.)

3. This is not a theoretical risk. `src/utils/data_recovery.py:292-299` documents, in its own comment, a **real historical numeric divergence caused by exactly this always-reconstruct-from-disk pattern**: recovering LCOE stats from the zonal CSV's per-admin-region *means* produced 48.7 USD/MWh for solar, versus 43.8 USD/MWh from Phase 5's real, live, pixel-level computation — a difference that reached the Phase 6 text report before being caught. The fix applied (part of the extended BLOCKER-002 work) was to overlay pixel-level stats parsed fresh from the LCOE TIF on top of the CSV recovery (`data_recovery.py:300-326`) — which patches the symptom (the specific field that diverged) but does not change the underlying architecture: Phase 6 still never uses the live object, it always re-derives from disk, technology by technology, on every run.

4. Present-day consequence: `recover_potential_from_disk` re-aggregates real Phase-4-persisted CSV columns (`.sum()` over `capacity_mw_sum`/`generation_*`/`area_km2_sum` — genuinely lossless by construction, and this is exactly what CHECKPOINT-2's zero-diff result confirms). `recover_lcoe_from_disk` goes further: it **re-opens the LCOE GeoTIFF and re-runs `np.percentile` on the raw pixel array** to reconstruct `mean`/`p10`/`p25`/`median`/`p75`/`p90`, redoing work Phase 5 already did in memory a few phase-timings earlier in the exact same process.

**Why CHECKPOINT-2's "zero diff vs CHECKPOINT-1" result does not clear this finding**: both runs being compared take the identical disk-reconstruction code path (`main.py` always passes a `Path`), so the comparison only proves disk-recovery is deterministic run-over-run — it says nothing about whether the live-object path (currently dead code) would produce the same numbers, because that path is never exercised.

**Severity rationale**: same class of issue, same root mechanism, and same file area (`data_recovery.py` reading Phase 4/5 disk artifacts for Phase 6) as BLOCKER-001/002/003, which were previously treated as BLOCKER-tier because they produced real numeric divergence from a proxy/approximation. This one already has a documented instance of exactly that (item 3 above) and the architectural cause was never removed, only patched at the specific symptom. Flagging as a BLOCKER candidate per instructions — **not fixed in this pass**, awaiting a decision on scope (likely: `main.py` passes `.model_dump()` of the live result when non-`None`, and `_normalize_potential`/`_normalize_lcoe` keep disk recovery only as the genuine fallback for skip-with-cache scenarios where no live object exists).

### 1b. [HIGH — CONFIRMED, verified empirically] Double persistence silently discards data, not just wastes I/O

`write-points-inventory.md` (written before this analysis) already flagged that `results_writer.py` calls `ArtifactManager.save_result()`/`save_manifest()` itself (`_persist_artifacts`, L576–625) **in addition to** `PipelineOrchestrator.run_phase()`'s own unconditional automatic `self._persist(phase_name, result)` (`pipeline_orchestrator.py:165`) — and characterized this as "doing real disk I/O twice per phase per run." Reading both call sites in full shows the actual consequence is worse than redundant I/O: **the second write overwrites the first with a strictly smaller object, permanently discarding fields the first write computed.**

- `ResultsWriter._persist_artifacts` (L576–625) builds its own `serializable` dict (L593–606) that includes `dominance_suitability_counts` and `dominance_lcoe_counts` — a per-technology pixel-count breakdown of both dominance maps — and saves it via its own `ArtifactManager(self.outputs_dir, country_code)` instance, to `phase_dir = artifact_mgr.phase_dir("results")`.
- This call happens *inside* `run()`, near the end (L402–406), **before** `run()` returns.
- `run()` then returns `results` (L408) — a dict that was only ever populated with `country`, `timestamp`, `timings`, `exported_tifs`, `elapsed_total` (L325–329, L372, L379–381). It **never contains** `dominance_suitability_counts`/`dominance_lcoe_counts` — those only ever existed in `_persist_artifacts`'s local `serializable` variable.
- `pipeline_orchestrator.run_phase()` (L153/165) then takes this returned `results` dict and calls its own `self._persist("results", results)`, which saves it to the **same phase directory**, overwriting the file `_persist_artifacts` just wrote.

**Empirical confirmation** (not just code-reading): this session's own CHECKPOINT-2 full pipeline run (`python main.py Portugal`, completed clean, see the CHECKPOINT-2 report) produced `outputs/PRT/results/result.pkl` containing exactly:
```
['country', 'elapsed_total', 'exported_tifs', 'timestamp', 'timings']
```
No `dominance_suitability_counts`, no `dominance_lcoe_counts` — confirming the overwrite happens on every real run, not just in theory.

**Practical impact today**: `grep -rn "dominance_suitability_counts\|dominance_lcoe_counts" src/` finds these two keys defined *only* at their `_persist_artifacts` construction site — nothing else in `src/` reads them back from `result.pkl`. So today this is a confirmed-real but currently-silent bug: no downstream consumer is broken *yet*, because nothing depends on the field that gets lost. But the code computing and writing those counts clearly intended them to be part of Phase 6's persisted output (why else compute and save them explicitly?), and any future feature that reads `outputs/{ISO3}/results/result.pkl` expecting a dominance breakdown will get a silent `KeyError`-shaped surprise rather than the data — with no error at the point where the data was actually lost.

**Not fixed in this pass** — flagged as a second BLOCKER candidate, likely resolvable alongside `write-points-inventory.md`'s existing double-persistence recommendation ("decide whether phase-level `ArtifactManager` calls or the orchestrator's automatic `_persist()` is authoritative, remove the redundant one"): if `_persist_artifacts` is kept, `run()`'s returned dict should include everything `_persist_artifacts` persists (so the orchestrator's overwrite is harmless); if the orchestrator's automatic persist is kept as authoritative, `_persist_artifacts`'s manual `ArtifactManager` call should be removed and `dominance_suitability_counts`/`dominance_lcoe_counts` folded into `run()`'s returned `results` dict instead.

---

## 2. Previously-flagged issues confirmed FIXED

| # | Old finding | Where it lived | Current status |
| --- | --- | --- | --- |
| 1 | `_enrich_lcoe_stats` recomputed p25/median/p75 from the raw LCOE TIF, duplicating a calculation Phase 5 should own | `arch-misalignments.md` §1a | **Fixed — function removed entirely.** `_format_report` now reads `stats = lcoe_results.get("techs", {}).get(tech, {}).get("stats", {})` (L1020–1024) directly, since Phase 5 now computes and persists these fields itself (see `docs/analysis-lcoe_calculator.md` §1 item 2). |
| 2 | `_recover_supply_curve_from_tif` reconstructed a "1 MW/pixel proxy" supply curve, admittedly diverging from Phase 5's real GW totals | `arch-misalignments.md` §1a, `refactoring-roadmap.md` BLOCKER-001 | **Fixed — function removed entirely.** Replaced by `_recover_supply_curve` (L229–239), which calls `recover_supply_curve_from_disk()` reading Phase 5's real persisted Parquet — module docstring (L29–30) explicitly cites BLOCKER-001. |
| 3 | `_compute_integrated_area` re-read Phase 4's suitable-pixel TIF and recomputed area, duplicating Phase 4's own calculation | `arch-misalignments.md` §1a, `refactoring-roadmap.md` BLOCKER-003, `write-points-inventory.md` §1 | **Fixed — function removed entirely.** `_format_report` now reads `area = sc.get("area_km2", 0.0)` (L1030) where `sc = get_scenario_data(potential_results, tech, "balanced")` — comment explicitly cites BLOCKER-003 ("read directly from Phase 4's real, persisted value instead of re-deriving from the suitable TIF"). This closes the loop on `docs/analysis-potential_calculator.md` §1 item 3, which could only confirm the producer side. |
| 4 | `_get_scenario_data` existed as both a class method and a duplicate module-level function — dead indirection | `arch-misalignments.md` §1a | **Fixed.** Neither exists in the current file. Replaced by a single shared `get_scenario_data()` imported from `src.utils.params_helpers` (L74, used L1019). |
| 5 | `_plot_executive_dashboard`/`_build_dashboard_layout` (~161 lines) never actually got moved to `DashboardPanels` despite the module docstring claiming the refactor was done | `arch-misalignments.md` §1a/§3 | **Fixed.** This is this engagement's own REFACTOR-007 (commit `6d59120`, same session). `run()` now calls `self.panels.draw_executive_dashboard(...)` (L357), `self.panels = DashboardPanels(self.styler)` (L110). `_draw_dominance_on_ax` intentionally stays in this module (needs `self._admin_gdf` and raster/GDF-specific rendering) — this was a deliberate scope decision at REFACTOR-007 time, not an oversight. |
| 6 | Suitability TIF discovery risked TOPSIS/OWA divergence vs. `lcoe_calculator.py`'s copy | `code-duplication.md` §2b (Med-High risk) | **Fixed.** L75/422 use the shared `raster_io.find_suitability_tif()` (BLOCKER-006) — confirmed identical to `lcoe_calculator.py`'s usage in `docs/analysis-lcoe_calculator.md` §1 item 5. Module docstring (L15–20) explicitly documents the fix and the prior bug (OWA silently overriding TOPSIS). |
| 7 | Dominance GeoTIFF export (`_write_uint8` closure) used raw `rasterio.open(..., "w", **profile)` | `code-duplication.md` §4a, `refactoring-roadmap.md` | **Fixed.** L950: `with safe_raster_write(str(path), ...) as dst:`. This engagement's REFACTOR-005 (commit `a9e9a1f`). |

---

## 3. Remaining LOW-severity items (unchanged from prior docs, re-confirmed present)

- **`_build_rgba`** (L698–747) still lives in this module rather than `map_styling.py`'s `GeoWorldStyler`, per `arch-misalignments.md`'s "Extract to" suggestion. Cosmetic/organizational only — confirmed still open, no new information.
- **`_build_integrated_summary_section`** (L1081–1109, module-level) is still a hand-rolled fixed-width ASCII table, and it remains unconfirmed whether `src/utils/reporting.py`'s `ReportSection`/`build_phase_report` machinery could already generalize it (not read in this pass — out of scope for the three assigned modules). Still open exactly as `arch-misalignments.md` left it.
- **`_find_lcoe_tif`** (L116–147) remains a private, per-phase candidate-glob lookup, structurally identical in *purpose* to `lcoe_calculator.py`'s still-open `_find_potential_suitable_tif`/`_find_resource_tif` (`docs/analysis-lcoe_calculator.md` §2d) — lower risk than the TOPSIS/OWA case since there's no documented behavioral divergence, just duplicated glob logic across phases.
- No bare `0.60` literal found anywhere in this file (confirmed by grep) — nothing to fix here.

---

## 4. Dead / duplicate code

- **`vulture src/ --min-confidence 0`**: zero findings beyond `unused class 'ResultsWriter'` (60% confidence) — the standard dynamically-instantiated-phase-class false positive shared by all processor modules (see `docs/analysis-lcoe_calculator.md` §4).
- **`ruff check --select F401,F821,F841`**: all checks pass, zero findings — including no flag on the `ArtifactManager` import (L66), because it genuinely is used (L590) in this file, unlike the dead import that was found and already removed from `lcoe_calculator.py`.
- No orphaned functions or duplicate constant tables found by manual read, beyond the double-persistence issue already covered in §1b (which is a logic bug, not dead code in the vulture/ruff sense — both call sites execute, the second just clobbers the first).

---

## 5. Summary

This is the most consequential of the three analyses in this series. Seven previously-flagged issues in this file are confirmed fixed by direct code inspection, several matching this engagement's own recent commits (REFACTOR-005, REFACTOR-007, BLOCKER-001/003/006). But the two questions this task specifically asked about — "is BUG_07 still present anywhere?" and "any other scientific/numerical risk in the same category?" — both turned up real, current, verifiable findings:

1. **§1a**: The BUG_07 pattern (Phase 6 reconstructing from disk instead of using the live object) is not just present, it is the *only* path Phase 6 ever takes today, for both Potential and LCOE results, on every single run — confirmed by tracing the actual call site in `main.py`, the dead dual-mode branch in `ResultsWriter._normalize_*`, and a real historical numeric divergence this exact pattern already caused once (documented in `data_recovery.py`'s own comments).
2. **§1b**: A previously-known "redundant I/O" observation turned out, on full read, to be an active data-loss bug — proven empirically against this session's own CHECKPOINT-2 output, not just inferred from reading the code.

Both are flagged as BLOCKER candidates per instructions. **Neither was fixed in this pass** — this document only identifies and documents them, per the task's explicit scope. Recommend the user decide sequencing/priority for both before any implementation work begins.
