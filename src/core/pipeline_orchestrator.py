"""
src/core/pipeline_orchestrator.py
==================================
Orchestrates pipeline phase execution with skip logic, input/output
validation, and artifact persistence.

Each phase follows the same contract:
  1. **If skip flag is set AND cache exists**: return cached result.
  2. **If skip flag is set AND no cache exists**: DO NOT EXECUTE — return None.
  3. If skip flag is False: execute the phase.
  4. Call input_getter() to assemble phase inputs.
  5. Optionally validate aligned layers (phases 2b and 3).
  6. Instantiate the phase class and call .run(**inputs).
  7. Optionally validate the result against an output model.
  8. Persist the result and update pipeline state.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type

import rasterio

from src.core.config_loader import ConfigLoader
from src.core.schemas import AlignedLayers
from src.core.validators import validate_aligned_layers, validate_result_object
from src.io.artifact_manager import ArtifactManager
from src.utils.logging_utils import set_logging_context
from src.utils.utils import collect_directory_files  

logger = logging.getLogger("geoworld.core.PipelineOrchestrator")

# Required aligned layers per phase (checked before execution).
_PHASE_REQUIRED_LAYERS: Dict[str, List[str]] = {
    "criteria":    ["elevation", "slope", "solar"],
    "suitability": ["slope", "land_cover"],
}


class PipelineOrchestrator:
    """
    Executes pipeline phases with consistent validation and state tracking.

    Attributes
    ----------
    cfg          ConfigLoader instance shared across all phases.
    outputs_dir  Root output directory for the current run.
    country_code ISO-3 code for the country being processed.
    country_name Full country name.
    skip         Dict of phase-name → bool skip flags.
    """

    def __init__(
        self,
        config_loader: ConfigLoader,
        outputs_dir: Path,
        country_code: str,
        country_name: str,
        skip_flags: Dict[str, bool],
    ) -> None:
        self.cfg          = config_loader
        self.outputs_dir  = Path(outputs_dir)
        self.country_code = country_code
        self.country_name = country_name
        self.skip         = skip_flags

        self.artifact_manager = ArtifactManager(outputs_dir, country_code)
        self.state = self.artifact_manager.load_state()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def is_skipped(self, phase_name: str) -> bool:
        """
        Check if a phase should be skipped based on the skip flags.

        Parameters
        ----------
        phase_name : str
            Phase identifier (e.g. "audit", "criteria").

        Returns
        -------
        bool
            True if the phase should be skipped, False otherwise.
        """
        return self.skip.get(phase_name, False)

    def run_phase(
        self,
        phase_name: str,
        phase_class: Any,
        input_getter: Callable[[], Dict[str, Any]],
        output_model: Optional[Type] = None,
        force_run: bool = False,
    ) -> Any:
        """
        Execute a single pipeline phase with strict skip semantics.

        **Skip Semantics (IMM_07)**
        --------------------------
        1. If ``skip=True`` and cache exists: return cached result.
        2. If ``skip=True`` and no cache: return None WITHOUT executing.
        3. If ``skip=False``: execute the phase (cache only used to avoid
           re-computation, but execution proceeds regardless).

        Parameters
        ----------
        phase_name    Short identifier matching skip flags and artifact paths
                      (e.g. ``"criteria"``, ``"suitability"``).
        phase_class   Class with signature ``__init__(cfg, outputs_dir)``
                      and a ``.run(**kwargs)`` method.
        input_getter  Zero-argument callable that assembles inputs lazily.
                      Not called when a cached result is loaded.
        output_model  Optional Pydantic model for result validation.
        force_run     Ignore skip flag and always execute when True.

        Returns
        -------
        Any
            Phase result, validated against output_model when provided,
            or None if the phase is skipped with no cached result.
        """
        set_logging_context(country=self.country_code, phase=phase_name)

        # ── 1. Check skip flag ───────────────────────────────────────────
        # IMM_07: Respect skip flag even if cache is invalid
        if not force_run and self.skip.get(phase_name, False):
            cached = self._try_load_cached(phase_name, output_model)
            if cached is not None:
                logger.info("  [SKIP] %s: using cached result.", phase_name)
                return cached
            else:
                logger.info(
                    "  [SKIP] %s: skip flag set and no valid cache — "
                    "phase not executed.",
                    phase_name,
                )
                return None

        # ── 2. Assemble inputs ───────────────────────────────────────────
        inputs = input_getter()

        # ── 3. Validate aligned layers when required ─────────────────────
        if phase_name in _PHASE_REQUIRED_LAYERS and "aligned" in inputs:
            inputs = self._validate_and_inject_aligned(phase_name, inputs)

        # ── 4. Execute phase ─────────────────────────────────────────────
        try:
            result = phase_class(self.cfg, self.outputs_dir).run(**inputs)
        except Exception as exc:
            logger.error(
                "Phase %s failed: %s", phase_name, exc, exc_info=True
            )
            raise

        # ── 5. Validate output ───────────────────────────────────────────
        if output_model is not None:
            result = validate_result_object(result, output_model)

        # ── 6. Persist and update state ──────────────────────────────────
        self._persist(phase_name, result)

        return result

    def run_phase_inline(
        self,
        phase_name: str,
        execute_fn: Callable[[], Any],
        force_run: bool = False,
    ) -> Any:
        """
        Execute a phase without instantiating a separate class.

        Used for lightweight phases (e.g. audit, align) that don't require
        complex state management or multi-step orchestration.

        Parameters
        ----------
        phase_name  Phase identifier (e.g. "audit").
        execute_fn  Zero-argument callable that performs the phase logic
                    and returns the result.
        force_run   Ignore skip flag and always execute when True.

        Returns
        -------
        Any
            Phase result (not validated — caller is responsible).
        """
        set_logging_context(country=self.country_code, phase=phase_name)

        if not force_run and self.skip.get(phase_name, False):
            logger.info("  [SKIP] %s: skip flag set.", phase_name)
            return None

        logger.info("  [RUN]  %s", phase_name)
        try:
            result = execute_fn()
        except Exception as exc:
            logger.error(
                "Phase %s failed: %s", phase_name, exc, exc_info=True
            )
            raise

        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _try_load_cached(
        self,
        phase_name: str,
        output_model: Optional[Type],
    ) -> Any:
        """
        Attempt to load a previously serialized phase result from disk.

        Returns the deserialized result or None if unavailable or invalid.
        """
        phase_state = self.artifact_manager.get_phase_state(phase_name)
        if not phase_state or not phase_state.get("result_path"):
            return None

        result_path = Path(phase_state["result_path"])
        if not result_path.exists():
            logger.warning(
                "  Cached result path missing (%s).",
                result_path,
            )
            return None

        return self.artifact_manager.load_result(
            result_path.parent,
            model_class=output_model,
        )

    def _validate_and_inject_aligned(
        self,
        phase_name: str,
        inputs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Validate aligned layers and return a new inputs dict containing
        the validated AlignedLayers object.

        The reference grid (shape and CRS) is inferred from the first
        available raster, so no separate reference file is required.

        Raises
        ------
        RuntimeError
            If no aligned raster can be found on disk.
        ValidationError
            If any required layer fails shape/CRS checks.
        """
        aligned = inputs["aligned"]
        required = _PHASE_REQUIRED_LAYERS[phase_name]

        # Normalise to plain dict for path iteration
        if isinstance(aligned, AlignedLayers):
            aligned_dict = aligned.to_dict()
        elif hasattr(aligned, "model_dump"):   # Pydantic v2 generic
            aligned_dict = aligned.model_dump()
        elif hasattr(aligned, "dict"):          # Pydantic v1 fallback
            aligned_dict = aligned.dict()
        else:
            aligned_dict = dict(aligned)

        # Infer reference grid from first available raster
        ref_path = next(
            (p for p in aligned_dict.values() if p and Path(p).exists()),
            None,
        )
        if ref_path is None:
            raise RuntimeError(
                f"Phase {phase_name}: no aligned raster found on disk to "
                "determine reference grid shape / CRS."
            )

        with rasterio.open(ref_path) as src:
            expected_shape = (src.height, src.width)
            expected_crs   = str(src.crs)

        validated = validate_aligned_layers(
            aligned_dict,
            required_layers=required,
            expected_shape=expected_shape,
            expected_crs=expected_crs,
        )

        return {**inputs, "aligned": validated}

    def _persist(self, phase_name: str, result: Any) -> None:
        """Serialize result to disk and update the pipeline state manifest."""
        phase_dir = self.artifact_manager.phase_dir(phase_name)

        result_path = self.artifact_manager.save_result(
            phase_dir, result, serializer="pickle"
        )
        manifest_path = self.artifact_manager.save_manifest(
            phase_dir,
            phase_name,
            files=self._collect_output_files(phase_dir),
            parameters={"country": self.country_code, "phase": phase_name},
        )
        self.artifact_manager.set_phase_state(
            phase_name,
            result_path=result_path,
            manifest_path=manifest_path,
            skip=False,
        )

    @staticmethod
    def _collect_output_files(phase_dir: Path) -> Dict[str, str]:
        """Return relative→absolute path mapping for all files in phase_dir."""
        return collect_directory_files(phase_dir)