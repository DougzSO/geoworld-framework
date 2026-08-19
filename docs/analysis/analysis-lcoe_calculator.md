---
id: analysis-lcoe-calculator
type: frozen
status: frozen
created: 2026-08-11
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [analysis-code-duplication, analysis-write-points-inventory]
linked_by: [docs-tasks]
scope: "Leitura pós-fix da Fase 5 (lcoe_calculator.py), congelada em 2026-08-11; origem textual do GAP-001 (mask_source) — NÃO reflete mudanças posteriores ao código."
---

# `lcoe_calculator.py` (Phase 5) — Analysis

Analysis only. No code changed in this pass. Full line-by-line read of `src/processors/lcoe_calculator.py` (1493 LOC, current `main` HEAD `6d59120`). Cross-referenced against `docs/code-duplication.md`, `docs/refactoring-roadmap.md`, `docs/arch-misalignments.md` (all written before the BLOCKER-001…006 and REFACTOR-001…007 fix commits) and against `git log` to distinguish what those documents flagged that has since been fixed from what is still genuinely open. Also cross-referenced against `results_writer.py` for one shared-lookup claim (confirmed by grep, not a full read of that file — full read follows in its own pass/doc).

---

## 1. Previously-flagged issues confirmed FIXED (verify-before-report — do not re-flag)

The following issues were documented in earlier analysis passes as real problems in this file. All are confirmed resolved by reading the current code, not just by the old docs — each is quoted with the current line evidence.

| # | Old finding | Where it lived | Current status |
| --- | --- | --- | --- |
| 1 | `_recover_supply_curve_from_tif` "1 MW/pixel proxy" needed downstream because Phase 5 never persisted the real supply curve | `refactoring-roadmap.md` BLOCKER-001 | **Fixed.** L1147–1151: `supply_curve.to_parquet(out_data / f"{country_code}_{tech}_supply_curve.parquet", ...)` — comment explicitly cites BLOCKER-001. Commit `c5986a7`. |
| 2 | LCOE stats only had p10/p90/mean; Phase 6 had to approximate p25/median/p75 | `refactoring-roadmap.md` BLOCKER-002 | **Fixed.** L1076–1080 compute `median`, `p25`, `p75` directly via `np.percentile` in the same pass as `p10`/`p90`/`mean`. Commit `14c1971`. |
| 3 | Two independently-maintained "emergency fallback capacity factor" tables (`lcoe_calculator._irena_defaults` vs `constants.DEFAULT_TECH_PARAMS`) that had drifted apart — flagged as the single highest-severity finding in `code-duplication.md` | `code-duplication.md` §3 item 5, `refactoring-roadmap.md` | **Fixed.** No `_irena_defaults` table exists anywhere in the current file. `_resolve_base_cf` (L498–524) falls back directly to `DEFAULT_TECH_PARAMS.get(tech, {}).get("capacity_factor", 0.25)`, imported from `src.core.constants` (L46). One table, one source. |
| 4 | Bare `0.60` suitability-threshold literal duplicated across `lcoe_calculator.py` (×2) and `potential_calculator.py` | `code-duplication.md` §3 items 3/4 | **Fixed for this file.** `_resolve_threshold` (L526–559) uses `FALLBACK_SUITABILITY_THRESHOLD` imported from `constants.py` (L47) in both the "no thresholds dict" and "scenario key missing" branches — no bare `0.60` left in `lcoe_calculator.py`. (`potential_calculator.py`'s own copy is checked separately in that module's analysis.) |
| 5 | `results_writer._find_suitability_tif` and `lcoe_calculator._find_suitability_tif` tried **opposite** TOPSIS/OWA precedence — a genuine behavioral divergence, not just duplication | `code-duplication.md` §2b, `refactoring-roadmap.md` (Med-High risk item) | **Fixed.** Neither file has a private `_find_suitability_tif` anymore. Both now import and call the single `src.utils.raster_io.find_suitability_tif()` (`lcoe_calculator.py` L68/590; confirmed by grep also at `results_writer.py` L75/422), whose docstring explicitly states "single source of truth for TOPSIS-vs-OWA raster discovery — BLOCKER-006" and always tries TOPSIS first. No remaining divergence between the two phases. |
| 6 | Dead `from src.io.artifact_manager import ArtifactManager` import | `refactoring-roadmap.md` REFACTOR-006 note | **Fixed.** No `ArtifactManager` import anywhere in the current file (L27–71 is the full import block). |
| 7 | Ad-hoc `rasterio.open(..., "w", **profile)` for the per-tech LCOE GeoTIFF, inconsistent with the rest of the pipeline | `code-duplication.md` §4a, `refactoring-roadmap.md` | **Fixed.** L1193: `with safe_raster_write(str(tif_path), **profile) as dst:` (REFACTOR-005, commit `a9e9a1f`). |

