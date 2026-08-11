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

A full ResultsWriter.run() call needs real geospatial fixtures (admin
boundaries, mainland GeoDataFrame, on-disk suitability TIFs) that this
suite does not yet have (QI-001 deferred synthetic-country/full-phase
fixtures to a future QI-002). Instead, these tests exercise the two real
components whose interaction caused the bug -- the dominance-computation
statics and a real ArtifactManager persist/load round trip -- plus a
structural guard against the manual-persistence pattern being reintroduced.
"""

import numpy as np

from src.core.constants import TECH_META, TECH_ORDER
from src.io.artifact_manager import ArtifactManager
from src.processors.results_writer import ResultsWriter


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
