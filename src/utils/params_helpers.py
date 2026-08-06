"""
src/utils/params_helpers.py
===========================
Type-safe parameters conversion and duck-typing utilities.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger("geoworld.utils.params_helpers")


def extract_params_dict(country_params: Any) -> Dict[str, Any]:
    """
    Safely resolves any country parameters instance down to a flat python dict.

    Compatible with:
      - Pydantic v2 CountryParams model (via model_dump)
      - Standard dictionary structures
      - None values (returns empty dict)
    """
    if country_params is None:
        return {}
    if hasattr(country_params, "model_dump"):
        return country_params.model_dump()
    if isinstance(country_params, dict):
        return country_params
    return {}


def get_scenario_data(
    potential_results: Dict[str, Any], tech: str, scenario: str
) -> Dict[str, Any]:
    """
    Extract the scenario dict for (tech, scenario) from a Phase 4
    potential_results dict, regardless of shape.

    Primary path: {"techs": {tech: {"scenarios": {scenario: {...}}}}}
    Legacy path:  {tech: {"scenarios": {scenario: {...}}}}

    Returns {} if not found at either path. Accepts a plain dict only —
    callers holding a PotentialResult Pydantic model should index its
    .techs attribute directly, or pass model.model_dump().
    """
    sc = (
        potential_results
        .get("techs", {})
        .get(tech, {})
        .get("scenarios", {})
        .get(scenario, {})
    )
    if sc:
        return sc
    return (
        potential_results
        .get(tech, {})
        .get("scenarios", {})
        .get(scenario, {})
    ) or {}