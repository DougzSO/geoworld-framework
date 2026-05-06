"""
logging_utils.py — Centralized logging configuration for the GeoWorld Framework.
==================================================================================
Provides:
  - ContextFilter        : Injects country/phase into all LogRecords via ContextVar.
  - StructuredLogHandler : Writes JSON Lines (.jsonl) for automated analysis.
  - GDALWarningFilter    : Suppresses benign GDAL warnings passed through rasterio.
  - gdal_quiet()         : Context manager for surgical GDAL suppression (8.2.2).
  - setup_logging()      : Configures the root logger with 3 handlers
                           (console, .log, .jsonl).
  - set_logging_context(): Sets country/phase on the current worker.

Security 8.2.2 — Centralized GDAL env vars:
  os.environ['CPL_LOG'] = ... in individual modules affects all child processes
  (subprocess, multiprocessing). The correct solution is gdal_quiet() for
  operation-scoped suppression, and a single os.environ in main.py for general use.
"""

from __future__ import annotations

import os
import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Optional

# =============================================================================
# GDAL ERROR HANDLER — surgical scope (8.2.2)
# =============================================================================

try:
    from osgeo import gdal as _gdal
    _HAS_GDAL_BINDINGS = True
except ImportError:
    _HAS_GDAL_BINDINGS = False


@contextmanager
def gdal_quiet():
    """
    Suppress GDAL/CPL output only during the ``with`` block.

    Replaces the ``os.environ['CPL_LOG'] = ...`` pattern which permanently
    affects ALL child processes (subprocess, multiprocessing).

    Thread-safety: GDAL maintains the error handler stack per thread internally.
    Without osgeo installed: safe no-op behavior (ImportError not propagated).

    Example::

        with gdal_quiet():
            reproject(source=rasterio.band(src, 1), destination=arr, ...)

        # Outside the block: previous GDAL handler automatically restored.
    """
    if _HAS_GDAL_BINDINGS:
        _gdal.PushErrorHandler("CPLQuietErrorHandler")
        try:
            yield
        finally:
            _gdal.PopErrorHandler()
    else:
        yield


# =============================================================================
# EXECUTION CONTEXT (per process — isolated in batch mode)
# =============================================================================

_COUNTRY_CTX: ContextVar[str] = ContextVar("geoworld_country", default="—")
_PHASE_CTX: ContextVar[str] = ContextVar("geoworld_phase", default="init")


def set_logging_context(country: str = "—", phase: str = "—") -> None:
    """
    Set the current execution context.

    Called at the start of each phase in main.py. Enriches ALL subsequent
    log records with country and phase without requiring changes to
    individual modules.

    Args:
        country: ISO country code or descriptive label.
        phase: Current pipeline phase name.
    """
    _COUNTRY_CTX.set(country)
    _PHASE_CTX.set(phase)


# =============================================================================
# FILTERS
# =============================================================================

class ContextFilter(logging.Filter):
    """
    Inject execution context fields into every LogRecord.

    Added to the ROOT logger, it automatically affects all child loggers
    (geoworld.processors.*, geoworld.io.*, etc.) without per-module
    configuration.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Inject country and phase context into the log record.

        Args:
            record: The log record to enrich.

        Returns:
            Always True (record is never suppressed by this filter).
        """
        record.country = _COUNTRY_CTX.get()
        record.phase = _PHASE_CTX.get()
        return True


