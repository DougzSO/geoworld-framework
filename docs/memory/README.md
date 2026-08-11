# /docs/memory — Reading Index

Persistent technical memory for the GeoWorld Framework repository. This is documentation-as-memory: it never overrides the code (see the Source of Truth rule in [`CLAUDE.md`](../../CLAUDE.md)) — it exists so a future session doesn't have to remap the project from scratch.

## Recommended reading order

1. [`01-overview.md`](01-overview.md) — What GeoWorld is, for whom, and why it exists.
2. [`02-architecture.md`](02-architecture.md) — Package layout, layering, and how modules relate.
3. [`03-pipeline.md`](03-pipeline.md) — The nine-phase execution flow and its data contracts.
4. [`04-algorithms.md`](04-algorithms.md) — AHP / TOPSIS / OWA / LCOE / GHG abatement / sensitivity methods and where they live.
5. [`07-configuration.md`](07-configuration.md) — `settings.yaml` vs. `parameters.json`, `.env`, precedence rules.
6. [`05-environment.md`](05-environment.md) — Python version, dependencies, `.venv`, raw-data requirements, reproducibility.
7. [`10-scripts-and-commands.md`](10-scripts-and-commands.md) — How to actually run the pipeline, per-phase, batch mode.
8. [`08-conventions.md`](08-conventions.md) — Coding, logging, docstring, and naming conventions already in use.
9. [`09-decisions.md`](09-decisions.md) — Redirect stub: the actual Context/Decision/Consequences/Status log moved to `docs/refactoring-roadmap.md`'s "Appendix: Technical Decisions" (consolidated with the BLOCKER tracker).
10. [`06-risk-areas.md`](06-risk-areas.md) — Fragile, untested, hardcoded, or hard-to-reproduce areas.
11. [`11-onboarding.md`](11-onboarding.md) — Condensed checklist for a new AI session or contributor.

For a file-by-file technical inventory (LOC, responsibilities, dependencies) see [`/SUMMARY.md`](../../SUMMARY.md) at the project root instead — this index is for narrative/decision context, `SUMMARY.md` is for the module map.

## Adding to this structure

This set of 11 files covers what a light initial exploration surfaced. As the project grows, add new numbered files rather than overloading an existing one (e.g., a future `12-tests.md` once a test suite exists, or a `13-publications.md` if specific figures/results get tied to specific manuscript submissions). Update this index whenever a file is added, renamed, or removed.
