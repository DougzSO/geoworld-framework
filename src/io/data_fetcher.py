"""
src/io/data_fetcher.py
======================
Automated download of external geospatial datasets.

Implements resilient HTTP/FTP I/O with exponential backoff, early failure
detection, and adaptive tiling for large countries.

Supported datasets:
    - GADM 4.1 administrative boundaries
    - ESA WorldCover 10 m land cover
    - Copernicus GLO-30/GLO-90 DEM
    - WorldPop 1 km population
    - OSM infrastructure (grid, roads) via Overpass API
"""

import json
import logging
import math
import shutil
import socket
import time
import zipfile
from collections import deque
from ftplib import FTP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import rasterio
import rasterio.warp
import requests
from dem_stitcher.stitcher import stitch_dem
from rasterio.merge import merge

from src.core.constants import ESA_NATIVE_RESOLUTION_DEG, NODATA_FLOAT
from src.utils.utils import get_mainland_bounds

logger = logging.getLogger("geoworld.io.DataFetcher")

_WORLDPOP_FALLBACK_YEARS: list[int] = [2020, 2019, 2018]
_ESA_MIN_TILE_BYTES: int = 1_024 * 1_024
_OVERPASS_MAX_BBOX_DEG2: float = 25.0

_HEALTH_CHECKS: Dict[str, str] = {
    "opentopo": "https://cloud.opentopography.org",
    "copernicus": "https://copernicus-dem-30m.s3.amazonaws.com",
}

_TERRASCOPE_MAX_BBOX_DEG2: float = 400.0
_TERRASCOPE_TILE_DEG: float = 15.0


# ===========================================================================
# Connectivity checks
# ===========================================================================

def _check_endpoint_reachable(url: str, timeout: int = 10) -> bool:
    """
    Check if an HTTP endpoint responds.
    
    Tries HEAD request first, then GET with Range header as fallback.
    
    Args:
        url: URL to check
        timeout: Request timeout in seconds
        
    Returns:
        True if endpoint is reachable, False otherwise
    """
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        if 200 <= resp.status_code < 400:
            return True
        resp = requests.get(
            url,
            timeout=timeout,
            stream=True,
            headers={"Range": "bytes=0-0"},
        )
        return 200 <= resp.status_code < 400
    except requests.RequestException:
        return False


def _check_dns_resolution(hostname: str, timeout: int = 5) -> bool:
    """
    Verify that a hostname resolves via DNS.
    
    Args:
        hostname: Hostname to resolve
        timeout: DNS resolution timeout in seconds
        
    Returns:
        True if hostname resolves, False otherwise
    """
    try:
        socket.setdefaulttimeout(timeout)
        socket.getaddrinfo(hostname, None)
        return True
    except (socket.gaierror, socket.timeout):
        return False


def _tile_bbox(
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    tile_deg: float,
) -> List[Tuple[float, float, float, float]]:
    """
    Split a bounding box into tiles of specified size.
    
    Args:
        minx: Minimum longitude
        miny: Minimum latitude
        maxx: Maximum longitude
        maxy: Maximum latitude
        tile_deg: Tile size in degrees
        
    Returns:
        List of (minx, miny, maxx, maxy) tuples for each tile
    """
    tiles = []
    lon = minx
    while lon < maxx:
        lat = miny
        while lat < maxy:
            tiles.append((
                lon,
                lat,
                min(lon + tile_deg, maxx),
                min(lat + tile_deg, maxy),
            ))
            lat += tile_deg
        lon += tile_deg
    return tiles


# ===========================================================================
# DataFetcher
# ===========================================================================

