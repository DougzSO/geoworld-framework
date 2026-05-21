"""
main.py
=======
Entry point for the GeoWorld Framework pipeline.

Orchestrates 8 phases of renewable energy potential analysis:
  Phase 1  — Data Quality Audit
  Phase 2a — Grid Alignment (spatial harmonisation)
  Phase 2b — Criteria Builder (normalised 0–1 suitability layers)
  Phase 3  — Suitability Builder (OWA + TOPSIS multi-criteria)
  Phase 4  — Potential Calculator (installable capacity & generation)
  Phase 5  — LCOE Calculator (levelised cost of electricity)
  Phase 6  — Results Synthesis (dominance maps, dashboard, GeoTIFFs)
  Phase 7  — GHG Abatement & Thermal Substitution
  Phase 8  — Sensitivity Analysis

Each phase is idempotent: it reads artefacts produced by the previous
phase on disk and can be skipped via settings.yaml (skip_*: true)
when outputs already exist.

Usage:
    python main.py PRT
    python main.py "South Africa"
    python main.py --batch country_list.txt 
"""

import argparse
import logging
import re
import sys
import time
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import geopandas as gpd

try:
    import geodatasets
    HAS_GEODATASETS = True
except ImportError:
    HAS_GEODATASETS = False

from src.core.config_loader import ConfigError, ConfigLoader
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
    TransportDecarbonizationCalculator
)
from src.utils.logging_utils import set_logging_context, setup_logging


# ===========================================================================
# CONSTANTS
# ===========================================================================

class LogFormat:
    """Centralized formatting constants for uniform log output."""
    
    SEPARATOR_MAJOR = "=" * 60
    SEPARATOR_MINOR = "-" * 52
    PAD_OK = "[  OK   ]"
    PAD_FAIL = "[MISSING]"


_FORBIDDEN_CHARS = re.compile(r'[/\\;|&<>\'"`${}()*?\[\]]')

_REQUIRED_LAYERS: dict[str, list[str]] = {
    "Phase 2b (Criteria)": ["elevation", "slope", "solar"],
    "Phase 3 (Suitability)": ["slope", "land_cover"],
}

_HARD_REQUIRED: set[str] = {"elevation", "slope", "solar", "land_cover"}

_ALIGNED_SUFFIX: dict[str, str] = {
    "elevation": "elevation",
    "slope": "slope",
    "solar": "solar",
    "wind": "wind",
    "land_cover": "lc",
    "population": "population",
    "roads": "roads",
    "plants": "plants",
    "lakes": "lakes",
    "rivers": "rivers",
    "seismic": "seismic",
    "grid": "grid",
}


# ===========================================================================
# HELPER FUNCTIONS
# ===========================================================================

def _safe_read_geodata(
    path: Optional[Path],
    layer_name: str,
    logger: logging.Logger
) -> Optional[gpd.GeoDataFrame]:
    """
    Read a vector file safely, returning None on any failure.
    
    Args:
        path: Path to the vector file
        layer_name: Descriptive name for logging
        logger: Logger instance
        
    Returns:
        GeoDataFrame if successful, None otherwise
    """
    if not path or not Path(path).exists():
        return None
    try:
        return gpd.read_file(path)
    except Exception as e:
        logger.error(f"Error reading {layer_name} at {path}: {e}")
        return None


def _load_context_gdf(
    target_country_name: str,
    logger: logging.Logger
) -> Optional[gpd.GeoDataFrame]:
    """
    Load a global land layer for cartographic context (neighbouring countries).
    
    Args:
        target_country_name: Name of the target country to exclude
        logger: Logger instance
        
    Returns:
        GeoDataFrame with context countries, or None if unavailable
    """
    try:
        if HAS_GEODATASETS:
            path = geodatasets.get_path("naturalearth.land")
        else:
            if (not hasattr(gpd, "datasets") or 
                not hasattr(gpd.datasets, "get_path")):
                logger.warning(
                    "geodatasets package not installed and "
                    "geopandas.datasets unavailable. Context layer disabled."
                )
                return None
            path = gpd.datasets.get_path("naturalearth_lowres")

        world = gpd.read_file(path)
        name_col = "name" if "name" in world.columns else "SOVEREIGNT"
        if name_col in world.columns:
            return world[
                world[name_col].str.lower() != target_country_name.lower()
            ]
        return world
    except Exception as e:
        logger.warning(f"Cartographic context unavailable: {e}")
        return None


