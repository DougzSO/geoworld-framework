# main.py
"""
main.py
=======
Entry point for the GeoWorld Framework pipeline.

Orchestrates 9 phases of renewable energy potential analysis:
  Phase 1  — Data Quality Audit
  Phase 2a — Grid Alignment (spatial harmonisation)
  Phase 2b — Criteria Builder (normalised 0–1 suitability layers)
  Phase 3  — Suitability Builder (OWA + TOPSIS multi-criteria)
  Phase 4  — Potential Calculator (installable capacity & generation)
  Phase 5  — LCOE Calculator (levelised cost of electricity)
  Phase 6  — Results Synthesis (dominance maps, dashboard, GeoTIFFs)
  Phase 7  — GHG Abatement & Thermal Substitution
  Phase 8  — Sensitivity Analysis
  Phase 9  — Transport Decarbonisation

Each phase is idempotent: reads artefacts produced by the previous phase
on disk and can be skipped via settings.yaml (skip_*: true) when outputs
already exist.

Usage:
    python main.py PRT
    python main.py "South Africa"
    python main.py --batch country_list.txt
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd

try:
    import geodatasets
    HAS_GEODATASETS = True
except ImportError:
    HAS_GEODATASETS = False

# ── Framework imports ──────────────────────────────────────────────────────
from src.core.config_loader import ConfigError, ConfigLoader
from src.core.pipeline_orchestrator import PipelineOrchestrator
from src.core.schemas import (
    CriteriaResult,
    LCOEResult,
    PotentialResult,
    SuitabilityResult,  # T1_06
)
from src.io.data_fetcher import DataFetcher
from src.io.data_manager import DataManager
from src.io.data_orchestrator import DataOrchestrator
from src.processors.criteria_builder import CriteriaBuilder
from src.processors.data_auditor import DataAuditor, get_mainland_gdf
from src.processors.ghg_abatement_calculator import GHGAbatementCalculator
from src.processors.grid_aligner import GridAligner
from src.processors.lcoe_calculator import LCOECalculator
from src.processors.potential_calculator import PotentialCalculator
from src.processors.raster_processor import RasterProcessor
from src.processors.results_writer import ResultsWriter
from src.processors.sensitivity_analyzer import SensitivityAnalyzer
from src.processors.suitability_builder import SuitabilityBuilder
from src.processors.transport_decarbonization_calculator import (
    TransportDecarbonizationCalculator,
)
from src.utils.logging_utils import set_logging_context, setup_logging


# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════


class LogFormat:
    """Centralised formatting constants for uniform log output."""

    SEPARATOR_MAJOR = "=" * 60
    SEPARATOR_MINOR = "-" * 52
    PAD_OK    = "[  OK   ]"
    PAD_SKIP  = "[ SKIP  ]"
    PAD_DISK  = "[ DISK  ]"
    PAD_FAIL  = "[MISSING]"


_FORBIDDEN_CHARS = re.compile(r'[/\\;|&<>\'"`${}()*?\[\]]')

# Layers expected after Phase 2a alignment — used only for skip-mode re-entry.
_ALIGNED_SUFFIX: Dict[str, str] = {
    "elevation":  "elevation",
    "slope":      "slope",
    "solar":      "solar",
    "wind":       "wind",
    "land_cover": "lc",
    "population": "population",
    "roads":      "roads",
    "plants":     "plants",
    "lakes":      "lakes",
    "rivers":     "rivers",
    "seismic":    "seismic",
    "grid":       "grid",
}

# Default criteria-TIF sub-path (single canonical location — IMM_04 resolved).
_CRITERIA_TIF_SUBPATH = "criteria_builder/tif"


# ═══════════════════════════════════════════════════════════════════════════
# SMALL PURE HELPERS
# ═══════════════════════════════════════════════════════════════════════════


def _safe_read_geodata(
    path: Optional[Path], layer_name: str, logger: logging.Logger
) -> Optional[gpd.GeoDataFrame]:
    """Read a vector file; return None on any failure."""
    if not path or not Path(path).exists():
        return None
    try:
        return gpd.read_file(path)
    except Exception as exc:
        logger.error("Error reading %s at %s: %s", layer_name, path, exc)
        return None


def _load_context_gdf(
    target_country_name: str, logger: logging.Logger
) -> Optional[gpd.GeoDataFrame]:
    """Load a global land layer for cartographic context.

    GeoPandas >= 1.0 removed the bundled `geopandas.datasets` module.
    This function tries `geodatasets` first (explicit dependency), then
    the legacy geopandas bundled datasets (GeoPandas < 1.0), and warns
    clearly if neither is available so the user knows why context is absent.
    """
    try:
        if HAS_GEODATASETS:
            world = gpd.read_file(geodatasets.get_path("naturalearth.land"))
        elif hasattr(gpd, "datasets") and hasattr(gpd.datasets, "get_path"):
            # GeoPandas < 1.0 only — removed in 1.0.
            world = gpd.read_file(gpd.datasets.get_path("naturalearth_lowres"))
        else:
            logger.warning(
                "Cartographic context disabled: `geodatasets` is not installed "
                "and geopandas.datasets is unavailable (GeoPandas >= 1.0 removed "
                "it). Install geodatasets: pip install geodatasets"
            )
            return None

        name_col = "name" if "name" in world.columns else "SOVEREIGNT"
        return (
            world[world[name_col].str.lower() != target_country_name.lower()]
            if name_col in world.columns
            else world
        )
    except Exception as exc:
        logger.warning("Cartographic context unavailable: %s", exc)
        return None


def _check_disk_space(
    output_dir: Path, required_gb: float, logger: logging.Logger
) -> bool:
    """Verify sufficient disk space before large raster operations."""
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(output_dir).free / (1024 ** 3)
        if free_gb < required_gb:
            logger.error(
                "Insufficient disk space — required: %.1f GB, available: %.1f GB.",
                required_gb,
                free_gb,
            )
            return False
        return True
    except Exception as exc:
        logger.warning("Could not verify disk space: %s", exc)
        return True   # non-fatal — let the OS raise if truly out of space


def _validate_target(target: str, cfg: ConfigLoader) -> Tuple[str, str]:
    """Parse, sanitise and resolve a country identifier via config whitelist.

    Returns:
        (country_name, country_code) tuple.

    Raises:
        ValueError: if the target is blank, contains forbidden characters,
                    or is not registered in parameters.json.
    """
    if not target or not target.strip():
        raise ValueError("Country argument is blank.")
    if _FORBIDDEN_CHARS.search(target) or ".." in target:
        raise ValueError(
            f"Invalid argument: '{target}' contains forbidden characters."
        )
    try:
        info = cfg.get_country_by_name(target)
        return info["country_name"], info["country_code"]
    except (KeyError, ConfigError):
        raise ValueError(
            f"Unmapped target: '{target}'. "
            "ISO-3166 code or alias must be registered in parameters.json."
        )


def _load_existing_aligned(
    processed_dir: Path, code: str
) -> Dict[str, Optional[Path]]:
    """Reconstruct aligned-layer paths for skip-mode re-entry."""
    d = processed_dir / code
    return {
        layer: (p if (p := d / f"{code}_{suffix}_aligned.tif").exists() else None)
        for layer, suffix in _ALIGNED_SUFFIX.items()
    }


# ═══════════════════════════════════════════════════════════════════════════
# PARAM MERGE (Phase 4 → Phase 5) — IMM_05 FIX
# ═══════════════════════════════════════════════════════════════════════════


def _merge_pot_result_into_params(
    params: Any,
    pot_result: Optional[PotentialResult],
    logger: logging.Logger,
) -> Any:
    """
    Inject capacity factors from Phase 4 into CountryParams if absent.

    IMM_05 Semantics
    ----------------
    CF in parameters.json = national reference mean (IEA/IRENA empirical data).
    CF from Phase 4       = spatially-resolved per-pixel estimate (resource → CF).

    Injection is a FALLBACK for countries without reference data in the JSON.
    If CF > 0.0 in the JSON, it is PRESERVED. If absent (0.0 or None), the
    Phase 4 empirical value is injected.

    Pydantic v2 note
    ----------------
    CountryParams is a frozen Pydantic v2 model with nested sub-models
    (solar: SolarParams, wind: TechParams, biomass: BiomassParams).
    model_copy(update={...}) requires the update dict to use the TOP-LEVEL
    field names of CountryParams (i.e. "solar", "wind", "biomass"), not flat
    aliases like "solar_capacity_factor". Nested updates are applied by
    rebuilding the sub-model with model_copy().

    BUG_06 (fix): The previous implementation passed flat keys like
    "solar_capacity_factor" to model_copy(), which Pydantic v2 silently
    ignored because no top-level field has that name. The update never
    actually took effect.

    Parameters
    ----------
    params      : CountryParams (Pydantic model) or dict.
    pot_result  : PotentialResult from Phase 4, or None.
    logger      : Active logger.

    Returns
    -------
    CountryParams or dict
        Updated params with CF injected ONLY where absent.
    """
    if pot_result is None:
        return params

    # ── dict fallback (legacy path) ───────────────────────────────────
    if isinstance(params, dict):
        merged = dict(params)
        for tech, tech_data in pot_result.techs.items():
            td = tech_data.params
            for key_suffix, phase4_key in (
                ("capacity_factor",      "capacity_factor"),
                ("land_use_factor",      "land_use_factor"),
                ("power_density_mw_km2", "power_density_mw_km2"),
            ):
                flat_key   = f"{tech}_{key_suffix}"
                phase4_val = td.get(phase4_key, 0.0)
                if phase4_val > 0.0 and not merged.get(flat_key):
                    merged[flat_key] = phase4_val
                    logger.info(
                        "  [param-merge] %s %s injected: %.4f (Phase 4 fallback)",
                        tech, key_suffix, phase4_val,
                    )
        return merged

    # ── Pydantic v2 path (CountryParams) ─────────────────────────────
    if not hasattr(params, "model_copy"):
        return params

    # Build nested update dict: {tech: {field: new_value}}
    nested_updates: Dict[str, Dict[str, float]] = {}

    for tech, tech_data in pot_result.techs.items():
        td = tech_data.params

        # Get current sub-model (e.g. params.solar → SolarParams)
        tech_obj = getattr(params, tech, None)
        if tech_obj is None:
            continue

        tech_changes: Dict[str, float] = {}

        for field_name, phase4_key in (
            ("capacity_factor",      "capacity_factor"),
            ("land_use_factor",      "land_use_factor"),
            ("power_density_mw_km2", "power_density_mw_km2"),
        ):
            phase4_val  = float(td.get(phase4_key, 0.0))
            current_val = float(getattr(tech_obj, field_name, 0.0) or 0.0)

            if phase4_val <= 0.0:
                continue

            if current_val == 0.0:
                # Injection: JSON did not define this value
                tech_changes[field_name] = phase4_val
                logger.info(
                    "  [param-merge] %s %s injected: %.4f (Phase 4 fallback)",
                    tech, field_name, phase4_val,
                )
            elif abs(phase4_val - current_val) > 0.05:
                # Divergence warning: JSON authority is preserved
                logger.warning(
                    "  [param-merge] %s %s mismatch — "
                    "JSON=%.4f, Phase4=%.4f (using JSON as authority)",
                    tech, field_name, current_val, phase4_val,
                )

        if tech_changes:
            nested_updates[tech] = tech_changes

    if not nested_updates:
        return params

    # Apply nested updates: rebuild each sub-model with model_copy(),
    # then rebuild CountryParams with the updated sub-models.
    top_level_update: Dict[str, Any] = {}
    for tech, changes in nested_updates.items():
        tech_obj = getattr(params, tech)
        # model_copy on the sub-model (SolarParams / TechParams / BiomassParams)
        # uses the actual field names of that model — these ARE flat (cf, luf, pd)
        top_level_update[tech] = tech_obj.model_copy(update=changes)

    return params.model_copy(update=top_level_update)


# ═══════════════════════════════════════════════════════════════════════════
# LOG HELPERS
# ═══════════════════════════════════════════════════════════════════════════


def _log_status_report(
    status: dict, elev_path: Optional[Path], logger: logging.Logger
) -> None:
    """Print a compact data-availability matrix after acquisition."""

    def _ok(value: Any) -> bool:
        if value is None or value is False:
            return False
        if isinstance(value, (list, tuple)):
            return len(value) > 0
        return True

    items = [
        ("Borders",      status.get("Borders")),
        ("Land Cover",   status.get("Land Cover")),
        ("Elevation",    elev_path),
        ("Wind",         status.get("Wind")),
        ("Solar",        status.get("Solar")),
        ("Power Plants", status.get("Plants")),
        ("Population",   status.get("Population")),
        ("Roads",        status.get("Roads")),
        ("Protected",    status.get("Protected")),
        ("Lakes",        status.get("Lakes")),
    ]

    logger.info(LogFormat.SEPARATOR_MINOR)
    logger.info("  DATA AVAILABILITY")
    logger.info(LogFormat.SEPARATOR_MINOR)
    for label, value in items:
        tag = LogFormat.PAD_OK if _ok(value) else LogFormat.PAD_FAIL
        logger.info("  %s %s", tag, label)
    logger.info(LogFormat.SEPARATOR_MINOR)


def _phase_timer(logger: logging.Logger, phase_label: str):
    """Context-manager that logs elapsed time per phase."""

    class _Timer:
        def __enter__(self):
            self._t0 = time.perf_counter()
            return self

        def __exit__(self, *_):
            logger.info(
                "  Phase %s completed in %.1fs",
                phase_label,
                time.perf_counter() - self._t0,
            )
            return False

    return _Timer()


# ═══════════════════════════════════════════════════════════════════════════
# PHASE INLINE RUNNERS  (DUP_23)
# ═══════════════════════════════════════════════════════════════════════════


def _run_phase_1_audit(
    orchestrator: PipelineOrchestrator,
    *,
    cfg: ConfigLoader,
    code: str,
    name: str,
    status: dict,
    elev_path: Optional[Path],
    slope_path: Optional[Path],
    mainland_gdf: gpd.GeoDataFrame,
    skip: dict,
    outputs_dir: Path,
    logger: logging.Logger,
) -> None:
    """Phase 1 — Data Quality Audit (DUP_23: unified skip/run logic)."""
    if orchestrator.is_skipped("audit"):
        audit_reports = list((outputs_dir / code / "audit").glob("*audit*.txt"))
        if audit_reports:
            logger.info(
                "  %s Phase 1 (Audit): skipped — prior report on disk.",
                LogFormat.PAD_SKIP,
            )
        else:
            logger.warning(
                "  Phase 1 (Audit): skipped AND no prior report found — "
                "data-quality metadata will be unavailable."
            )
        return

    with _phase_timer(logger, "Phase 1 (Audit)"):
        set_logging_context(country=code, phase="audit")
        DataAuditor(cfg).run(
            country_name=name,
            country_code=code,
            status=status,
            elev_path=elev_path,
            slope_path=slope_path,
            pop_path=status.get("Population"),
            plants_df=status.get("Plants"),
            country_gdf=mainland_gdf,
            skip_land_cover=skip["land_cover"],
        )


def _run_phase_2a_align(
    orchestrator: PipelineOrchestrator,
    *,
    cfg: ConfigLoader,
    code: str,
    name: str,
    status: dict,
    elev_path: Optional[Path],
    slope_path: Optional[Path],
    mainland_gdf: gpd.GeoDataFrame,
    processed_dir: Path,
    logger: logging.Logger,
) -> Dict[str, Optional[Path]]:
    """Phase 2a — Grid Alignment (DUP_23: unified skip/run logic).

    Returns:
        Dict mapping layer names to aligned-TIF paths. Values may be None
        if the layer was unavailable. Returns an empty dict (never None) so
        callers can always call .get() without an isinstance guard.
    """
    if orchestrator.is_skipped("align"):
        aligned = _load_existing_aligned(processed_dir, code)
        if any(aligned.values()):
            logger.info(
                "  %s Phase 2a (Align): skipped — loading aligned layers from disk.",
                LogFormat.PAD_SKIP,
            )
        else:
            logger.warning(
                "  Phase 2a (Align): skipped AND no aligned files found in %s. "
                "Downstream phases may fail.",
                processed_dir / code,
            )
        return aligned

    if not _check_disk_space(processed_dir, required_gb=5.0, logger=logger):
        raise RuntimeError("Insufficient disk space for Phase 2a.")

    with _phase_timer(logger, "Phase 2a (Grid Alignment)"):
        set_logging_context(country=code, phase="align")
        result = GridAligner(cfg, processed_dir).run(
            country_code=code,
            mainland_gdf=mainland_gdf,
            elev_path=elev_path,
            slope_path=slope_path,
            solar_path=status.get("Solar"),
            pop_path=status.get("Population"),
            roads_path=status.get("Roads"),
            wind_paths=status.get("Wind") or [],
            lc_tiles=status.get("Land Cover", []),
            plants_df=status.get("Plants"),
            lakes_path=status.get("Lakes"),
            rivers_path=status.get("Rivers"),
            seismic_path=status.get("Seismic"),
            grid_path=status.get("Grid"),
        )
        # Guarantee a dict is always returned even if GridAligner returns None
        return result if isinstance(result, dict) else {}


# ═══════════════════════════════════════════════════════════════════════════
# PIPELINE EXECUTION
# ═══════════════════════════════════════════════════════════════════════════


def run_geoworld(target: str) -> bool:
    """Execute the full GeoWorld pipeline for a single country.

    Args:
        target: Country name or ISO-3166-alpha-3 code.

    Returns:
        True on success, False on any unrecoverable error.
    """
    base_dir = Path(__file__).parent
    cfg      = ConfigLoader(base_dir)

    # ── Validate target ──────────────────────────────────────────────────
    try:
        name, code = _validate_target(target, cfg)
    except ValueError as exc:
        print(f"\n  VALIDATION ERROR: {exc}\n")
        return False

    logger = setup_logging(
        log_level=cfg.system.get("log_level", "INFO"),
        log_dir=cfg.logs_path,
        country_code=code,
        pid_suffix=True,
    )
    pipeline_cfg = cfg.system.get("pipeline", {})

    # ── Build skip-flag dict ─────────────────────────────────────────────
    skip = {
        k: pipeline_cfg.get(f"skip_{k}", False)
        for k in (
            "audit", "land_cover", "align", "criteria", "suitability",
            "potential", "lcoe", "results", "abatement", "sensitivity",
            "transport",
        )
    }

    # ── Infrastructure ───────────────────────────────────────────────────
    dm                = DataManager(cfg)
    fetcher           = DataFetcher(dm.raw_path, cfg=cfg)
    orchestrator_data = DataOrchestrator(dm, fetcher, cfg, logger)
    rp                = RasterProcessor()

    try:
        set_logging_context(country=code, phase="setup")
        logger.info("Starting analysis: %s (%s)", name, code)

        # get_country() logs its own parameter table via _log_summary
        params = cfg.get_country(code)

        # ── Boundary acquisition ─────────────────────────────────────────
        status = dm.check_country_availability(name, code)
        if not status["Borders"] and fetcher.download_gadm(name, code):
            status = dm.check_country_availability(name, code)
        if not status["Borders"]:
            logger.error("Country boundary data absent. Pipeline aborted.")
            return False

        border_gdf   = gpd.read_file(status["Borders"])
        use_mainland = (
            params.use_mainland_only
            if hasattr(params, "use_mainland_only")
            else pipeline_cfg.get("use_mainland_only", True)
        )
        mainland_gdf = get_mainland_gdf(border_gdf) if use_mainland else border_gdf
        context_gdf  = _load_context_gdf(name, logger)

        # ── Parallel data acquisition ────────────────────────────────────
        status = orchestrator_data.acquire_all(name, code, mainland_gdf)

        # Derive slope from DEM if missing
        elev_path  = status.get("Elevation_Path")
        slope_path = status.get("Slope_Path")
        if (
            elev_path
            and Path(elev_path).exists()
            and (not slope_path or not Path(slope_path).exists())
        ):
            expected_slope = Path(elev_path).parent / f"{code}_slope.tif"
            logger.info("Deriving slope from DEM …")
            rp.calculate_slope(elev_path, expected_slope)
            slope_path = expected_slope if expected_slope.exists() else None

        _log_status_report(status, elev_path, logger)

        # ── Path setup ───────────────────────────────────────────────────
        processed_dir = Path(
            getattr(cfg, "processed_path", base_dir / "data" / "processed")
        )
        raw_outputs = cfg.system.get("paths", {}).get("outputs", "outputs")
        outputs_dir = (
            Path(raw_outputs)
            if Path(raw_outputs).is_absolute()
            else base_dir / raw_outputs
        )

        # Pre-flight check: critical layers must all be present
        if not all([elev_path, slope_path, status.get("Solar"), status.get("Land Cover")]):
            logger.error(
                "Critical input layers missing (elevation, slope, solar, or "
                "land cover). Pipeline aborted."
            )
            return False

        admin_gdf = _safe_read_geodata(status.get("Admin1"), "Admin Level 1", logger)

        # ── Pipeline orchestrator (phases 2b – 9) ───────────────────────
        orchestrator = PipelineOrchestrator(
            config_loader=cfg,
            outputs_dir=outputs_dir,
            country_code=code,
            country_name=name,
            skip_flags=skip,
        )

        # Canonical country-level output root
        country_out = outputs_dir / code

        # ════════════════════════════════════════════════════════════════
        # PHASE 1 — Data Quality Audit
        # ════════════════════════════════════════════════════════════════
        set_logging_context(country=code, phase="audit")
        _run_phase_1_audit(
            orchestrator,
            cfg=cfg,
            code=code,
            name=name,
            status=status,
            elev_path=elev_path,
            slope_path=slope_path,
            mainland_gdf=mainland_gdf,
            skip=skip,
            outputs_dir=outputs_dir,
            logger=logger,
        )

        # ════════════════════════════════════════════════════════════════
        # PHASE 2a — Grid Alignment
        # ════════════════════════════════════════════════════════════════
        set_logging_context(country=code, phase="align")
        aligned_data = _run_phase_2a_align(
            orchestrator,
            cfg=cfg,
            code=code,
            name=name,
            status=status,
            elev_path=elev_path,
            slope_path=slope_path,
            mainland_gdf=mainland_gdf,
            processed_dir=processed_dir,
            logger=logger,
        )

        # Canonical criteria-TIF directory (IMM_04: single definition)
        criteria_tifs_dir: Path = country_out / _CRITERIA_TIF_SUBPATH

        # ════════════════════════════════════════════════════════════════
        # PHASE 2b — Criteria Builder
        # output_model=CriteriaResult: validates paths and n_criteria count.
        # ════════════════════════════════════════════════════════════════
        with _phase_timer(logger, "Phase 2b (Criteria)"):
            set_logging_context(country=code, phase="criteria")
            criteria_result = orchestrator.run_phase(
                phase_name="criteria",
                phase_class=CriteriaBuilder,
                input_getter=lambda: {
                    "country_code":   code,
                    "country_name":   name,
                    "mainland_gdf":   mainland_gdf,
                    "aligned":        aligned_data,
                    "country_params": params,
                    "land_suit":      cfg.land_suitability,
                    "wdpa_path":      status.get("Protected"),
                    "lakes_path":     status.get("Lakes"),
                    "rivers_path":    status.get("Rivers"),
                    "seismic_path":   status.get("Seismic"),
                    "context_gdf":    context_gdf,
                    "plants_df":      status.get("Plants"),
                },
                output_model=CriteriaResult,
            )

        # ════════════════════════════════════════════════════════════════
        # PHASE 3 — Suitability (OWA + TOPSIS)
        # output_model=SuitabilityResult: validates per-tech stats and paths.
        # T1_06: SuitabilityResult schema added; techs with total exclusion
        #        are recorded as SuitabilityError (not ValidationError).
        # ════════════════════════════════════════════════════════════════
        if not criteria_tifs_dir.exists() and not skip["criteria"]:
            logger.warning(
                "Phase 3: criteria outputs not found at %s — "
                "run Phase 2b first or disable skip_criteria.",
                criteria_tifs_dir,
            )

        with _phase_timer(logger, "Phase 3 (Suitability)"):
            set_logging_context(country=code, phase="suitability")
            suitability_result = orchestrator.run_phase(
                phase_name="suitability",
                phase_class=SuitabilityBuilder,
                input_getter=lambda: {
                    "country_code":   code,
                    "country_name":   name,
                    "mainland_gdf":   mainland_gdf,
                    "criteria_dir":   criteria_tifs_dir,
                    "context_gdf":    context_gdf,
                    "lc_aligned":     aligned_data.get("land_cover"),
                    "slope_aligned":  aligned_data.get("slope"),
                    "country_params": params,
                },
                output_model=SuitabilityResult,
            )

        # Canonical suitability-TIF directory (phases 4–9)
        suitability_tifs_dir: Path = country_out / "suitability" / "tif"

        # ════════════════════════════════════════════════════════════════
        # PHASE 4 — Potential Calculator
        # output_model=PotentialResult: validates capacity and generation.
        # ════════════════════════════════════════════════════════════════
        with _phase_timer(logger, "Phase 4 (Potential)"):
            set_logging_context(country=code, phase="potential")
            pot_result = orchestrator.run_phase(
                phase_name="potential",
                phase_class=PotentialCalculator,
                input_getter=lambda: {
                    "country_code":    code,
                    "country_name":    name,
                    "mainland_gdf":    mainland_gdf,
                    "suitability_dir": suitability_tifs_dir,
                    "context_gdf":     context_gdf,
                    "country_params":  params,
                },
                output_model=PotentialResult,
            )

        # Merge recovered CF / LUF / PD back into params (IMM_05 guard)
        # BUG_06 (fix): nested model_copy() used for Pydantic v2 CountryParams.
        params = _merge_pot_result_into_params(params, pot_result, logger)

        # ════════════════════════════════════════════════════════════════
        # PHASE 5 — LCOE Calculator
        # output_model=LCOEResult: validates LCOE stats per technology.
        # ════════════════════════════════════════════════════════════════
        with _phase_timer(logger, "Phase 5 (LCOE)"):
            set_logging_context(country=code, phase="lcoe")
            lcoe_result = orchestrator.run_phase(
                phase_name="lcoe",
                phase_class=LCOECalculator,
                input_getter=lambda: {
                    "country_code":    code,
                    "country_name":    name,
                    "mainland_gdf":    mainland_gdf,
                    "suitability_dir": suitability_tifs_dir,
                    "criteria_dir":    criteria_tifs_dir,
                    "context_gdf":     context_gdf,
                    "country_params":  params,
                },
                output_model=LCOEResult,
            )

        # ════════════════════════════════════════════════════════════════
        # PHASES 6–9: output_model=None
        #
        # T1_07: Fases 6–9 produzem artefactos como ficheiros (GeoTIFFs,
        # CSVs, relatórios, plots) — não objectos Pydantic estruturados.
        # Não existe um schema único que capture a saída heterogénea de
        # cada uma destas fases; a validação ocorre ao nível do
        # artifact_manager via verificações de integridade do manifesto.
        # ════════════════════════════════════════════════════════════════

        # ════════════════════════════════════════════════════════════════
        # PHASE 6 — Results Synthesis
        # output_model=None: outputs are GeoTIFFs, PNGs, and text reports.
        # ════════════════════════════════════════════════════════════════
        with _phase_timer(logger, "Phase 6 (Results)"):
            set_logging_context(country=code, phase="results")

            # BUG_07 (fix): lcoe_result is a LCOEResult model (or None),
            # not a Path. ResultsWriter expects a Path for lcoe_dir.
            # Resolve to the canonical LCOE output directory regardless
            # of whether lcoe_result was loaded from disk or computed now.
            lcoe_dir_for_results = country_out / "lcoe"

            orchestrator.run_phase(
                phase_name="results",
                phase_class=ResultsWriter,
                input_getter=lambda: {
                    "country_code":    code,
                    "country_name":    name,
                    "mainland_gdf":    mainland_gdf,
                    "suitability_dir": suitability_tifs_dir,
                    "potential_dir":   country_out / "potential",
                    "lcoe_dir":        lcoe_dir_for_results,
                    "context_gdf":     context_gdf,
                    "abatement_dir":   None,
                },
                output_model=None,
            )

        # ════════════════════════════════════════════════════════════════
        # PHASE 7 — GHG Abatement
        # output_model=None: outputs are CSV tables and a text report.
        # ════════════════════════════════════════════════════════════════
        with _phase_timer(logger, "Phase 7 (Abatement)"):
            set_logging_context(country=code, phase="abatement")
            abat_result = orchestrator.run_phase(
                phase_name="abatement",
                phase_class=GHGAbatementCalculator,
                input_getter=lambda: {
                    "country_code":  code,
                    "country_name":  name,
                    "mainland_gdf":  mainland_gdf,
                    "potential_dir": country_out / "potential",
                    "lcoe_dir":      country_out / "lcoe",
                    "plants_df":     status.get("Plants"),
                    "context_gdf":   context_gdf,
                    "admin_gdf":     admin_gdf,
                },
                output_model=None,
            )

        # ════════════════════════════════════════════════════════════════
        # PHASE 8 — Sensitivity Analysis
        # output_model=None: outputs are CSVs and PNG plots (SA-1 to SA-6).
        # ════════════════════════════════════════════════════════════════
        with _phase_timer(logger, "Phase 8 (Sensitivity)"):
            set_logging_context(country=code, phase="sensitivity")
            sa_cfg = pipeline_cfg.get("sensitivity", {})
            orchestrator.run_phase(
                phase_name="sensitivity",
                phase_class=SensitivityAnalyzer,
                input_getter=lambda: {
                    "country_code":        code,
                    "suitability_results": suitability_result,
                    "criteria_dir":        criteria_tifs_dir,
                    "country_name":        name,
                    "country_params":      params,
                    "pot_results":         pot_result,
                    "abat_result":         abat_result,
                    "run_sa1":             sa_cfg.get("run_sa1", True),
                    "run_sa2":             sa_cfg.get("run_sa2", True),
                    "run_sa3":             sa_cfg.get("run_sa3", True),
                    "run_sa4":             sa_cfg.get("run_sa4", True),
                    "run_sa5":             sa_cfg.get("run_sa5", True),
                    "run_sa6":             sa_cfg.get("run_sa6", True),
                    "n_mc_samples":        sa_cfg.get("n_mc_samples", 1000),
                },
                output_model=None,
            )

        # ════════════════════════════════════════════════════════════════
        # PHASE 9 — Transport Decarbonisation
        # output_model=None: outputs are scenario CSVs and a summary report.
        # ════════════════════════════════════════════════════════════════
        with _phase_timer(logger, "Phase 9 (Transport Decarbonisation)"):
            set_logging_context(country=code, phase="transport")
            moderate_threshold = (
                cfg.system
                .get("potential", {})
                .get("scenarios", {})
                .get("moderate", {})
                .get("suitability_threshold", 0.5)
            )
            orchestrator.run_phase(
                phase_name="transport",
                phase_class=TransportDecarbonizationCalculator,
                input_getter=lambda: {
                    "country_code":          code,
                    "country_name":          name,
                    "mainland_gdf":          mainland_gdf,
                    "pot_results":           pot_result,
                    "lcoe_results":          lcoe_result,
                    "suitability_dir":       suitability_tifs_dir,
                    "context_gdf":           context_gdf,
                    "country_params":        params,
                    "suitability_threshold": moderate_threshold,
                },
                output_model=None,
            )

        set_logging_context(country=code, phase="done")
        logger.info("Pipeline completed for %s (%s).", name, code)
        return True

    except Exception as exc:
        logger.error("Pipeline failed for %s: %s", target, exc, exc_info=True)
        return False


# ═══════════════════════════════════════════════════════════════════════════
# BATCH & CLI
# ═══════════════════════════════════════════════════════════════════════════


def _batch_worker(country: str) -> Tuple[str, bool]:
    """Top-level picklable function for ProcessPoolExecutor workers."""
    return country, run_geoworld(country)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="GeoWorld Framework — Renewable Energy Potential Pipeline",
        usage="%(prog)s [-h] [--workers N] (country [country ...] | --batch FILE)",
    )
    parser.add_argument(
        "country",
        nargs="*",
        help="Country name or ISO-3166-alpha-3 code",
    )
    parser.add_argument(
        "--batch",
        metavar="FILE",
        help="Text file listing one country per line",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Parallel workers for batch mode (default: 1)",
    )
    args = parser.parse_args()

    country_arg = " ".join(args.country).strip() if args.country else None

    if country_arg and args.batch:
        parser.error("Provide either positional country args or --batch, not both.")
    if not country_arg and not args.batch:
        parser.error("Provide a country name/code or --batch file.")

    global_logger = setup_logging()
    global_logger.info(LogFormat.SEPARATOR_MAJOR)
    global_logger.info("  GEOWORLD FRAMEWORK")
    global_logger.info(LogFormat.SEPARATOR_MAJOR)
    t0 = time.perf_counter()

    if args.batch:
        batch_file = Path(args.batch)
        if not batch_file.exists():
            global_logger.error("Batch file not found: %s", batch_file)
            sys.exit(1)

        with open(batch_file, encoding="utf-8") as fh:
            countries = [
                ln.strip()
                for ln in fh
                if ln.strip() and not ln.startswith("#")
            ]

        global_logger.info(
            "Batch mode: %d countries, %d workers.", len(countries), args.workers
        )

        batch_results: List[Tuple[str, bool]] = []
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(_batch_worker, c): c for c in countries}
            for future in as_completed(futures):
                country = futures[future]
                try:
                    batch_results.append(future.result())
                except Exception as exc:
                    global_logger.error("Worker crash for %s: %s", country, exc)
                    batch_results.append((country, False))

        global_logger.info("\n" + LogFormat.SEPARATOR_MINOR)
        for country, success in batch_results:
            tag = "[SUCCESS]" if success else "[FAILED] "
            global_logger.info("  %-20s %s", country, tag)

    else:
        success = run_geoworld(country_arg)
        global_logger.info("Result: %s", "COMPLETED" if success else "FAILED")

    global_logger.info("Total elapsed time: %.1fs", time.perf_counter() - t0)