class DataFetcher:
    """
    Automated downloader for external geospatial datasets.

    Args:
        raw_path: Root directory for raw data storage
        cfg: Framework configuration (optional)
    """

    def __init__(self, raw_path: Path, cfg=None):
        """
        Initialize data fetcher with configuration.
        
        Args:
            raw_path: Root directory for raw data storage
            cfg: ConfigLoader instance (optional)
        """
        self.raw_path = raw_path
        self.cfg = cfg
        self._dem_resolution = self._resolve_dem_resolution()

        dl_cfg = cfg.system.get("download", {}) if cfg else {}
        self._request_delay_s = float(dl_cfg.get("request_delay_s", 1.0))
        self._max_retries = int(dl_cfg.get("max_retries", 3))
        self._backoff_base = float(dl_cfg.get("backoff_base_s", 5.0))

    def _resolve_dem_resolution(self) -> float:
        """
        Determine DEM resolution from configuration.
        
        Returns:
            DEM resolution in degrees (default: 0.005)
        """
        try:
            if self.cfg:
                return float(
                    self.cfg.geospatial
                    .get("resolutions", {})
                    .get("dem_slope", 0.005)
                )
        except Exception:
            pass
        return 0.005

    def _request_with_retry(
        self,
        method: str,
        url: str,
        timeout: int = 120,
        max_retries: Optional[int] = None,
        **kwargs,
    ) -> requests.Response:
        """
        HTTP request with automatic retry and rate-limit compliance.
        
        Handles 429/503 responses with Retry-After headers and exponential
        backoff for transient failures.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            url: Target URL
            timeout: Request timeout in seconds
            max_retries: Maximum retry attempts (uses instance default if None)
            **kwargs: Additional arguments passed to requests.request()
            
        Returns:
            Response object if successful
            
        Raises:
            Last exception encountered if all retries fail
        """
        retries = max_retries if max_retries is not None else self._max_retries
        last_exc: Exception = RuntimeError("No attempts made.")

        for attempt in range(retries):
            try:
                resp = requests.request(
                    method, url, timeout=timeout, **kwargs,
                )
                if resp.status_code in (429, 503):
                    retry_after = resp.headers.get("Retry-After")
                    wait = (
                        int(retry_after)
                        if retry_after
                        else int(self._backoff_base * (2 ** attempt))
                    )
                    logger.warning(
                        "HTTP %d on attempt %d/%d. Waiting %ds.",
                        resp.status_code,
                        attempt + 1,
                        retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                if self._request_delay_s > 0:
                    time.sleep(self._request_delay_s)
                return resp

            except requests.RequestException as exc:
                last_exc = exc
                if attempt < retries - 1:
                    wait = self._backoff_base * (3 ** attempt)
                    logger.warning(
                        "Attempt %d/%d failed: %s. Retrying in %ds.",
                        attempt + 1,
                        retries,
                        exc,
                        wait,
                    )
                    time.sleep(wait)

        raise last_exc

    def _get_with_retry(
        self,
        url: str,
        timeout: int = 120,
        **kwargs,
    ) -> requests.Response:
        """
        Convenience wrapper for GET requests with retry logic.
        
        Args:
            url: Target URL
            timeout: Request timeout in seconds
            **kwargs: Additional arguments passed to _request_with_retry()
            
        Returns:
            Response object if successful
        """
        return self._request_with_retry("GET", url, timeout=timeout, **kwargs)

    def _safe_extract(self, zip_path: Path, target_dir: Path) -> None:
        """
        Extract ZIP with Zip Slip protection.
        
        Validates that all archive members extract within target directory
        to prevent path traversal attacks.
        
        Args:
            zip_path: Path to ZIP archive
            target_dir: Target extraction directory
            
        Raises:
            ValueError: If any member would extract outside target directory
        """
        target_resolved = target_dir.resolve()
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                dest = (target_dir / member).resolve()
                try:
                    dest.relative_to(target_resolved)
                except ValueError:
                    raise ValueError(
                        f"Zip Slip detected: '{member}' escapes target. "
                        f"Archive rejected."
                    )
            zf.extractall(target_dir)

    def download_gadm(
        self,
        country_name: str,
        country_code: str,
    ) -> bool:
        """
        Download GADM 4.1 administrative boundaries.
        
        Falls back to NaturalEarth if GADM is unavailable.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            True if download succeeded, False otherwise
        """
        folder = self.raw_path / "countries_borders" / country_name
        folder.mkdir(parents=True, exist_ok=True)
        zip_p = folder / f"{country_code}_gadm.zip"
        url = (
            f"https://geodata.ucdavis.edu/gadm/gadm4.1/shp/"
            f"gadm41_{country_code}_shp.zip"
        )

        logger.info("Downloading GADM borders for %s...", country_code)
        try:
            r = self._get_with_retry(url, timeout=120)
            zip_p.write_bytes(r.content)
            self._safe_extract(zip_p, folder)
            zip_p.unlink()
            logger.info("GADM borders saved to: %s", folder)
            return True

        except ValueError as e:
            if zip_p.exists():
                zip_p.unlink()
            logger.error("GADM ZIP rejected (security): %s", e)
            return False

        except Exception as e:
            logger.warning("GADM download failed: %s", e)
            return self._download_naturalearth_fallback(
                country_name,
                country_code,
            )

    def _download_naturalearth_fallback(
        self,
        country_name: str,
        country_code: str,
    ) -> bool:
        """
        Fallback to NaturalEarth 1:110m boundaries.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            True if fallback succeeded, False otherwise
        """
        import geopandas as gpd

        folder = self.raw_path / "countries_borders" / country_name
        folder.mkdir(parents=True, exist_ok=True)
        out_shp = folder / f"{country_code}_naturalearth_fallback.shp"

        logger.warning(
            "Using NaturalEarth fallback for %s (~1:110m).",
            country_code,
        )

        try:
            try:
                import geodatasets
                world = gpd.read_file(
                    geodatasets.get_path("naturalearth.land"),
                )
            except (ImportError, Exception):
                if hasattr(gpd, "datasets") and hasattr(
                    gpd.datasets, "get_path",
                ):
                    world = gpd.read_file(
                        gpd.datasets.get_path("naturalearth_lowres"),
                    )
                else:
                    logger.error(
                        "Neither geodatasets nor geopandas.datasets "
                        "available. Install geodatasets: "
                        "pip install geodatasets"
                    )
                    return False

            mask = pd.Series([False] * len(world))
            for col in ("iso_a3", "ISO_A3", "ADM0_A3"):
                if col in world.columns:
                    mask |= world[col].str.upper() == country_code.upper()
            for col in ("name", "NAME", "SOVEREIGNT"):
                if col in world.columns:
                    mask |= world[col].str.lower() == country_name.lower()

            country_gdf = world[mask]
            if country_gdf.empty:
                logger.error(
                    "%s not found in NaturalEarth. "
                    "Download manually from naturalearthdata.com.",
                    country_code,
                )
                return False

            country_gdf.to_file(str(out_shp))
            logger.info(
                "NaturalEarth border saved: %s (%d polygon(s)).",
                out_shp.name,
                len(country_gdf),
            )
            return True

        except Exception as e:
            logger.error("NaturalEarth fallback failed: %s", e)
            return False

    def download_land_cover(
        self,
        country_name: str,
        country_geom: Any,
        credentials: Dict[str, str],
        mainland_geom: Any = None,
    ) -> bool:
        """
        Download ESA WorldCover 10m tiles via Terrascope.

        For large countries (bbox > 400 deg²), the search area is
        automatically tiled into sub-requests to avoid empty results
        from the catalogue API.
        
        Args:
            country_name: Full country name
            country_geom: Country geometry (GeoDataFrame or GeoSeries)
            credentials: Dict with terrascope_username and terrascope_password
            mainland_geom: Optional mainland-only geometry
            
        Returns:
            True if download succeeded, False otherwise
        """
        try:
            from terracatalogueclient import Catalogue
        except ImportError:
            logger.error("terracatalogueclient not installed.")
            return False

        temp_dir = self.raw_path / "temp_download" / country_name
        final_dir = self.raw_path / "land_cover" / country_name
        temp_dir.mkdir(parents=True, exist_ok=True)
        final_dir.mkdir(parents=True, exist_ok=True)

        geom = mainland_geom if mainland_geom is not None else country_geom
        raw_bounds = geom.bounds
        if hasattr(raw_bounds, "values"):
            raw_bounds = tuple(raw_bounds.values)
        minx, miny, maxx, maxy = raw_bounds

        if minx > maxx:
            logger.warning(
                "Anti-meridian crossing detected (minx=%.1f > maxx=%.1f). "
                "Splitting into two hemispheres.",
                minx,
                maxx,
            )
            bbox_list = [
                (minx, miny, 180.0, maxy),
                (-180.0, miny, maxx, maxy),
            ]
        else:
            bbox_list = [(minx, miny, maxx, maxy)]

        username = credentials.get("terrascope_username", "")
        password = credentials.get("terrascope_password", "")
        if not username or not password:
            logger.error("Terrascope credentials missing in .env.")
            return False

        collections = [
            "urn:eop:VITO:ESA_WorldCover_10m_2021_V1",
            "urn:eop:VITO:ESA_WorldCover_10m_2020_V1",
            "urn:eop:VITO:ESA_WorldCover_10m_2020_V2",
        ]

        try:
            catalogue = Catalogue().authenticate_non_interactive(
                username,
                password,
            )

            all_sub_bboxes = []
            for bx, by, bxx, bxy in bbox_list:
                area = (bxx - bx) * (bxy - by)
                if area > _TERRASCOPE_MAX_BBOX_DEG2:
                    sub_tiles = _tile_bbox(
                        bx, by, bxx, bxy, _TERRASCOPE_TILE_DEG,
                    )
                    logger.info(
                        "LC bbox %.0f deg² exceeds threshold — "
                        "tiling into %d sub-queries (%.0f° tiles).",
                        area,
                        len(sub_tiles),
                        _TERRASCOPE_TILE_DEG,
                    )
                    all_sub_bboxes.extend(sub_tiles)
                else:
                    all_sub_bboxes.append((bx, by, bxx, bxy))

            seen_titles: set = set()
            map_products = []

            for ti, (sx, sy, ex, ey) in enumerate(all_sub_bboxes):
                buf = 0.05
                bbox_str = (
                    f"{sx - buf:.6f},{sy - buf:.6f},"
                    f"{ex + buf:.6f},{ey + buf:.6f}"
                )

                for coll_id in collections:
                    try:
                        products = list(
                            catalogue.get_products(coll_id, bbox=bbox_str),
                        )
                        candidates = [
                            p for p in products if "MAP" in p.title.upper()
                        ]
                        if not candidates and products:
                            candidates = products

                        for p in candidates:
                            if p.title not in seen_titles:
                                seen_titles.add(p.title)
                                map_products.append(p)

                        if candidates:
                            break
                    except Exception as ex:
                        logger.debug(
                            "Collection %s tile %d failed: %s",
                            coll_id,
                            ti,
                            ex,
                        )

                if (ti + 1) % 10 == 0 or ti == len(all_sub_bboxes) - 1:
                    logger.info(
                        "  LC search: %d/%d sub-bboxes, "
                        "%d unique tiles found.",
                        ti + 1,
                        len(all_sub_bboxes),
                        len(map_products),
                    )

            if not map_products:
                logger.warning(
                    "No ESA WorldCover tiles found across %d sub-queries.",
                    len(all_sub_bboxes),
                )
                return False

            logger.info(
                "Downloading %d ESA WorldCover tile(s)...",
                len(map_products),
            )

            for attempt in range(3):
                try:
                    catalogue.download_products(
                        map_products, str(temp_dir), force=True,
                    )
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    logger.warning(
                        "Download attempt %d/3 failed: %s",
                        attempt + 1,
                        e,
                    )
                    time.sleep(self._backoff_base * (2 ** attempt))

            pipeline_cfg = (
                self.cfg.system.get("pipeline", {}) if self.cfg else {}
            )
            do_compress = pipeline_cfg.get("compress_lc_tiles", True)
            target_res_m = float(
                pipeline_cfg.get("lc_tile_resolution_m", 100),
            )
            target_res_deg = target_res_m / 111_320.0

            copied = 0
            for tif in temp_dir.rglob("*.tif"):
                is_map = "_Map" in tif.name or "ESA_WorldCover" in tif.name
                if not is_map or tif.stat().st_size < _ESA_MIN_TILE_BYTES:
                    continue
                try:
                    with rasterio.open(tif) as src:
                        native_check = ESA_NATIVE_RESOLUTION_DEG * 1.1
                        if abs(src.res[0]) > native_check:
                            continue
                        dst_path = final_dir / tif.name

                        if do_compress:
                            new_w = max(
                                1,
                                round(
                                    src.width * (src.res[0] / target_res_deg)
                                ),
                            )
                            new_h = max(
                                1,
                                round(
                                    src.height * (src.res[1] / target_res_deg)
                                ),
                            )
                            new_transform = (
                                rasterio.transform.from_bounds(
                                    *src.bounds,
                                    width=new_w,
                                    height=new_h,
                                )
                            )
                            data_rs = np.zeros(
                                (new_h, new_w), dtype=np.uint8,
                            )
                            rasterio.warp.reproject(
                                source=rasterio.band(src, 1),
                                destination=data_rs,
                                src_transform=src.transform,
                                src_crs=src.crs,
                                dst_transform=new_transform,
                                dst_crs=src.crs,
                                resampling=(
                                    rasterio.warp.Resampling.nearest
                                ),
                            )
                            profile = src.profile.copy()
                            profile.update(
                                width=new_w,
                                height=new_h,
                                transform=new_transform,
                                compress="lzw",
                                tiled=True,
                                blockxsize=256,
                                blockysize=256,
                                dtype="uint8",
                            )
                            with rasterio.open(
                                dst_path, "w", **profile
                            ) as dst:
                                dst.write(data_rs, 1)
                        else:
                            shutil.copy2(tif, dst_path)
                    copied += 1
                except Exception as e:
                    logger.warning(
                        "Tile processing error %s: %s", tif.name, e
                    )

            shutil.rmtree(temp_dir, ignore_errors=True)
            mode = (
                f"resampled to {target_res_m:.0f}m"
                if do_compress
                else "original 10m"
            )
            logger.info(
                "Land Cover: %d tile(s) saved (%s) to %s.",
                copied,
                mode,
                final_dir,
            )
            return copied > 0

        except Exception as e:
            logger.error("Terrascope download failed: %s", e)
            shutil.rmtree(temp_dir, ignore_errors=True)
            return False

    def download_elevation(
        self,
        country_name: str,
        country_code: str,
        gdf: Any,
    ) -> Optional[Path]:
        """
        Download DEM: Copernicus GLO-30/90 first, OpenTopography fallback.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            gdf: Country GeoDataFrame
            
        Returns:
            Path to downloaded DEM if successful, None otherwise
        """
        dem_dir = self.raw_path / "elevation" / country_name
        dem_dir.mkdir(parents=True, exist_ok=True)
        dem_path = dem_dir / f"{country_code}_elevation.tif"

        bounds = get_mainland_bounds(gdf)

        if not _check_endpoint_reachable(
            _HEALTH_CHECKS["copernicus"], timeout=10,
        ):
            logger.warning(
                "Copernicus S3 unreachable. Skipping to fallback.",
            )
        else:
            try:
                mainland_geom = gdf.to_crs("EPSG:4326").unary_union
            except Exception:
                mainland_geom = gdf.unary_union
            result = self._download_elevation_copernicus(
                bounds,
                dem_dir,
                dem_path,
                country_code,
                mainland_geom=mainland_geom,
            )
            if result is not None:
                return result

        logger.warning(
            "Copernicus unavailable — falling back to OpenTopography SRTM "
            "(coverage: 60°N to 56°S).",
        )
        if not _check_dns_resolution("cloud.opentopography.org"):
            logger.error(
                "Cannot resolve cloud.opentopography.org. "
                "Place DEM manually at: %s",
                dem_path,
            )
            return None

        return self._download_elevation_opentopo(
            bounds, dem_dir, dem_path, country_code,
        )

    def _download_elevation_copernicus(
        self,
        bounds: tuple,
        dem_dir: Path,
        dem_path: Path,
        country_code: str,
        mainland_geom=None,
    ) -> Optional[Path]:
        """
        Download Copernicus GLO-30/90 DEM with adaptive block sizing.
        
        Args:
            bounds: Bounding box (minx, miny, maxx, maxy)
            dem_dir: Directory for temporary DEM tiles
            dem_path: Final output path
            country_code: ISO-3166-alpha-3 code
            mainland_geom: Optional mainland geometry for masking
            
        Returns:
            Path to mosaicked DEM if successful, None otherwise
        """
        width_deg = bounds[2] - bounds[0]
        height_deg = bounds[3] - bounds[1]
        bbox_area = width_deg * height_deg

        dl_cfg = self.cfg.system.get("download", {}) if self.cfg else {}
        target_blocks = int(dl_cfg.get("dem_target_blocks", 300))

        block_deg_raw = math.sqrt(
            max(bbox_area, 1.0) / max(target_blocks, 1)
        )
        block_deg = float(np.clip(round(block_deg_raw), 2.0, 4.0))

        n_x = max(1, int(math.ceil(width_deg / block_deg)))
        n_y = max(1, int(math.ceil(height_deg / block_deg)))
        total_blocks = n_x * n_y

        if bbox_area > 1000:
            dem_sources = ("glo_90",)
            source_label = "glo_90"
        else:
            dem_sources = ("glo_30", "glo_90")
            source_label = "glo_30 → glo_90"

        min_res = float(dl_cfg.get("dem_min_resolution_large", 0.008))
        dst_res = self._dem_resolution
        if bbox_area > 1000:
            dst_res = max(dst_res, min_res)

        logger.info("=" * 60)
        logger.info("  COPERNICUS DEM — %s", country_code)
        logger.info(
            "  Area: %.0f deg²  Blocks: %d (%.1f° each)  "
            "Source: %s  Res: %.4f°",
            bbox_area,
            total_blocks,
            block_deg,
            source_label,
            dst_res,
        )
        est_min = total_blocks * 25 / 60
        logger.info("  Estimated time: %.0f min", est_min)
        logger.info("=" * 60)

        temp_files: List[Path] = []
        failed_blocks = 0
        skipped_blocks = 0
        consecutive_net_errors = 0
        MAX_CONSECUTIVE_NET_ERRORS = 15
        block_times: deque = deque(maxlen=10)
        t_start = time.time()

        block_idx = 0
        for i in range(n_x):
            for j in range(n_y):
                block_idx += 1
                t_block = time.time()
                tmp_p = dem_dir / f"_tmp_cop_{i}_{j}.tif"

                if tmp_p.exists():
                    try:
                        with rasterio.open(tmp_p) as _check:
                            if _check.width > 0 and _check.height > 0:
                                temp_files.append(tmp_p)
                                consecutive_net_errors = 0
                                continue
                    except Exception:
                        tmp_p.unlink(missing_ok=True)

                block = [
                    bounds[0] + i * (width_deg / n_x),
                    bounds[1] + j * (height_deg / n_y),
                    bounds[0] + (i + 1) * (width_deg / n_x),
                    bounds[1] + (j + 1) * (height_deg / n_y),
                ]

                X, prof = None, None
                block_error_type = None

                for src in dem_sources:
                    try:
                        X, prof = stitch_dem(
                            block,
                            dem_name=src,
                            dst_resolution=dst_res,
                        )
                        break
                    except Exception as e:
                        err_str = str(e).lower()

                        if (
                            "coverage" in err_str
                            or "no intersection" in err_str
                        ):
                            block_error_type = "coverage"
                            continue

                        if "404" in err_str:
                            block_error_type = "coverage"
                            logger.debug(
                                "  Block %d/%d: HTTP 404 (no DEM tile at "
                                "lat=%.1f–%.1f, lon=%.1f–%.1f). Skipping.",
                                block_idx,
                                total_blocks,
                                block[1],
                                block[3],
                                block[0],
                                block[2],
                            )
                            continue

                        if any(
                            kw in err_str
                            for kw in (
                                "resolve",
                                "connection",
                                "timeout",
                                "403",
                                "curl",
                                "ssl",
                                "refused",
                                "reset by peer",
                                "broken pipe",
                            )
                        ):
                            block_error_type = "network"
                            consecutive_net_errors += 1
                            logger.warning(
                                "  Block %d/%d: network error (%s). "
                                "Consecutive: %d/%d.",
                                block_idx,
                                total_blocks,
                                e,
                                consecutive_net_errors,
                                MAX_CONSECUTIVE_NET_ERRORS,
                            )

                            wait = min(
                                30,
                                self._backoff_base
                                * (2 ** min(consecutive_net_errors, 4)),
                            )
                            time.sleep(wait)
                            break

                        block_error_type = "unknown"
                        logger.warning(
                            "  Block %d/%d: unexpected error: %s",
                            block_idx,
                            total_blocks,
                            e,
                        )
                        break

                if consecutive_net_errors >= MAX_CONSECUTIVE_NET_ERRORS:
                    logger.warning(
                        "  %d consecutive network errors. "
                        "Copernicus appears down. Stopping download.",
                        consecutive_net_errors,
                    )
                    break

                if X is None or np.all(X <= 0):
                    if block_error_type == "coverage":
                        skipped_blocks += 1
                    else:
                        failed_blocks += 1
                    continue

                consecutive_net_errors = 0

                prof.update(compress="lzw", driver="GTiff")
                with rasterio.open(tmp_p, "w", **prof) as dst:
                    dst.write(X, 1)
                temp_files.append(tmp_p)
                del X

                elapsed_block = time.time() - t_block
                block_times.append(elapsed_block)

                if (
                    block_idx % 10 == 0
                    or block_idx == total_blocks
                    or block_idx <= 3
                ):
                    avg = sum(block_times) / len(block_times)
                    remaining = total_blocks - block_idx
                    eta = remaining * avg
                    eta_str = (
                        f"{eta / 3600:.1f}h"
                        if eta > 3600
                        else f"{eta / 60:.0f}min"
                    )
                    logger.info(
                        "  Block %d/%d | %.1fs/blk | ETA %s | "
                        "ok %d | skip %d | fail %d",
                        block_idx,
                        total_blocks,
                        avg,
                        eta_str,
                        len(temp_files),
                        skipped_blocks,
                        failed_blocks,
                    )
            else:
                continue
            break

        total_s = time.time() - t_start
        logger.info(
            "  Copernicus download: %d ok, %d skipped (no coverage), "
            "%d failed in %.1f min.",
            len(temp_files),
            skipped_blocks,
            failed_blocks,
            total_s / 60,
        )

        if not temp_files:
            logger.warning("Copernicus DEM: no blocks obtained.")
            return None

        coverage_ratio = len(temp_files) / max(
            total_blocks - skipped_blocks, 1
        )
        if coverage_ratio < 0.50 and len(temp_files) < 20:
            logger.warning(
                "  Only %.0f%% coverage (%d blocks). "
                "DEM may be too sparse for reliable analysis.",
                coverage_ratio * 100,
                len(temp_files),
            )

        return self._mosaic_and_save(
            temp_files, dem_path, prefix="cop",
        )

    def _download_elevation_opentopo(
        self,
        bounds: tuple,
        dem_dir: Path,
        dem_path: Path,
        country_code: str,
    ) -> Optional[Path]:
        """
        Download SRTMGL1 (30m) tiles from OpenTopography.
        
        Args:
            bounds: Bounding box (minx, miny, maxx, maxy)
            dem_dir: Directory for temporary DEM tiles
            dem_path: Final output path
            country_code: ISO-3166-alpha-3 code
            
        Returns:
            Path to mosaicked DEM if successful, None otherwise
        """
        SRTM_BASE = (
            "https://cloud.opentopography.org/otr/getdem"
            "?demtype=SRTMGL1&south={s}&north={n}&west={w}&east={e}"
            "&outputFormat=GTiff"
        )
        SRTM_COVERAGE = (-56.0, 60.0)

        minx, miny, maxx, maxy = bounds
        miny = max(miny, SRTM_COVERAGE[0])
        maxy = min(maxy, SRTM_COVERAGE[1])

        if miny >= maxy:
            logger.error(
                "Country outside SRTM coverage (%.1f–%.1f). "
                "Manual DEM required.",
                SRTM_COVERAGE[0],
                SRTM_COVERAGE[1],
            )
            return None

        chunk = 5.0
        lon_starts = np.arange(minx, maxx, chunk).tolist()
        lat_starts = np.arange(miny, maxy, chunk).tolist()
        total = len(lon_starts) * len(lat_starts)

        logger.info(
            "OpenTopography SRTM: %d chunks (%.0f° tiles).",
            total,
            chunk,
        )

        temp_files: List[Path] = []
        failed = 0
        for ci, lon in enumerate(lon_starts):
            for cj, lat in enumerate(lat_starts):
                url = SRTM_BASE.format(
                    s=lat,
                    n=min(lat + chunk, maxy),
                    w=lon,
                    e=min(lon + chunk, maxx),
                )
                tmp_p = dem_dir / f"_tmp_srtm_{ci}_{cj}.tif"
                try:
                    resp = self._get_with_retry(url, timeout=60)
                    ct = resp.headers.get("Content-Type", "")
                    if "json" in ct or len(resp.content) < 1024:
                        continue
                    tmp_p.write_bytes(resp.content)
                    temp_files.append(tmp_p)
                except Exception as e:
                    failed += 1
                    logger.debug(
                        "SRTM tile lat=%.0f lon=%.0f failed: %s",
                        lat,
                        lon,
                        e,
                    )

        if not temp_files:
            logger.error(
                "SRTM: no tiles downloaded (%d failed). "
                "Place DEM at: %s",
                failed,
                dem_path,
            )
            return None

        logger.info(
            "SRTM: %d tiles (%d failed/ocean). Mosaicking...",
            len(temp_files),
            failed,
        )
        return self._mosaic_and_save(temp_files, dem_path, prefix="srtm")

    def _mosaic_and_save(
        self,
        temp_files: List[Path],
        out_path: Path,
        prefix: str = "tmp",
        keep_checkpoints: bool = False,
    ) -> Optional[Path]:
        """
        Mosaic single-band GeoTIFFs into one compressed output.
        
        Args:
            temp_files: List of temporary tile paths
            out_path: Final output path
            prefix: Prefix for logging
            keep_checkpoints: If True, keep temporary tiles
            
        Returns:
            Path to output file if successful, None otherwise
        """
        srcs = []
        try:
            srcs = [rasterio.open(p) for p in temp_files]
            mosaic, transform = merge(srcs)
            meta = srcs[0].meta.copy()
            meta.update(
                driver="GTiff",
                height=mosaic.shape[1],
                width=mosaic.shape[2],
                transform=transform,
                compress="lzw",
                nodata=NODATA_FLOAT,
            )
            with rasterio.open(out_path, "w", **meta) as dst:
                dst.write(mosaic)
            logger.info("Elevation saved: %s", out_path.name)
            return out_path
        except Exception as e:
            logger.error("Mosaic failed: %s", e)
            return None
        finally:
            for s in srcs:
                s.close()
            if not keep_checkpoints:
                for p in temp_files:
                    if p.exists():
                        p.unlink()

    def download_worldpop(
        self,
        country_code: str,
        year: int = 2020,
    ) -> Optional[Path]:
        """
        Download 1 km population raster from WorldPop FTP.
        
        Args:
            country_code: ISO-3166-alpha-3 code
            year: Target year (falls back to 2020, 2019, 2018)
            
        Returns:
            Path to downloaded file if successful, None otherwise
        """
        folder = self.raw_path / "population"
        folder.mkdir(parents=True, exist_ok=True)
        iso = country_code.upper()
        years = sorted(set([year] + _WORLDPOP_FALLBACK_YEARS), reverse=True)

        for yr in years:
            dest = folder / f"{country_code.lower()}_pop_{yr}.tif"
            logger.info("WorldPop FTP: %s / %d...", iso, yr)
            try:
                ftp = FTP("ftp.worldpop.org.uk")
                ftp.login()
                directory = f"/GIS/Population/Global_2000_2020/{yr}/{iso}/"
                ftp.cwd(directory)
                files = ftp.nlst()
                target = next(
                    (
                        f
                        for f in files
                        if "1km" in f and f.endswith(".tif")
                    ),
                    None,
                ) or next(
                    (f for f in files if f.endswith(".tif")),
                    None,
                )

                if target:
                    logger.info("Downloading: %s", target)
                    with open(dest, "wb") as fh:
                        ftp.retrbinary(f"RETR {target}", fh.write)
                    ftp.quit()
                    return dest

                ftp.quit()
                logger.warning("No TIF for %s/%d.", iso, yr)
                time.sleep(2.0)

            except Exception as e:
                logger.warning("WorldPop FTP %d failed: %s", yr, e)

        logger.error("WorldPop unavailable for %s.", iso)
        return None

    def _parse_osm_elements(
        self,
        elements: list,
        include_nodes: bool,
    ) -> list:
        """
        Convert Overpass JSON elements to GeoJSON features.
        
        Args:
            elements: List of OSM elements from Overpass response
            include_nodes: If True, include point geometries from nodes
            
        Returns:
            List of GeoJSON features
        """
        features = []
        for el in elements:
            geom = None
            if (
                include_nodes
                and el["type"] == "node"
                and "lat" in el
                and "lon" in el
            ):
                geom = {
                    "type": "Point",
                    "coordinates": [el["lon"], el["lat"]],
                }
            elif el["type"] == "way" and "geometry" in el:
                coords = [
                    [pt["lon"], pt["lat"]]
                    for pt in el["geometry"]
                    if "lon" in pt and "lat" in pt
                ]
                if len(coords) >= 2:
                    geom = {"type": "LineString", "coordinates": coords}
            if geom:
                features.append(
                    {
                        "type": "Feature",
                        "geometry": geom,
                        "properties": el.get("tags", {}),
                    }
                )
        return features

    def _overpass_query(
        self,
        body: str,
        bbox_str: str,
        label: str,
    ) -> list:
        """
        Execute a single Overpass QL request.
        
        Args:
            body: Overpass QL query body (with {bbox} placeholder)
            bbox_str: Bounding box string (south,west,north,east)
            label: Descriptive label for logging
            
        Returns:
            List of OSM elements
        """
        query = (
            "[out:json][timeout:900];\n"
            f"(\n  {body.format(bbox=bbox_str)}\n);\n"
            "out geom;\n"
        )
        try:
            resp = self._request_with_retry(
                "POST",
                "https://overpass-api.de/api/interpreter",
                timeout=900,
                data={"data": query},
            )
            return resp.json().get("elements", [])
        except requests.exceptions.Timeout:
            logger.warning(
                "Overpass timeout for %s bbox %s.",
                label,
                bbox_str,
            )
            return []
        except Exception as e:
            logger.warning("Overpass error (%s): %s", label, e)
            return []

    def _download_osm_features(
        self,
        label: str,
        query_body: str,
        mainland_gdf: Any,
        out_file: Path,
        include_nodes: bool = True,
    ) -> Optional[Path]:
        """
        Generic OSM downloader with automatic tiling for large countries.
        
        Args:
            label: Feature type label for logging
            query_body: Overpass QL query body template
            mainland_gdf: Country GeoDataFrame
            out_file: Output GeoJSON path
            include_nodes: If True, include node geometries
            
        Returns:
            Path to output file if successful, None otherwise
        """
        if out_file.exists():
            logger.info("OSM %s already exists: %s", label, out_file.name)
            return out_file

        logger.info("Downloading OSM %s via Overpass...", label)
        minx, miny, maxx, maxy = mainland_gdf.total_bounds
        bbox_area = (maxx - minx) * (maxy - miny)

        all_elements: list = []
        seen_ids: set = set()

        if bbox_area <= _OVERPASS_MAX_BBOX_DEG2:
            bbox_str = f"{miny:.4f},{minx:.4f},{maxy:.4f},{maxx:.4f}"
            all_elements = self._overpass_query(query_body, bbox_str, label)
        else:
            tile_deg = 5.0
            n_lon = max(1, int(math.ceil((maxx - minx) / tile_deg)))
            n_lat = max(1, int(math.ceil((maxy - miny) / tile_deg)))
            total_tiles = n_lon * n_lat

            logger.info(
                "OSM %s: tiling %dx%d (%d tiles) for %.0f deg² bbox.",
                label,
                n_lon,
                n_lat,
                total_tiles,
                bbox_area,
            )

            lon_step = (maxx - minx) / n_lon
            lat_step = (maxy - miny) / n_lat

            for i in range(n_lon):
                for j in range(n_lat):
                    sub_s = miny + j * lat_step
                    sub_n = miny + (j + 1) * lat_step
                    sub_w = minx + i * lon_step
                    sub_e = minx + (i + 1) * lon_step
                    bbox_str = (
                        f"{sub_s:.4f},{sub_w:.4f},{sub_n:.4f},{sub_e:.4f}"
                    )

                    elements = self._overpass_query(
                        query_body,
                        bbox_str,
                        label,
                    )
                    for el in elements:
                        eid = (el.get("type"), el.get("id"))
                        if eid not in seen_ids:
                            seen_ids.add(eid)
                            all_elements.append(el)

                    done = i * n_lat + j + 1
                    if done % 10 == 0 or done == total_tiles:
                        logger.info(
                            "  OSM %s: %d/%d tiles, %d elements.",
                            label,
                            done,
                            total_tiles,
                            len(all_elements),
                        )
                    time.sleep(self._request_delay_s)

        if not all_elements:
            logger.warning(
                "No OSM %s data found. For large countries, "
                "download from geofabrik.de and place at: %s",
                label,
                out_file,
            )
            return None

        features = self._parse_osm_elements(all_elements, include_nodes)
        if not features:
            logger.warning(
                "OSM %s: %d elements but no valid geometries.",
                label,
                len(all_elements),
            )
            return None

        geojson = {"type": "FeatureCollection", "features": features}
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump(geojson, fh, ensure_ascii=False)

        logger.info(
            "OSM %s: %s (%d features).",
            label,
            out_file.name,
            len(features),
        )
        return out_file

    def download_osm_grid(
        self,
        country_name: str,
        country_code: str,
        mainland_gdf: Any,
    ) -> Optional[Path]:
        """
        Download transmission lines and substations from OSM.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            mainland_gdf: Mainland GeoDataFrame
            
        Returns:
            Path to output file if successful, None otherwise
        """
        out_file = (
            self.raw_path
            / "infrastructure"
            / "grid"
            / f"{country_code}_grid_osm.geojson"
        )
        return self._download_osm_features(
            label="grid",
            query_body=(
                'way["power"="line"]({bbox});\n'
                '  node["power"="substation"]({bbox});'
            ),
            mainland_gdf=mainland_gdf,
            out_file=out_file,
            include_nodes=True,
        )

    def download_osm_roads(
        self,
        country_name: str,
        country_code: str,
        mainland_gdf: Any,
    ) -> Optional[Path]:
        """
        Download major road segments from OSM.
        
        Args:
            country_name: Full country name
            country_code: ISO-3166-alpha-3 code
            mainland_gdf: Mainland GeoDataFrame
            
        Returns:
            Path to output file if successful, None otherwise
        """
        out_file = (
            self.raw_path
            / "infrastructure"
            / "roads"
            / f"{country_code}_roads_osm.geojson"
        )
        return self._download_osm_features(
            label="roads",
            query_body=(
                'way["highway"~"^(motorway|trunk|primary|secondary|'
                'tertiary)$"]({bbox});'
            ),
            mainland_gdf=mainland_gdf,
            out_file=out_file,
            include_nodes=False,
        )