"""
tests/unit/test_results_writer.py
==================================
Regression guards for BLOCKER-011 (Phase 6 double persistence).

Before the fix, ResultsWriter._persist_artifacts() manually saved a dict
containing "dominance_suitability_counts"/"dominance_lcoe_counts" via its
own ArtifactManager call, but PipelineOrchestrator.run_phase()'s automatic
persistence step ran afterward and overwrote the same result.pkl with the
plainer dict ResultsWriter.run() returned -- which never had those fields.
The bug was silent: nothing raised, the fields were just gone on disk.

Two levels of coverage:
  - Isolated: the dominance-computation statics + a real ArtifactManager
    persist/load round trip, plus a structural guard against the
    manual-persistence pattern being reintroduced.
  - Full path: ResultsWriter.run() driven by the real
    PipelineOrchestrator.run_phase() -- the actual call sequence where
    the bug occurred (run() returns a dict, run_phase()'s automatic
    _persist() saves it). Geospatial rendering and Phase 4/5 disk
    recovery (unrelated to this bug, and not yet covered by fixtures --
    QI-001 deferred synthetic-country/full-phase fixtures to a future
    QI-002) are stubbed; everything that actually touches persistence
    (raster reads, real dominance computation, GeoTIFF export, and the
    real ArtifactManager save/load the orchestrator performs) is real.
"""

import numpy as np
import rasterio
import geopandas as gpd
from rasterio.transform import Affine
from shapely.geometry import box

from src.core.constants import TECH_META, TECH_ORDER
from src.core.pipeline_orchestrator import PipelineOrchestrator
from src.io.artifact_manager import ArtifactManager
from src.processors.results_writer import ResultsWriter
from src.utils.map_styling import GeoWorldStyler
from src.visualization.dashboard_panels import DashboardPanels


class _FakeConfig:
    """
    Minimal cfg stand-in -- only the attributes ResultsWriter and
    PipelineOrchestrator actually touch (cfg.system, cfg.raw_path).
    Not a ConfigLoader subclass; duck-typing is sufficient since neither
    consumer does an isinstance check.
    """

    def __init__(self, raw_path):
        self.system = {"visualization": {}, "pipeline": {"map_dpi_export": 96}}
        self.raw_path = raw_path


def test_results_writer_has_no_manual_persistence():
    """
    Structural guard: ResultsWriter must not duplicate the orchestrator's
    automatic persistence. A phase-level ArtifactManager call whose saved
    dict differs from run()'s return value is silently overwritten by
    PipelineOrchestrator.run_phase()'s automatic _persist() call.
    """
    assert not hasattr(ResultsWriter, "_persist_artifacts")

    import src.processors.results_writer as rw_module

    assert not hasattr(rw_module, "ArtifactManager")