This is a materially cleaner file than the one the older docs describe — six of seven previously-flagged issues in this specific module are closed, four of them exactly matching the BLOCKER-series commits already in `git log`.

---

## 2. Open findings

### 2a. [MEDIUM] Mask provenance (`mask_source`) is computed, logged once, then discarded

`_prepare_modulation_source` (L927–973) determines whether the accurate Phase-4 suitable-pixel mask was available or whether Phase 5 had to fall back to threshold-based masking, and returns that as `mask_source` (`"phase4_potential_mask"` or `f"suitability_threshold_{thr:.2f}"`, L946–956). It is logged once at INFO level (L965–972) — but the caller, `_run_technology` (L820), captures it into a local variable that is **never used again**: not passed into `_compute_cf_and_lcoe`, not written into `stats`, not present in the serialized `results["techs"][tech]` returned to the orchestrator (L753–760), and therefore not in `result.pkl`, not in the LCOE report, not in any persisted artifact.

**Why it matters**: this is exactly the kind of provenance signal that should survive into the output. Today, if Phase 4's mask were ever missing for a given tech/country (stale cache, partial re-run, first-ever run without `potential/tifs/`), Phase 5 would silently degrade to the "less accurate but functional" (L279) threshold-based population — and nothing in the persisted `result.pkl` or the human-readable report would show that this happened. The only trace is a console log line that disappears once the run ends. Someone auditing a result six months from now (or comparing two countries' LCOE numbers) has no way to tell, from the artifacts alone, whether both used the Phase-4-consistent pixel population or one silently used the degraded fallback.

**Not the same as BUG_07** (see §3) — this is not stale-disk-vs-live-object reconstruction, it's a computed diagnostic that is dropped rather than persisted. Distinct failure mode, similar spirit (silent loss of information about which code path actually ran).

**Confidence**: confirmed by full-text grep — `mask_source` appears only at L820 (assignment), L945/946/952/956/969 (definition site), L973 (return). No occurrence anywhere else in the file. `ruff --select F841` does not flag it because it's part of a tuple-unpack, not a simple unused local, but the outcome — the value is dead once assigned — is the same.

**Not fixed in this pass** — flagged as a candidate for a future task (e.g., add `"mask_source": mask_source` to the `stats` dict in `_compute_cf_and_lcoe`, one line).

### 2b. [INFO] Phase-4-mask-missing fallback path has zero validation coverage so far

Directly related to 2a: the threshold-based fallback branch in `_prepare_modulation_source` (L954–956) has not been exercised by any CHECKPOINT-1/CHECKPOINT-2 validation run, because Phase 4 always runs (or loads from a valid cache) before Phase 5 in every run performed so far in this engagement. This isn't a defect — it's a legitimate, currently-cold code path. Worth knowing before trusting it blindly if a future refactor touches this area: it is untested in practice, only by construction.

### 2c. [LOW] Two data-quality-gate magic numbers still not centralized

