"""
src/processors/lcoe_calculator.py
=================================
Phase 5: Spatial-Economic Levelized Cost of Energy (LCOE) Modelling.

Architecture notes (post-refactor):
  - parameters.json  → única fonte de CF, LUF, density, threshold por país
  - settings.yaml    → apenas infra, paths, offsets de cenário
  - build_tech_params() → lê estrutura aninhada de CountryParams
  - settings.yaml NÃO é consultado para parâmetros tecnológicos científicos

Refactoring changes:
  - Extracted AHP, TOPSIS, OWA logic to src/utils/
  - Extracted economics (CRF, LCOE, supply curve) to src/utils/economics.py
  - Extracted raster I/O to src/utils/raster_io.py
  - Unified reporting via src/utils/reporting.py
  - Unified visualization via GeoWorldStyler.render_raster_map()

FIX (Grupo C):
  - Phase 5 now uses Phase 4 suitable-pixel mask from potential/tifs/
  - LCOE calculated only for apt pixels (matching Phase 4 definition)
  - Supply curve uses same pixel population as Phase 4 potential
  - TOPSIS preferred over OWA balanced for consistency
  - data_recovery prioritizes TIF for national statistics
"""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import Affine

from src.core.constants import (
    CF_ABS_CEILING,
    DEFAULT_TECH_PARAMS,
    FALLBACK_SUITABILITY_THRESHOLD,
    LCOE_BENCHMARK_USD_MWH,
    NODATA_FLOAT,
    TECH_ORDER,
    build_tech_params,
)
from src.core.validators import (
    validate_raster_crs,
    validate_raster_exists,
    validate_raster_shape,
)
from src.utils.economics import (
    capital_recovery_factor,
    compute_cf_bounds,
    compute_lcoe,
    compute_supply_curve,
    extract_supply_curve_thresholds,
    modulate_capacity_factor,
)
from src.utils.geo_stats import build_pixel_area_array, zonal_stats_raster
from src.utils.map_styling import GeoWorldStyler
from src.utils.reporting import ReportSection, build_phase_report
from src.utils.timing import timer
from src.utils.utils import safe_raster_open

logger = logging.getLogger("geoworld.processors.LCOECalculator")


# ═══════════════════════════════════════════════════════════════════════════
# Baseline LCOE Econometrics — IRENA 2024 Tier-3 Fallbacks
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_LCOE_PARAMS: Dict[str, Dict] = {
    "solar": {
        "capex_usd_kw": 760.0,
        "opex_usd_kw_yr": 13.0,
        "lifetime_years": 25,
        "discount_rate": 0.060,
        "color": "#F9A825",
        "label": "Solar PV",
        "resource_stem": "solar_resource",
    },
    "wind": {
        "capex_usd_kw": 1360.0,
        "opex_usd_kw_yr": 44.0,
        "lifetime_years": 25,
        "discount_rate": 0.060,
        "color": "#1565C0",
        "label": "Wind Onshore",
        "resource_stem": "wind_resource",
    },
    "biomass": {
        "capex_usd_kw": 2720.0,
        "opex_usd_kw_yr": 109.0,
        "lifetime_years": 20,
        "discount_rate": 0.070,
        "color": "#2E7D32",
        "label": "Biomass / Bioenergy",
        "resource_stem": "biomass_resource",
    },
}

# Minimum fraction of apt pixels that must have finite resource values
# before we fall back to suitability-based modulation.
_MIN_RESOURCE_COVERAGE: float = 0.05

# OWA scenario used to read suitability TIFs from Phase 3 output.
# ✅ FIX (Grupo C): now used only as fallback; Phase 4 mask is preferred
_DEFAULT_OWA_SCENARIO: str = "balanced"

# Scenario used to load Phase 4 suitable-pixel masks
_PHASE4_SCENARIO: str = "balanced"


# ═══════════════════════════════════════════════════════════════════════════
# Main Class
# ═══════════════════════════════════════════════════════════════════════════