def test_dominance_pixel_counts_survive_persistence_round_trip(tmp_path):
    """
    Behavioral guard: dominance pixel-count fields must be part of the dict
    ResultsWriter.run() returns, so they survive the real ArtifactManager
    persist/load round trip PipelineOrchestrator.run_phase() performs
    automatically -- the exact mechanism that discarded them before the fix.

    Uses the real dominance-computation statics (not hand-typed numbers)
    on a small synthetic layout: each technology dominates one disjoint
    quadrant of suitability and has a strictly cheaper LCOE there.
    """
    H, W = 4, 4
    suit_arrays = {
        "solar":   np.array([[0.9, 0.1, 0.0, 0.0]] * H, dtype=np.float32),
        "wind":    np.array([[0.1, 0.9, 0.0, 0.0]] * H, dtype=np.float32),
        "biomass": np.array([[0.0, 0.0, 0.9, 0.0]] * H, dtype=np.float32),
    }
    # Column 3 is 0.0 (invalid, filtered out by _build_lcoe_dominance's
    # "arr > 0" check) for every tech, so it counts as "no dominant tech"
    # rather than an argmin tie resolving to whichever tech is first in
    # TECH_ORDER.
    lcoe_arrays = {
        "solar":   np.where(
            np.array([[True, False, False, False]] * H), 40.0, 0.0
        ).astype(np.float32),
        "wind":    np.where(
            np.array([[False, True, False, False]] * H), 60.0, 0.0
        ).astype(np.float32),
        "biomass": np.where(
            np.array([[False, False, True, False]] * H), 80.0, 0.0
        ).astype(np.float32),
    }

    dom_suit, _, _ = ResultsWriter._build_suitability_dominance(
        suit_arrays, H, W
    )
    dom_lcoe, _, _ = ResultsWriter._build_lcoe_dominance(lcoe_arrays, H, W)

    # Mirrors exactly the assembly ResultsWriter.run() performs at its
    # "Dominance pixel-count summary" step (BLOCKER-011).
    results = {
        "country": "PRT",
        "timestamp": "2026-01-01T00:00:00",
        "timings": {},
        "exported_tifs": [],
        "elapsed_total": 1.0,
        "dominance_suitability_counts": {
            TECH_META[tech]["label"]: int((dom_suit == i + 1).sum())
            for i, tech in enumerate(TECH_ORDER)
        },
        "dominance_lcoe_counts": {
            TECH_META[tech]["label"]: int((dom_lcoe == i + 1).sum())
            for i, tech in enumerate(TECH_ORDER)
        },
    }

    # Real persistence round trip -- the exact mechanism (ArtifactManager,
    # pickle) PipelineOrchestrator.run_phase()'s automatic _persist() uses.
    artifact_mgr = ArtifactManager(tmp_path, "PRT")
    phase_dir = artifact_mgr.phase_dir("results")
    artifact_mgr.save_result(phase_dir, results, serializer="pickle")
    loaded = artifact_mgr.load_result(phase_dir)

    assert "dominance_suitability_counts" in loaded
    assert "dominance_lcoe_counts" in loaded
    assert (
        loaded["dominance_suitability_counts"]
        == results["dominance_suitability_counts"]
    )
    assert loaded["dominance_lcoe_counts"] == results["dominance_lcoe_counts"]

    # Sanity: each tech dominates exactly one of the 4 columns x 4 rows.
    assert sum(loaded["dominance_suitability_counts"].values()) == 3 * H
    assert sum(loaded["dominance_lcoe_counts"].values()) == 3 * H


