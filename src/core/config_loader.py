"""
config_loader.py
================
Centralized configuration loader for the GeoWorld Framework.

Reads ``settings.yaml`` (infrastructure / operational defaults) and
``parameters.json`` (per-country scientific parameters) and exposes a
validated, flat dictionary for each country via :meth:`get_country`.

Credentials are sourced exclusively from ``.env`` via ``python-dotenv``.

Public API
----------
    cfg = ConfigLoader(Path("."))
    info   = cfg.get_country_by_name("Portugal")
    params = cfg.get_country("PRT")
    ls     = cfg.land_suitability
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml
from dotenv import load_dotenv

logger = logging.getLogger("geoworld.core.ConfigLoader")


# ===========================================================================
# Exception
# ===========================================================================

class ConfigError(Exception):
    """Raised when a required parameter is missing or fails validation."""


# ===========================================================================
# Validation helpers
# ===========================================================================

_LUF_LIMITS = {"solar": 0.25, "wind": 0.10, "biomass": 0.50}


def _validate_land_use_factor(
    value: float,
    tech: str,
    country: str,
) -> None:
    """
    Validate that land-use factor stays within physically plausible bounds.
    
    Args:
        value: Land-use factor to validate
        tech: Technology name (solar, wind, biomass)
        country: Country code for error reporting
        
    Raises:
        ConfigError: If value exceeds technology-specific maximum
    """
    cap = _LUF_LIMITS.get(tech, 0.50)
    if value > cap:
        raise ConfigError(
            f"[{country}] {tech}_land_use_factor={value} exceeds "
            f"maximum {cap}. Check parameters.json."
        )


def _validate_owa_weights(
    weights: Tuple[float, ...],
    scenario: str,
    country: str,
) -> None:
    """
    Validate OWA weights sum to 1.0 and are in non-increasing order.
    
    Args:
        weights: Tuple of OWA weight values
        scenario: Scenario name for error reporting
        country: Country code for error reporting
        
    Raises:
        ConfigError: If weights don't sum to 1.0 or are not non-increasing
    """
    total = sum(weights)
    if abs(total - 1.0) > 0.01:
        raise ConfigError(
            f"[{country}/OWA/{scenario}] Weights sum to {total:.3f}, "
            f"expected 1.0."
        )
    if not all(weights[i] >= weights[i + 1] for i in range(len(weights) - 1)):
        raise ConfigError(
            f"[{country}/OWA/{scenario}] Weights must be non-increasing. "
            f"Got: {weights}"
        )


def _validate_biomass_yields(
    yields: Dict[int, float],
    country: str
) -> None:
    """
    Validate that wetland and mangrove classes have zero biomass yield.
    
    ESA classes 90 (wetland) and 95 (mangroves) must have zero yield
    as these ecosystems should not be exploited for biomass.
    
    Args:
        yields: Dictionary mapping ESA class to yield (t/ha/yr)
        country: Country code for error reporting
        
    Raises:
        ConfigError: If wetland or mangrove classes have non-zero yield
    """
    for cls, name in {90: "Herbaceous wetland", 95: "Mangroves"}.items():
        val = yields.get(cls, 0.0)
        if val > 0:
            raise ConfigError(
                f"[{country}] Biomass yield for class {cls} ({name}) = "
                f"{val} t/ha/yr. Must be 0.0."
            )


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge override dictionary into base dictionary.
    
    Args:
        base: Base dictionary
        override: Dictionary with values to override/add
        
    Returns:
        New dictionary with merged values (does not modify inputs)
    """
    result = dict(base)
    for k, v in override.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


# ===========================================================================
# ConfigLoader
# ===========================================================================

