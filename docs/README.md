# docs/ — Documentation Index

Two kinds of documentation live here, deliberately kept separate:

- **`memory/`** — narrative/current-state documentation (architecture, pipeline, conventions, configuration, risk areas). Meant to describe *today's* code; correct it in place when it goes stale. Start at [`memory/README.md`](memory/README.md) for the recommended reading order.
- **`BACKLOG.md`** — the live BLOCKER/REFACTOR/QUALITY-INFRASTRUCTURE tracker, plus a technical-decisions log in its Appendix. This is the one file in `docs/` that's expected to keep changing — new BLOCKER/REFACTOR/QI entries get appended, existing ones get a `**Status**` line rather than being rewritten.
- **`analysis/`** — frozen historical evidence: line-by-line module analyses and the original three-document audit (`arch-misalignments.md`, `code-duplication.md`, `write-points-inventory.md`). These are dated snapshots, cross-referenced by exact line number from `BACKLOG.md` entries — never rewritten, even after the code they describe changes. If a finding here is now fixed, that status lives in `BACKLOG.md`, not as an edit to the original file.

## Recommended order for a new session

1. [`memory/README.md`](memory/README.md) — architecture/pipeline/conventions context, in its own numbered reading order.
2. [`BACKLOG.md`](BACKLOG.md) — what's open, what's done, what's deliberately deferred (see BLOCKER-010).
3. `analysis/` — only when doing structural work that needs the original line-by-line evidence trail behind a specific `BACKLOG.md` entry.

## Also relevant

- [`../SUMMARY.md`](../SUMMARY.md) — file-by-file technical inventory (LOC, responsibilities, dependencies) at the repo root.
- [`00-project-state-and-reorg-plan.md`](00-project-state-and-reorg-plan.md) — the one-time synthesis/reorg-planning document that proposed this structure and was verified against live code before being applied. A point-in-time record of that verification pass, not maintained going forward the way `memory/` and `BACKLOG.md` are.