def test_results_writer_run_via_orchestrator_persists_dominance_counts(
    tmp_path, monkeypatch
):
    """
    Full-path regression guard for BLOCKER-011: exercises the actual
    sequence where the bug occurred -- ResultsWriter.run() followed by
    PipelineOrchestrator.run_phase()'s automatic persistence -- instead of
    re-testing the pieces in isolation like the two tests above.

    Admin-boundary shapefile loading, dominance-map/dashboard rendering,
    and Phase 4/5 disk recovery are stubbed (expensive/unrelated to this
    bug). Suitability/LCOE raster reads, the real dominance-computation
    statics, GeoTIFF export, and the real ArtifactManager save/load the
    orchestrator performs are all exercised for real.
    """
    country_code = "PRT"
    country_name = "Portugal"
    H, W = 4, 4
    transform = Affine(0.01, 0.0, -9.5, 0.0, -0.01, 39.5)
    crs = "EPSG:4326"

    outputs_dir = tmp_path / "outputs"
    suitability_dir = tmp_path / "suit_input"

    def _write_tif(path, arr):
        path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(
            str(path), "w", driver="GTiff", height=H, width=W, count=1,
            dtype="float32", crs=crs, transform=transform,
        ) as dst:
            dst.write(arr.astype(np.float32), 1)

    # Each tech dominates one disjoint suitability quadrant / has a
    # strictly cheaper LCOE there; column 3 is 0.0 (invalid) everywhere,
    # same layout as the isolated dominance test above.
    suit_layout = {
        "solar":   np.array([[0.9, 0.1, 0.0, 0.0]] * H),
        "wind":    np.array([[0.1, 0.9, 0.0, 0.0]] * H),
        "biomass": np.array([[0.0, 0.0, 0.9, 0.0]] * H),
    }
    for tech, arr in suit_layout.items():
        _write_tif(
            suitability_dir / f"{country_code}_{tech}_suitability.tif", arr
        )

    lcoe_layout = {
        "solar": np.where(
            np.array([[True, False, False, False]] * H), 40.0, 0.0
        ),
        "wind": np.where(
            np.array([[False, True, False, False]] * H), 60.0, 0.0
        ),
        "biomass": np.where(
            np.array([[False, False, True, False]] * H), 80.0, 0.0
        ),
    }
    for tech, arr in lcoe_layout.items():
        _write_tif(
            outputs_dir / country_code / "lcoe" / "tif"
            / f"{country_code}_{tech}_lcoe_usd_mwh.tif",
            arr,
        )

    minx, maxy = transform.c, transform.f
    maxx, miny = minx + transform.a * W, maxy + transform.e * H
    mainland_gdf = gpd.GeoDataFrame(
        {"geometry": [box(minx, miny, maxx, maxy)]}, crs=crs
    )

    # Stub Phase 4/5 disk recovery -- not what this test verifies; the
    # dead-live-object-path issue in that recovery mechanism is tracked
    # separately as BLOCKER-010 and is explicitly out of scope here.
    monkeypatch.setattr(
        ResultsWriter,
        "_normalize_potential",
        lambda self, data, country_code: {
            "techs": {
                tech: {
                    "scenarios": {
                        "balanced": {
                            "capacity_gw": 1.0,
                            "generation_twh": 2.0,
                            "area_km2": 100.0,
                        }
                    }
                }
                for tech in TECH_ORDER
            }
        },
    )
    monkeypatch.setattr(
        ResultsWriter,
        "_normalize_lcoe",
        lambda self, data, country_code: {
            "techs": {
                tech: {"stats": {"mean": 50.0, "p10": 40.0, "p90": 60.0}}
                for tech in TECH_ORDER
            }
        },
    )
    # Stub rendering -- covered separately by the pipeline's own smoke
    # tests and CHECKPOINT validations; not relevant to a persistence
    # regression, and needs real shapefile/basemap fixtures this suite
    # does not have.
    monkeypatch.setattr(
        GeoWorldStyler, "load_admin_boundaries", lambda self, *a, **kw: None
    )
    monkeypatch.setattr(
        ResultsWriter, "_plot_dominance_map", lambda self, *a, **kw: None
    )
    monkeypatch.setattr(
        DashboardPanels,
        "draw_executive_dashboard",
        lambda self, *a, **kw: None,
    )

    cfg = _FakeConfig(tmp_path)
    orchestrator = PipelineOrchestrator(
        config_loader=cfg,
        outputs_dir=outputs_dir,
        country_code=country_code,
        country_name=country_name,
        skip_flags={"results": False},
    )

    # The real path where BLOCKER-011 lived: ResultsWriter.run() returns
    # a dict, then run_phase()'s automatic _persist() saves it via
    # ArtifactManager -- previously overwriting a richer dict that
    # ResultsWriter itself had already saved manually.
    result = orchestrator.run_phase(
        phase_name="results",
        phase_class=ResultsWriter,
        input_getter=lambda: {
            "country_code": country_code,
            "country_name": country_name,
            "mainland_gdf": mainland_gdf,
            "suitability_dir": suitability_dir,
            "potential_dir": outputs_dir / country_code / "potential",
            "lcoe_dir": outputs_dir / country_code / "lcoe",
            "context_gdf": None,
            "abatement_dir": None,
        },
        output_model=None,
    )

    assert "dominance_suitability_counts" in result
    assert "dominance_lcoe_counts" in result
    assert sum(result["dominance_suitability_counts"].values()) > 0
    assert sum(result["dominance_lcoe_counts"].values()) > 0

    # The actual regression check: read back what the orchestrator
    # persisted to disk, not just what run() returned in memory.
    artifact_mgr = ArtifactManager(outputs_dir, country_code)
    phase_dir = artifact_mgr.phase_dir("results")
    persisted = artifact_mgr.load_result(phase_dir)

    assert persisted is not None
    assert "dominance_suitability_counts" in persisted
    assert "dominance_lcoe_counts" in persisted
    assert (
        persisted["dominance_suitability_counts"]
        == result["dominance_suitability_counts"]
    )
    assert (
        persisted["dominance_lcoe_counts"] == result["dominance_lcoe_counts"]
    )
