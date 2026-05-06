"""
src/io/data_orchestrator.py
===========================
Orchestrates raw data acquisition for the GeoWorld Framework.

Coordinates sequential and parallel downloads, then returns a
normalised status dict consumed by ``main.py`` and all downstream
processors.

Status dict contract
--------------------
Keys follow a stable naming scheme. Each dataset maps to either
a ``Path``, a ``List[Path]``, a ``pd.DataFrame``, or ``None``:

    Borders        : Path | None
    Land Cover     : List[Path]
    Elevation_Path : Path | None
    Slope_Path     : Path | None
    Solar          : Path | None
    Wind           : List[Path]
    Plants         : pd.DataFrame | None
    Population     : Path | None
    Roads          : Path | None
    Protected      : Path | None
    Lakes          : Path | None
    Rivers         : Path | None
    Seismic        : Path | None
    Grid           : Path | None
    Admin1         : Path | None
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict

import geopandas as gpd

from src.core.config_loader import ConfigLoader
from src.io.data_fetcher import DataFetcher
from src.io.data_manager import DataManager

logger = logging.getLogger("geoworld.io.DataOrchestrator")


class DataOrchestrator:
    """
    Manages discovery, validation, and parallel acquisition of raw inputs.

    Args:
        dm: File-discovery layer
        fetcher: Download layer
        cfg: Framework configuration
        ext_logger: External logger instance (optional)
    """

    def __init__(
        self,
        dm: DataManager,
        fetcher: DataFetcher,
        cfg: ConfigLoader,
        ext_logger: logging.Logger | None = None,
    ):
        """
        Initialize the data orchestrator.

        Args:
            dm: DataManager instance for file discovery
            fetcher: DataFetcher instance for downloading
            cfg: ConfigLoader instance
            ext_logger: Optional external logger; uses module logger if None
        """
        self.dm = dm
        self.fetcher = fetcher
        self.cfg = cfg
        self.log = ext_logger or logger

    def acquire_all(
        self,
        name: str,
        code: str,
        mainland_gdf: gpd.GeoDataFrame,
    ) -> Dict[str, Any]:
        """
        Acquire all datasets, downloading anything that is missing.

        Performs disk discovery first, then downloads missing datasets.
        Land cover and elevation are handled sequentially due to size
        and authentication requirements. Ancillary datasets are
        downloaded in parallel.

        Args:
            name: Full country name
            code: ISO-3166-alpha-3 country code
            mainland_gdf: Mainland geometry GeoDataFrame

        Returns:
            Normalised status dict (see module docstring for key contract)
        """
        raw = self.dm.check_country_availability(name, code)

        status: Dict[str, Any] = {
            "Borders": raw.get("Borders"),
            "Land Cover": raw.get("Land Cover", []),
            "Elevation_Path": raw.get("Elevation"),
            "Slope_Path": raw.get("Slope"),
            "Solar": raw.get("Solar"),
            "Wind": raw.get("Wind", []),
            "Plants": None,
            "Population": raw.get("Population"),
            "Roads": raw.get("Roads"),
            "Protected": raw.get("Protected"),
            "Lakes": raw.get("Lakes"),
            "Rivers": raw.get("Rivers"),
            "Seismic": raw.get("Seismic"),
            "Grid": raw.get("Grid"),
            "Admin1": raw.get("Admin1"),
        }

        # Land cover — sequential due to authentication token sensitivity
        if not status["Land Cover"]:
            self.log.warning(
                "Land Cover missing. Downloading via Terrascope..."
            )
            try:
                try:
                    geom_union = mainland_gdf.geometry.union_all()
                except AttributeError:
                    geom_union = mainland_gdf.geometry.unary_union
                ok = self.fetcher.download_land_cover(
                    country_name=name,
                    country_geom=geom_union,
                    credentials=self.cfg.credentials,
                    mainland_geom=geom_union,
                )
                if ok:
                    status["Land Cover"] = self.dm._find_land_cover(
                        name, code
                    )
                else:
                    self.log.error(
                        "Land Cover download returned no tiles."
                    )
            except Exception as e:
                self.log.error(
                    "Land Cover acquisition failed: %s", e, exc_info=True
                )
        else:
            self.log.info(
                "Land Cover: %d tile(s) found.",
                len(status["Land Cover"]),
            )

        # Elevation — sequential due to large multi-block download
        if (
            not status["Elevation_Path"]
            or not status["Elevation_Path"].exists()
        ):
            self.log.warning("Elevation missing. Downloading DEM...")
            status["Elevation_Path"] = self.fetcher.download_elevation(
                name, code, mainland_gdf,
            )

        # Ancillary data — parallel where possible
        self._acquire_ancillary(name, code, mainland_gdf, status)

        # Power plants — loaded as DataFrame, not path
        status["Plants"] = self.dm.load_power_plants(name, code)

        return status

    def _acquire_ancillary(
        self,
        name: str,
        code: str,
        mainland_gdf: gpd.GeoDataFrame,
        status: Dict[str, Any],
    ) -> None:
        """
        Download population, grid, and roads in parallel if missing.

        Modifies status dict in-place with downloaded dataset paths.

        Args:
            name: Full country name
            code: ISO-3166-alpha-3 country code
            mainland_gdf: Mainland geometry GeoDataFrame
            status: Status dict to update in-place
        """
        tasks: Dict[str, tuple] = {}

        if not status["Population"]:
            tasks["Population"] = (
                self.fetcher.download_worldpop,
                [code],
            )

        if not status["Grid"]:
            tasks["Grid"] = (
                self.fetcher.download_osm_grid,
                [name, code, mainland_gdf],
            )

        if not status["Roads"]:
            tasks["Roads"] = (
                self.fetcher.download_osm_roads,
                [name, code, mainland_gdf],
            )

        if not tasks:
            return

        self.log.info(
            "Downloading %d ancillary dataset(s) in parallel...",
            len(tasks),
        )

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(func, *args): layer
                for layer, (func, args) in tasks.items()
            }
            for future in as_completed(futures):
                layer = futures[future]
                try:
                    result = future.result()
                    status[layer] = result
                    self.log.info(
                        "  %s: downloaded → %s",
                        layer,
                        getattr(result, "name", result),
                    )
                except Exception as e:
                    self.log.error(
                        "%s download failed: %s", layer, e
                    )
                    status[layer] = None