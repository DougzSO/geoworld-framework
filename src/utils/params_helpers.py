"""
src/utils/params_helpers.py
===========================
Type-safe parameters conversion and duck-typing utilities.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.core.schemas import CountryParams

logger = logging.getLogger("geoworld.utils.params_helpers")


def extract_params_dict(country_params: Optional[CountryParams]) -> Dict[str, Any]:
    """
    Resolves a validated CountryParams instance down to a flat python dict
    via ``model_dump()``.

    REMOVED (BLOCKER-005 validation half / QI-003 fragility note): this used
    to also accept a plain ``dict``, passing it through unvalidated. That
    bypassed CountryParams'/TechParams' Pydantic validation, so a dict-shaped
    country_params with missing or malformed fields (e.g. a tech block
    missing ``threshold``) never failed loudly. A validated ``CountryParams``
    instance (or ``None``) is now required.
    """
    if country_params is None:
        return {}
    if not isinstance(country_params, CountryParams):
        raise TypeError(
            "country_params must be a validated CountryParams instance or "
            f"None, got {type(country_params).__name__}. Build it via "
            "ConfigLoader.get_country()."
        )
    return country_params.model_dump()


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