def _find_criteria_dir(outputs_dir: Path, code: str) -> Optional[Path]:
    """
    Locate directory containing criteria GeoTIFFs produced by Phase 2b.
    
    Args:
        outputs_dir: Base outputs directory
        code: Country ISO code
        
    Returns:
        Path to criteria directory if found, None otherwise
    """
    for subpath in ("tif", "tifs", ""):
        d = outputs_dir / code / "criteria_builder" / subpath
        if d.exists() and any(d.glob("*.tif")):
            return d
    return None


def _check_disk_space(
    output_dir: Path,
    required_gb: float,
    logger: logging.Logger
) -> bool:
    """
    Verify sufficient disk space before large raster operations.
    
    Args:
        output_dir: Directory where outputs will be written
        required_gb: Minimum required space in gigabytes
        logger: Logger instance
        
    Returns:
        True if sufficient space available, False otherwise
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(output_dir).free / (1024 ** 3)
        if free_gb < required_gb:
            logger.error(
                f"Insufficient disk space. Required: {required_gb:.1f} GB, "
                f"available: {free_gb:.1f} GB."
            )
            return False
        return True
    except Exception as e:
        logger.warning(f"Could not verify disk space: {e}")
        return True


def _validate_target(target: str, cfg: ConfigLoader) -> tuple[str, str]:
    """
    Parse, sanitize and resolve a country identifier via config whitelist.
    
    Args:
        target: Country name or ISO code provided by user
        cfg: Configuration loader instance
        
    Returns:
        Tuple of (country_name, country_code)
        
    Raises:
        ValueError: If target is invalid or not whitelisted
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


def _load_existing_aligned(processed_dir: Path, code: str) -> dict:
    """
    Load previously aligned rasters from disk (for skip-mode re-entry).
    
    Args:
        processed_dir: Directory containing processed data
        code: Country ISO code
        
    Returns:
        Dictionary mapping layer names to file paths (or None if missing)
    """
    d = processed_dir / code
    return {
        layer: (
            d / f"{code}_{suffix}_aligned.tif"
            if (d / f"{code}_{suffix}_aligned.tif").exists()
            else None
        )
        for layer, suffix in _ALIGNED_SUFFIX.items()
    }


def _validate_aligned_layers(
    aligned_data: dict,
    phase_name: str,
    code: str,
    logger: logging.Logger
) -> None:
    """
    Ensure required aligned layers exist; raise on hard failures.
    
    Args:
        aligned_data: Dictionary of aligned layer paths
        phase_name: Name of the phase being validated
        code: Country ISO code
        logger: Logger instance
        
    Raises:
        ConfigError: If hard-required layers are missing
    """
    required = _REQUIRED_LAYERS.get(phase_name, [])
    missing = [lyr for lyr in required if not aligned_data.get(lyr)]
    if not missing:
        return

    hard = [m for m in missing if m in _HARD_REQUIRED]
    soft = [m for m in missing if m not in _HARD_REQUIRED]

    if soft:
        logger.warning(
            f"{phase_name}: Optional layers missing — {soft}. "
            "Proceeding with reduced criteria set."
        )
    if hard:
        msg = (
            f"{phase_name}: Required layers missing — {hard}. "
            "Cannot proceed."
        )
        logger.error(msg)
        raise ConfigError(msg)


def _log_parameter_dashboard(
    logger: logging.Logger,
    name: str,
    code: str,
    params: dict,
    cfg: ConfigLoader,
    pipeline_cfg: dict,
) -> None:
    """
    Emit a concise parameter summary for reproducibility.
    
    Args:
        logger: Logger instance
        name: Country name
        code: Country ISO code
        params: Country-specific parameters
        cfg: Configuration loader
        pipeline_cfg: Pipeline configuration dictionary
    """
    res_cfg = cfg.geospatial.get("resolutions", {})
    base_res = res_cfg.get("suitability", "adaptive")
    adap_cfg = res_cfg.get("adaptive", {})

    logger.info(LogFormat.SEPARATOR_MINOR)
    logger.info(f"  {name} ({code})")
    logger.info(LogFormat.SEPARATOR_MINOR)
    
    use_mainland = params.get('use_mainland_only', True)
    geometry_mode = 'Mainland only' if use_mainland else 'Full territory'
    logger.info(f"  Geometry : {geometry_mode}")

    if str(base_res).lower() == "adaptive":
        tp = adap_cfg.get("target_pixels", 50_000)
        lo = adap_cfg.get("min_deg", 0.001)
        hi = adap_cfg.get("max_deg", 0.05)
        logger.info(f"  Resolution : Adaptive ~{tp:,} px ({lo}–{hi} deg)")
    else:
        logger.info(f"  Resolution : Fixed {base_res} deg")

    opt_offset = (
        cfg.system
        .get("potential", {})
        .get("scenarios", {})
        .get("optimistic", {})
        .get("suitability_threshold", -0.10)
    )
    logger.info(f"  Suitability optimistic offset : {opt_offset:+.2f}")
    
    protected_mode = params.get('protected_as_exclusion', True)
    protected_text = 'Hard exclusion' if protected_mode else 'Penalisation'
    logger.info(f"  Protected-area mode : {protected_text}")
    logger.info(LogFormat.SEPARATOR_MINOR)