class GDALWarningFilter(logging.Filter):
    """
    Suppress benign GDAL warning messages passed through the rasterio logger.

    Complements the os.environ setting defined in main.py.

    Suppressed patterns:
      - COG re-layout (PVOUT.tif tiling invalidated)
      - TIFFReadDirectory (verbose metadata read)
    """

    _SUPPRESS = (
        "optimizations in its layout",
        "This file used to have",
        "CPLE_AppDefined",
        "TIFFReadDirectory",
        "TIFFReadDirectoryCheckOrder",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        """
        Suppress log records matching known benign GDAL warning patterns.

        Args:
            record: The log record to evaluate.

        Returns:
            False if the record should be suppressed, True otherwise.
        """
        msg = record.getMessage()
        return not any(pat in msg for pat in self._SUPPRESS)


# =============================================================================
# HANDLERS
# =============================================================================

class StructuredLogHandler(logging.FileHandler):
    """
    Handler that writes structured logs in JSON Lines format (.jsonl).

    One record per line — compatible with jq, pandas, ELK Stack and BigQuery.

    Output format::

        {
            "ts": "2026-01-01T12:00:00",
            "level": "INFO",
            "phase": "audit",
            "country": "PRT",
            "logger": "geoworld.processors.DataAuditor",
            "message": "...",
            "duration_s": 0.6
        }
    """

    def emit(self, record: logging.LogRecord) -> None:
        """
        Format and write a log record as a JSON line.

        Args:
            record: The log record to serialize.
        """
        try:
            entry: dict = {
                "ts": datetime.fromtimestamp(record.created).strftime(
                    "%Y-%m-%dT%H:%M:%S"
                ),
                "level": record.levelname,
                "phase": getattr(record, "phase", "—"),
                "country": getattr(record, "country", "—"),
                "logger": record.name,
                "message": record.getMessage(),
            }

            for key in (
                "layer",
                "duration_s",
                "status",
                "metric",
                "value",
                "area_km2",
                "n_pixels",
            ):
                val = getattr(record, key, None)
                if val is not None:
                    entry[key] = val

            self.stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self.flush()

        except Exception:
            self.handleError(record)


# =============================================================================
# CENTRAL SETUP
# =============================================================================

def setup_logging(
    log_level: str = "INFO",
    log_dir: Optional[Path] = None,
    country_code: str = "INIT",
    pid_suffix: bool = False,
) -> logging.Logger:
    """
    Configure the root logger with up to 3 handlers.

    Handlers:
      1. Console (stdout)   — human-readable, no verbose timestamp.
      2. File (.log)        — human-readable, full timestamp (persistent).
      3. File (.jsonl)      — structured JSON Lines for automated analysis.

    Fix 5.6: pid_suffix=True appends the process PID to the log filename,
    preventing name collisions when multiple batch workers start in the
    same second.

    Note 8.2.2: os.environ['CPL_LOG'] is set ONCE in main.py as a fallback
    for libraries that do not use the GDAL error handler stack. Individual
    modules must not redefine os.environ — use gdal_quiet() for
    operation-scoped suppression.

    Args:
        log_level: Logging level string (e.g., 'INFO', 'DEBUG', 'WARNING').
        log_dir: Directory for log files. If None, only console handler is set.
        country_code: Country code used in log file names.
        pid_suffix: If True, append PID to log file names.

    Returns:
        Configured 'geoworld' logger.
    """

    level = getattr(logging, log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    ctx_filter = ContextFilter()
    gdal_filter = GDALWarningFilter()
    root.addFilter(ctx_filter)

    # ── Handler 1: Console ─────────────────────────────────────────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    console_handler.addFilter(gdal_filter)
    root.addHandler(console_handler)

    if log_dir:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        pid_part = f"_{os.getpid()}" if pid_suffix else ""

        file_fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

        # ── Handler 2: File .log ───────────────────────────────────────────
        log_path = (
            log_dir / f"geoworld_{country_code}_{ts_str}{pid_part}.log"
        )
        file_handler = logging.FileHandler(
            str(log_path),
            encoding="utf-8"
        )
        file_handler.setFormatter(file_fmt)
        file_handler.addFilter(gdal_filter)
        root.addHandler(file_handler)

        # ── Handler 3: File .jsonl ─────────────────────────────────────────
        jsonl_path = (
            log_dir / f"geoworld_{country_code}_{ts_str}{pid_part}.jsonl"
        )
        json_handler = StructuredLogHandler(
            str(jsonl_path),
            encoding="utf-8"
        )
        json_handler.addFilter(gdal_filter)
        root.addHandler(json_handler)

    # ── Suppress noisy third-party libraries ──────────────────────────────
    _NOISY_LIBS = [
        "rasterio",
        "fiona",
        "shapely",
        "matplotlib",
        "PIL",
        "urllib3",
        "requests",
        "pyproj",
        "numexpr",
        "asyncio",
        "concurrent",
    ]

    for lib in _NOISY_LIBS:
        logging.getLogger(lib).setLevel(logging.WARNING)

    return logging.getLogger("geoworld")