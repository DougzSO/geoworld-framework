---
id: archive-reorg-plan
type: archive
status: archived
created: 2026-08-11
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [analysis-arch-misalignments, analysis-code-duplication, analysis-write-points-inventory]
linked_by: [docs-tasks, docs-decisions]
scope: "Registro pontual de uma reorganização anterior de docs/ (2026-08-11), superseded pela reorganização de 2026-08-18; NÃO é mantido, não editar — só consultar como histórico."
---

# GeoWorld Framework — Consolidated Project State & Documentation Reorganization Plan

**Status of this document**: synthesis produced outside the codebase (by Claude, not Claude Code), from 20 `.md` files supplied by the project author plus the full BLOCKER-001→012 session log. Not verified against current source directly — treat every claim below as "true as of the source documents read," and have Claude Code re-verify against live code before relying on anything here for a code change.

**Purpose**: (1) give any future session — human or AI — one place to find current state and reading order instead of reconciling 20 files; (2) surface gaps that existed across the source documents but were never assigned a tracking number; (3) propose a reorganization of `docs/` and `docs/memory/` so the two currently-separate documentation generations don't keep drifting apart.

**Do not treat this document as more authoritative than the code.** Where this document and the source code disagree, the code is right and this document is stale — update it, don't trust it blindly.

---

## Part 1 — Recommended reading order for a new session (human or AI)

