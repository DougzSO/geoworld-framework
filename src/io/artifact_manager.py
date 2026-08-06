"""
src/io/artifact_manager.py
==========================
Centralized management of pipeline artefacts on disk.
Handles manifest files, serialization/deserialization of results,
and consistent path resolution.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Type

from src.core.schemas import PipelineState

logger = logging.getLogger("geoworld.io.ArtifactManager")


class ArtifactManager:
    """Manages reading and writing of pipeline artefacts with integrity checks."""

    def __init__(self, outputs_dir: Path, country_code: str):
        self.outputs_dir = Path(outputs_dir)
        self.country_code = country_code
        self.base_dir = self.outputs_dir / country_code
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = self.base_dir / "pipeline_state.json"

    # ─── State Management ────────────────────────────────────────────────

    def load_state(self) -> PipelineState:
        """Load pipeline state from disk, or create new if missing."""
        if self._state_file.exists():
            try:
                with open(self._state_file, "r") as f:
                    data = json.load(f)
                return PipelineState(**data)
            except Exception as e:
                logger.warning("Corrupted state file, recreating: %s", e)
        return PipelineState(
            country_code=self.country_code,
            last_updated=datetime.now().isoformat(),
        )

    def save_state(self, state: PipelineState) -> None:
        """Atomically write pipeline state to disk."""
        state.last_updated = datetime.now().isoformat()
        # Use atomic write: write to temp, then rename
        tmp = self._state_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(state.model_dump(), f, indent=2, default=str)
        shutil.move(str(tmp), str(self._state_file))

    def get_phase_state(self, phase_name: str) -> Optional[Dict[str, Any]]:
        """Return state of a specific phase, or None."""
        state = self.load_state()
        return state.phases.get(phase_name)

    def set_phase_state(
        self,
        phase_name: str,
        result_path: Optional[Path] = None,
        manifest_path: Optional[Path] = None,
        skip: bool = False,
        extra: Optional[Dict] = None,
    ) -> None:
        """Update state for a phase and persist."""
        state = self.load_state()
        entry = {
            "timestamp": datetime.now().isoformat(),
            "skip": skip,
        }
        if result_path:
            entry["result_path"] = str(result_path)
        if manifest_path:
            entry["manifest_path"] = str(manifest_path)
        if extra:
            entry.update(extra)
        state.phases[phase_name] = entry
        self.save_state(state)

    # ─── Manifest Handling ──────────────────────────────────────────────

    def save_manifest(
    self,
    phase_dir: Path,
    phase_name: str,
    files: Optional[Dict[str, str]] = None,
    parameters: Optional[Dict] = None,
    checksums: Optional[Dict[str, str]] = None,
    ) -> Path:
        """Generate a manifest.json for a phase output directory."""
        phase_dir = Path(phase_dir)
        phase_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "phase": phase_name,
            "country": self.country_code,
            "timestamp": datetime.now().isoformat(),
            "files": files or {},
            "parameters": parameters or {},
            "checksums": checksums or {},
        }

        # ✅ CORREÇÃO: Proteger contra files=None antes do loop
        if not checksums and files:
            for pattern, path in files.items():
                full_path = phase_dir / path
                if full_path.exists():
                    manifest["checksums"][path] = self._compute_hash(full_path)

        manifest_path = phase_dir / "manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2, default=str)
        return manifest_path

    def load_manifest(self, phase_dir: Path) -> Dict[str, Any]:
        """Load manifest.json from a phase directory."""
        manifest_path = phase_dir / "manifest.json"
        if not manifest_path.exists():
            return {}
        with open(manifest_path, "r") as f:
            return json.load(f)

    # ─── Result Serialization ────────────────────────────────────────────

    def save_result(
        self,
        phase_dir: Path,
        result_obj: Any,
        serializer: str = "pickle",
    ) -> Path:
        """
        Serialize a result object (e.g., dict or dataclass) to disk.

        Supports pickle (fast) or JSON (human-readable, but limited).
        """
        phase_dir = Path(phase_dir)
        phase_dir.mkdir(parents=True, exist_ok=True)
        if serializer == "pickle":
            path = phase_dir / "result.pkl"
            with open(path, "wb") as f:
                pickle.dump(result_obj, f, protocol=pickle.HIGHEST_PROTOCOL)
        elif serializer == "json":
            path = phase_dir / "result.json"
            with open(path, "w") as f:
                json.dump(result_obj, f, default=str, indent=2)
        else:
            raise ValueError(f"Unsupported serializer: {serializer}")
        return path

    def load_result(
    self,
    phase_dir: Path,
    model_class: Optional[Type] = None,
    ) -> Any:
        """
        Load a phase result from disk.
        
        If model_class is provided, validates that the loaded object matches
        the expected type. No conversion is attempted — if the cached object
        doesn't match the expected model, it is discarded and None is returned.
        """
        result_path = phase_dir / "result.pkl"
        if not result_path.exists():
            return None

        try:
            with open(result_path, "rb") as f:
                obj = pickle.load(f)
        except Exception as exc:
            logger.warning(f"Failed to load {phase_dir}: {exc}")
            return None

        # ✅ CORREÇÃO: apenas verificar tipo, não converter
        if model_class is not None:
            if isinstance(obj, model_class):
                # ✅ Objeto já é do tipo esperado — retornar como está
                return obj
            else:
                # ✅ Tipo incompatível — retornar None (força re-execução)
                logger.warning(
                    "  Cached result type mismatch: expected %s but got %s. "
                    "Cache invalidated — re-running phase.",
                    model_class.__name__,
                    type(obj).__name__,
                )
                return None

        return obj

    # ─── Path Resolution ────────────────────────────────────────────────

    def phase_dir(self, phase_name: str) -> Path:
        """Return the directory for a given phase."""
        # Mapeamento de nomes de fase para nomes de diretório (padronização)
        mapping = {
            "audit": "audit",
            "align": "processed",  # especial, fica em data/processed/
            "criteria": "criteria_builder",
            "suitability": "suitability",
            "potential": "potential",
            "lcoe": "lcoe",
            "results": "results",
            "abatement": "abatement",
            "sensitivity": "sensitivity",
            "transport": "transport",
        }
        dir_name = mapping.get(phase_name, phase_name)
        return self.base_dir / dir_name

    def _compute_hash(self, file_path: Path, algo: str = "sha256") -> str:
        """Compute file hash for integrity checks."""
        h = hashlib.new(algo)
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    # ✅ ITEM 6: Melhorar mensagem de erro em validate_result_object
    def validate_result_object(
        self,
        result_obj: Any,
        model_class: Type,
        phase_name: str,
    ) -> None:
        """
        Validate that a result object matches its expected Pydantic model.
        
        Args:
            result_obj: Object to validate
            model_class: Expected Pydantic model class
            phase_name: Name of the phase (for error messages)
            
        Raises:
            TypeError: If validation fails
        """
        if not isinstance(result_obj, model_class):
            # ✅ MUDANÇA: Mensagem de erro mais descritiva
            raise TypeError(
                f"[{phase_name}] Result validation failed:\n"
                f"  Expected: {model_class.__name__}\n"
                f"  Received: {type(result_obj).__name__}\n"
                f"  Hint: Ensure the phase returns {model_class.__name__}(**data) "
                f"or a compatible dict structure."
            )