# 08 — Conventions and Standards

These are observed patterns, not imposed ones — follow them when writing new code in this repository so it stays consistent with the existing ~28k lines.

## Module docstrings

Every module in `src/` opens with a structured docstring: a title line, an underline of `=`, a short purpose paragraph, and often named subsections (`Architecture`, `Usage`, `References`, `Changelog`/`Refactoring notes`, `Used by:`). New modules should follow this shape — see `src/utils/ahp.py` or `src/processors/lcoe_calculator.py` for representative examples.

## In-docstring changelog notes

Several modules track fix/refactor history directly in their docstring using tags like `FIX (Grupo C):`, `BUG_02 (fix):`, `DUP_22`, `T1_06`, `T2_09` (e.g. `sensitivity_analyzer.py`, `lcoe_calculator.py`, `main.py`'s import comment `# T1_06`). These appear to be internal ticket/issue references from the author's own tracking, not a formal ticketing system integrated with this repo.

> ⚠️ Point to validate: the meaning of the `T1_06`, `T2_09`, `BUG_02`, `DUP_22`-style tags (what system, if any, they reference) wasn't confirmed — treat them as historical breadcrumbs, not an active issue tracker to query.

## Language

Docstrings and some inline comments mix **English and Portuguese** within the same file (e.g. `schemas.py`'s docstring is entirely in Portuguese; `lcoe_calculator.py`'s "Refactoring changes" section is in Portuguese while the rest of the file is English). New comments/docstrings should default to English to match the majority convention and the public README, unless extending a section that is already Portuguese-only.

## Logging

Always via `logging.getLogger("geoworld.<package>.<Module>")` (e.g. `"geoworld.utils.topsis"`, `"geoworld.io.DataOrchestrator"`) — never bare `print()` for operational output. Logging setup itself is centralized in `src/utils/logging_utils.py`; don't call `logging.basicConfig()` elsewhere. Per-country/phase logging context is injected via `set_logging_context()`, not by manually formatting the country into every message.

## Matplotlib usage

Object-oriented `Figure`/`Axes` API only (`fig, ax = plt.subplots(...)` or manual `Figure()` construction), never the stateful `pyplot` interface (`plt.plot(...)` etc.) inside library code — this is called out explicitly in `criteria_builder.py`'s docstring as a thread-safety requirement, since map rendering runs under `ThreadPoolExecutor`. Backend is forced to `Agg` (see `transport_decarbonization_calculator.py`: `matplotlib.use("Agg")`) since this runs headless.

## Shared helpers before new ones

Before writing a new raster-loading, area-computation, or report-formatting helper, check `src/utils/{raster_io,geo_stats,utils}.py` and `src/utils/reporting.py` first — a documented goal of the v2.0 refactor was eliminating duplicated versions of exactly these helpers across processor modules (see `09-decisions.md`).

## Type contracts

New data that crosses a phase boundary and gets persisted to disk should get a Pydantic v2 model in `src/core/schemas.py`, following the existing pattern: `frozen=True` for config-like models, `__getitem__`/`.get()` dict-compatibility for models still consumed by not-yet-migrated legacy call sites (see `schemas.py`'s own "Compatibility" design principle).

## Naming

- Phase-owning classes are named `{Purpose}{Role}` — `DataAuditor`, `GridAligner`, `CriteriaBuilder`, `SuitabilityBuilder`, `PotentialCalculator`, `LCOECalculator`, `ResultsWriter`, `GHGAbatementCalculator`, `SensitivityAnalyzer`, `TransportDecarbonizationCalculator`. A new phase should follow this pattern and live in `src/processors/`.
- Output files follow `{ISO3}_{tech}_{artifact}.{ext}` (e.g. `PRT_solar_suitability.tif`, `PRT_wind_weights.json`).

## No test suite

There is currently no `tests/` directory and no `pytest`/`unittest` usage found anywhere in the repository. There is therefore no established testing convention to follow yet — see `06-risk-areas.md`.
