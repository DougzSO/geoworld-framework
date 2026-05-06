"""
src/utils/timing.py
===================
Shared timing context manager for all GeoWorld pipeline processors.

Usage::

    from src.utils.timing import timer

    with timer("my_step", timings):
        ...  # code to measure

Fix 2.2.1: eliminates ~40 lines of duplicated code that existed
independently in suitability_builder.py, criteria_builder.py,
potential_calculator.py and dominance_calculator.py.

Fix 2.2.2: single signature timer(label, timings) — eliminates the
variant without timings_dict that existed in criteria_builder.py.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Dict

logger = logging.getLogger("geoworld")


@contextmanager
def timer(label: str, timings: Dict[str, float]):
    """
    Measure the elapsed time of a block and record it in timings.

    Args:
        label: Key to insert into timings.
        timings: Shared timings dictionary (modified in-place).

    Example::

        timings: Dict[str, float] = {}
        with timer("load_criteria", timings):
            data = load_something()
        # timings == {"load_criteria": 1.23}
    """
    _log = logging.getLogger("geoworld")
    _log.info("[%s] starting...", label)
    t0 = time.perf_counter()

    try:
        yield
    finally:
        elapsed = time.perf_counter() - t0
        timings[label] = round(elapsed, 2)
        _log.info("[%s] completed in %.1fs", label, elapsed)