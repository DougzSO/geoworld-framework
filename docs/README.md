---
id: docs-readme
type: index
status: active
created: 2026-08-18
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: []
linked_by: [mem-readme, mem-11-onboarding]
scope: "Índice de topo de docs/ e única ordem de leitura canônica; não contém tarefas, decisões ou status de sessão — esses vivem em TASKS.md/DECISIONS.md/SPRINT.md."
---

# docs/ — Documentation Index

**This is the one reading order for `docs/` — every other file that lists a reading order (`memory/README.md`, `memory/11-onboarding.md`) points back here instead of keeping its own.**

Five kinds of documentation live here, deliberately kept separate:

- **`TASKS.md`** — the single live tracker for all open work (BLOCKER/REFACTOR/QI/INVAR/Campaign/GAP/DOC items). The only file where a task may be registered — see `CLAUDE-DOCS-RULES.md`.
- **`DECISIONS.md`** — append-only architectural-decisions log (D1, D2, D3…). Never edited or overwritten, only appended to.
- **`SPRINT.md`** — current status, session-coordination rules, and the last 5 sessions' log.
- **`memory/`** — narrative/current-state documentation (architecture, pipeline, conventions, configuration, risk areas). Meant to describe *today's* code; correct it in place when it goes stale. Each fact has exactly one owner file — others link to it rather than repeating it.
- **`analysis/`** — frozen historical evidence: line-by-line module analyses and the original three-document audit. Dated snapshots, cross-referenced by exact line number from `archive/backlog-full-2026-08.md`. Never rewritten, even after the code they describe changes.
- **`archive/`** — historical/superseded material, kept for the record, never edited going forward: the pre-2026-08-18 `BACKLOG.md` (`backlog-full-2026-08.md`), the original `CURRENT-SPRINT.md`, the full old session log, and the one-time reorg-planning document.

## The reading order

1. This file.
2. [`SPRINT.md`](SPRINT.md) — status in one line, current focus.
3. [`TASKS.md`](TASKS.md) — what's open, by priority.
4. [`memory/01-overview.md`](memory/01-overview.md) → [`11-onboarding.md`](memory/11-onboarding.md), in that numeric order — architecture/pipeline/algorithms/environment/risk/configuration/conventions/scripts/onboarding. (`memory/09-decisions.md` is a stub pointing to step 5.)
5. [`DECISIONS.md`](DECISIONS.md) — why things are the way they are.
6. `analysis/` — only when doing structural work that needs the original line-by-line evidence trail behind a specific `TASKS.md`/`DECISIONS.md` entry.
7. `archive/` — only to investigate history/context predating 2026-08-18.

## Also relevant

- [`../SUMMARY.md`](../SUMMARY.md) — file-by-file technical inventory (LOC, responsibilities, dependencies) at the repo root.
- [`CLAUDE-DOCS-RULES.md`](CLAUDE-DOCS-RULES.md) — mandatory maintenance rules for this documentation set (task closure, decision logging, session logging, what's prohibited).