class ConfigLoader:
    """
    Single source of truth for all pipeline configuration.

    Loads ``settings.yaml`` (infrastructure) and ``parameters.json``
    (per-country science) and returns a flat parameter dictionary for
    each country that is consumed by all downstream processors.
    
    Attributes:
        base_dir: Root directory of the framework
    """

    def __init__(self, base_dir: Path):
        """
        Initialize configuration loader.
        
        Args:
            base_dir: Root directory containing configs/ folder
            
        Raises:
            FileNotFoundError: If settings.yaml or parameters.json missing
        """
        self.base_dir = Path(base_dir)
        self._settings: Dict[str, Any] = {}
        self._params: Dict[str, Any] = {}

        self._yaml_path = self.base_dir / "configs" / "settings.yaml"
        self._json_path = self.base_dir / "configs" / "parameters.json"

        load_dotenv(self.base_dir / ".env")
        self._load_yaml()
        self._load_json()
        logger.info("ConfigLoader initialized.")

    def _load_yaml(self) -> None:
        """
        Load settings.yaml from configs directory.
        
        Raises:
            FileNotFoundError: If settings.yaml does not exist
        """
        if not self._yaml_path.exists():
            raise FileNotFoundError(
                f"settings.yaml not found: {self._yaml_path}"
            )
        with open(self._yaml_path, "r", encoding="utf-8") as fh:
            self._settings = yaml.safe_load(fh) or {}
        logger.debug("settings.yaml loaded.")

    def _load_json(self) -> None:
        """
        Load parameters.json from configs directory.
        
        Raises:
            FileNotFoundError: If parameters.json does not exist
        """
        if not self._json_path.exists():
            raise FileNotFoundError(
                f"parameters.json not found: {self._json_path}"
            )
        with open(self._json_path, "r", encoding="utf-8") as fh:
            self._params = json.load(fh)
        n = len(self._params.get("countries", {}))
        logger.debug("parameters.json loaded — %d countries.", n)

    def get_country_by_name(self, identifier: str) -> Dict[str, Any]:
        """
        Resolve a country name or ISO code to metadata dictionary.
        
        Args:
            identifier: Country name (case-insensitive) or ISO-3166 code
            
        Returns:
            Dictionary with keys 'country_code' and 'country_name'
            
        Raises:
            ConfigError: If no match is found in parameters.json
        """
        countries = self._params.get("countries", {})
        ident_up = identifier.upper().strip()
        ident_low = identifier.lower().strip()

        for code, data in countries.items():
            if (
                code.upper() == ident_up
                or data.get("name", "").lower() == ident_low
            ):
                return {
                    "country_code": code.upper(),
                    "country_name": data["name"],
                }

        raise ConfigError(
            f"Country '{identifier}' not found. "
            f"Available: {list(countries.keys())}"
        )

    def get_country(self, code: str) -> Dict[str, Any]:
        """
        Return a validated, flat parameter dictionary for a country.

        The flat key scheme is kept stable so that downstream processors
        (CriteriaBuilder, SuitabilityBuilder, PotentialCalculator,
        LCOECalculator, GHGAbatementCalculator) need no adaptation.
        
        Args:
            code: ISO-3166-alpha-3 country code
            
        Returns:
            Flat dictionary with all validated parameters
            
        Raises:
            ConfigError: If country not found or validation fails
        """
        code = code.upper().strip()
        countries = self._params.get("countries", {})
        if code not in countries:
            raise ConfigError(
                f"Country '{code}' not found. "
                f"Available: {list(countries.keys())}"
            )

        c = countries[code]
        s, w, b = c["solar"], c["wind"], c["biomass"]
        owa_raw = c["owa"]

        # Validation
        _validate_land_use_factor(float(s["land_use_factor"]), "solar", code)
        _validate_land_use_factor(float(w["land_use_factor"]), "wind", code)
        _validate_land_use_factor(
            float(b["collection_factor"]), "biomass", code
        )

        owa: Dict[str, Tuple[float, ...]] = {}
        for scenario in ("optimistic", "balanced", "conservative"):
            weights = tuple(float(x) for x in owa_raw[scenario])
            _validate_owa_weights(weights, scenario, code)
            owa[scenario] = weights

        yields = {
            int(k): float(v) for k, v in b["yield_by_land_cover"].items()
        }
        _validate_biomass_yields(yields, code)

        # Build flat result
        result: Dict[str, Any] = {
            # Identity
            "country_code": code,
            "country_name": c["name"],

            # Solar
            "solar_land_use_factor": float(s["land_use_factor"]),
            "solar_pvout_weight": float(s.get("pvout_weight", 1.0)),
            "solar_ghi_weight": float(s.get("ghi_weight", 0.0)),
            "solar_dni_weight": float(s.get("dni_weight", 0.0)),
            "solar_threshold": float(s["threshold"]),
            "solar_power_density_mw_km2": float(s["power_density_mw_km2"]),

            # Wind
            "wind_land_use_factor": float(w["land_use_factor"]),
            "wind_capacity_density_mw_km2": float(
                w["capacity_density_mw_km2"]
            ),
            "wind_threshold": float(w["threshold"]),

            # Biomass
            "biomass_collection_factor": float(b["collection_factor"]),
            "biomass_power_density_mw_km2": float(
                b["power_density_mw_km2"]
            ),
            "biomass_threshold": float(b["threshold"]),
            "biomass_yield_by_land_cover": b["yield_by_land_cover"],

            # Terrain
            "slope_threshold_deg": int(c["slope_threshold_deg"]),
            "protected_as_exclusion": bool(
                c.get("protected_as_exclusion", True)
            ),
            "forest_as_exclusion": bool(c.get("forest_as_exclusion", True)),

            # OWA — string-serialized for backward compatibility
            "owa_default_scenario": owa_raw["default_scenario"],
            "owa_scenario_optimistic": ",".join(
                str(x) for x in owa_raw["optimistic"]
            ),
            "owa_scenario_balanced": ",".join(
                str(x) for x in owa_raw["balanced"]
            ),
            "owa_scenario_conservative": ",".join(
                str(x) for x in owa_raw["conservative"]
            ),

            # Parsed objects
            "_owa_weights": owa,
            "_biomass_yields": yields,
            "_land_suitability": self.land_suitability,
        }

        # LCOE (per-country overrides)
        result["lcoe_country_params"] = c.get("lcoe", {})

        # Criteria defaults from settings.yaml
        criteria = self._settings.get("criteria_defaults", {})
        rivers = criteria.get("rivers", {})
        roads = criteria.get("roads", {})
        pop = criteria.get("population", {})

        result["river_max_dist_km"] = float(rivers.get("max_dist_km", 5.0))
        result["river_safety_buffer_km"] = float(
            rivers.get("safety_buffer_km", 0.5)
        )
        result["river_max_dist_biomass_km"] = float(
            rivers.get("max_dist_biomass_km", 30.0)
        )
        result["road_max_dist_km"] = float(roads.get("max_dist_km", 15.0))
        result["pop_density_threshold"] = float(
            pop.get("density_threshold", 300.0)
        )

        # Abatement (deep merge: defaults ← country)
        abatement_defaults = (
            self._params.get("abatement_defaults", {}).get("default", {})
        )
        country_abatement = c.get("abatement", {})
        merged = _deep_merge(abatement_defaults, country_abatement)

        result["abatement_carbon_price_usd_tco2e"] = float(
            merged.get("carbon_price_usd_tco2e", 75.0)
        )
        result["abatement_penetration_factor"] = float(
            merged.get("penetration_factor", 0.6)
        )
        result["abatement_lcoe_threshold_usd_mwh"] = float(
            merged.get("lcoe_threshold_usd_mwh", 60.0)
        )
        result["abatement_thermal_types"] = merged.get(
            "thermal_types", ["coal", "gas", "oil"]
        )
        result["abatement_emission_factors"] = merged.get(
            "thermal_emission_factors_tco2e_gwh",
            {"coal": 820.0, "gas": 490.0, "oil": 750.0},
        )
        result["abatement_thermal_cf"] = merged.get(
            "thermal_cf", {"coal": 0.55, "gas": 0.45, "oil": 0.50}
        )
        result["abatement_thermal_marginal_cost"] = merged.get(
            "thermal_marginal_cost",
            {"coal": 35.0, "gas": 48.0, "oil": 90.0}
        )
        result["abatement_thermal_capacity_mw"] = merged.get(
            "thermal_capacity_mw", {}
        )

        # Geometry
        pipeline_cfg = self._settings.get("pipeline", {})
        global_mainland = bool(pipeline_cfg.get("use_mainland_only", True))
        result["use_mainland_only"] = bool(
            c.get("use_mainland_only", global_mainland)
        )

        # Capacity factors
        for tech in ("solar", "wind", "biomass"):
            result[f"{tech}_capacity_factor"] = self.get_capacity_factor(
                code, tech
            )

        self._log_summary(code, result, owa)
        return result

    @property
    def land_suitability(self) -> Dict[int, Dict[str, float]]:
        """
        Land-cover suitability scores keyed by ESA WorldCover class.
        
        Returns:
            Dictionary mapping ESA class ID to technology suitability scores
        """
        raw = self._params.get("land_suitability", {})
        return {
            int(k): {
                "solar": float(v["solar"]),
                "wind": float(v["wind"]),
                "biomass": float(v["biomass"]),
            }
            for k, v in raw.items()
        }

    @property
    def raw_path(self) -> Path:
        """
        Path to raw data directory.
        
        Returns:
            Path from GEOWORLD_RAW_DATA env var, or default from settings
        """
        override = os.getenv("GEOWORLD_RAW_DATA")
        if override:
            return Path(override)
        return self.base_dir / self._settings.get("paths", {}).get(
            "raw_data", "data/raw"
        )

    @property
    def processed_path(self) -> Path:
        """
        Path to processed data directory.
        
        Returns:
            Path from settings.yaml or default 'data/processed'
        """
        return self.base_dir / self._settings.get("paths", {}).get(
            "processed_data", "data/processed"
        )

    @property
    def logs_path(self) -> Path:
        """
        Path to logs directory, created if it doesn't exist.
        
        Returns:
            Path from settings.yaml or default 'outputs/logs'
        """
        p = self.base_dir / self._settings.get("paths", {}).get(
            "logs_dir", "outputs/logs"
        )
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def system(self) -> Dict[str, Any]:
        """
        Full settings.yaml dictionary.
        
        Returns:
            Complete system settings
        """
        return self._settings

    @property
    def geospatial(self) -> Dict[str, Any]:
        """
        Geospatial configuration section from settings.yaml.
        
        Returns:
            Geospatial settings dictionary
        """
        return self._settings.get("geospatial", {})

    @property
    def credentials(self) -> Dict[str, str]:
        """
        API credentials loaded from environment variables.
        
        Returns:
            Dictionary with credential keys and values
        """
        return {
            "terrascope_username": os.getenv("TERRASCOPE_USERNAME", ""),
            "terrascope_password": os.getenv("TERRASCOPE_PASSWORD", ""),
        }

    @property
    def visualization(self) -> Dict[str, Any]:
        """
        Visualization configuration from settings.yaml.
        
        Returns:
            Visualization settings dictionary
        """
        return self._settings.get("visualization", {})

    def get_lcoe_params(self, code: str) -> Dict[str, Dict]:
        """
        Get merged LCOE parameters (settings.yaml ← parameters.json).

        Keys prefixed with ``_`` in the country override are stripped.
        
        Args:
            code: ISO-3166-alpha-3 country code
            
        Returns:
            Dictionary with LCOE parameters per technology
        """
        code = code.upper().strip()
        yaml_lcoe = self._settings.get("lcoe", {}).get("technologies", {})
        country_lcoe = (
            self._params.get("countries", {}).get(code, {}).get("lcoe", {})
        )
        merged: Dict[str, Dict] = {}
        for tech in ("solar", "wind", "biomass"):
            base = dict(yaml_lcoe.get(tech, {}))
            patch = {
                k: v
                for k, v in country_lcoe.get(tech, {}).items()
                if not k.startswith("_")
            }
            base.update(patch)
            merged[tech] = base
        return merged

    def get_capacity_factor(
        self,
        code: str,
        tech: str
    ) -> Optional[float]:
        """
        Get capacity factor for a country/technology pair.

        Precedence:
          1. Country-specific value from parameters.json
          2. None — calling module applies its own default
        
        Args:
            code: ISO-3166-alpha-3 country code
            tech: Technology name (solar, wind, biomass)
            
        Returns:
            Capacity factor if defined in parameters, None otherwise
        """
        code = code.upper().strip()
        tech = tech.lower().strip()
        country_cf = (
            self._params.get("countries", {})
            .get(code, {})
            .get(tech, {})
            .get("capacity_factor")
        )
        if country_cf is not None:
            cf = float(country_cf)
            logger.debug(
                "[%s/%s] capacity_factor=%.3f (country)", code, tech, cf
            )
            return cf
        logger.debug(
            "[%s/%s] No country CF; module default applies.", code, tech
        )
        return None

    def _log_summary(
        self,
        code: str,
        p: Dict,
        owa: Dict
    ) -> None:
        """
        Log a concise summary of loaded country parameters.
        
        Args:
            code: Country ISO code
            p: Flat parameter dictionary
            owa: OWA weights dictionary
        """
        sep = "-" * 52
        criteria = self._settings.get("criteria_defaults", {})
        rivers = criteria.get("rivers", {})
        roads = criteria.get("roads", {})

        logger.info(sep)
        logger.info("  %s (%s)", p["country_name"], code)
        logger.info(
            "  Solar  : LUF=%.2f  thr=%.2f  density=%.1f MW/km²",
            p["solar_land_use_factor"],
            p["solar_threshold"],
            p["solar_power_density_mw_km2"],
        )
        logger.info(
            "  Wind   : LUF=%.3f  thr=%.2f  density=%.1f MW/km²",
            p["wind_land_use_factor"],
            p["wind_threshold"],
            p["wind_capacity_density_mw_km2"],
        )
        logger.info(
            "  Biomass: col=%.2f  thr=%.2f  density=%.1f MW/km²",
            p["biomass_collection_factor"],
            p["biomass_threshold"],
            p["biomass_power_density_mw_km2"],
        )
        logger.info(
            "  OWA    : default=%s  scenarios=%s",
            p["owa_default_scenario"],
            list(owa.keys()),
        )
        logger.info(
            "  Criteria: river=%.1f km  road=%.1f km  slope=%d°",
            rivers.get("max_dist_km", 5.0),
            roads.get("max_dist_km", 15.0),
            p["slope_threshold_deg"],
        )
        logger.info(sep)