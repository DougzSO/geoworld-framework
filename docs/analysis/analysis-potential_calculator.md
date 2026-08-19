---
id: analysis-potential-calculator
type: frozen
status: frozen
created: 2026-08-11
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [analysis-code-duplication, analysis-write-points-inventory]
linked_by: [docs-tasks]
scope: "Leitura pós-fix da Fase 4 (potential_calculator.py), congelada em 2026-08-11; NÃO reflete mudanças posteriores ao código."
---

# `potential_calculator.py` (Phase 4) — Analysis

Analysis only. No code changed in this pass. Full line-by-line read of `src/processors/potential_calculator.py` (1024 LOC, current `main` HEAD `6d59120`). Cross-referenced against `docs/code-duplication.md`, `docs/refactoring-roadmap.md`, `docs/write-points-inventory.md` and `git log`, same methodology as `docs/analysis-lcoe_calculator.md`.

---

## 1. Previously-flagged issues confirmed FIXED

| # | Old finding | Where it lived | Current status |
| --- | --- | --- | --- |
| 1 | Bare `0.60` suitability-threshold fallback at L439 | `code-duplication.md` §3 item 3, `refactoring-roadmap.md` | **Fixed.** L444: `params["thresholds"].get(scenario, FALLBACK_SUITABILITY_THRESHOLD)`, constant imported from `src.core.constants` (L68). Confirmed by grep — zero occurrences of a bare `0.60`/`0.6,` anywhere in the file. |
| 2 | Suitable-pixel mask TIF export used raw `rasterio.open(tif_out, 'w', driver='GTiff', ...)`, not `safe_raster_write` | `code-duplication.md` §4a, `refactoring-roadmap.md`, `write-points-inventory.md` | **Fixed.** L500: `with safe_raster_write(tif_out, driver='GTiff', ...) as dst:`. This is this engagement's own REFACTOR-005 (commit `a9e9a1f`). |
| 3 | Phase 4's zonal CSV had no `area_km2` column, forcing `data_recovery.py` and `results_writer.py` to each independently re-derive area from the suitable-pixel TIF ("area computed identically three separate times", the highest-multiplicity duplication found across `write-points-inventory.md`'s whole sweep) | `write-points-inventory.md` §1, `refactoring-roadmap.md` BLOCKER-003 | **Fixed on the producer side.** L530–541: a second `zonal_stats_raster(area_arr, apt_mask, ...)` pass computes real geodesic `area_km2_sum` per admin region and is merged into `zonal_df` before the CSV write, comment explicitly citing BLOCKER-003 ("merged in so downstream recovery can sum `area_km2_sum` instead of re-deriving area from the suitable-pixel TIF"). Matches commit `ee8ae66`. **Not independently re-verified here** whether `results_writer.py`/`data_recovery.py` actually now read this column instead of still recomputing — that is checked directly in `docs/analysis-results_writer.md` (this module's job was only to confirm it now correctly exports the real value, which it does). |
| 4 | Duplicate/local pixel-area computation instead of the canonical helper | comment marker "dup_geo_area" in the file itself | **Fixed** — L430–431 explicitly comments "✅ FIX (dup_geo_area): usar SEMPRE `build_pixel_area_array()` canônico" and calls the shared `src.utils.geo_stats.build_pixel_area_array()`. No local re-implementation found. |
| 5 | Suitability TIF discovery risked TOPSIS/OWA divergence (same category as the `lcoe_calculator.py`/`results_writer.py` case) | `code-duplication.md` §2, `refactoring-roadmap.md` | **Fixed / never actually at risk here.** L196–198 uses the shared `raster_io.find_suitability_tif(..., allow_owa_fallback=False)`, with an explicit comment: "BLOCKER-006: centralized resolver... `allow_owa_fallback=False` preserves this phase's prior strict TOPSIS-or-nothing behavior — Phase 4 has never used OWA." Correctly the strictest of the three phases (4/5/6) that use this shared function. |

---

## 2. Open findings

### 2a. [LOW] Read-path convention only half-adopted in this file

`safe_raster_write` is imported and used correctly (L86, L500 — see §1 item 2). But `safe_raster_open` (the read-side sibling, used consistently by `lcoe_calculator.py`) is **not imported at all** in this file. All three read opens use raw `rasterio.open()` directly: L215 (reference metadata), L225 (per-tech input validation), L422 (per-tech suitability array load). Functionally this is safe — `with rasterio.open(...) as src:` is itself a valid context manager and closes correctly — the only real difference is `safe_raster_open` raises a clear `FileNotFoundError` with the path before attempting the open, versus rasterio's own less-specific `RasterioIOError` on a missing file. Not a correctness bug, just an inconsistent half-adoption of the shared-helper convention within a single file (write side migrated, read side didn't). No change made in this pass.

