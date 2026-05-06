"""
data_manager.py
===============
Locates raw geospatial data files on disk for a given country.

Responsible solely for file discovery — downloading is handled by
``DataFetcher`` and processing by the ``processors/`` package.

Expected directory layout under ``raw_path``::

    countries_borders/<Country>/   — GADM shapefiles
    land_cover/<Country>/          — ESA WorldCover tiles
    elevation/<Country>/           — DEM + slope rasters
    solar_potential/               — PVOUT rasters
    wind_potential/                — wind power density rasters
    population/                    — WorldPop TIFs
    infrastructure/roads/          — GRIP / OSM road data
    infrastructure/grid/           — OSM GeoJSON / shapefiles
    hydrology/lakes/               — HydroLAKES shapefile
    hydrology/rivers/              — HydroRIVERS shapefile
    risks/                         — seismic hazard TIF
    protected_areas/<Country>/     — WDPA shapefiles
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger("geoworld.io.DataManager")

_ESA_LC_PATTERN = re.compile(
    r"ESA_WorldCover_10m_.*_Map\.tif$", re.IGNORECASE,
)


class DataManager:
    """
    Locate raw geospatial datasets on disk for a given country.

    Args:
        config_loader: ConfigLoader instance providing raw data root path
    """

    def __init__(self, config_loader):
        """
        Initialize data manager.
        
        Args:
            config_loader: ConfigLoader instance
        """
        self.cfg = config_loader
        self.raw_path = self._resolve_raw_path()
        self._plants_df: Optional[pd.DataFrame] = None
        logger.info("Raw data root: %s", self.raw_path)

    def _resolve_raw_path(self) -> Path:
        """
        Resolve raw data directory path from configuration.
        
        Returns:
            Path to raw data directory
        """
        path = self.cfg.raw_path
        if not path.exists():
            logger.warning(
                "Raw data directory not found: %s. "
                "Set GEOWORLD_RAW_DATA in .env.",
                path,
            )
        return path

    def _country_folders(
        self,
        country_name: str,
        country_code: str,
    ) -> List[str]:
        """
        Generate deduplicated candidate folder names for a country.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            List of unique folder name candidates
        """
        seen: set[str] = set()
        result: List[str] = []
        candidates = (country_name, country_code, country_code.upper())
        for candidate in candidates:
            if candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
        return result

    def _find_files(
        self,
        folder: str,
        keywords: List[str],
        extension: str = ".tif",
    ) -> List[Path]:
        """
        Recursive keyword-based file search within a subdirectory.
        
        Args:
            folder: Subdirectory under raw_path to search
            keywords: List of keywords to match in filenames
            extension: File extension to filter (default: .tif)
            
        Returns:
            Sorted list of matching file paths
        """
        search_dir = self.raw_path / folder
        if not search_dir.exists():
            return []
        return sorted(
            p
            for p in search_dir.rglob(f"*{extension}")
            if any(kw.lower() in p.name.lower() for kw in keywords)
        )

    def _find_borders(
        self,
        country_name: str,
        country_code: str,
    ) -> Optional[Path]:
        """
        Locate the GADM level-0 boundary shapefile.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            Path to level-0 shapefile if found, None otherwise
        """
        for folder in self._country_folders(country_name, country_code):
            results = self._find_files(
                f"countries_borders/{folder}",
                [country_code],
                ".shp",
            )
            level0 = [r for r in results if r.stem.endswith("_0")]
            if level0:
                return level0[0]
            if results:
                return results[0]
        return None

    def get_admin_level_1(
        self,
        country_name: str,
        country_code: str,
    ) -> Optional[Path]:
        """
        Locate GADM level-1 boundaries.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            Path to level-1 shapefile (gadm41_<CODE>_1.shp) if found
        """
        for folder in self._country_folders(country_name, country_code):
            results = self._find_files(
                f"countries_borders/{folder}",
                [f"{country_code}_1"],
                ".shp",
            )
            valid = [f for f in results if f.stem.endswith("_1")]
            if valid:
                return valid[0]
        return None

    def _find_land_cover(
        self,
        country_name: str,
        country_code: str,
    ) -> List[Path]:
        """
        Locate ESA WorldCover tiles.

        Falls back to any .tif in the directory if the official naming
        pattern yields no results (for manually placed tiles).
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            List of land cover tile paths
        """
        for folder in self._country_folders(country_name, country_code):
            lc_dir = self.raw_path / "land_cover" / folder
            if not lc_dir.exists():
                continue
            tiles = sorted(
                f
                for f in lc_dir.rglob("*.tif")
                if _ESA_LC_PATTERN.search(f.name)
            )
            if tiles:
                logger.debug(
                    "Land Cover: %d ESA tile(s) in %s",
                    len(tiles),
                    lc_dir,
                )
                return tiles

            all_tifs = sorted(lc_dir.rglob("*.tif"))
            if all_tifs:
                logger.debug(
                    "Land Cover: %d tile(s) via fallback pattern in %s",
                    len(all_tifs),
                    lc_dir,
                )
                return all_tifs
        return []

    def load_wind_data(
        self,
        country_name: str,
        country_code: str,
    ) -> List[Path]:
        """
        Return available wind power density rasters.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            List of wind data file paths
        """
        return self._find_files(
            "wind_potential",
            [country_code, country_name],
        )

    def load_solar_data(
        self,
        country_name: str,
        country_code: str,
    ) -> Optional[Path]:
        """
        Return the solar potential (PVOUT) raster, if present.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            Path to solar PVOUT raster if found, None otherwise
        """
        matches = self._find_files("solar_potential", ["pvout"])
        if not matches:
            matches = self._find_files(
                "solar_potential",
                [country_code, country_name],
            )
        return matches[0] if matches else None

    def get_elevation_paths(
        self,
        country_name: str,
        country_code: str,
    ) -> Tuple[Optional[Path], Optional[Path]]:
        """
        Return elevation and slope raster paths.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            Tuple of (elevation_path, slope_path), either may be None
        """
        for folder in self._country_folders(country_name, country_code):
            d = self.raw_path / "elevation" / folder
            elev = d / f"{country_code}_elevation.tif"
            if elev.exists():
                slope = d / f"{country_code}_slope.tif"
                return elev, (slope if slope.exists() else None)
        return None, None

    def _load_plants_csv(self) -> Optional[pd.DataFrame]:
        """
        Load and cache the global power plant CSV.
        
        Returns:
            DataFrame with power plant data if successful, None otherwise
        """
        if self._plants_df is not None:
            return self._plants_df

        path = self.raw_path / "global_power_plant_database.csv"
        if not path.exists():
            logger.warning("global_power_plant_database.csv not found.")
            return None
        try:
            df = pd.read_csv(path, low_memory=False)
            df.columns = [c.strip().lower() for c in df.columns]
            self._plants_df = df
            return df
        except Exception as e:
            logger.error("Failed to read power plant database: %s", e)
            return None

    def load_power_plants(
        self,
        country_name: str,
        country_code: str,
    ) -> Optional[pd.DataFrame]:
        """
        Filter the global power plant database for a specific country.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            DataFrame with country's power plants if found, None otherwise
        """
        df = self._load_plants_csv()
        if df is None:
            return None

        required = {"country", "country_long"}
        if not required.issubset(df.columns):
            logger.error(
                "Required columns missing: %s",
                required - set(df.columns),
            )
            return None

        mask = (
            df["country_long"].str.lower() == country_name.lower()
        ) | (
            df["country"].str.upper() == country_code.upper()
        )
        result = df[mask].copy()
        logger.info("Power plants found: %d", len(result))
        return result if not result.empty else None

    def load_protected_areas(
        self,
        country_name: str,
        country_code: str,
    ) -> Optional[Path]:
        """
        Locate WDPA polygon shapefiles (priority: shp_0 → shp_1 → shp_2).
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            Path to WDPA shapefile if found, None otherwise
        """
        for folder in self._country_folders(country_name, country_code):
            base = self.raw_path / "protected_areas" / folder
            if not base.exists():
                continue
            for sub in ("shp_0", "shp_1", "shp_2"):
                sub_dir = base / sub
                if not sub_dir.exists():
                    continue
                candidates = sorted(sub_dir.glob("*polygons.shp"))
                if candidates:
                    return candidates[0]
            candidates = sorted(
                p
                for p in base.rglob("*polygons.shp")
                if p.stat().st_size > 1024
            )
            if candidates:
                return candidates[0]

        logger.debug("Protected areas not found for %s.", country_code)
        return None

    def load_population_data(self, country_code: str) -> Optional[Path]:
        """
        Locate the most recent WorldPop raster for a country.
        
        Args:
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            Path to most recent population raster if found, None otherwise
        """
        folder = self.raw_path / "population"
        if not folder.exists():
            return None

        candidates = sorted(
            folder.glob(f"{country_code.lower()}_pop_*.tif"),
        )
        if not candidates:
            return None

        def _year(p: Path) -> int:
            m = re.search(r"(\d{4})", p.name)
            return int(m.group(1)) if m else 0

        return max(candidates, key=_year)

    def load_roads_data(self, country_code: str) -> Optional[Path]:
        """
        Locate road data: GRIP shapefiles first, then OSM GeoJSON fallback.
        
        Args:
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            Path to road data file if found, None otherwise
        """
        roads_base = self.raw_path / "infrastructure" / "roads"

        lookup_path = roads_base / "regions_lookup.json"
        if lookup_path.exists():
            try:
                with open(lookup_path, "r", encoding="utf-8") as fh:
                    lookup = json.load(fh)
                target_region = next(
                    (
                        region
                        for region, codes in lookup.get(
                            "regions", {}
                        ).items()
                        if country_code.upper() in codes
                    ),
                    None,
                )
                if target_region:
                    region_folder = roads_base / target_region
                    shp_files = sorted(region_folder.glob("*.shp"))
                    if shp_files:
                        return shp_files[0]
            except Exception as e:
                logger.debug("GRIP lookup failed: %s", e)

        osm_file = roads_base / f"{country_code}_roads_osm.geojson"
        if osm_file.exists():
            return osm_file

        logger.debug("No road data found for %s.", country_code)
        return None

    def load_grid_data(self, country_code: str) -> Optional[Path]:
        """
        Locate power grid data (GeoJSON preferred, then shapefiles).
        
        Args:
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            Path to grid data file if found, None otherwise
        """
        folder = self.raw_path / "infrastructure" / "grid"
        if not folder.exists():
            return None
        for pattern in (
            f"*{country_code}*.geojson",
            f"*{country_code}*.shp",
        ):
            files = sorted(folder.glob(pattern))
            if files:
                return files[0]
        return None

    def load_lakes_data(self) -> Optional[Path]:
        """
        Locate global HydroLAKES shapefile.
        
        Returns:
            Path to lakes shapefile if found, None otherwise
        """
        lakes_dir = self.raw_path / "hydrology" / "lakes"
        if not lakes_dir.exists():
            return None
        candidates = sorted(lakes_dir.glob("*.shp"))
        return candidates[0] if candidates else None

    def load_rivers_data(self) -> Optional[Path]:
        """
        Locate global HydroRIVERS shapefile.
        
        Returns:
            Path to rivers shapefile if found, None otherwise
        """
        rivers_dir = self.raw_path / "hydrology" / "rivers"
        if not rivers_dir.exists():
            return None
        candidates = sorted(rivers_dir.glob("*.shp"))
        return candidates[0] if candidates else None

    def load_seismic_data(self) -> Optional[Path]:
        """
        Locate global seismic hazard raster.
        
        Returns:
            Path to seismic hazard TIF if found, None otherwise
        """
        path = self.raw_path / "risks" / "seismic_hazard_global.tif"
        return path if path.exists() else None

    def check_country_availability(
        self,
        country_name: str,
        country_code: str,
    ) -> Dict[str, Any]:
        """
        Check presence of all raw datasets for a country.

        Returns a dict mapping dataset names to paths (or None if missing).
        Datasets are returned without downloading - use DataOrchestrator
        for automatic acquisition.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            Dictionary mapping dataset names to paths or None
        """
        elev, slope = self.get_elevation_paths(country_name, country_code)
        plants_path = self.raw_path / "global_power_plant_database.csv"

        return {
            "Borders": self._find_borders(country_name, country_code),
            "Land Cover": self._find_land_cover(country_name, country_code),
            "Elevation": elev,
            "Slope": slope,
            "Solar": self.load_solar_data(country_name, country_code),
            "Wind": self.load_wind_data(country_name, country_code),
            "Power Plants": plants_path if plants_path.exists() else None,
            "Population": self.load_population_data(country_code),
            "Roads": self.load_roads_data(country_code),
            "Protected": self.load_protected_areas(
                country_name, country_code
            ),
            "Lakes": self.load_lakes_data(),
            "Rivers": self.load_rivers_data(),
            "Seismic": self.load_seismic_data(),
            "Grid": self.load_grid_data(country_code),
            "Admin1": self.get_admin_level_1(country_name, country_code),
        }