class LCOECalculator:
    """
    Tier-structured economic solver computing spatial distribution of power
    generation costs per technology.

    Parameter precedence (highest → lowest):
      1. parameters.json  (via build_tech_params / CountryParams)
      2. DEFAULT_LCOE_PARAMS  (IRENA 2024 hard fallbacks)

    settings.yaml is intentionally NOT consulted for any scientific
    (CF, density, threshold) values.

    ✅ FIX (Grupo C): Phase 5 now uses Phase 4 suitable-pixel masks
    (from potential/tifs/) to ensure pixel populations are identical.
    """

    def __init__(self, cfg: Any, outputs_dir: Path):
        self.cfg = cfg
        self.outputs_dir = Path(outputs_dir)
        self.viz_cfg = cfg.system.get("visualization", {})
        self._admin_gdf: Optional[gpd.GeoDataFrame] = None

        # Loaded/updated per country in run()
        self.tech_params: Dict[str, Dict] = {}
        self.pot_params: Dict[str, Dict] = {}

        pipeline_dpi = cfg.system.get("pipeline", {}).get("map_dpi_export", 150)
        self.styler = GeoWorldStyler(self.viz_cfg, global_dpi=pipeline_dpi)

    # ─────────────────────────────────────────────────────────────────────
    # Parameter loading
    # ─────────────────────────────────────────────────────────────────────

    def _load_tech_params(
        self, country_code: Optional[str] = None
    ) -> Dict[str, Dict]:
        """
        Merge LCOE financial parameters.

        Source hierarchy:
          1. DEFAULT_LCOE_PARAMS (module-level, IRENA 2024)
          2. parameters.json country-level LCOE block (via cfg.get_lcoe_params)

        settings.yaml is NOT consulted for LCOE parameters (T2_09).
        """
        params: Dict[str, Dict] = {k: dict(v) for k, v in DEFAULT_LCOE_PARAMS.items()}

        # ── Tier 2: parameters.json country LCOE block ───────────────────
        if country_code:
            try:
                country_lcoe = self.cfg.get_lcoe_params(country_code)
                for tech in params:
                    tc = country_lcoe.get(tech, {})
                    for key in ("capex_usd_kw", "opex_usd_kw_yr", "discount_rate"):
                        if key in tc:
                            params[tech][key] = float(tc[key])
                    if "lifetime_years" in tc:
                        params[tech]["lifetime_years"] = int(tc["lifetime_years"])
                logger.info(
                    "  [LCOE Params] Country parameters applied for %s.",
                    country_code,
                )
            except AttributeError:
                # cfg.get_lcoe_params not implemented — silent skip
                pass
            except Exception as exc:
                logger.warning(
                    "  [LCOE Params] Falling back to global defaults for %s: %s",
                    country_code,
                    exc,
                )

        # ── Sanity-check: discount_rate and lifetime must be positive ────
        for tech, tp in params.items():
            if tp.get("discount_rate", 0.0) <= 0.0:
                fallback = DEFAULT_LCOE_PARAMS[tech]["discount_rate"]
                logger.warning(
                    "  [%s] discount_rate=%.4f ≤ 0 after merge — "
                    "reset to DEFAULT %.4f.",
                    tech,
                    tp.get("discount_rate", 0.0),
                    fallback,
                )
                params[tech]["discount_rate"] = fallback

            if tp.get("lifetime_years", 0) <= 0:
                fallback = DEFAULT_LCOE_PARAMS[tech]["lifetime_years"]
                logger.warning(
                    "  [%s] lifetime_years=%d ≤ 0 after merge — "
                    "reset to DEFAULT %d.",
                    tech,
                    tp.get("lifetime_years", 0),
                    fallback,
                )
                params[tech]["lifetime_years"] = fallback

        return params

    def _load_pot_params(
        self, country_params: Optional[Dict]
    ) -> Dict[str, Dict]:
        """
        Build physical technology parameters (LUF, CF, density, thresholds)
        exclusively from parameters.json via build_tech_params().

        The returned dict has the structure:
          {
            "solar":   {"land_use_factor": ..., "capacity_factor": ...,
                        "power_density_mw_km2": ...,
                        "thresholds": {"optimistic": ..., "balanced": ...,
                                       "conservative": ...},
                        "cf_ceiling": ..., "cf_floor": ...},
            "wind":    {...},
            "biomass": {...},
          }
        """
        params = build_tech_params(self.cfg.system, country_params)
        logger.info(
            "  [pot_params] Solar LUF=%.2f CF=%.3f | "
            "Wind density=%.1f MW/km² CF=%.3f | "
            "Biomass CF=%.3f  thr_balanced=%.2f",
            params["solar"]["land_use_factor"],
            params["solar"]["capacity_factor"],
            params["wind"]["power_density_mw_km2"],
            params["wind"]["capacity_factor"],
            params["biomass"]["capacity_factor"],
            params["solar"]["thresholds"]["balanced"],
        )
        return params

    # ─────────────────────────────────────────────────────────────────────
    # ✅ NOVO: Phase 4 suitable-pixel mask resolution (FIX Grupo C)
    # ─────────────────────────────────────────────────────────────────────

    def _find_potential_suitable_tif(
        self,
        country_code: str,
        tech: str,
        scenario: str = _PHASE4_SCENARIO,
    ) -> Optional[Path]:
        """
        Locate the Phase 4 suitable-pixel mask TIF.

        Phase 4 exports to:
          outputs/{CODE}/potential/tifs/{CODE}_{tech}_suitable_{scenario}.tif

        This mask defines the exact pixel population used by Phase 4 to
        calculate potential. Phase 5 must use the same mask to ensure
        LCOE statistics are for the same set of pixels.

        Returns None if the file is not found (Phase 4 pre-refactor or
        skipped). In that case, Phase 5 falls back to threshold-based
        suitability masking (less accurate but functional).
        """
        tif_dir = self.outputs_dir / country_code / "potential" / "tifs"

        candidates = [
            tif_dir / f"{country_code}_{tech}_suitable_{scenario}.tif",
            tif_dir / f"{country_code.lower()}_{tech}_suitable_{scenario}.tif",
        ]

        for path in candidates:
            if path.exists():
                logger.debug("  [%s] Phase 4 suitable TIF: %s", tech, path.name)
                return path

        matches = sorted(tif_dir.glob(f"*{tech}*suitable*{scenario}*.tif"))
        if matches:
            logger.debug("  [%s] Phase 4 suitable TIF (glob): %s", tech, matches[0].name)
            return matches[0]

        return None

    def _load_potential_suitable_mask(
        self,
        country_code: str,
        tech: str,
        expected_shape: Tuple[int, int],
        scenario: str = _PHASE4_SCENARIO,
    ) -> Optional[np.ndarray]:
        """
        Load Phase 4 suitable-pixel mask from disk.

        The mask is a uint8 array with:
          - 255 = pixel is suitable for this technology in this scenario
          - 0 = pixel is not suitable (or NoData)

        This ensures Phase 5 LCOE calculations use exactly the same
        pixel population as Phase 4 potential calculations.

        Args:
            country_code:  ISO country code
            tech:          Technology identifier (solar, wind, biomass)
            expected_shape: Expected raster shape (H, W)
            scenario:      Scenario name (balanced by default)

        Returns:
            Boolean mask array of shape expected_shape, or None if the
            TIF is not found or cannot be read.
        """
        tif_path = self._find_potential_suitable_tif(country_code, tech, scenario)

        if tif_path is None:
            logger.debug(
                "  [%s] Phase 4 suitable mask not found at %s — "
                "will fall back to suitability thresholding.",
                tech,
                self.outputs_dir / country_code / "potential" / "tifs",
            )
            return None

        try:
            with safe_raster_open(tif_path) as src:
                arr = src.read(1).astype(np.uint8)
                # Pixels with value 255 are suitable
                mask = (arr == 255)

            if mask.shape != expected_shape:
                logger.warning(
                    "  [%s] Phase 4 suitable mask shape %s ≠ expected %s — "
                    "discarding.",
                    tech,
                    mask.shape,
                    expected_shape,
                )
                return None

            n_suitable = int(mask.sum())
            logger.info(
                "  [%s] Phase 4 suitable mask loaded: %d pixels (%d%% of territory)",
                tech,
                n_suitable,
                int(100.0 * n_suitable / max(mask.size, 1)),
            )
            return mask

        except Exception as exc:
            logger.warning(
                "  [%s] Could not read Phase 4 suitable mask: %s — "
                "will fall back to suitability thresholding.",
                tech,
                exc,
            )
            return None

    # ─────────────────────────────────────────────────────────────────────
    # Suitability / Resource TIF resolution
    # ─────────────────────────────────────────────────────────────────────

    def _find_suitability_tif(
        self,
        suitability_dir: Path,
        tech: str,
        country_code: str,
    ) -> Optional[Path]:
        """
        Locate the suitability TIF for *tech*.

        ✅ FIX (Grupo C): Priority order changed to prefer TOPSIS over OWA.

        Phase 3 produces:
          - TOPSIS (primary): {CODE}_{tech}_suitability.tif
          - OWA per scenario: {CODE}_{tech}_suitability_owa_{scenario}.tif

        We prefer TOPSIS to match Phase 4 behavior. OWA is only used
        as fallback (backward compatibility).

        Candidates (in order):
          1. TOPSIS: {CODE}_{tech}_suitability.tif
          2. OWA balanced: {CODE}_{tech}_suitability_owa_balanced.tif
          3. Lower-case variants
          4. Glob fallback
        """
        candidates: List[Path] = [
            # Primary: TOPSIS, matching Phase 4 behavior
            suitability_dir / f"{country_code}_{tech}_suitability.tif",
            # Secondary: OWA balanced fallback
            suitability_dir / f"{country_code}_{tech}_suitability_owa_balanced.tif",
            # Lower-case country code variants
            suitability_dir / f"{country_code.lower()}_{tech}_suitability.tif",
            suitability_dir
            / f"{country_code.lower()}_{tech}_suitability_owa_balanced.tif",
        ]

        for path in candidates:
            if path.exists():
                logger.debug("  [%s] suitability TIF: %s", tech, path.name)
                return path

        # Glob fallback
        matches = sorted(suitability_dir.glob(f"*{tech}*suitability*.tif"))
        if matches:
            logger.debug("  [%s] suitability TIF (glob): %s", tech, matches[0].name)
            return matches[0]

        return None

    def _find_resource_tif(
        self,
        criteria_dir: Path,
        tech: str,
        country_code: str,
    ) -> Optional[Path]:
        """
        Locate the normalised resource raster for *tech* in *criteria_dir*.

        Tries multiple naming conventions produced by CriteriaBuilder:
          • {CODE}_{stem}.tif   (e.g. PRT_wind_resource.tif)
          • {code}_{stem}.tif   (lower-case)
          • {stem}.tif
          • glob fallback *{stem}*.tif
        """
        stem = DEFAULT_LCOE_PARAMS[tech].get(
            "resource_stem", f"{tech}_resource"
        )
        candidates = [
            criteria_dir / f"{country_code.upper()}_{stem}.tif",
            criteria_dir / f"{country_code.lower()}_{stem}.tif",
            criteria_dir / f"{stem}.tif",
        ]
        for path in candidates:
            if path.exists():
                logger.debug("  [%s] resource TIF: %s", tech, path.name)
                return path

        matches = sorted(criteria_dir.glob(f"*{stem}*.tif"))
        if matches:
            logger.debug("  [%s] resource TIF (glob): %s", tech, matches[0].name)
            return matches[0]

        logger.warning(
            "  [%s] No resource TIF found in %s (stem=%s). "
            "Suitability array will be used as modulation proxy.",
            tech,
            criteria_dir,
            stem,
        )
        return None

    @staticmethod
    def _load_resource_arr(
        res_path: Path, H: int, W: int, tech: str
    ) -> Optional[np.ndarray]:
        """
        Load a resource raster and validate it has sufficient finite coverage.

        Resource TIFs from CriteriaBuilder are normalised [0, 1].
        We only mask true NODATA sentinels (< 0 or == nodata value),
        NOT zero-values, because resource=0 is physically meaningful.

        Returns None when the file is effectively empty so callers can
        fall back to suitability modulation.
        """
        try:
            with safe_raster_open(res_path) as src:
                raw = src.read(1).astype(np.float32)
                nodata = src.nodata
        except Exception as exc:
            logger.warning(
                "  [%s] Cannot read resource TIF %s: %s", tech, res_path.name, exc
            )
            return None

        # Mask only true NODATA sentinels
        if nodata is not None:
            arr = np.where((raw == nodata) | np.isnan(raw), np.nan, raw).astype(
                np.float32
            )
        else:
            arr = np.where(raw < 0, np.nan, raw).astype(np.float32)

        finite_frac = float(np.isfinite(arr).sum()) / max(arr.size, 1)
        if finite_frac < _MIN_RESOURCE_COVERAGE:
            logger.warning(
                "  [%s] Resource TIF '%s' has only %.1f%% finite pixels "
                "(threshold %.0f%%) — likely empty. "
                "Falling back to suitability-based modulation.",
                tech,
                res_path.name,
                finite_frac * 100,
                _MIN_RESOURCE_COVERAGE * 100,
            )
            return None

        if arr.shape != (H, W):
            logger.warning(
                "  [%s] Resource TIF shape %s ≠ suitability shape %s — "
                "discarding.",
                tech,
                arr.shape,
                (H, W),
            )
            return None

        finite_vals = arr[np.isfinite(arr)]
        if finite_vals.size > 0:
            mean_v = float(np.mean(finite_vals))
            std_v = float(np.std(finite_vals))
            cv_v = std_v / mean_v if mean_v != 0 else 0.0
            logger.info(
                "  [%s] resource TIF: %.1f%% finite | "
                "mean=%.4f std=%.4f cv=%.4f | [%.4f, %.4f]",
                tech,
                finite_frac * 100,
                mean_v,
                std_v,
                cv_v,
                float(np.min(finite_vals)),
                float(np.max(finite_vals)),
            )
        else:
            logger.warning("  [%s] Resource TIF has no finite values.", tech)

        return arr

    # ─────────────────────────────────────────────────────────────────────
    # Parameter resolvers (CF, threshold)
    # ─────────────────────────────────────────────────────────────────────

    def _resolve_base_cf(self, tech: str, pp: Dict) -> float:
        """
        Return the base capacity factor for *tech* from pot_params.

        Precedence:
          1. pp["capacity_factor"]  — from build_tech_params / parameters.json
          2. DEFAULT_TECH_PARAMS[tech]["capacity_factor"]  — Tier 2 global
             baseline (constants.py), the same source build_tech_params()
             itself falls back to (emergency only).

        settings.yaml is intentionally NOT in the lookup chain.
        """
        base_cf = float(pp.get("capacity_factor", 0.0))
        if base_cf > 0.0:
            return base_cf

        # Emergency fallback — should never be reached if parameters.json
        # is properly populated.
        fallback = DEFAULT_TECH_PARAMS.get(tech, {}).get("capacity_factor", 0.25)
        logger.warning(
            "  [%s] capacity_factor=0 from build_tech_params — "
            "emergency fallback to DEFAULT_TECH_PARAMS CF=%.3f. "
            "Check parameters.json for this country.",
            tech,
            fallback,
        )
        return fallback

    @staticmethod
    def _resolve_threshold(
        pp: Dict, scenario: str = _DEFAULT_OWA_SCENARIO
    ) -> float:
        """
        Return the suitability threshold for *scenario* from pot_params.

        pp["thresholds"] is a dict produced by build_tech_params:
          {"optimistic": 0.65, "balanced": 0.75, "conservative": 0.85}

        Falls back to FALLBACK_SUITABILITY_THRESHOLD only if the key is
        genuinely absent.
        """
        thresholds = pp.get("thresholds", {})
        if not thresholds:
            logger.warning(
                "  pot_params has no 'thresholds' key — using fallback %.2f. "
                "Check build_tech_params output.",
                FALLBACK_SUITABILITY_THRESHOLD,
            )
            return FALLBACK_SUITABILITY_THRESHOLD

        thr = thresholds.get(scenario)
        if thr is None:
            # Try first available scenario
            thr = next(iter(thresholds.values()), FALLBACK_SUITABILITY_THRESHOLD)
            logger.warning(
                "  Threshold for scenario '%s' not found in %s — "
                "using %.2f.",
                scenario,
                list(thresholds.keys()),
                thr,
            )
        return float(thr)

    # ─────────────────────────────────────────────────────────────────────
    # Execution root
    # ─────────────────────────────────────────────────────────────────────

    def run(
        self,
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        suitability_dir: Path,
        criteria_dir: Path,
        context_gdf: Optional[gpd.GeoDataFrame] = None,
        country_params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Execute Phase 5: spatial LCOE from suitability maps + resource rasters."""

        # ── 0. Load parameters for this country ─────────────────────────
        self.tech_params = self._load_tech_params(country_code)
        self.pot_params = self._load_pot_params(country_params)

        started_at = datetime.now()
        timings: Dict[str, float] = {}

        # ── 1. Discover suitability TIFs ─────────────────────────────────
        suitability_dir = Path(suitability_dir)
        suit_paths: Dict[str, Path] = {}
        ref_suit: Optional[Path] = None

        for tech in TECH_ORDER:
            sp = self._find_suitability_tif(suitability_dir, tech, country_code)
            if sp is not None:
                suit_paths[tech] = sp
                if ref_suit is None:
                    ref_suit = sp
            else:
                logger.warning(
                    "  Suitability TIF not found for %s in %s",
                    tech,
                    suitability_dir,
                )

        if not suit_paths:
            raise RuntimeError(
                "No suitability TIFs found. Run Phase 3 first.\n"
                f"  searched in: {suitability_dir}"
            )

        # ── 2. Load reference metadata from first valid TIF ──────────────
        with safe_raster_open(ref_suit) as src:
            expected_crs = str(src.crs)
            expected_shape = (src.height, src.width)

        # ── 3. Validate each suitability TIF and log statistics ──────────
        for tech, path in suit_paths.items():
            validate_raster_exists(path, f"suitability_{tech}")
            validate_raster_crs(path, expected_crs)
            validate_raster_shape(path, expected_shape)

            with safe_raster_open(path) as src:
                arr = src.read(1).astype(np.float32)
                if src.nodata is not None:
                    arr = np.where(arr == src.nodata, np.nan, arr)
            finite = arr[np.isfinite(arr)]
            if finite.size:
                logger.info(
                    "  Input %-12s: min=%.3f  max=%.3f  mean=%.3f  "
                    "finite=%.1f%%",
                    tech,
                    float(np.min(finite)),
                    float(np.max(finite)),
                    float(np.mean(finite)),
                    100.0 * finite.size / arr.size,
                )
            else:
                logger.warning("  Input %-12s: no finite values.", tech)

        # ── 4. Prepare output directories ────────────────────────────────
        base = self.outputs_dir / country_code / "lcoe"
        out_fig = base / "figures"
        out_data = base / "data"
        out_rep = base / "reports"
        out_tif = base / "tif"  # ← GeoTIFF output
        for d in (out_fig, out_data, out_rep, out_tif):
            d.mkdir(parents=True, exist_ok=True)

        self._admin_gdf = self.styler.load_admin_boundaries(
            country_name, mainland_gdf, Path(self.cfg.raw_path)
        )
        if self._admin_gdf is not None:
            logger.info(
                "  Admin boundaries: %d regions loaded", len(self._admin_gdf)
            )

        # ── 5. Process each technology ───────────────────────────────────
        results: Dict[str, Any] = {
            "country_code": country_code,
            "timestamp": started_at.isoformat(),
            "techs": {},
            "timings": timings,
            "elapsed_total": 0.0,
        }

        for tech in TECH_ORDER:
            if tech not in suit_paths:
                logger.warning(
                    "  [%s] Skipping — suitability TIF not found.", tech
                )
                continue

            res_path = self._find_resource_tif(
                Path(criteria_dir), tech, country_code
            )
            if res_path:
                validate_raster_exists(res_path, f"resource_{tech}")

            logger.info(
                "\n  %s\n  Technology: %s\n  %s",
                "─" * 50,
                self.tech_params[tech]["label"],
                "─" * 50,
            )

            with timer(tech, timings):
                results["techs"][tech] = self._run_technology(
                    tech,
                    suit_paths[tech],
                    res_path,
                    country_code,
                    country_name,
                    mainland_gdf,
                    context_gdf,
                    out_fig,
                    out_data,
                    out_tif,
                    expected_shape,
                )

        # ── 6. Comparison map ─────────────────────────────────────────────
        with timer("comparison_map", timings):
            self._plot_comparison(
                results,
                country_name,
                country_code,
                mainland_gdf,
                context_gdf,
                out_fig / f"{country_code}_lcoe_comparison.png",
            )

        results["elapsed_total"] = round(
            (datetime.now() - started_at).total_seconds(), 1
        )

        # ── 7. Report ─────────────────────────────────────────────────────
        report_text = self._format_report(results, country_name, country_code)
        logger.info("\n%s", report_text)
        (
            out_rep
            / f"{country_code}_lcoe_{started_at.strftime('%Y%m%d_%H%M%S')}.txt"
        ).write_text(report_text, encoding="utf-8")

        # ── 8. Completeness check ─────────────────────────────────────────
        generated = [t for t in TECH_ORDER if t in results["techs"]]
        missing = [t for t in TECH_ORDER if t not in generated]
        if missing:
            logger.warning("  Missing LCOE results for: %s", ", ".join(missing))
        else:
            logger.info("  All expected LCOE results generated successfully.")

        # ── 9. Build serialisable result for the orchestrator ─────────────
        serializable_results: Dict[str, Any] = {
            "country": results["country_code"],
            "timestamp": results["timestamp"],
            "elapsed_total": results["elapsed_total"],
            "techs": {},
        }

        for tech, data in results["techs"].items():
            raw_t = data.get("transform")
            if raw_t is not None and hasattr(raw_t, "a"):
                coerced: Optional[Tuple] = (
                    raw_t.a,
                    raw_t.b,
                    raw_t.c,
                    raw_t.d,
                    raw_t.e,
                    raw_t.f,
                )
            elif raw_t is not None:
                coerced = tuple(raw_t)
            else:
                coerced = None

            tech_serial: Dict[str, Any] = {
                "tech": tech,
                "label": data.get("label"),
                "params": data.get("params"),
                "stats": data.get("stats", {}),
                "transform": coerced,
                "crs": data.get("crs"),
            }

            sc = data.get("supply_curve")
            if sc is not None and not sc.empty:
                tech_serial["supply_curve_summary"] = {
                    "n_points": int(len(sc)),
                    "min_lcoe": float(sc["lcoe_usd_mwh"].min()),
                    "max_lcoe": float(sc["lcoe_usd_mwh"].max()),
                    "max_capacity_gw": float(sc["cum_capacity_gw"].max()),
                }

            serializable_results["techs"][tech] = tech_serial

        return serializable_results

    # ─────────────────────────────────────────────────────────────────────
    # Refactored per-technology pipeline
    # ─────────────────────────────────────────────────────────────────────

    def _run_technology(
        self,
        tech: str,
        suit_path: Path,
        res_path: Optional[Path],
        country_code: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        out_fig: Path,
        out_data: Path,
        out_tif: Path,
        expected_shape: Tuple[int, int],
    ) -> Dict[str, Any]:
        """
        Execute the full LCOE pipeline for one technology.

        ✅ FIX (Grupo C): Now loads Phase 4 suitable-pixel mask and uses it
        for consistency.
        """
        # 1. Load rasters and prepare financials
        suit_arr, transform, crs, H, W = self._load_input_rasters(
            suit_path, res_path, tech
        )
        res_arr = (
            self._load_resource_arr(res_path, H, W, tech) if res_path else None
        )

        lp = self.tech_params[tech]
        pp = self.pot_params[tech]

        crf, base_cf, thr, base_lcoe = self._prepare_financials(tech, lp, pp)

        # ✅ FIX (Grupo C): Load Phase 4 suitable-pixel mask
        phase4_apt_mask = self._load_potential_suitable_mask(
            country_code=country_code,
            tech=tech,
            expected_shape=(H, W),
            scenario=_PHASE4_SCENARIO,
        )

        apt_mask, source_arr, source_label, mask_source = self._prepare_modulation_source(
            suit_arr, res_arr, thr, tech, H, W, phase4_apt_mask=phase4_apt_mask
        )

        # 2. Compute CF and LCOE maps
        cf_local, lcoe_map, stats = self._compute_cf_and_lcoe(
            tech,
            lp,
            pp,
            apt_mask,
            source_arr,
            source_label,
            base_cf,
            crf,
            base_lcoe,
            suit_arr,
        )

        # 3. Capacity map and supply curve
        px_area = build_pixel_area_array(transform, H, W)
        cap_map = np.where(
            np.isfinite(lcoe_map),
            px_area * pp["land_use_factor"] * pp["power_density_mw_km2"],
            np.nan,
        )
        supply_curve = compute_supply_curve(lcoe_map, cap_map)

        # 4. Export outputs (plots, GeoTIFF, zonal stats)
        self._export_technology_outputs(
            tech,
            lcoe_map,
            cap_map,
            supply_curve,
            stats,
            transform,
            crs,
            country_name,
            mainland_gdf,
            context_gdf,
            out_fig,
            out_data,
            out_tif,
            country_code,
        )

        return {
            "tech": tech,
            "label": lp["label"],
            "params": lp,
            "stats": stats,
            "supply_curve": supply_curve,
            "lcoe_map": lcoe_map,  # kept in-memory only; not serialised
            "transform": transform,
            "crs": crs,
        }

    # ─────────────────────────────────────────────────────────────────────
    # Sub‑helpers for _run_technology
    # ─────────────────────────────────────────────────────────────────────

    def _load_input_rasters(
        self, suit_path: Path, res_path: Optional[Path], tech: str
    ) -> Tuple[np.ndarray, Affine, str, int, int]:
        """
        Load suitability raster and return (arr, transform, crs, H, W).

        BUG_08 (fix): nodata masking is now done INSIDE the rasterio context
        manager, not after closing the file.
        """
        with safe_raster_open(suit_path) as src:
            suit_arr = src.read(1).astype(np.float32)
            transform = src.transform
            crs = str(src.crs)
            H, W = src.height, src.width
            if src.nodata is not None:
                suit_arr = np.where(suit_arr == src.nodata, np.nan, suit_arr)
        return suit_arr, transform, crs, H, W

    def _prepare_financials(
        self, tech: str, lp: Dict, pp: Dict
    ) -> Tuple[float, float, float, float]:
        """Compute CRF, base CF, threshold and base LCOE."""
        discount_rate = max(float(lp.get("discount_rate", 0.0)), 1e-6)
        lifetime = max(int(lp.get("lifetime_years", 0)), 1)

        crf = capital_recovery_factor(discount_rate, lifetime)
        base_cf = self._resolve_base_cf(tech, pp)
        thr = self._resolve_threshold(pp, _DEFAULT_OWA_SCENARIO)
        base_lcoe = compute_lcoe(
            lp["capex_usd_kw"], lp["opex_usd_kw_yr"], crf, base_cf
        )

        logger.info(
            "  [%s] CRF=%.4f | base_cf=%.3f | base_lcoe=%.1f USD/MWh | "
            "CAPEX=%d | OPEX=%d | r=%.1f%% | n=%d yr | thr=%.2f",
            tech,
            crf,
            base_cf,
            base_lcoe if np.isfinite(base_lcoe) else -1,
            int(lp["capex_usd_kw"]),
            int(lp["opex_usd_kw_yr"]),
            discount_rate * 100,
            lifetime,
            thr,
        )
        return crf, base_cf, thr, base_lcoe

    def _prepare_modulation_source(
        self,
        suit_arr: np.ndarray,
        res_arr: Optional[np.ndarray],
        thr: float,
        tech: str,
        H: int,
        W: int,
        phase4_apt_mask: Optional[np.ndarray] = None,
    ) -> Tuple[np.ndarray, np.ndarray, str, str]:
        """
        Determine which array (resource or suitability) is used as modulation
        source, and build the apt_mask.

        ✅ FIX (Grupo C): Now accepts and prioritizes Phase 4 suitable-pixel
        mask for consistency between Phases 4 and 5.

        Returns:
            (apt_mask, source_arr, source_label, mask_source)
            where mask_source is either "phase4_potential_mask" or
            "suitability_threshold_{threshold}".
        """
        if phase4_apt_mask is not None:
            # ✅ Use Phase 4 mask, but still intersect with finite suitability
            apt_mask = phase4_apt_mask & np.isfinite(suit_arr)
            mask_source = "phase4_potential_mask"
        else:
            # Fallback: threshold-based masking
            apt_mask = np.isfinite(suit_arr) & (suit_arr >= thr)
            mask_source = f"suitability_threshold_{thr:.2f}"

        if res_arr is not None and np.isfinite(res_arr).sum() > 0:
            source_arr = res_arr
            source_label = "resource_tif"
        else:
            source_arr = suit_arr
            source_label = "suitability_proxy"

        logger.info(
            "  [%s] Modulation source: %s | mask=%s | apt_pixels=%d (%.1f%%)",
            tech,
            source_label,
            mask_source,
            int(apt_mask.sum()),
            100.0 * apt_mask.sum() / max(apt_mask.size, 1),
        )
        return apt_mask, source_arr, source_label, mask_source

    def _compute_cf_and_lcoe(
        self,
        tech: str,
        lp: Dict,
        pp: Dict,
        apt_mask: np.ndarray,
        source_arr: np.ndarray,
        source_label: str,
        base_cf: float,
        crf: float,
        base_lcoe: float,
        suit_arr: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray, Dict]:
        """
        Compute local capacity factor and LCOE maps, with fallback logic
        for biomass when resource cv is too low.

        ✅ FIX (Grupo C): LCOE map is now computed ONLY for apt pixels,
        ensuring consistency with Phase 4 pixel population.

        Uses modulate_capacity_factor from src/utils/economics.py with
        cf_ceiling and cf_floor from TechParams (pp) if available.
        """
        # Source statistics over apt pixels
        valid_source = apt_mask & np.isfinite(source_arr)
        src_apt = source_arr[valid_source]

        if src_apt.size == 0:
            raise RuntimeError(
                f"[{tech}] No valid source pixels. "
                "Check suitability maps and country parameters."
            )

        src_mean = max(float(np.mean(src_apt)), 1e-6)
        src_cv = float(np.std(src_apt)) / src_mean

        logger.info(
            "  [%s] source stats over apt pixels: n=%d | mean=%.4f | std=%.4f | cv=%.4f",
            tech,
            src_apt.size,
            src_mean,
            float(np.std(src_apt)),
            src_cv,
        )

        # ── Biomass: fallback if resource array is effectively constant ──
        if tech == "biomass" and source_label == "resource_tif" and src_cv < 0.01:
            logger.warning(
                "  [BIOMASS] Resource TIF has cv=%.4f < 0.01 — "
                "effectively constant. Falling back to suitability proxy.",
                src_cv,
            )
            source_arr = suit_arr
            source_label = "suitability_proxy (resource constant)"
            valid_source = apt_mask & np.isfinite(source_arr)
            src_apt = source_arr[valid_source]
            if src_apt.size == 0:
                raise RuntimeError(
                    f"[{tech}] No valid suitability pixels after fallback."
                )
            src_mean = max(float(np.mean(src_apt)), 1e-6)
            src_cv = float(np.std(src_apt)) / src_mean
            logger.info(
                "  [%s] suitability proxy: mean=%.4f std=%.4f cv=%.4f",
                tech,
                src_mean,
                float(np.std(src_apt)),
                src_cv,
            )

        # ── CF band using economics.py helper ──────────────────────────
        cf_floor, cf_ceiling = compute_cf_bounds(
            base_cf,
            tech,
            cf_ceiling_override=pp.get("cf_ceiling"),
            cf_floor_override=pp.get("cf_floor"),
            cf_absolute_ceiling=CF_ABS_CEILING,
        )

        cf_local = modulate_capacity_factor(
            base_cf, source_arr, src_mean, cf_floor, cf_ceiling
        )

        # ── LCOE map ────────────────────────────────────────────────────
        # ✅ FIX (Grupo C): Only compute LCOE for apt pixels
        annual_cost = lp["capex_usd_kw"] * crf + lp["opex_usd_kw_yr"]

        lcoe_map = np.full_like(cf_local, np.nan, dtype=np.float32)
        valid_lcoe_mask = apt_mask & np.isfinite(cf_local) & (cf_local > 0)
        lcoe_map[valid_lcoe_mask] = (
            (annual_cost / (cf_local[valid_lcoe_mask] * 8760.0)) * 1000.0
        ).astype(np.float32)

        # ── Statistics ──────────────────────────────────────────────────
        lcoe_valid = lcoe_map[np.isfinite(lcoe_map) & (lcoe_map > 0)]
        if lcoe_valid.size > 0:
            stats = {
                "base_lcoe": float(base_lcoe)
                if np.isfinite(base_lcoe)
                else 0.0,
                "mean": float(np.mean(lcoe_valid)),
                "p10": float(np.percentile(lcoe_valid, 10)),
                "p90": float(np.percentile(lcoe_valid, 90)),
                "n_pixels": int(lcoe_valid.size),
                "crf": round(crf, 4),
                "base_cf": round(base_cf, 4),
                "source": source_label,
            }
            logger.info(
                "  [%s] LCOE: mean=%.1f | p10=%.1f | p90=%.1f | n=%d",
                tech,
                stats["mean"],
                stats["p10"],
                stats["p90"],
                stats["n_pixels"],
            )
        else:
            logger.error("  [%s] lcoe_valid is EMPTY — all pixels NaN.", tech)
            stats = {
                "crf": round(crf, 4),
                "base_cf": round(base_cf, 4),
                "source": source_label,
            }

        return cf_local, lcoe_map, stats

    def _export_technology_outputs(
        self,
        tech: str,
        lcoe_map: np.ndarray,
        cap_map: np.ndarray,
        supply_curve: pd.DataFrame,
        stats: Dict,
        transform: Affine,
        crs: str,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        out_fig: Path,
        out_data: Path,
        out_tif: Path,
        country_code: str,
    ) -> None:
        """Export GeoTIFF, zonal stats, and plots for one technology."""
        # Zonal stats
        if self._admin_gdf is not None and not self._admin_gdf.empty:
            zonal_df = zonal_stats_raster(
                lcoe_map,
                np.isfinite(lcoe_map) & (lcoe_map > 0),
                self._admin_gdf,
                transform,
                crs,
                "lcoe",
                sort_by="lcoe_mean",
            )
            if not zonal_df.empty:
                zonal_df.sort_values("lcoe_mean").to_csv(
                    str(out_data / f"{country_code}_{tech}_lcoe_zonal.csv"),
                    index=False,
                    encoding="utf-8",
                )

        # Plots
        self._plot_lcoe_map(
            lcoe_map,
            transform,
            crs,
            tech,
            stats,
            country_name,
            mainland_gdf,
            context_gdf,
            out_fig / f"{country_code}_{tech}_lcoe_map.png",
        )
        self._plot_supply_curve(
            supply_curve,
            tech,
            stats,
            country_name,
            out_fig / f"{country_code}_{tech}_supply_curve.png",
        )

        # GeoTIFF
        tif_path = out_tif / f"{country_code}_{tech}_lcoe_usd_mwh.tif"

        out_arr = np.where(
            np.isfinite(lcoe_map), lcoe_map, NODATA_FLOAT
        ).astype(np.float32)
        profile = dict(
            driver="GTiff",
            dtype="float32",
            width=lcoe_map.shape[1],
            height=lcoe_map.shape[0],
            count=1,
            crs=crs,
            transform=transform,
            nodata=NODATA_FLOAT,
            compress="lzw",
            tiled=True,
            blockxsize=256,
            blockysize=256,
        )
        with rasterio.open(str(tif_path), "w", **profile) as dst:
            dst.write(out_arr, 1)
        logger.info("  [%s] LCOE TIF: %s", tech, tif_path.name)

    # ─────────────────────────────────────────────────────────────────────
    # Visualisation helpers (using render_raster_map)
    # ─────────────────────────────────────────────────────────────────────

    def _plot_lcoe_map(
        self,
        lcoe_map: np.ndarray,
        transform: Affine,
        crs: str,
        tech: str,
        stats: Dict,
        country_name: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        out_path: Path,
    ) -> None:
        """
        Render spatial LCOE map for one technology using unified render_raster_map.

        BUG_09 / DUP_22: now delegates to GeoWorldStyler.render_raster_map().
        """
        lp = self.tech_params[tech]
        p10 = stats.get("p10", 20)
        p90 = stats.get("p90", 200)
        vmin = max(p10 or 10, 10)
        vmax = min(p90 or 300, 300)

        self.styler.render_raster_map(
            score=lcoe_map,
            transform=transform,
            crs=crs,
            mainland_gdf=mainland_gdf,
            context_gdf=context_gdf,
            cmap_name="RdYlGn",
            cmap_reverse=True,
            cmap_vmin_frac=0.0,
            cmap_vmax_frac=1.0,
            vmin=vmin,
            vmax=vmax,
            exclude_mask=None,  # LCOE has no hard-exclusion overlay
            title_main=f"{lp['label']} — Levelized Cost of Energy",
            title_sub=country_name,
            params_text=(
                f"CAPEX: ${lp['capex_usd_kw']:.0f}/kW  |  "
                f"OPEX: ${lp['opex_usd_kw_yr']:.0f}/kW/yr  |  "
                f"r: {lp['discount_rate'] * 100:.0f}%"
            ),
            stats_text=(
                f"Mean: {stats.get('mean', 0):.1f}  |  "
                f"P10: {stats.get('p10', 0):.1f}  |  "
                f"P90: {stats.get('p90', 0):.1f}  USD/MWh"
            ),
            cbar_label=r"LCOE (USD MWh$^{-1}$)",
            admin_gdf=self._admin_gdf,
            out_path=out_path,
            right_in_override=1.55,
        )

    def _plot_supply_curve(
        self,
        supply_curve: pd.DataFrame,
        tech: str,
        stats: Dict,
        country_name: str,
        out_path: Path,
    ) -> None:
        """Plot merit order economic curve for one technology."""
        if supply_curve.empty:
            return

        lp = self.tech_params[tech]
        bench = LCOE_BENCHMARK_USD_MWH.get(tech, {})

        fig, ax = plt.subplots(figsize=(10, 6), dpi=self.styler.dpi)
        fig.patch.set_facecolor(self.styler.fig_bg)
        ax.set_facecolor("#FAFAFA")

        n_pts = min(5000, len(supply_curve))
        idx = np.linspace(0, len(supply_curve) - 1, n_pts, dtype=int)
        sc = supply_curve.iloc[idx].reset_index(drop=True)

        ax.fill_between(
            sc["cum_capacity_gw"],
            sc["lcoe_usd_mwh"],
            alpha=0.18,
            color=lp["color"],
            zorder=2,
        )
        ax.plot(
            sc["cum_capacity_gw"],
            sc["lcoe_usd_mwh"],
            color=lp["color"],
            linewidth=2.2,
            zorder=3,
            label=f"{lp['label']} — Cost Curve",
        )

        if bench:
            ax.axhspan(
                bench["p25"],
                bench["p75"],
                alpha=0.08,
                color="#888888",
                zorder=1,
                label=f"IRENA 2024 IQR: {bench['p25']:.0f}–{bench['p75']:.0f}",
            )
            ax.axhline(
                bench["median"],
                color="#555555",
                linewidth=1.4,
                linestyle="--",
                zorder=4,
                label=f"IRENA Median: {bench['median']:.0f}",
            )

        if "base_lcoe" in stats and stats["base_lcoe"]:
            ax.axhline(
                stats["base_lcoe"],
                color=lp["color"],
                linewidth=1.2,
                linestyle=":",
                zorder=4,
                alpha=0.75,
                label=f"Nat. Base LCOE: {stats['base_lcoe']:.1f}",
            )

        ax.set_xlabel("Cumulative Installable Capacity (GW)", fontsize=11)
        ax.set_ylabel(r"LCOE (USD MWh$^{-1}$)", fontsize=11)
        ax.set_title(
            f"{lp['label']} — Merit Order Curve\n"
            f"{country_name}  |  Balanced Scenario",
            fontsize=12,
            fontweight="bold",
            pad=12,
        )
        ax.legend(fontsize=9, loc="upper left")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.3, linestyle="--")
        ax.set_xlim(left=0)

        fig.tight_layout()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(
            str(out_path),
            bbox_inches="tight",
            dpi=self.styler.dpi,
            facecolor=fig.get_facecolor(),
        )
        plt.close(fig)

    def _plot_comparison(
        self,
        results: Dict,
        country_name: str,
        country_code: str,
        mainland_gdf: gpd.GeoDataFrame,
        context_gdf: Optional[gpd.GeoDataFrame],
        out_path: Path,
    ) -> None:
        """
        Compose side-by-side LCOE maps for all technologies via PIL.

        BUG_10: now calls render_raster_map directly via create_comparison_via_pil.
        """
        plot_data_list = []

        for tech in TECH_ORDER:
            r = results["techs"].get(tech, {})
            lcoe_map = r.get("lcoe_map")
            if lcoe_map is None:
                continue

            lp = self.tech_params[tech]
            stats = r.get("stats", {})
            p10 = stats.get("p10", 20)
            p90 = stats.get("p90", 200)
            vmin = max(p10 or 10, 10)
            vmax = min(p90 or 300, 300)

            plot_data_list.append(
                {
                    "score": lcoe_map,
                    "transform": r["transform"],
                    "crs": r.get("crs", "EPSG:4326"),
                    "mainland_gdf": mainland_gdf,
                    "context_gdf": context_gdf,
                    "cmap_name": "RdYlGn",
                    "cmap_reverse": True,
                    "cmap_vmin_frac": 0.0,
                    "cmap_vmax_frac": 1.0,
                    "vmin": vmin,
                    "vmax": vmax,
                    "exclude_mask": None,
                    "title_main": f"{lp['label']} — LCOE",
                    "title_sub": country_name,
                    "params_text": "",
                    "stats_text": (
                        f"Mean: {stats.get('mean', 0):.1f}  |  "
                        f"P10: {stats.get('p10', 0):.1f}  |  "
                        f"P90: {stats.get('p90', 0):.1f}  USD/MWh"
                    ),
                    "cbar_label": r"LCOE (USD MWh$^{-1}$)",
                    "admin_gdf": self._admin_gdf,
                    "out_path": (
                        Path(tempfile.gettempdir()) / f"{tech}_lcoe_tmp.png"
                    ),
                    "right_in_override": 1.55,
                }
            )

        if plot_data_list:
            self.styler.create_comparison_via_pil(
                individual_plot_func=self.styler.render_raster_map,
                plot_data_list=plot_data_list,
                country_name=country_name,
                title_line1=f"Levelized Cost of Energy (LCOE) — {country_name}",
                title_line2="Resource-Adjusted Capacity Factor  |  Balanced Scenario",
                out_path=out_path,
                gap_px=20,
            )

    # ─────────────────────────────────────────────────────────────────────
    # Text report using reporting.py
    # ─────────────────────────────────────────────────────────────────────

    def _format_report(
        self, results: Dict, country_name: str, code: str
    ) -> str:
        """
        Build text report using the unified reporting module.
        """
        tech_sections: List[ReportSection] = []

        for tech in TECH_ORDER:
            if tech not in results["techs"]:
                continue
            lp = self.tech_params[tech]
            stats = results["techs"][tech].get("stats", {})

            rows = [
                (
                    f"CAPEX: {lp['capex_usd_kw']:.0f} USD/kW  |  "
                    f"OPEX: {lp['opex_usd_kw_yr']:.0f} USD/kW/yr  |  "
                    f"r: {lp['discount_rate'] * 100:.0f}%  |  "
                    f"n: {lp['lifetime_years']} yr"
                ),
                (
                    f"CRF: {stats.get('crf', 0.0):.4f}  |  "
                    f"Base CF: {stats.get('base_cf', 0.0):.3f}  |  "
                    f"Modulation: {stats.get('source', 'n/a')}"
                ),
            ]

            # Metrics
            metric_rows = [
                (
                    "Base LCOE (National mean CF)",
                    f"{stats.get('base_lcoe', 0):.1f} USD/MWh",
                ),
                (
                    "Spatial mean LCOE",
                    f"{stats.get('mean', 0):.1f} USD/MWh",
                ),
                ("P10 — Cheapest 10%", f"{stats.get('p10', 0):.1f} USD/MWh"),
                (
                    "P90 — Most expensive 10%",
                    f"{stats.get('p90', 0):.1f} USD/MWh",
                ),
            ]
            rows.extend(metric_rows)

            # Supply curve thresholds
            sc = results["techs"][tech].get("supply_curve")
            if sc is not None and not sc.empty:
                tot = float(sc["cum_capacity_gw"].max())
                thresholds = extract_supply_curve_thresholds(sc)
                for thr, gw in sorted(thresholds.items()):
                    pct = (gw / tot * 100) if tot > 0 else 0.0
                    rows.append(
                        (
                            f"Available capacity <= {thr} USD/MWh",
                            f"{gw:.2f} GW ({pct:.1f}%)",
                        )
                    )

            tech_sections.append(ReportSection(title=f"{lp['label'].upper()}", rows=rows))

        # Build full report
        return build_phase_report(
            title="LCOE REPORT — Levelized Cost of Energy (Phase 5)",
            country_name=country_name,
            country_code=code,
            timestamp=results["timestamp"],
            sections=tech_sections,
            timings=results.get("timings", {}),
            elapsed_total=results.get("elapsed_total", 0.0),
            width=72,
        )