Both already flagged in `refactoring-roadmap.md` as low-priority cleanup, still open, confirmed still present as-is:
- `_MIN_RESOURCE_COVERAGE = 0.05` (module constant, L112) — minimum finite-pixel fraction before a resource TIF is trusted.
- `src_cv < 0.01` (inline literal, L1021, inside `_compute_cf_and_lcoe`'s biomass fallback) — coefficient-of-variation threshold below which a biomass resource TIF is treated as "flat/unreliable."

Neither affects correctness (both are logged, disclosed fallback triggers), just discoverability/configurability. No change recommended here beyond what `refactoring-roadmap.md` already proposed.

### 2d. [LOW] TIF-discovery consolidation is now partial, not absent

`find_suitability_tif` (the TOPSIS/OWA-sensitive lookup) was fully consolidated into `raster_io.py` (§1 item 5). But `_find_potential_suitable_tif` (L261–298, Phase 4 mask lookup) and `_find_resource_tif` (L376–416, Phase 2b resource lookup) remain private, per-phase-duplicated candidate-glob methods, matching the still-open part of `refactoring-roadmap.md`'s consolidation proposal. Lower risk than the TOPSIS/OWA case since these don't have a documented behavioral divergence with another phase's copy today — just duplicated glob logic. No change recommended now, just confirming the roadmap item's status is "partially done" rather than "done" or "not started."

### 2e. [INFO, pre-existing, disclosed] Biomass CV<0.01 fallback conflates suitability score with resource yield

When a biomass resource TIF is present but effectively constant (`src_cv < 0.01`, L1021–1043), Phase 5 falls back to using the *suitability* array (a 0–1 composite score of land cover/proximity/etc.) as the *capacity-factor modulation source* instead of a genuine yield-based resource raster. This is intentional, logged (`logger.warning`, L1022–1026), and reflected in the persisted `stats["source"]` field ("suitability_proxy (resource constant)") — so unlike §2a this **is** traceable in the output. Flagging only as a standing scientific caveat: suitability and resource yield are conceptually different quantities, and pixels get their local CF boosted/reduced based on land-cover/proximity suitability rather than actual biomass energy yield whenever this path triggers. Not new, not hidden, not actionable without a domain decision — documented here only because the task asked for "any other scientific/numerical risk in the same category."

---

## 3. BUG_07-pattern check ("Phase 6 always reconstructs from disk instead of using the live object")

**Not applicable to this file.** `lcoe_calculator.py` is Phase 5 — it is a *producer*, not a *consumer*, of prior-phase results. It reads raw rasters from Phases 3/4 (suitability TIFs, resource TIFs, the Phase-4 suitable-pixel mask), which is the intended architecture (each phase computes from raw geospatial inputs, not from a previous phase's in-memory Python object), not a bug pattern. There is no code path in this file where a live, just-computed, in-memory result is discarded in favor of a stale disk read — that failure mode belongs to modules that consume *this* phase's output (Phase 6, `sensitivity_analyzer.py`, `transport_decarbonization_calculator.py`'s `_PotentialView`/`_LCOEView`), not to Phase 5 itself. The closest analog found here is §2a (mask provenance lost on the *write* side, not a stale-read bug on the *consume* side) — flagged separately above precisely because it is a different mechanism, not mislabeled as BUG_07.

---

## 4. Dead / duplicate code

- **`vulture src/ --min-confidence 0`** (whole-package, cross-file usage resolution): zero findings for this file beyond `unused class 'LCOECalculator'` (60% confidence) — a standard false positive shared by every processor class, since `PipelineOrchestrator.run_phase()` instantiates phase classes dynamically (`phase_class(self.cfg, self.outputs_dir).run(...)`) rather than via a statically-visible call vulture can trace. No real dead code detected by static analysis.
- **`ruff check --select F401,F821,F841`**: all checks pass, zero findings.
- Manual read confirms no leftover dead functions, no orphaned imports, no duplicate constant tables (the one that existed — `_irena_defaults` — is gone, §1 item 3).

---

## 5. Summary

Of seven issues this file carried in earlier analysis passes, six are confirmed fixed by direct code inspection (not just by trusting the old docs), matching six distinct commits already in `git log`. One new issue was found in this pass: `mask_source` provenance is computed and logged but not persisted (§2a, MEDIUM). Three low/info items remain open exactly as previously scoped, with no new severity information. No BUG_07-pattern instance exists in this file — Phase 5 is architecturally not exposed to that failure mode. No actionable dead/duplicate code found by either static tooling or manual read.

**Recommended severity for future work, if picked up**: §2a (mask provenance) is the only item with a plausible argument for BLOCKER-tier attention, and even that is a traceability gap, not a numeric-correctness bug — the LCOE numbers themselves are correct under either code path, they're just unlabeled. Everything else in this document is LOW/INFO and can be deferred indefinitely without risk.
