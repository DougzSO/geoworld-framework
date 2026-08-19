---
id: mem-readme
type: index
status: active
created: 2026-08-17
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [docs-readme]
linked_by: []
scope: "Lista de conteúdo de memory/ para consulta rápida por tópico; a ordem de leitura oficial vive só em docs/README.md, não aqui."
---

# /docs/memory — Reading Index

**The reading order for `docs/` (including this folder) lives in exactly one place: [`../README.md`](../README.md).** This file only lists what's in `memory/` itself, for quick lookup — it does not duplicate the reading order.

| File | Covers |
| --- | --- |
| [`01-overview.md`](01-overview.md) | What GeoWorld is, for whom, and why it exists. |
| [`02-architecture.md`](02-architecture.md) | Package layout, layering, module relationships. |
| [`03-pipeline.md`](03-pipeline.md) | The nine-phase execution flow and data contracts. |
| [`04-algorithms.md`](04-algorithms.md) | AHP / TOPSIS / OWA / LCOE / GHG abatement / sensitivity methods. |
| [`05-environment.md`](05-environment.md) | Python version, dependencies, `.venv`, raw data, reproducibility, RNG seeds. |
| [`06-risk-areas.md`](06-risk-areas.md) | Fragile/untested/hardcoded areas — owner of the test-suite-state fact. |
| [`07-configuration.md`](07-configuration.md) | `settings.yaml` vs. `parameters.json` — owner of the `skip_*` flag state and "adding a country" facts. |
| [`08-conventions.md`](08-conventions.md) | Coding, logging, docstring, naming conventions. |
| [`09-decisions.md`](09-decisions.md) | Redirect stub → [`../DECISIONS.md`](../DECISIONS.md). |
| [`10-scripts-and-commands.md`](10-scripts-and-commands.md) | How to actually run the pipeline. |
| [`11-onboarding.md`](11-onboarding.md) | Condensed checklist for a new AI session or contributor. |

For a file-by-file technical inventory (LOC, responsibilities, dependencies) see [`/SUMMARY.md`](../../SUMMARY.md) at the project root — this index is for narrative/decision context, `SUMMARY.md` is for the module map.

## Adding to this structure

Add new numbered files rather than overloading an existing one (e.g., a future `12-tests.md`, or a `13-publications.md`). Update this table whenever a file is added, renamed, or removed — and update `../README.md`'s reading order too if the change affects it.