### 2b. [LOW / documentation accuracy] `use_owa=True` is documented but does not exist in code

The module docstring (L22–24) states: "The OWA GeoTIFFs are available in the suitability directory and can be selected by passing `use_owa=True` to `_run_technology` (reserved for future scenario-specific runs; not yet wired in the orchestrator)." Grepping the entire file for `use_owa` finds exactly one hit — that same docstring sentence. `_run_technology`'s actual signature (L388–400) has no `use_owa` parameter, and no code path in the file references it. This is aspirational/stale documentation describing a feature that does not exist in the current implementation (the docstring's own "not yet wired" phrasing suggests it was written as a forward-looking note, not a description of shipped behavior — but as written it reads like an existing, just-unused, capability). Not a functional risk since nothing calls it, but worth correcting the docstring so a future reader doesn't go looking for a parameter that isn't there.

### 2c. [LOW] `_plot_comparison` re-derives and re-reads PNG paths from disk within the same run

`_plot_comparison` (L763–834) is called once per `run()`, after the per-technology loop has already finished. For each tech it independently reconstructs the expected output path — `self.outputs_dir / country_code / "potential" / "figures" / f"{country_code}_{tech}_potential_map.png"` (L775–778) — checks `png.exists()`, and re-opens it with `PIL.Image.open()`, rather than being handed the image (or even just the path) directly from where `_plot_potential_map` wrote it a few lines earlier inside `_run_technology` (L568–572, same `out_fig / f"{country_code}_{tech}_potential_map.png"` expression, independently typed). This is structurally the same shape as the BUG_07 pattern (reconstructing from disk instead of using what was just produced in-memory/in-process) but meaningfully lower-risk than a true BUG_07 instance: it's a same-run read of a freshly-written file, not a cross-run read of potentially-stale cached data, and the artifact being read is a cosmetic composite PNG, not a scientific value feeding downstream calculations. Flagged because the task asked specifically about this pattern's recurrence — worth knowing that the two independently-typed path expressions must be kept in sync manually (a future rename of one would silently break the comparison panel, not raise an error, since `_plot_comparison` degrades to "skip missing tech" rather than failing).

---

## 3. BUG_07-pattern check ("Phase 6 always reconstructs from disk instead of using the live object")

**Not applicable to this file's scientific computation path.** `potential_calculator.py` is Phase 4 — like Phase 5, it is a producer that reads raw suitability rasters from Phase 3 by design, not a consumer discarding another phase's live in-memory result object in favor of a stale disk read. The one disk-reconstruction pattern found (§2c) is real but cosmetic (PNG compositing, same-run, non-scientific) — flagged separately rather than mislabeled as a true BUG_07 instance, consistent with the same distinction drawn in `docs/analysis-lcoe_calculator.md` §3.

---

## 4. Dead / duplicate code

- **`vulture src/ --min-confidence 0`** (whole-package): zero findings beyond `unused class 'PotentialCalculator'` (60% confidence) — the same standard false positive as every dynamically-instantiated phase class (see `docs/analysis-lcoe_calculator.md` §4 for the mechanism).
- **`ruff check --select F401,F821,F841`**: all checks pass, zero findings.
- No orphaned imports, no duplicate constant tables, no leftover dead functions found by manual read.

---

## 5. Summary

`potential_calculator.py` is the cleanest of the three modules read so far relative to its own history: all five previously-flagged issues that targeted this file specifically are confirmed fixed by direct code inspection, three of them matching this engagement's own BLOCKER-003/006 and REFACTOR-005 commits. Three new LOW-severity findings surfaced in this pass (§2a–2c), none of them affecting numeric correctness — one convention-consistency gap, one stale docstring, and one same-run disk-reread pattern structurally similar to (but materially lower-risk than) BUG_07. No BLOCKER-tier candidate found in this file. No dead or duplicate code found by static tooling or manual read.
