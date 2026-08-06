import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

import geopandas as gpd

from src.core.config_loader import ConfigLoader
from src.io.data_fetcher import DataFetcher
from src.io.data_manager import DataManager

logger = logging.getLogger("geoworld.io.DataOrchestrator")


class DataOrchestrator:
    """
    Manages discovery, validation, and parallel acquisition of raw inputs.

    Applies intelligent, layer-specific filtering to prevent cross-country
    data contamination. Layers with country-specific folders are filtered
    by country code; global layers are used as-is.

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
        
        # ✅ LAYER-SPECIFIC FILTERING CONFIGURATION
        # Layers that have country-specific files → require filtering
        self.country_filtered_layers = {
            "Wind",          # Pattern: IND_power-density_*.tif (prefix)
            "Elevation",     # Pattern: IND_elevation.tif (prefix)
            "Slope",         # Pattern: IND_slope.tif (prefix)
            "Grid",          # Pattern: varies by country (prefix or folder)
            "Protected",     # Pattern: WDPA_*_IND_*.shp (contains)
            "Borders",       # Pattern: gadm41_IND_*.shp (contains)
        }
        
        # Layers that are GLOBAL (no country filtering needed)
        self.global_layers = {
            "Solar",         # Single PVOUT.tif for all countries
            "Lakes",         # HydroLAKES_polys_v10.shp (global)
            "Rivers",        # HydroRIVERS_v10.shp (global)
            "Seismic",       # seismic_hazard_global.tif (global)
        }
        
        # Layers with SPECIAL HANDLING (custom logic in acquire_all)
        self.special_layers = {
            "Land Cover",    # ESA tiles, already handles country filtering
            "Population",    # Pattern: ind_pop_2020.tif (country-aware naming)
            "Roads",         # Downloaded via OSM API (country-aware)
            "Admin1",        # Admin boundaries (country-aware download)
        }

    def acquire_all(
        self,
        name: str,
        code: str,
        mainland_gdf: gpd.GeoDataFrame,
    ) -> Dict[str, Any]:
        """
        Acquire all datasets with intelligent layer-specific filtering.

        Performs disk discovery first, then applies country filtering only
        to layers that require it. Downloads missing datasets sequentially
        or in parallel depending on layer characteristics.

        Args:
            name: Full country name
            code: ISO-3166-alpha-3 country code
            mainland_gdf: Mainland geometry GeoDataFrame

        Returns:
            Normalised status dict (see module docstring for key contract)
        """
        raw = self.dm.check_country_availability(name, code)

        # ✅ INTELLIGENT FILTERING: Apply filtering only where needed
        status: Dict[str, Any] = {
            "Borders": self._filter_if_needed(
                raw.get("Borders"), code, name, "Borders"
            ),
            "Land Cover": raw.get("Land Cover", []),  # ESA handles filtering internally
            "Elevation_Path": self._filter_if_needed(
                raw.get("Elevation"), code, name, "Elevation"
            ),
            "Slope_Path": self._filter_if_needed(
                raw.get("Slope"), code, name, "Slope"
            ),
            "Solar": raw.get("Solar"),  # Global layer — no filtering
            "Wind": self._filter_if_needed(
                raw.get("Wind", []), code, name, "Wind"
            ),
            "Plants": None,  # DataFrame, handled separately
            "Population": raw.get("Population"),  # Has country-aware naming (ind_pop_2020)
            "Roads": raw.get("Roads"),  # Downloaded via OSM API (country-aware)
            "Protected": self._filter_if_needed(
                raw.get("Protected"), code, name, "Protected"
            ),
            "Lakes": raw.get("Lakes"),  # Global layer — no filtering
            "Rivers": raw.get("Rivers"),  # Global layer — no filtering
            "Seismic": raw.get("Seismic"),  # Global layer — no filtering
            "Grid": self._filter_if_needed(
                raw.get("Grid"), code, name, "Grid"
            ),
            "Admin1": raw.get("Admin1"),  # Has country-aware naming (admin_IND_*)
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

    def _filter_if_needed(
        self,
        data: Any,
        country_code: str,
        country_name: str,
        layer_name: str,
    ) -> Any:
        """
        Apply country filtering ONLY to layers that require it.
        
        Routes filtering based on layer type:
        - Single file: apply _filter_single_file()
        - File list: apply _filter_file_list()
        - None: return None
        
        Args:
            data: Data from raw_data dict (could be Path, List[Path], or None)
            country_code: ISO-3166-alpha-3 country code
            country_name: Full country name (e.g., "India", "Russia")
            layer_name: Layer name for routing and logging
        
        Returns:
            Filtered data (or original if layer doesn't need filtering)
        """
        # Check if this layer needs filtering
        if layer_name not in self.country_filtered_layers:
            self.log.debug(
                "[%s] No country filtering required (global or special layer)",
                layer_name.lower()
            )
            return data
        
        # Apply appropriate filtering strategy
        if isinstance(data, list):
            return self._filter_file_list(data, country_code, country_name, layer_name)
        else:
            return self._filter_single_file(data, country_code, country_name, layer_name)

    def _filter_file_list(
        self,
        file_list: List,
        country_code: str,
        country_name: str,
        layer_name: str,
    ) -> List:
        """
        Filter a list of files to include only those matching the country.
        
        Uses layer-specific patterns to identify country-relevant files.
        
        Args:
            file_list: List of file paths
            country_code: ISO-3166-alpha-3 country code
            country_name: Full country name
            layer_name: Layer name for pattern selection
        
        Returns:
            Filtered list of files matching country
        """
        if not file_list:
            return []
        
        filtered = []
        rejected = []
        
        for file_path in file_list:
            filename = Path(str(file_path)).name
            path_str = str(file_path)
            
            if self._matches_country_pattern(filename, path_str, country_code, country_name, layer_name):
                filtered.append(file_path)
            else:
                rejected.append(filename)
        
        # Detailed logging
        if rejected:
            self.log.warning(
                "[%s] Rejected %d file(s) from other countries: %s",
                layer_name.lower(),
                len(rejected),
                ", ".join(rejected[:3])  # Show first 3 to avoid spam
            )
        
        if filtered:
            filenames = [Path(str(f)).name for f in filtered]
            self.log.info(
                "[%s] %d file(s) matched for %s: %s",
                layer_name.lower(),
                len(filtered),
                country_code,
                ", ".join(filenames[:3])  # Show first 3
            )
        else:
            self.log.warning(
                "[%s] No files matched for %s (rejected %d from other countries)",
                layer_name.lower(),
                country_code,
                len(rejected)
            )
        
        return filtered

    def _filter_single_file(
        self,
        file_path: Any,
        country_code: str,
        country_name: str,
        layer_name: str,
    ) -> Optional[Path]:
        """
        Filter a single file to ensure it matches the country.
        
        Uses layer-specific patterns to identify country-relevant files.
        
        Args:
            file_path: Single file path (could be None)
            country_code: ISO-3166-alpha-3 country code
            country_name: Full country name
            layer_name: Layer name for pattern selection
        
        Returns:
            Original file path if it matches country, None otherwise
        """
        if not file_path:
            return None
        
        filename = Path(str(file_path)).name
        path_str = str(file_path)
        
        if self._matches_country_pattern(filename, path_str, country_code, country_name, layer_name):
            self.log.debug(
                "[%s] File accepted for %s: %s",
                layer_name.lower(),
                country_code,
                filename
            )
            return file_path
        else:
            self.log.warning(
                "[%s] File rejected (wrong country): %s",
                layer_name.lower(),
                filename
            )
            return None

    def _matches_country_pattern(
        self,
        filename: str,
        path_str: str,
        country_code: str,
        country_name: str,
        layer_name: str,
    ) -> bool:
        """
        Check if a file matches country-specific patterns for the given layer.
        
        Different layers have different naming conventions:
        - Wind: IND_power-density_*.tif (prefix pattern)
        - Borders: gadm41_IND_*.shp (embedded pattern)
        - Protected: WDPA_*_IND_*.shp (embedded pattern)
        - Elevation/Slope: IND_elevation.tif (prefix pattern)
        
        Args:
            filename: File name only
            path_str: Full path string
            country_code: ISO-3166-alpha-3 country code
            country_name: Full country name
            layer_name: Layer name for pattern selection
        
        Returns:
            True if file matches country patterns
        """
        filename_upper = filename.upper()
        path_upper = path_str.upper()
        code_upper = country_code.upper()
        name_upper = country_name.upper()
        
        # Layer-specific pattern matching
        if layer_name == "Wind":
            # Pattern: IND_power-density_*.tif or IND_wind-speed_*.tif
            prefixes = [f"{code_upper}_", f"{country_code.lower()}_"]
            return any(filename_upper.startswith(prefix.upper()) for prefix in prefixes)
        
        elif layer_name == "Borders":
            # Pattern: gadm41_IND_*.shp
            return f"GADM41_{code_upper}_" in filename_upper
        
        elif layer_name == "Protected":
            # Pattern: WDPA_*_IND_*.shp or files containing country code
            return (
                f"_{code_upper}_" in filename_upper or
                f"_{code_upper}." in filename_upper or
                code_upper in filename_upper
            )
        
        elif layer_name in ["Elevation", "Slope", "Grid"]:
            # Pattern: IND_elevation.tif, IND_slope.tif (prefix pattern)
            # Also check for country folder structure
            prefixes = [f"{code_upper}_", f"{country_code.lower()}_"]
            has_prefix = any(filename_upper.startswith(prefix.upper()) for prefix in prefixes)
            has_country_in_path = (
                name_upper in path_upper or 
                code_upper in path_upper
            )
            return has_prefix or has_country_in_path
        
        else:
            # Default: check for country code anywhere in filename or path
            return (
                code_upper in filename_upper or
                name_upper in path_upper or
                code_upper in path_upper
            )

    def _acquire_ancillary(
        self,
        name: str,
        code: str,
        mainland_gdf: gpd.GeoDataFrame,
        status: Dict[str, Any],
    ) -> None:
        """
        Download population, grid, and roads in parallel if missing.

        Each missing layer is submitted as an independent task to a
        ThreadPoolExecutor.  Results are written back into ``status``
        in-place so that the caller sees the updated paths without any
        additional return value.

        Failures are logged as warnings and leave the corresponding
        ``status`` key unchanged (None or its previous value), allowing
        downstream phases to decide whether the missing layer is critical.

        Args:
            name: Full country name
            code: ISO-3166-alpha-3 country code
            mainland_gdf: Mainland geometry GeoDataFrame
            status: Status dict to update in-place
        """
        # ✅ Item 10: Method was left incomplete — only Population and Grid
        # tasks were registered; Roads was missing entirely, the executor
        # was never started, and status was never updated with the results.
        #
        # The completed implementation:
        #   1. Registers all three ancillary layers (Population, Grid, Roads).
        #   2. Skips layers that are already present on disk.
        #   3. Runs missing downloads in parallel via ThreadPoolExecutor.
        #   4. Writes results back into `status` in-place.
        #   5. Logs a clear summary of what was downloaded vs skipped.

        # ── Build task registry ──────────────────────────────────────────
        # Each entry: status_key → (callable, positional_args)
        tasks: Dict[str, tuple] = {}

        if not status.get("Population"):
            tasks["Population"] = (
                self.fetcher.download_worldpop,
                [code],
            )
        else:
            self.log.info(
                "  [ancillary] Population: already on disk — skipping download."
            )

        if not status.get("Grid"):
            tasks["Grid"] = (
                self.fetcher.download_osm_grid,
                [name, code, mainland_gdf],
            )
        else:
            self.log.info(
                "  [ancillary] Grid: already on disk — skipping download."
            )

        if not status.get("Roads"):
            tasks["Roads"] = (
                self.fetcher.download_osm_roads,
                [name, code, mainland_gdf],
            )
        else:
            self.log.info(
                "  [ancillary] Roads: already on disk — skipping download."
            )

        if not tasks:
            self.log.info(
                "  [ancillary] All ancillary layers present — no downloads needed."
            )
            return

        self.log.info(
            "  [ancillary] Downloading %d missing layer(s) in parallel: %s",
            len(tasks),
            ", ".join(tasks.keys()),
        )

        # ── Execute in parallel ──────────────────────────────────────────
        # Cap workers at the number of tasks to avoid spawning idle threads.
        # OSM Overpass already enforces its own rate limiting internally via
        # _request_delay_s, so thread contention is not a concern here.
        n_workers = min(len(tasks), 3)

        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            # Map future → status key so we can update the right entry
            future_to_key = {
                executor.submit(fn, *args): key
                for key, (fn, args) in tasks.items()
            }

            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    result = future.result()
                    if result is not None:
                        status[key] = result
                        self.log.info(
                            "  [ancillary] %s: downloaded → %s",
                            key,
                            Path(result).name
                            if hasattr(result, "__fspath__")
                            else result,
                        )
                    else:
                        self.log.warning(
                            "  [ancillary] %s: download returned None — "
                            "layer will be unavailable for this run.",
                            key,
                        )
                except Exception as exc:
                    self.log.warning(
                        "  [ancillary] %s: download raised an exception — "
                        "layer will be unavailable for this run. Error: %s",
                        key,
                        exc,
                        exc_info=True,
                    )