def _log_status_report(
    status: dict,
    elev_path: Optional[Path],
    logger: logging.Logger
) -> None:
    """
    Print a compact availability matrix after data acquisition.
    
    Args:
        status: Dictionary with data availability status
        elev_path: Path to elevation data
        logger: Logger instance
    """
    logger.info(LogFormat.SEPARATOR_MINOR)
    logger.info("  DATA AVAILABILITY")
    logger.info(LogFormat.SEPARATOR_MINOR)

    def _ok(value) -> bool:
        if value is None or value is False:
            return False
        if isinstance(value, (list, tuple)):
            return len(value) > 0
        return True

    items = [
        ("Borders", status.get("Borders")),
        ("Land Cover", status.get("Land Cover")),
        ("Elevation", elev_path),
        ("Wind", status.get("Wind")),
        ("Solar", status.get("Solar")),
        ("Power Plants", status.get("Plants")),
        ("Population", status.get("Population")),
        ("Roads", status.get("Roads")),
        ("Protected", status.get("Protected")),
        ("Lakes", status.get("Lakes")),
    ]

    for label, value in items:
        tag = LogFormat.PAD_OK if _ok(value) else LogFormat.PAD_FAIL
        logger.info(f"  {tag} {label}")

    logger.info(LogFormat.SEPARATOR_MINOR)


def _phase_timer(logger: logging.Logger, phase_label: str):
    """
    Context-manager-style helper to log elapsed time per phase.
    
    Args:
        logger: Logger instance
        phase_label: Descriptive label for the phase
        
    Returns:
        Context manager that logs execution time on exit
    """
    class _Timer:
        def __init__(self):
            self.t0 = None
            
        def __enter__(self):
            self.t0 = time.perf_counter()
            return self
            
        def __exit__(self, *exc):
            elapsed = time.perf_counter() - self.t0
            logger.info(f"  {phase_label} completed in {elapsed:.1f}s")
            return False
            
    return _Timer()


# ===========================================================================
# PIPELINE EXECUTION
# ===========================================================================

