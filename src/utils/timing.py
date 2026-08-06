"""
src/utils/timing.py
===================
Shared timing context manager for all GeoWorld pipeline processors.

Usage::

    from src.utils.timing import timer

    # With timings dict (suitability_builder, potential_calculator):
    with timer("my_step", timings):
        ...

    # Log-only mode (criteria_builder — no dict needed):
    with timer("my_step"):
        ...

Fix 2.2.1: eliminates ~40 lines of duplicated code that existed
independently in suitability_builder.py, criteria_builder.py,
potential_calculator.py and dominance_calculator.py.

Fix 2.2.2: timings is now optional — criteria_builder uses log-only mode.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Dict, Optional

logger = logging.getLogger("geoworld")


@contextmanager
def timer(label: str, timings: Optional[Dict[str, float]] = None):
    """
    Measure the elapsed time of a block and optionally record it in timings.

    Args:
        label:   Key to insert into timings dict and log label.
        timings: Optional shared timings dictionary (modified in-place).
                 When None, elapsed time is only logged — not persisted.

    Examples::

        # Persistent mode — used by suitability_builder, potential_calculator:
        timings: Dict[str, float] = {}
        with timer("load_criteria", timings):
            data = load_something()
        # timings == {"load_criteria": 1.23}

        # Log-only mode — used by criteria_builder (no dict needed):
        with timer("solar_resource"):
            score = compute_solar_resource(...)
    """
    _log = logging.getLogger("geoworld")
    _log.info("[%s] starting...", label)
    t0 = time.perf_counter()

    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        if timings is not None:
            timings[label] = round(elapsed, 2)
        _log.info("[%s] completed in %.1fs", label, elapsed)