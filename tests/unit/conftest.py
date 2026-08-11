"""
tests/unit/conftest.py
========================
Shared pytest configuration for tests/unit/.

Forces the non-interactive Agg backend before any test imports matplotlib.
Every src/processors/*.py module calls matplotlib.use("Agg") itself before
importing its companion src/utils/*_plots.py module, so this is a
no-op in the real pipeline. But src/utils/*_plots.py modules do not set
the backend themselves (by design -- they assume the caller already has),
so a test that imports one directly -- as test_sensitivity_plots.py does --
hits matplotlib's default backend resolution, which can attempt the
interactive TkAgg backend and fail in a headless environment.
"""

import matplotlib

matplotlib.use("Agg")
