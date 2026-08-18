"""
scratchpad/oat_sa2_concentration_sensitivity.py
================================================
OAT sensitivity harness for SA-2's Dirichlet `concentration` parameter
(Sensitivity & Config Migration Campaign, row 1 -- sa2_monte_carlo_weights()'s
`concentration`, hardcoded at 20.0 in src/utils/sensitivity_math.py, never
wired to settings.yaml/parameters.json).

INDEPENDENT RECONSTRUCTION -- NOT A REPRODUCTION.
docs/BACKLOG.md (BLOCKER-016) cites a prototype, "scratchpad/
threshold_crossing_prototype.py", that first swept concentration in
{10, 20, 40} for BRA/solar and reported decisive_fraction ranging from
17.9% (conc=10) to 45.4% (conc=40), a ~2.5x swing. That script no longer
exists in the working tree or anywhere in git history (`git log --all` for
its filename returns nothing across all branches/commits) and could not be
recovered. This script is a fresh implementation written directly against
the current sa2_monte_carlo_weights() signature; it does not replay the
lost prototype's code or inputs. Any numeric agreement between this
script's BRA/solar results and the old 17.9%/45.4% figures is coincidental
corroboration, not a reproduction -- the two cannot be shown to share
identical inputs (same criteria rasters, same AHP weights, same RNG path)
since the original is gone. Report both outcomes (agreement or divergence)
as-is.

Standalone script -- does NOT go through main.py / PipelineOrchestrator.
Calls sa2_monte_carlo_weights() (pure function, src/utils/sensitivity_math.py)
directly, reusing already-cached Phase 2b criteria TIFs, Phase 3 AHP weight
JSONs, and Phase 4's persisted balanced-scenario THRESHOLD tag (read straight
from the suitable-pixel GeoTIFF -- recover_potential_from_disk()'s
reconstructed dict does not carry a "threshold" field, so _balanced_
threshold() would silently fall back to 0.60 instead of the real 0.75; this
script avoids that gap rather than reproducing it). No production code or
config is modified; no default in parameters.json/settings.yaml is touched.

Analysis only. No production code changed by running this script.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import rasterio

REPO_ROOT = Path(r"D:\Douglas\DOUTORADO\geoworld_framework")
sys.path.insert(0, str(REPO_ROOT))

from src.core.config_loader import ConfigLoader  # noqa: E402
from src.processors.sensitivity_analyzer import SensitivityAnalyzer  # noqa: E402
from src.utils.raster_io import load_reference_meta  # noqa: E402
from src.utils.sensitivity_math import sa2_monte_carlo_weights  # noqa: E402

CONCENTRATIONS = [10, 20, 40]
COUNTRIES = ["PRT", "BRA"]
TECHS = ["solar", "wind", "biomass"]
N_SAMPLES = 1000  # matches configs/settings.yaml's pipeline.sensitivity.n_mc_samples
SEED = 42  # matches sa2_monte_carlo_weights()'s own default


def _balanced_threshold_from_tif(code: str, tech: str) -> float:
    """Read the real balanced-scenario threshold from Phase 4's persisted
    THRESHOLD tag on the suitable-pixel GeoTIFF -- see module docstring for
    why this is used instead of recover_potential_from_disk()."""
    tif = REPO_ROOT / "outputs" / code / "potential" / "tifs" / f"{code}_{tech}_suitable_balanced.tif"
    with rasterio.open(str(tif)) as src:
        thr = src.tags().get("THRESHOLD")
    if thr is None:
        raise ValueError(f"No THRESHOLD tag found in {tif}")
    return float(thr)


def load_country_context(code: str) -> Dict[str, Any]:
    cfg = ConfigLoader(REPO_ROOT)
    outputs_dir = REPO_ROOT / "outputs"
    criteria_dir = outputs_dir / code / "criteria_builder" / "tif"

    _, _, height, width = load_reference_meta(criteria_dir, code)

    analyzer = SensitivityAnalyzer(cfg, outputs_dir)
    weights_by_tech = analyzer._load_weights_from_disk(code)

    thresholds_by_tech = {
        tech: _balanced_threshold_from_tif(code, tech) for tech in TECHS
    }

    return {
        "criteria_dir": criteria_dir,
        "height": height,
        "width": width,
        "weights_by_tech": weights_by_tech,
        "thresholds_by_tech": thresholds_by_tech,
    }


def sweep_tech(code: str, tech: str, ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    weights = ctx["weights_by_tech"].get(tech, {})
    if not weights:
        print(f"[{code}/{tech}] No AHP weights found on disk -- skipping.")
        return []

    threshold = ctx["thresholds_by_tech"][tech]
    criteria_names = list(weights.keys())

    rows = []
    for concentration in CONCENTRATIONS:
        mc = sa2_monte_carlo_weights(
            ctx["criteria_dir"], criteria_names, weights,
            ctx["height"], ctx["width"], threshold=threshold,
            n_samples=N_SAMPLES, concentration=float(concentration), seed=SEED,
        )
        rows.append({
            "country":           code,
            "tech":              tech,
            "concentration":     concentration,
            "threshold":         mc["threshold"],
            "n_apt_base":        mc["n_apt_base"],
            "decisive_fraction": mc["decisive_fraction"],
            "boundary_fraction": mc["boundary_fraction"],
            "moderate_fraction": mc["moderate_fraction"],
        })
        print(
            f"[{code}/{tech:8s}] concentration={concentration:>3d}  "
            f"thr={threshold:.2f}  n_apt_base={mc['n_apt_base']:>7,}  "
            f"decisive={mc['decisive_fraction']*100:6.1f}%  "
            f"boundary={mc['boundary_fraction']*100:6.1f}%  "
            f"moderate={mc['moderate_fraction']*100:6.1f}%"
        )
    return rows


def main() -> None:
    all_rows: List[Dict[str, Any]] = []
    print("=" * 90)
    print("SA-2 concentration OAT sweep -- independent reconstruction (see module docstring)")
    print(f"Countries: {COUNTRIES}  Techs: {TECHS}  Concentrations: {CONCENTRATIONS}")
    print("=" * 90)

    for code in COUNTRIES:
        ctx = load_country_context(code)
        for tech in TECHS:
            all_rows.extend(sweep_tech(code, tech, ctx))

    bra_solar = {
        r["concentration"]: r["decisive_fraction"]
        for r in all_rows if r["country"] == "BRA" and r["tech"] == "solar"
    }
    print("\n" + "=" * 90)
    if 10 in bra_solar and 40 in bra_solar and bra_solar[10] > 0:
        ratio = bra_solar[40] / bra_solar[10]
        print(
            f"BRA/solar decisive_fraction: conc=10 -> {bra_solar[10]*100:.1f}%  "
            f"conc=40 -> {bra_solar[40]*100:.1f}%  ratio={ratio:.2f}x  "
            f"(old, unreproducible claim was 17.9% -> 45.4%, ~2.5x)"
        )
    else:
        print("BRA/solar rows missing -- cannot compute 10-vs-40 ratio.")
    print("=" * 90)

    out_path = REPO_ROOT / "scratchpad" / "oat_sa2_concentration_sensitivity_results.json"
    out_path.write_text(json.dumps(all_rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nFull results written to {out_path}")


if __name__ == "__main__":
    main()
