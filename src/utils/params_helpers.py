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