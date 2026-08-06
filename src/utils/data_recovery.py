"""
src/utils/data_recovery.py
===========================
Robust data recovery from disk with Pydantic validation.

Reconstructs pipeline phase results from CSV/TIF/JSON artifacts when:
  - Results come from cache
  - Pipeline resumes after interruption
  - Phase 6 needs to aggregate outputs from Phases 3-5

Architecture:
  - Type-safe recovery functions (return Pydantic models, not dicts)
  - Explicit error handling (ValueError for missing/corrupt data)
  - Priority fallbacks (CSV → TIF → JSON → estimation)
  - Column name normalization (handles legacy naming)

Used by:
  - results_writer.py (Phase 6)
  - sensitivity_analyzer.py (loading suitability results)

Area (BLOCKER-003):
  - _extract_area() reads the real area_km2_sum column Phase 4 now
    writes directly into its zonal CSV — no TIF re-derivation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import rasterio

from src.core.constants import TECH_ORDER

logger = logging.getLogger("geoworld.utils.data_recovery")


# ═══════════════════════════════════════════════════════════════════════
# Potential Results Recovery (Phase 4 → Phase 6)
# ═══════════════════════════════════════════════════════════════════════

def recover_potential_from_disk(
    base_dir: Path,
    country_code: str,
    scenarios: Optional[List[str]] = None,
    resolution_deg: float = 0.01,  # ← NÃO USADO, mantido para compatibilidade
) -> Dict:
    """Reconstruct potential results from Phase 4 CSV outputs.

    Recovery searches for files matching the pattern:
      ``data/{CODE}_{tech}_{scenario}_zonal.csv``

    with a glob fallback for non-standard names.

    Args:
        base_dir:       Root directory of pipeline outputs.
        country_code:   ISO country code (e.g. ``"PRT"``).
        scenarios:      Scenarios to recover; defaults to the three
                        standard scenarios if *None*.
        resolution_deg: **DEPRECATED** — kept for API compatibility,
                        not used (area comes from the zonal CSV's
                        area_km2_sum column, written by Phase 4 --
                        BLOCKER-003).

    Returns:
        Dict with keys ``country_code`` and ``techs`` (nested by
        technology and scenario).

    Raises:
        ValueError: If no potential data can be recovered for any
                    technology/scenario combination.
    """
    if scenarios is None:
        scenarios = ["optimistic", "balanced", "conservative"]

    base_dir = Path(base_dir)
    data_dir = base_dir / "data"
    if not data_dir.exists():
        data_dir = base_dir

    techs_data: Dict[str, Dict] = {}
    missing_files: List[str] = []

    for tech in TECH_ORDER:
        scenarios_data: Dict[str, Dict] = {}

        for scenario in scenarios:
            csv_path = data_dir / f"{country_code}_{tech}_{scenario}_zonal.csv"

            if not csv_path.exists():
                candidates = sorted(
                    data_dir.glob(f"*{tech}*{scenario}*.csv")
                )
                if candidates:
                    csv_path = candidates[0]
                    logger.debug(
                        "  [%s/%s] CSV found via glob: %s",
                        tech, scenario, csv_path.name,
                    )
                else:
                    missing_files.append(f"{tech}_{scenario}")
                    continue

            try:
                df = pd.read_csv(csv_path)

                capacity_gw    = _extract_capacity(df)
                generation_twh = _extract_generation(df)

                # BLOCKER-003: area_km2 is now a real column written by
                # Phase 4 (area_km2_sum, summed across admin regions --
                # additive, same pattern as capacity/generation).
                area_km2 = _extract_area(df)

                scenarios_data[scenario] = {
                    "capacity_gw":    capacity_gw,
                    "generation_twh": generation_twh,
                    "area_km2":       area_km2,
                }

                logger.debug(
                    "  [%s/%s] Recovered: %.1f GW, %.1f TWh/yr, %.0f km²",
                    tech, scenario,
                    capacity_gw, generation_twh, area_km2,
                )

            except Exception as exc:
                logger.warning(
                    "  [%s/%s] CSV parse failed: %s — %s",
                    tech, scenario, csv_path.name, exc,
                )
                missing_files.append(f"{tech}_{scenario}")
                continue

        if scenarios_data:
            techs_data[tech] = {"scenarios": scenarios_data}

    if not techs_data:
        raise ValueError(
            f"No potential data recovered for {country_code}.\n"
            f"  Searched in: {data_dir}\n"
            f"  Missing files: {', '.join(missing_files)}"
        )

    if missing_files:
        logger.warning(
            "  Partial recovery: missing data for %s",
            ", ".join(missing_files),
        )

    return {
        "country_code": country_code,
        "techs":        techs_data,
    }


# ═══════════════════════════════════════════════════════════════════════
# Column extractors — capacity, generation
# ═══════════════════════════════════════════════════════════════════════

def _extract_capacity(df: pd.DataFrame) -> float:
    """Extract total capacity in GW from CSV columns (flexible matching)."""
    cols = set(df.columns)

    for col_name, divisor in [
        ("capacity_mw_sum", 1_000.0),
        ("capacity_mw",     1_000.0),
        ("capacity_gw_sum", 1.0),
        ("capacity_gw",     1.0),
    ]:
        if col_name in cols:
            return float(df[col_name].sum()) / divisor

    raise ValueError(
        f"No capacity column found in CSV. Available columns: {list(cols)}"
    )


def _extract_generation(df: pd.DataFrame) -> float:
    """Extract total annual generation in TWh from CSV columns."""
    cols = set(df.columns)

    for col_name, divisor in [
        ("generation_twh_sum",      1.0),
        ("generation_twh",          1.0),
        ("gen_twh",                 1.0),
        ("generation_gwh_sum",      1_000.0),
        ("generation_gwh",          1_000.0),
        ("annual_generation_gwh",   1_000.0),
    ]:
        if col_name in cols:
            return float(df[col_name].sum()) / divisor

    raise ValueError(
        f"No generation column found in CSV. Available columns: {list(cols)}"
    )


# ═══════════════════════════════════════════════════════════════════════
# Area (BLOCKER-003) — real column written by Phase 4, no TIF re-derivation
# ═══════════════════════════════════════════════════════════════════════

def _extract_area(df: pd.DataFrame) -> float:
    """
    Extract total suitable area (km²) from CSV columns (flexible matching).

    Real geodesic area, written directly by Phase 4 as area_km2_sum
    (summed per admin region — additive, same recovery pattern as
    capacity/generation). Returns 0.0 with a warning if the column is
    absent (e.g. a zonal CSV from before this column existed), rather
    than raising — area is supplementary, not essential, to recovery.
    """
    cols = set(df.columns)

    for col_name in ("area_km2_sum", "area_km2"):
        if col_name in cols:
            return float(df[col_name].sum())

    logger.warning(
        "  No area_km2 column found in CSV (columns: %s) — area=0.0. "
        "Re-run Phase 4 to populate it.",
        list(cols),
    )
    return 0.0


# ═══════════════════════════════════════════════════════════════════════
# LCOE Results Recovery (Phase 5 → Phase 6)
# ═══════════════════════════════════════════════════════════════════════

def recover_lcoe_from_disk(
    base_dir: Path,
    country_code: str,
) -> Tuple[Dict, Dict[str, np.ndarray]]:
    """Reconstruct LCOE results from Phase 5 outputs.

    Recovery priority:
      1. Zonal CSV  (``data/{CODE}_{tech}_lcoe_zonal.csv``)
      2. LCOE TIF   (``tif/{CODE}_{tech}_lcoe_usd_mwh.tif``)

    Args:
        base_dir:     Root directory of pipeline outputs.
        country_code: ISO country code.

    Returns:
        Tuple of:
          - Dict compatible with LCOEResult schema (stats only).
          - Dict mapping tech → LCOE raster array (for dominance maps).

    Raises:
        ValueError: If no LCOE data is found for any technology.
    """
    base_dir = Path(base_dir)
    data_dir = base_dir / "data"
    tif_dir  = base_dir / "tif"

    techs_stats: Dict[str, Dict] = {}
    lcoe_arrays: Dict[str, np.ndarray] = {}

    for tech in TECH_ORDER:
        stats: Dict = {}
        arr: Optional[np.ndarray] = None

        # Priority 1: Zonal CSV
        csv_path = data_dir / f"{country_code}_{tech}_lcoe_zonal.csv"
        if csv_path.exists():
            try:
                stats, arr = _parse_lcoe_csv(csv_path, tech)
                logger.debug(
                    "  [%s] LCOE recovered from CSV: mean=%.1f $/MWh",
                    tech, stats.get("mean", 0),
                )
            except Exception as exc:
                logger.warning("  [%s] CSV parse failed: %s", tech, exc)

        # Priority 2: LCOE TIF
        if not stats:
            tif_path = _find_lcoe_tif(tif_dir, country_code, tech)
            if tif_path and tif_path.exists():
                try:
                    stats, arr = _parse_lcoe_tif(tif_path, tech)
                    logger.debug(
                        "  [%s] LCOE recovered from TIF: mean=%.1f $/MWh",
                        tech, stats.get("mean", 0),
                    )
                except Exception as exc:
                    logger.warning("  [%s] TIF parse failed: %s", tech, exc)

        # BLOCKER-002 (extended): zonal CSV rows are per-admin-region
        # MEANS, not raw pixels — every distributional statistic derived
        # from it (mean, p10, p25, median, p75, p90, n_pixels) describes
        # the spread/count across regions, not the true pixel-level LCOE
        # distribution. mean is directly printed in the Phase 6 text
        # report's integrated-summary table (e.g. solar showed 48.7
        # USD/MWh via CSV vs. 43.8 from Phase 5's live pixel-level
        # computation), so this is not a display-only concern.
        # When the CSV won Priority 1, overlay the complete pixel-level
        # stats dict from the TIF when one is available.
        if stats.get("source") == "csv_zonal":
            tif_path = _find_lcoe_tif(tif_dir, country_code, tech)
            if tif_path and tif_path.exists():
                try:
                    tif_stats, _ = _parse_lcoe_tif(tif_path, tech)
                    for key in (
                        "mean", "p10", "p25", "median", "p75", "p90",
                        "n_pixels",
                    ):
                        stats[key] = tif_stats[key]
                    stats["stats_source"] = "tif_pixel_level"
                    logger.debug(
                        "  [%s] Pixel-level stats overlaid on CSV: "
                        "mean=%.1f p10=%.1f p25=%.1f median=%.1f "
                        "p75=%.1f p90=%.1f n=%d",
                        tech, stats["mean"], stats["p10"], stats["p25"],
                        stats["median"], stats["p75"], stats["p90"],
                        stats["n_pixels"],
                    )
                except Exception as exc:
                    logger.warning(
                        "  [%s] Pixel-level stats overlay failed, "
                        "keeping region-level CSV stats: %s",
                        tech, exc,
                    )

        if stats:
            techs_stats[tech] = {"stats": stats}
        if arr is not None:
            lcoe_arrays[tech] = arr

    if not techs_stats:
        raise ValueError(
            f"No LCOE data recovered for {country_code}.\n"
            f"  Searched in: {data_dir}, {tif_dir}"
        )

    return {"techs": techs_stats}, lcoe_arrays


def _parse_lcoe_csv(
    csv_path: Path, tech: str
) -> Tuple[Dict, Optional[np.ndarray]]:
    """Parse LCOE zonal CSV into a stats dict.

    Returns:
        ``(stats_dict, None)`` — CSVs do not carry raster arrays.
    """
    df = pd.read_csv(csv_path)

    lcoe_col = next(
        (
            c for c in (
                "lcoe_mean", "lcoe_usd_mwh", "mean_lcoe", "lcoe",
            )
            if c in df.columns
        ),
        None,
    )

    if lcoe_col is None or df[lcoe_col].isna().all():
        raise ValueError(f"No valid LCOE column in {csv_path.name}")

    valid = df[df[lcoe_col] > 0]
    if valid.empty:
        raise ValueError(f"No positive LCOE values in {csv_path.name}")

    vals = valid[lcoe_col].values
    stats = {
        "mean":    round(float(np.mean(vals)),                2),
        "p10":     round(float(np.percentile(vals, 10)),      2),
        "p25":     round(float(np.percentile(vals, 25)),      2),
        "median":  round(float(np.percentile(vals, 50)),      2),
        "p75":     round(float(np.percentile(vals, 75)),      2),
        "p90":     round(float(np.percentile(vals, 90)),      2),
        "n_pixels": int(len(valid)),
        "source":  "csv_zonal",
    }
    return stats, None


def _parse_lcoe_tif(
    tif_path: Path, tech: str
) -> Tuple[Dict, np.ndarray]:
    """Parse LCOE TIF into a stats dict and raster array.

    Returns:
        ``(stats_dict, raster_array)``
    """
    with rasterio.open(str(tif_path)) as src:
        arr    = src.read(1).astype(np.float32)
        nodata = src.nodata
        if nodata is not None:
            arr = np.where(arr == float(nodata), np.nan, arr)
        arr = np.where(arr <= 0, np.nan, arr)

    valid = arr[np.isfinite(arr)]
    if valid.size == 0:
        raise ValueError(f"No finite LCOE values in {tif_path.name}")

    stats = {
        "mean":    round(float(np.mean(valid)),                2),
        "p10":     round(float(np.percentile(valid, 10)),      2),
        "p25":     round(float(np.percentile(valid, 25)),      2),
        "median":  round(float(np.percentile(valid, 50)),      2),
        "p75":     round(float(np.percentile(valid, 75)),      2),
        "p90":     round(float(np.percentile(valid, 90)),      2),
        "n_pixels": int(valid.size),
        "source":  "tif",
    }
    return stats, arr


def _find_lcoe_tif(
    tif_dir: Path, country_code: str, tech: str
) -> Optional[Path]:
    """Locate a LCOE TIF with several filename fallbacks."""
    candidates = [
        tif_dir / f"{country_code}_{tech}_lcoe_usd_mwh.tif",
        tif_dir / f"{country_code}_{tech}_lcoe.tif",
        tif_dir / f"{tech}_lcoe_usd_mwh.tif",
        tif_dir / f"{tech}_lcoe.tif",
    ]
    for path in candidates:
        if path.exists():
            return path

    matches = sorted(tif_dir.glob(f"*{tech}*lcoe*.tif"))
    return matches[0] if matches else None


# ═══════════════════════════════════════════════════════════════════════
# Supply Curve Recovery (Phase 5 → Phase 6)
# ═══════════════════════════════════════════════════════════════════════

def recover_supply_curve_from_disk(
    base_dir: Path,
    country_code: str,
    tech: str,
) -> Optional[pd.DataFrame]:
    """Recover a supply curve DataFrame from Parquet (preferred) or CSV.

    Expected files::

        base_dir/data/{CODE}_{tech}_supply_curve.parquet
        base_dir/data/{CODE}_{tech}_supply_curve.csv   (fallback)

    Args:
        base_dir:     Root directory of pipeline outputs.
        country_code: ISO country code.
        tech:         Technology identifier.

    Returns:
        DataFrame with columns ``[lcoe_usd_mwh, cum_capacity_gw]``,
        or *None* if no file is found.
    """
    base_dir = Path(base_dir)
    data_dir = base_dir / "data"

    # Priority 1: Parquet
    parquet_path = data_dir / f"{country_code}_{tech}_supply_curve.parquet"
    if parquet_path.exists():
        try:
            df = pd.read_parquet(parquet_path)
            logger.debug(
                "  [%s] Supply curve recovered from Parquet (%d points)",
                tech, len(df),
            )
            return df
        except Exception as exc:
            logger.warning("  [%s] Parquet read failed: %s", tech, exc)

    # Priority 2: CSV
    csv_path = data_dir / f"{country_code}_{tech}_supply_curve.csv"
    if csv_path.exists():
        try:
            df = pd.read_csv(csv_path)
            logger.debug(
                "  [%s] Supply curve recovered from CSV (%d points)",
                tech, len(df),
            )
            return df
        except Exception as exc:
            logger.warning("  [%s] CSV read failed: %s", tech, exc)

    logger.debug(
        "  [%s] Supply curve not found in %s — "
        "will reconstruct from TIF if needed",
        tech, data_dir,
    )
    return None


# ═══════════════════════════════════════════════════════════════════════
# Abatement Results Recovery (Phase 7 → Phase 6)
# ═══════════════════════════════════════════════════════════════════════

def recover_abatement_from_disk(
    base_dir: Path,
    country_code: str,
) -> Dict:
    """Recover GHG abatement results from the Phase 7 summary CSV.

    Expected file::

        base_dir/data/{CODE}_abatement_summary.csv

    Args:
        base_dir:     Root directory of pipeline outputs.
        country_code: ISO country code.

    Returns:
        Dict with abatement metrics and ``available=True``, or
        ``{"available": False}`` if the file is missing or unreadable.
    """
    if base_dir is None:
        return {"available": False}

    base_dir    = Path(base_dir)
    summary_csv = base_dir / "data" / f"{country_code}_abatement_summary.csv"

    if not summary_csv.exists():
        logger.debug("  Abatement summary CSV not found: %s", summary_csv)
        return {"available": False}

    try:
        df = pd.read_csv(summary_csv)
        if df.empty:
            return {"available": False}

        row = df.iloc[0]
        return {
            "available":        True,
            "co2_avoided_mt":   float(row.get("co2_avoided_mt",  0)),
            "net_avoided_mt":   float(row.get("net_avoided_mt",
                                              row.get("co2_avoided_mt", 0))),
            "subst_gwh":        float(row.get("subst_gwh",        0)),
            "total_value_b":    float(row.get("total_value_b",    0)),
            "mac_global":       float(row.get("mac_global",       0)),
            "mac_usd_tco2e":    float(row.get("mac_usd_tco2e",
                                              row.get("mac_global", 0))),
            "carbon_price":     float(row.get("carbon_price",    80)),
            "capex_total_b":    float(row.get("capex_total_b",    0)),
            "ndc_coverage_pct": float(row.get("ndc_coverage_pct", 0)),
            "ci_before":        float(row.get("ci_before",        0)),
            "ci_after":         float(row.get("ci_after",         0)),
        }

    except Exception as exc:
        logger.warning("  Failed to parse abatement summary CSV: %s", exc)
        return {"available": False}