1. This document (state + what's open).
2. `docs/memory/01-overview.md` through `11-onboarding.md`, in the order `docs/memory/README.md` gives — for architecture/pipeline/conventions context. **Caveat**: as of this writing, `06-risk-areas.md` and `08-conventions.md` are known stale on the "no test suite" claim (see Part 5). Treat the whole set as directionally correct but not fact-checked against current code.
3. Part 3 of this document (reconciliation table) — what's actually done vs. what the roadmap still lists as open.
4. Part 4 of this document (untracked gaps) — before starting any new work, check nothing here overlaps what you're about to do.
5. Only then, if doing structural/architectural work: `docs/analysis-*.md` and `docs/arch-misalignments.md` / `code-duplication.md` / `write-points-inventory.md` for the full original evidence trail behind any specific finding.

---

## Part 2 — Where the project stands, in one paragraph

Nine-phase pipeline (Audit → Grid Align → Criteria → Suitability → Potential → LCOE → Results → GHG Abatement → Sensitivity; Transport/Phase 9 exists but is dormant, `skip_transport: true`, disabled by a known unrelated `AttributeError`). All nine numeric-correctness BLOCKERs found in the original three-document audit (`arch-misalignments.md`, `code-duplication.md`, `write-points-inventory.md`) have been fixed, validated bit-for-bit against baselines, and committed locally (BLOCKER-001 through 009, plus 011). A pure-math unit test suite exists (77 tests as of the last session, `pytest tests/`). Four structural REFACTORs (001, 002, 005, 007) are done. Two items remain open and undone: **BLOCKER-010** (the root architectural issue — Phase 6 always reconstructs from disk, never uses the live in-process object — deferred deliberately, high risk, needs its own session) and **BLOCKER-012** (Transport double-persistence, dormant because Phase 9 doesn't run). See Part 3 for the full item-by-item status and Part 4 for gaps that were never given a number at all.

---

## Part 3 — Reconciliation: `refactoring-roadmap.md` vs. actual current status

`refactoring-roadmap.md` was written as a forward-looking plan, before most of this work happened. This table maps every item in it to what's actually true now, based on the BLOCKER-001→012 session log.

### BLOCKERS

| ID | Roadmap description | Actual status |
|---|---|---|
| BLOCKER-001 | Persist Phase 5 supply curve to Parquet | ✅ Done, validated, committed (`c5986a7`) |
| BLOCKER-002 | Persist LCOE percentile stats (p25/median/p75) | ✅ Done, validated, committed (`14c1971`) |
| BLOCKER-003 | Persist integrated suitable area from Phase 4 | ✅ Done, validated, committed (`ee8ae66`) |
| BLOCKER-004 | Fix divergent capacity-factor fallback table | ✅ Done, part of the 004/005/007 batch (`4ca6e7f`) |
| BLOCKER-005 | Consolidate `0.60` threshold literal + validation | ⚠️ **Partially done.** Literal consolidation done (`4ca6e7f`). The "add real schema/config validation at load time" half of this item — described in the roadmap as feeding into QI-003 — **status unconfirmed**. Check `src/core/config_loader.py`/`schemas.py` directly before assuming this is finished. |
| BLOCKER-006 | Centralize TIF-discovery in `raster_io.py`, resolve TOPSIS/OWA precedence divergence | ✅ Done, validated, committed (`023c00c`). Confirmed by three independent analysis passes (results_writer, lcoe_calculator, potential_calculator) all now calling the shared `find_suitability_tif()`. |
| BLOCKER-007 | Centralize dict-shape adapters in `params_helpers.py` | ✅ Done, part of the 004/005/007 batch (`4ca6e7f`). **Caveat**: validated by code inspection only for the Phase 9 (`transport_decarbonization_calculator.py`) call site, since Phase 9 doesn't run — not empirically re-verified end-to-end. |
| BLOCKER-008 | (not in original roadmap — surfaced during the engagement) SA-5 `cf_renewable` Sobol bug | ✅ Done, validated (Sobol ST index before/after documented), committed |
| BLOCKER-009 | (originated as a BLOCKER-002 sub-finding) LCOE mean/p10/p90/n_pixels region-mean vs. pixel-level | ✅ Done, validated, committed (`7926ec3`) |
| BLOCKER-010 | Root `main.py` architecture: Phase 6 (and likely others) always reconstruct from disk, never use the live object | ❌ **Not done — deliberately deferred.** Full evidence chain already mapped in `docs/analysis-results_writer.md` §1a (see Part 4). This is the subject of your planned Fase 1. |
| BLOCKER-011 | Results Writer (Phase 6) double persistence discards dominance pixel counts | ✅ Done, validated (full-path integration test through the real orchestrator), committed |
| BLOCKER-012 | Transport (Phase 9) same double-persistence bug as 011 | ❌ **Not done — documented only**, dormant because `skip_transport: true`. **Scope gap**: see Part 4 — this is not the only phase with the pattern. |

### REFACTORs

| ID | Roadmap description | Actual status |
|---|---|---|
| REFACTOR-001 | Extract `sensitivity_analyzer.py` plotting → `sensitivity_plots.py` | ✅ Done, committed (`873cce9`) |
| REFACTOR-002 | Extract `transport_decarbonization_calculator.py` plotting → `transport_plots.py` | ✅ Done, committed (`c31b338`) |
| REFACTOR-003 | Split `data_fetcher.py` by dataset | ❌ Not started, per available evidence |
| REFACTOR-004 | Extract SA1–6 methods, split `sensitivity_analyzer.run()` | ❌ Not started, per available evidence |
| REFACTOR-005 | Migrate Phase 4/5/6 raw raster writes → `safe_raster_write()` | ✅ Done, committed (`a9e9a1f`) |
| REFACTOR-006 | Resolve `ArtifactManager` double-persistence (5 phases) | ⚠️ **Only 1 of 5 phases actually resolved** (Results Writer, via BLOCKER-011). Roadmap's own deliverable list named all five: `results_writer.py`, `suitability_builder.py`, `sensitivity_analyzer.py`, `ghg_abatement_calculator.py`, `transport_decarbonization_calculator.py`. Only the first is fixed; the other four (one of which — Transport — has its own BLOCKER-012 number, the other three do not) are untouched. See Part 4. |
| REFACTOR-007 | Complete `results_writer.py` → `dashboard_panels.py`/`map_styling.py` extraction | ✅ Done, committed (`6d59120`) |
| REFACTOR-008 | Split `map_styling.py` PIL compositing + decorations | ❌ Not started, per available evidence |
| REFACTOR-009 | Extract Transport hub-siting logic | ❌ Not started, per available evidence |
| REFACTOR-010 | Move remaining hardcoded config-adjacent values | ❌ Not started, per available evidence |

### QUALITY INFRASTRUCTURE

| ID | Roadmap description | Actual status |
|---|---|---|
| QI-001 | Pure-math unit tests (`ahp`, `topsis`, `owa`, `exclusion`, `economics`, `normalization`) | ✅ **Done, exceeds scope.** Roadmap only asked for 6 modules; actual QI-001 delivered `ahp`, `topsis`, `owa`, `economics` (61 tests) — **`exclusion.py` and `normalization.py` test coverage unconfirmed**, check before assuming complete. |
| QI-002 | Synthetic-country integration test | ❌ Not started, per available evidence. Roadmap explicitly sequenced this *after* BLOCKER-001/002/003 (done) — now unblocked if picked up. |
| QI-003 | Startup config/schema validation script | ❌ Not started, per available evidence. Depends on BLOCKER-004 (done) and BLOCKER-005's validation half (unconfirmed, see above). |
| QI-004 | Unit tests for BLOCKER-003/006/007's new shared utilities (`raster_io`, `params_helpers`) | ❌ Not started, per available evidence. `test_results_writer.py` (from the BLOCKER-011 session) is adjacent but not the same thing. |

**Checkpoints from the roadmap**: CHECKPOINT 1 (roadmap's own gate, "before touching `results_writer.py`'s remaining structure") — passed, matches your session's own CHECKPOINT 1. CHECKPOINT 2 (roadmap's gate, "web-platform ready", requiring QI-002 + QI-003) — **not reached**; your session's own CHECKPOINT 2 was a narrower full-run regression check, not the roadmap's broader gate. Don't conflate the two checkpoint-2's when reading old notes.

---

## Part 4 — Gaps that exist in the source evidence but were never assigned a tracking number

These are real findings already sitting in the uploaded documents, not new investigation — just never promoted to a blocker/refactor ID, so they're easy to lose.

### 4a. Three phases share Results Writer's double-persistence pattern, unconfirmed whether they lose data

`write-points-inventory.md`'s own write-point table documents **five** phases doing manual `ArtifactManager` persistence in addition to the orchestrator's automatic one: Suitability (Phase 3, `suitability_builder.py:529,537`), Results (Phase 6 — **fixed**, BLOCKER-011), Abatement (Phase 7, `ghg_abatement_calculator.py:912,920`), Sensitivity (Phase 8, `sensitivity_analyzer.py:2006,2011`), Transport (Phase 9 — **documented, not fixed**, BLOCKER-012).

Only Results and Transport have ever been individually investigated for whether the second write actually *discards* data (both confirmed yes, at different confidence levels — Results empirically, Transport by code inspection). **Suitability, Abatement, and Sensitivity have never been checked.** They may be harmless duplicate I/O (if `run()`'s returned dict is a superset of what's manually persisted) or they may silently drop fields the same way Results did. This is unknown, not assumed-safe.

**Recommended handling**: three short verification tasks (not fixes) — for each of `suitability_builder.py`, `ghg_abatement_calculator.py`, `sensitivity_analyzer.py`, diff the manually-persisted dict's keys against `run()`'s returned dict's keys. If a superset, no data loss, downgrade to a REFACTOR-tier cleanup (same category as REFACTOR-006, just I/O waste). If not, promote to a new BLOCKER number each.

### 4b. `mask_source` provenance dropped in Phase 5 (LCOE)

`docs/analysis-lcoe_calculator.md` §2a, MEDIUM severity, not fixed. Phase 5 computes and logs (once, console only) whether it used Phase 4's accurate suitable-pixel mask or degraded to a threshold-based fallback — then discards the value. Never reaches `result.pkl`, the LCOE report, or any persisted artifact. If Phase 4's mask were ever missing for a given tech/country, nothing in the output would show it. Never given a BLOCKER number. Directly relevant to your planned Fase 3 (result plausibility/provenance auditing) — recommend giving this a number now rather than rediscovering it later.

### 4c. `_LCOEView.mean_lcoe` in Transport tolerates four different dict shapes for one value

`code-duplication.md` §1b: the most defensive of the three duplicated dict-shape adapters, handling four distinct layouts plus a Pydantic-model path. Flagged there as "evidence the upstream shape is genuinely inconsistent, not just inconsistently read" — i.e., a symptom pointing at the same root cause as BLOCKER-010. BLOCKER-007 (done) consolidated the *adapters*; it did not resolve *why* four shapes exist in the first place. That "why" is BLOCKER-010 territory. Worth stating explicitly when BLOCKER-010 is picked up, so its scope includes explaining/eliminating this, not just fixing the `main.py` → `Path` issue.

### 4d. GeoDataFrame written as plain CSV loses spatial fidelity (Transport)

`write-points-inventory.md`: `transport_decarbonization_calculator.py:520` writes `hubs_gdf.to_csv(...)` — a GeoDataFrame through pandas' plain CSV writer, which degrades the geometry column to WKT-or-dropped instead of using `.to_file()` (GeoJSON/GPKG) to preserve CRS and geometry type. Flagged but not investigated further (unconfirmed whether anything currently re-reads this file as spatial data, which determines real severity). Low priority given Phase 9 is dormant, but should not be forgotten before Phase 9 is ever reactivated.

### 4e. `results_writer.py`'s own docstring overclaims what was extracted

`arch-misalignments.md` §1 (pre-REFACTOR-007): the module docstring claimed dashboard extraction was already done when it wasn't. REFACTOR-007 has since actually done it — but this is a reminder that **docstrings in this codebase have previously stated completed work that wasn't complete**, at least once. Worth a light spot-check of other "per the docstring, X was already extracted" claims elsewhere in the codebase before trusting them at face value during Fase 2 (reorganization).

### 4f. `write_criteria_summary()` and a handful of report-writers bypass the shared `build_phase_report()`/`write_text` convention

`write-points-inventory.md`, I/O Abstraction Gaps table, last two rows — low priority, noted for completeness, not actioned. Relevant only if Fase 2 goes looking for reporting-layer consolidation.

---

## Part 5 — Specific staleness in `docs/memory/0X-*.md`, confirmed by cross-reference

- **`06-risk-areas.md`** and **`08-conventions.md`**: both state "no `tests/` directory, no `pytest`/`unittest` anywhere in the repo." False as of QI-001 (61+ tests exist). Needs correction, not removal — the rest of both files' content (config-separation risk, large-module risk, etc.) remains accurate.
- **`09-decisions.md`, D7** ("No automated test suite"): same issue — status should move from "Uncertain" to "Superseded, see QI-001."
- **`SUMMARY.md`**: LOC counts for `results_writer.py` (states 1553, now 1108 post-REFACTOR-007), and likely `lcoe_calculator.py`/`potential_calculator.py` (analysis docs show slightly different counts than SUMMARY — 1493 vs. 1523 for LCOE, 1024 vs. 1007 for Potential) are stale. Low priority to fix by hand — better regenerated mechanically (`wc -l` per file) than hand-edited, since it will drift again.
- **`07-configuration.md`**'s ⚠️ note and **`10-scripts-and-commands.md`**'s ⚠️ note, both about `skip_*` flags being in a non-default "only phases 4–6 active" state at documentation time — **unconfirmed whether still true**. Given the BLOCKER/CHECKPOINT sessions ran full 8-phase runs (Transport still skipped), the flags have clearly changed since these notes were written. Needs a fresh check of current `settings.yaml`, not a guess.
- **`02-architecture.md`**'s ⚠️ note about `src/visualization/` missing `__init__.py` — status unconfirmed, never resolved in any blocker/refactor log reviewed. Cheap to check, cheap to fix if still true.

---

## Part 6 — Proposed `docs/` reorganization

Current state: `docs/` holds 7 free-floating analysis/planning files (`analysis-lcoe_calculator.md`, `analysis-potential_calculator.md`, `analysis-results_writer.md`, `arch-misalignments.md`, `code-duplication.md`, `refactoring-roadmap.md`, `write-points-inventory.md`); `docs/memory/` holds the 11 numbered narrative files + `README.md`; `SUMMARY.md` sits at repo root. Two problems: (1) the `docs/` analysis files and `docs/memory/` narrative files were never reconciled with each other (this document is a one-time fix for that, not a standing mechanism); (2) there's no single place that says "here's what's currently being worked on" — that context has been living in your own manually-copied task list outside the repo entirely.

**⚠️ Correction, post-dating the original draft of this document**: the Task-2 documentation-consolidation command (commit `6643074`) already made `docs/refactoring-roadmap.md` a **living blocker tracker** — it now holds structured BLOCKER-008/009/010/011 entries (reconstructed from commit messages) and absorbed the D1–D7 decision log as an appendix, replacing `docs/memory/09-decisions.md` (now a redirect stub). This means `refactoring-roadmap.md` is **no longer a frozen historical snapshot** like the other six analysis files below — it is actively maintained and will keep receiving new entries (BLOCKER-013+, etc.). The structure below is corrected accordingly: `refactoring-roadmap.md` moves out of `analysis/` and becomes the canonical backlog file, replacing the separate `backlog/` folder concept from the original draft (one live file is simpler than one-file-per-item, given how small the corpus is today).

**Proposed structure** (for Claude Code to implement, not applied here):

```
docs/
├── README.md                      ← new: top-level index, reading order, points here + memory/
├── STATE.md                       ← this document, trimmed to Parts 2-5, kept current going forward
├── BACKLOG.md                     ← docs/refactoring-roadmap.md, renamed and kept in docs/ root as the live blocker/refactor/QI tracker (NOT moved into analysis/ — this file keeps changing)
├── memory/                        ← unchanged location, content corrected per Part 5; 09-decisions.md becomes/stays a redirect stub pointing to BACKLOG.md's appendix
│   ├── README.md
│   ├── 01-overview.md … 11-onboarding.md
└── analysis/                      ← the 6 remaining analysis docs, moved here as-is (frozen historical record — do not rewrite, they're accurate snapshots of when they were written, and are already cross-referenced BY LINE NUMBER from BACKLOG.md's entries)
    ├── arch-misalignments.md
    ├── code-duplication.md
    ├── write-points-inventory.md
    ├── analysis-lcoe_calculator.md
    ├── analysis-potential_calculator.md
    └── analysis-results_writer.md
```

**What NOT to do**: don't delete or rewrite the `analysis/` files — they're dated, accurate-at-the-time evidence with exact line numbers, useful precisely because they're a snapshot. Rewriting them to match current line numbers would destroy their value as a historical record and risks silently introducing errors into evidence that's currently correct. If a finding in one of them is now fixed, that belongs in `BACKLOG.md`'s entries (which already do this) or `STATE.md`'s reconciliation table (Part 3 above), never as an edit to the original analysis file. **`refactoring-roadmap.md`/`BACKLOG.md` is the one exception to "don't touch historical docs" — it is explicitly meant to keep growing.**

**What SHOULD change**: `docs/memory/0X-*.md`'s specific stale claims (Part 5) should be corrected in place, since those files are meant to describe *current* state, not a historical snapshot — that's their whole purpose, unlike the frozen `analysis/` files.

---

## Part 7 — Decision on `results_writer.py`'s role (resolved)

**Decision**: `results_writer.py` (Phase 6) is the designated final-results aggregator. Every future pipeline phase added to GeoWorld must, on completion, feed its finished output into Phase 6, so all primary/final result generation, testing, and auditing has one entry point.

**Binding condition, not optional**: this only works safely if Phase 6 **aggregates, never recomputes**. It reads values already calculated and persisted by the phase that owns them; it never reopens a raster or re-derives a statistic another phase already produced. This is the exact distinction that separates a safe "final aggregator" from the pattern that caused BLOCKER-001/002/003/009 (Phase 6 silently recalculating LCOE percentiles, supply curve, and area instead of reading Phase 4/5's real persisted values). "Aggregator" means *consolidation of correct upstream values*, not *a second place where science happens*.

**Consequence for BLOCKER-010's priority**: this decision promotes BLOCKER-010 (the `main.py`/BUG_07 root issue — Phase 6 always reconstructs from disk because the live in-process object is never passed through) from "deferred, high-risk, low immediate return" to a **prerequisite** for the chosen architecture. Every new phase added under this design will otherwise reproduce the same fragile disk-reconstruction pattern that already caused four confirmed numeric-correctness bugs. Fase 1's investigation and eventual fix of BLOCKER-010 should be scoped and framed accordingly — not as one item among many, but as the foundation the "Phase 6 = final aggregator" design depends on.

---

## Part 8 — How to use this document going forward

- Save this file to `docs/STATE.md` (or `docs/00-project-state-and-reorg-plan.md` if you'd rather keep the reorg-plan framing visible in the filename — your call, not consequential).
- The Fase 0 command to Claude Code should: (a) verify this document's factual claims against live code (LOC counts, `skip_*` flag state, whether `exclusion.py`/`normalization.py` have tests, whether BLOCKER-005's validation half exists) and correct anything wrong before trusting it further; (b) apply the Part 6 reorganization; (c) fix the Part 5 staleness in `docs/memory/`; (d) do the three Part 4a verification tasks (Suitability/Abatement/Sensitivity double-persistence) and report findings, without fixing anything yet.
- Once that command's output comes back, this document's Part 3/4 tables should be corrected to match reality, and this document becomes `docs/STATE.md` going forward — updated at the end of each future session, not left to drift like the two document generations it's replacing did.