def run_geoworld(target: str) -> bool:
    """
    Execute the full GeoWorld pipeline for a single country.
    
    Args:
        target: Country name or ISO code
        
    Returns:
        True if pipeline completed successfully, False otherwise
    """
    base_dir = Path(__file__).parent
    cfg = ConfigLoader(base_dir)

    try:
        name, code = _validate_target(target, cfg)
    except ValueError as e:
        print(f"\n  VALIDATION ERROR: {e}\n")
        return False

    logger = setup_logging(
        log_level=cfg.system.get("log_level", "INFO"),
        log_dir=cfg.logs_path,
        country_code=code,
        pid_suffix=True,
    )
    pipeline_cfg = cfg.system.get("pipeline", {})
    skip = {
        k: pipeline_cfg.get(f"skip_{k}", False)
        for k in (
            "audit", "land_cover", "align", "criteria", "suitability",
            "potential", "lcoe", "results", "abatement", "sensitivity",
            "transport",
        )
    }

    dm = DataManager(cfg)
    fetcher = DataFetcher(dm.raw_path, cfg=cfg)
    orchestrator = DataOrchestrator(dm, fetcher, cfg, logger)
    rp = RasterProcessor()

    try:
        set_logging_context(country=code, phase="setup")
        logger.info(f"Starting analysis: {name} ({code})")
        params = cfg.get_country(code)
        _log_parameter_dashboard(logger, name, code, params, cfg, pipeline_cfg)

        # Boundary acquisition
        status = dm.check_country_availability(name, code)
        if not status["Borders"] and fetcher.download_gadm(name, code):
            status = dm.check_country_availability(name, code)
        if not status["Borders"]:
            logger.error("Country boundary data absent. Pipeline aborted.")
            return False

        border_gdf = gpd.read_file(status["Borders"])
        use_mainland = params.get(
            "use_mainland_only",
            pipeline_cfg.get("use_mainland_only", True),
        )
        mainland_gdf = (
            get_mainland_gdf(border_gdf) if use_mainland else border_gdf
        )
        context_gdf = _load_context_gdf(name, logger)

        # Parallel data acquisition
        status = orchestrator.acquire_all(name, code, mainland_gdf)

        # Derive slope from DEM if needed
        elev_path = status.get("Elevation_Path")
        slope_path = status.get("Slope_Path")
        if (elev_path and Path(elev_path).exists() and 
            (not slope_path or not Path(slope_path).exists())):
            expected_slope = Path(elev_path).parent / f"{code}_slope.tif"
            logger.info("Deriving slope from DEM")
            rp.calculate_slope(elev_path, expected_slope)
            slope_path = expected_slope if expected_slope.exists() else None

        _log_status_report(status, elev_path, logger)

        # Path setup
        processed_dir = Path(
            getattr(cfg, "processed_path", base_dir / "data" / "processed")
        )
        raw_outputs = cfg.system.get("paths", {}).get("outputs", "outputs")
        outputs_dir = (
            Path(raw_outputs)
            if Path(raw_outputs).is_absolute()
            else base_dir / raw_outputs
        )

        # Pre-flight check: critical layers
        critical_layers = [
            elev_path,
            slope_path,
            status.get("Solar"),
            status.get("Land Cover")
        ]
        if not all(critical_layers):
            logger.error(
                "Critical input layers missing (elevation, slope, solar, or "
                "land cover). Pipeline aborted."
            )
            return False

        # Read admin boundaries once for phases that need them
        admin_gdf = _safe_read_geodata(
            status.get("Admin1"),
            "Admin Level 1",
            logger
        )

        # PHASE 1: Data Quality Audit
        if not skip["audit"]:
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
        else:
            logger.info("Phase 1 (Audit): skipped.")

        # PHASE 2a: Grid Alignment
        aligned_data = None
        if not skip["align"]:
            if not _check_disk_space(
                processed_dir, required_gb=5.0, logger=logger
            ):
                return False
            with _phase_timer(logger, "Phase 2a (Grid Alignment)"):
                set_logging_context(country=code, phase="align")
                aligned_data = GridAligner(cfg, processed_dir).run(
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
        else:
            logger.info("Phase 2a (Grid Alignment): skipped.")

        aligned_data = (
            aligned_data or _load_existing_aligned(processed_dir, code)
        )

        # PHASE 2b: Criteria Builder
        if not skip["criteria"]:
            _validate_aligned_layers(
                aligned_data, "Phase 2b (Criteria)", code, logger
            )
            with _phase_timer(logger, "Phase 2b (Criteria)"):
                set_logging_context(country=code, phase="criteria")
                CriteriaBuilder(cfg, outputs_dir).run(
                    country_code=code,
                    country_name=name,
                    mainland_gdf=mainland_gdf,
                    aligned=aligned_data,
                    params=params,
                    land_suit=cfg.land_suitability,
                    wdpa_path=status.get("Protected"),
                    lakes_path=status.get("Lakes"),
                    rivers_path=status.get("Rivers"),
                    seismic_path=status.get("Seismic"),
                    context_gdf=context_gdf,
                    plants_df=status.get("Plants"),
                )
        else:
            logger.info("Phase 2b (Criteria): skipped.")

        # PHASE 3: Suitability (OWA + TOPSIS)
        suitability_results = None
        if not skip["suitability"]:
            _validate_aligned_layers(
                aligned_data, "Phase 3 (Suitability)", code, logger
            )
            set_logging_context(country=code, phase="suitability")
            criteria_tifs_dir = _find_criteria_dir(outputs_dir, code)
            if not criteria_tifs_dir:
                logger.warning(
                    "Phase 3: Criteria outputs not found. "
                    "Run Phase 2b first or disable skip_criteria."
                )
            else:
                with _phase_timer(logger, "Phase 3 (Suitability)"):
                    suitability_results = SuitabilityBuilder(
                        cfg, outputs_dir
                    ).run(
                        country_code=code,
                        country_name=name,
                        mainland_gdf=mainland_gdf,
                        criteria_dir=criteria_tifs_dir,
                        context_gdf=context_gdf,
                        lc_aligned=aligned_data.get("land_cover"),
                        slope_aligned=aligned_data.get("slope"),
                        country_params=params,
                    )
        else:
            logger.info("Phase 3 (Suitability): skipped.")

        # PHASE 4: Potential
        suitability_tifs_dir = outputs_dir / code / "suitability" / "tifs"
        pot_results = None
        lcoe_results = None

        if not skip["potential"]:
            set_logging_context(country=code, phase="potential")
            if suitability_tifs_dir.exists():
                with _phase_timer(logger, "Phase 4 (Potential)"):
                    pot_results = PotentialCalculator(
                        cfg, outputs_dir
                    ).run(
                        country_code=code,
                        country_name=name,
                        mainland_gdf=mainland_gdf,
                        suitability_dir=suitability_tifs_dir,
                        context_gdf=context_gdf,
                        country_params=params,
                    )
            else:
                logger.warning(
                    "Phase 4: Suitability TIFFs not found at "
                    f"{suitability_tifs_dir}. Skipping."
                )
        else:
            logger.info("Phase 4 (Potential): skipped.")

        # PHASE 5: LCOE
        if not skip["lcoe"]:
            set_logging_context(country=code, phase="lcoe")
            criteria_tifs_dir = _find_criteria_dir(outputs_dir, code)
            if suitability_tifs_dir.exists():
                with _phase_timer(logger, "Phase 5 (LCOE)"):
                    lcoe_results = LCOECalculator(cfg, outputs_dir).run(
                        country_code=code,
                        country_name=name,
                        mainland_gdf=mainland_gdf,
                        suitability_dir=suitability_tifs_dir,
                        criteria_dir=criteria_tifs_dir or suitability_tifs_dir,
                        context_gdf=context_gdf,
                        country_params=params,
                    )
            else:
                logger.warning(
                    "Phase 5: Suitability TIFFs not found. Skipping LCOE."
                )
        else:
            logger.info("Phase 5 (LCOE): skipped.")

        # PHASE 6: Results Synthesis
        if not skip["results"]:
            set_logging_context(country=code, phase="results")
            potential_dir_p = outputs_dir / code / "potential"
            lcoe_dir_p = outputs_dir / code / "lcoe"
            required_dirs = (
                suitability_tifs_dir, potential_dir_p, lcoe_dir_p
            )
            if all(d.exists() for d in required_dirs):
                with _phase_timer(logger, "Phase 6 (Results)"):
                    ResultsWriter(cfg, outputs_dir).run(
                        country_code=code,
                        country_name=name,
                        mainland_gdf=mainland_gdf,
                        suitability_dir=suitability_tifs_dir,
                        potential_dir=potential_dir_p,
                        lcoe_dir=lcoe_dir_p,
                        context_gdf=context_gdf,
                        abatement_dir=None,
                    )
            else:
                logger.warning(
                    "Phase 6: Upstream outputs incomplete. "
                    "Skipping synthesis."
                )
        else:
            logger.info("Phase 6 (Results): skipped.")

        # PHASE 7: GHG Abatement
        abat_results = None
        if not skip["abatement"]:
            set_logging_context(country=code, phase="abatement")
            potential_dir = outputs_dir / code / "potential"
            lcoe_dir = outputs_dir / code / "lcoe"
            if potential_dir.exists() and lcoe_dir.exists():
                with _phase_timer(logger, "Phase 7 (Abatement)"):
                    abat_results = GHGAbatementCalculator(
                        cfg, outputs_dir
                    ).run(
                        country_code=code,
                        country_name=name,
                        mainland_gdf=mainland_gdf,
                        potential_dir=potential_dir,
                        lcoe_dir=lcoe_dir,
                        plants_df=status.get("Plants"),
                        context_gdf=context_gdf,
                        admin_gdf=admin_gdf,
                    )
            else:
                logger.warning(
                    "Phase 7: Potential/LCOE outputs missing. "
                    "Skipping abatement."
                )
        else:
            logger.info("Phase 7 (Abatement): skipped.")

        # PHASE 8: Sensitivity Analysis
        if not skip["sensitivity"]:
            set_logging_context(country=code, phase="sensitivity")
            sa_cfg = pipeline_cfg.get("sensitivity", {})
            criteria_dir_sa = _find_criteria_dir(outputs_dir, code)
            if criteria_dir_sa:
                with _phase_timer(logger, "Phase 8 (Sensitivity)"):
                    SensitivityAnalyzer(cfg, outputs_dir).run(
                        country_code=code,
                        country_name=name,
                        suitability_results=suitability_results,
                        criteria_dir=criteria_dir_sa,
                        lcoe_params=lcoe_results,
                        pot_results=pot_results,
                        abat_result=abat_results,
                        run_sa1=sa_cfg.get("run_sa1", True),
                        run_sa2=sa_cfg.get("run_sa2", True),
                        run_sa3=sa_cfg.get("run_sa3", True),
                        run_sa4=sa_cfg.get("run_sa4", True),
                        run_sa5=sa_cfg.get("run_sa5", True),
                        run_sa6=sa_cfg.get("run_sa6", True),
                        n_mc_samples=sa_cfg.get("n_mc_samples", 1000),
                    )
            else:
                logger.warning(
                    "Phase 8: Criteria outputs not found. "
                    "Skipping sensitivity analysis."
                )
        else:
            logger.info("Phase 8 (Sensitivity): skipped.")

        # PHASE 9: Transport Decarbonisation
        transport_results = None
        if not skip["transport"]:
            set_logging_context(country=code, phase="transport")
            potential_dir = outputs_dir / code / "potential"
            lcoe_dir = outputs_dir / code / "lcoe"
            if potential_dir.exists():
                with _phase_timer(logger, "Phase 9 (Transport Decarbonisation)"):
                    transport_params_path = (
                        base_dir / "configs" / "transport_parameters.json"
                    )
                    transport_calc = TransportDecarbonizationCalculator(
                        cfg, outputs_dir, 
                        transport_params_path=transport_params_path
                    )
                    transport_results = transport_calc.run(
                        country_code=code,
                        country_name=name,
                        mainland_gdf=mainland_gdf,
                        pot_results=pot_results,
                        lcoe_results=lcoe_results,
                        suitability_dir=suitability_tifs_dir,
                        context_gdf=context_gdf,
                    )
            else:
                logger.warning(
                    "Phase 9: Potential outputs missing. "
                    "Run Phase 4 first or disable skip_potential."
                )
        else:
            logger.info("Phase 9 (Transport): skipped.")

        set_logging_context(country=code, phase="done")
        logger.info(f"Pipeline completed for {name} ({code}).")
        return True

    except Exception as e:
        logger.error(f"Pipeline failed for {target}: {e}", exc_info=True)
        return False


# ===========================================================================
# BATCH & CLI
# ===========================================================================

def _batch_worker(country: str) -> tuple[str, bool]:
    """
    Top-level function (picklable) for ProcessPoolExecutor workers.
    
    Args:
        country: Country name or ISO code
        
    Returns:
        Tuple of (country, success_status)
    """
    return country, run_geoworld(country)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "GeoWorld Framework — Renewable Energy Potential Pipeline"
        ),
        usage="%(prog)s [-h] [--workers N] (country [country ...] | --batch FILE)",
    )
    parser.add_argument(
        "country",
        nargs="*",
        help="Country name or ISO-3166-alpha-3 code",
    )
    parser.add_argument(
        "--batch",
        type=str,
        default=None,
        help="Text file listing one country per line",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel workers for batch mode",
    )
    args = parser.parse_args()

    country_arg = " ".join(args.country).strip() if args.country else None

    if country_arg and args.batch:
        parser.error(
            "Provide either positional country args or --batch, not both."
        )
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
            global_logger.error(f"Batch file not found: {batch_file}")
            sys.exit(1)

        with open(batch_file, "r", encoding="utf-8") as f:
            countries = [
                ln.strip() for ln in f
                if ln.strip() and not ln.startswith("#")
            ]

        global_logger.info(
            f"Batch mode: {len(countries)} countries, {args.workers} workers."
        )

        results: list[tuple[str, bool]] = []
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(_batch_worker, c): c for c in countries
            }
            for future in as_completed(futures):
                country = futures[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    global_logger.error(
                        f"Worker crash for {country}: {e}"
                    )
                    results.append((country, False))

        global_logger.info("\n" + LogFormat.SEPARATOR_MINOR)
        for country, success in results:
            tag = "[SUCCESS]" if success else "[FAILED] "
            global_logger.info(f"  {country:<20} {tag}")
    else:
        success = run_geoworld(country_arg)
        tag = "COMPLETED" if success else "FAILED"
        global_logger.info(f"Result: {tag}")

    elapsed = time.perf_counter() - t0
    global_logger.info(f"Total elapsed time: {elapsed:.1f}s")