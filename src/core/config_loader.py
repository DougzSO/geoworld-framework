"""
config_loader.py
================
Centralized configuration loader for the GeoWorld Framework.

Reads ``settings.yaml`` (infrastructure / operational defaults) and
``parameters.json`` (per-country scientific parameters).  Returns a
validated, typed ``CountryParams`` contract for each country.

Credentials are sourced exclusively from ``.env`` via python-dotenv.

Configuration source precedence (highest → lowest)
---------------------------------------------------
  1. parameters.json  — country-specific block
  2. parameters.json  — abatement_defaults.default  (abatement only)
  3. settings.yaml    — potential.technologies       (tech fallbacks)
  4. constants.py     — DEFAULT_TECH_PARAMS          (last resort)

Public API
----------
  cfg = ConfigLoader(Path("."))
  info   = cfg.get_country_by_name("Portugal")   # → {"country_code", "country_name"}
  params = cfg.get_country("PRT")                 # → CountryParams
  ls     = cfg.land_suitability                   # → Dict[int, Dict[str, float]]
  lcoe   = cfg.get_lcoe_params("PRT")             # → {"solar": LCOETechParams, ...}
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml
from dotenv import load_dotenv

from src.core.schemas import (
    AbatementParams,
    BiomassParams,
    CountryParams,
    LCOETechParams,
    OWAParams,
    SolarParams,
    TechParams,
)

logger = logging.getLogger("geoworld.core.ConfigLoader")


# ===========================================================================
# Exception
# ===========================================================================


class ConfigError(Exception):
    """Raised when a required parameter is missing or fails validation."""


# ===========================================================================
# Internal helpers
# ===========================================================================


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge *override* into *base* without mutating either.

    Keys starting with ``_`` in *override* are ignored (internal markers).
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
    (per-country science) and returns a typed ``CountryParams`` for each
    country via :meth:`get_country`.

    Attributes
    ----------
    base_dir  Root directory of the framework (contains ``configs/``).
    """

    def __init__(self, base_dir: Path) -> None:
        """
        Initialize the loader and read both configuration files.

        Args:
            base_dir: Directory containing the ``configs/`` folder.

        Raises:
            FileNotFoundError: If ``settings.yaml`` or ``parameters.json``
                               are absent.
        """
        self.base_dir = Path(base_dir)
        self._settings: Dict[str, Any] = {}
        self._params:   Dict[str, Any] = {}

        self._yaml_path = self.base_dir / "configs" / "settings.yaml"
        self._json_path = self.base_dir / "configs" / "parameters.json"

        load_dotenv(self.base_dir / ".env")
        self._load_yaml()
        self._load_json()
        logger.info("ConfigLoader initialized from %s", self.base_dir)

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    def _load_yaml(self) -> None:
        if not self._yaml_path.exists():
            raise FileNotFoundError(
                f"settings.yaml not found: {self._yaml_path}"
            )
        with open(self._yaml_path, "r", encoding="utf-8") as fh:
            self._settings = yaml.safe_load(fh) or {}
        logger.debug("settings.yaml loaded.")

    def _load_json(self) -> None:
        if not self._json_path.exists():
            raise FileNotFoundError(
                f"parameters.json not found: {self._json_path}"
            )
        with open(self._json_path, "r", encoding="utf-8") as fh:
            self._params = json.load(fh)
        n = len(self._params.get("countries", {}))
        logger.debug("parameters.json loaded — %d countries.", n)

    # ------------------------------------------------------------------
    # Country resolution
    # ------------------------------------------------------------------

    def get_country_by_name(self, identifier: str) -> Dict[str, str]:
        """
        Resolve a country name or ISO-3166 code to a metadata dict.

        Args:
            identifier: Case-insensitive country name or ISO-3 code.

        Returns:
            ``{"country_code": str, "country_name": str}``

        Raises:
            ConfigError: If no match is found.
        """
        countries = self._params.get("countries", {})
        upper = identifier.upper().strip()
        lower = identifier.lower().strip()

        for code, data in countries.items():
            if code.upper() == upper or data.get("name", "").lower() == lower:
                return {
                    "country_code": code.upper(),
                    "country_name": data["name"],
                }

        raise ConfigError(
            f"Country '{identifier}' not found. "
            f"Available: {sorted(countries)}"
        )

    # ------------------------------------------------------------------
    # Primary builder
    # ------------------------------------------------------------------

    def get_country(self, code: str) -> CountryParams:
        """
        Return a validated, typed ``CountryParams`` for the given code.

        Alias resolution is performed here (once) so downstream modules
        never need to know raw JSON key names:
          - biomass ``collection_factor``  → ``land_use_factor``
          - wind ``capacity_density_mw_km2`` → ``power_density_mw_km2``

        Args:
            code: ISO-3166-alpha-3 country code.

        Returns:
            Immutable ``CountryParams`` instance.

        Raises:
            ConfigError: If the country is absent or data fails validation.
        """
        code = code.upper().strip()
        countries = self._params.get("countries", {})
        if code not in countries:
            raise ConfigError(
                f"Country '{code}' not found. Available: {sorted(countries)}"
            )

        c        = countries[code]
        raw_sol  = c["solar"]
        raw_wind = c["wind"]
        raw_bio  = c["biomass"]
        raw_owa  = c["owa"]

        solar   = self._build_solar(raw_sol)
        wind    = self._build_wind(raw_wind)
        biomass = self._build_biomass(raw_bio)
        owa     = self._build_owa(raw_owa, code)

        lcoe_solar, lcoe_wind, lcoe_biomass = self._build_lcoe_trio(
            code, c.get("lcoe", {})
        )
        abatement = self._build_abatement(c.get("abatement", {}))
        criteria  = self._build_criteria_defaults()

        pipeline_cfg = self._settings.get("pipeline", {})
        use_mainland = bool(
            c.get("use_mainland_only",
                  pipeline_cfg.get("use_mainland_only", True))
        )

        params = CountryParams(
            country_code=code,
            country_name=c["name"],
            solar=solar,
            wind=wind,
            biomass=biomass,
            owa=owa,
            lcoe_solar=lcoe_solar,
            lcoe_wind=lcoe_wind,
            lcoe_biomass=lcoe_biomass,
            abatement=abatement,
            slope_threshold_deg=int(c["slope_threshold_deg"]),
            protected_as_exclusion=bool(c.get("protected_as_exclusion", True)),
            forest_as_exclusion=bool(c.get("forest_as_exclusion", True)),
            use_mainland_only=use_mainland,
            river_max_dist_km=criteria["river_max_dist_km"],
            river_safety_buffer_km=criteria["river_safety_buffer_km"],
            river_max_dist_biomass_km=criteria["river_max_dist_biomass_km"],
            road_max_dist_km=criteria["road_max_dist_km"],
            pop_density_threshold=criteria["pop_density_threshold"],
            land_suitability=self.land_suitability,
        )

        self._log_summary(params)
        return params

    # ------------------------------------------------------------------
    # Sub-builders (private)
    # ------------------------------------------------------------------

    def _build_solar(self, raw: Dict[str, Any]) -> SolarParams:
        """Build SolarParams from raw JSON block, applying CF from nested key."""
        return SolarParams(
            land_use_factor=float(raw["land_use_factor"]),
            power_density_mw_km2=float(raw["power_density_mw_km2"]),
            threshold=float(raw["threshold"]),
            capacity_factor=float(raw.get("capacity_factor", 0.0)),
            pvout_weight=float(raw.get("pvout_weight", 1.0)),
            ghi_weight=float(raw.get("ghi_weight", 0.0)),
            dni_weight=float(raw.get("dni_weight", 0.0)),
        )

    def _build_wind(self, raw: Dict[str, Any]) -> TechParams:
        """
        Build wind TechParams resolving the capacity_density alias.

        JSON uses ``capacity_density_mw_km2``; the canonical pipeline
        name is ``power_density_mw_km2`` (consistent with solar/biomass).
        """
        density = float(
            raw.get("capacity_density_mw_km2",
                    raw.get("power_density_mw_km2", 6.5))
        )
        return TechParams(
            land_use_factor=float(raw["land_use_factor"]),
            power_density_mw_km2=density,
            threshold=float(raw["threshold"]),
            capacity_factor=float(raw.get("capacity_factor", 0.0)),
        )

    def _build_biomass(self, raw: Dict[str, Any]) -> BiomassParams:
        """
        Build BiomassParams resolving the collection_factor alias.

        JSON uses ``collection_factor``; it maps to both
        ``land_use_factor`` (TechParams base) and ``collection_factor``
        (BiomassParams explicit field) so both access patterns work.
        """
        luf = float(
            raw.get("collection_factor",
                    raw.get("land_use_factor", 0.30))
        )
        yields: Dict[str, float] = {
            str(k): float(v)
            for k, v in raw.get("yield_by_land_cover", {}).items()
        }
        return BiomassParams(
            land_use_factor=luf,
            power_density_mw_km2=float(raw["power_density_mw_km2"]),
            threshold=float(raw["threshold"]),
            capacity_factor=float(raw.get("capacity_factor", 0.0)),
            collection_factor=luf,
            yield_by_land_cover=yields,
        )

    def _build_owa(self, raw: Dict[str, Any], code: str) -> OWAParams:
        """Build and validate OWAParams from raw JSON block."""
        try:
            return OWAParams(
                default_scenario=raw.get("default_scenario", "balanced"),
                optimistic=tuple(float(x) for x in raw["optimistic"]),
                balanced=tuple(float(x) for x in raw["balanced"]),
                conservative=tuple(float(x) for x in raw["conservative"]),
            )
        except Exception as exc:
            raise ConfigError(
                f"[{code}] Invalid OWA configuration: {exc}"
            ) from exc

    def _build_lcoe_trio(
        self,
        code: str,
        country_lcoe: Dict[str, Any],
    ) -> Tuple[LCOETechParams, LCOETechParams, LCOETechParams]:
        """
        Build LCOE params for all three technologies.

        Merge order: settings.yaml defaults ← country-specific overrides.
        """
        yaml_base = self._settings.get("lcoe", {}).get("technologies", {})

        def _merge(tech: str) -> LCOETechParams:
            base = dict(yaml_base.get(tech, {}))
            patch = {
                k: v
                for k, v in country_lcoe.get(tech, {}).items()
                if not k.startswith("_")
            }
            base.update(patch)
            try:
                return LCOETechParams(
                    capex_usd_kw=float(base["capex_usd_kw"]),
                    opex_usd_kw_yr=float(base["opex_usd_kw_yr"]),
                    lifetime_years=int(base["lifetime_years"]),
                    discount_rate=float(base["discount_rate"]),
                )
            except (KeyError, TypeError) as exc:
                raise ConfigError(
                    f"[{code}] Missing LCOE parameter for {tech}: {exc}"
                ) from exc

        return _merge("solar"), _merge("wind"), _merge("biomass")

    def _build_abatement(
        self, country_abatement: Dict[str, Any]
    ) -> AbatementParams:
        """
        Build AbatementParams by deep-merging global defaults with
        country-specific overrides.
        """
        defaults = (
            self._params.get("abatement_defaults", {}).get("default", {})
        )
        merged = _deep_merge(defaults, country_abatement)

        return AbatementParams(
            carbon_price_usd_tco2e=float(
                merged.get("carbon_price_usd_tco2e", 75.0)
            ),
            penetration_factor=float(
                merged.get("penetration_factor", 0.60)
            ),
            existing_renewable_share=float(
                merged.get("existing_renewable_share", 0.0)
            ),
            grid_total_gwh=float(
                merged.get("grid_total_gwh", 0.0)
            ),
            lcoe_threshold_usd_mwh=float(
                merged.get("lcoe_threshold_usd_mwh", 60.0)
            ),
            thermal_types=list(
                merged.get("thermal_types", ["coal", "gas", "oil"])
            ),
            emission_factors=dict(
                merged.get(
                    "thermal_emission_factors_tco2e_gwh",
                    {"coal": 820.0, "gas": 490.0, "oil": 750.0},
                )
            ),
            thermal_cf=dict(
                merged.get(
                    "thermal_cf",
                    {"coal": 0.55, "gas": 0.45, "oil": 0.35},
                )
            ),
            thermal_marginal_cost=dict(
                merged.get(
                    "thermal_marginal_cost",
                    {"coal": 35.0, "gas": 48.0, "oil": 90.0},
                )
            ),
            thermal_capacity_mw=dict(
                merged.get("thermal_capacity_mw", {})
            ),
        )

    def _build_criteria_defaults(self) -> Dict[str, float]:
        """Extract infrastructure-level criteria defaults from settings.yaml."""
        criteria = self._settings.get("criteria_defaults", {})
        rivers = criteria.get("rivers", {})
        roads  = criteria.get("roads", {})
        pop    = criteria.get("population", {})
        return {
            "river_max_dist_km":         float(rivers.get("max_dist_km", 5.0)),
            "river_safety_buffer_km":    float(rivers.get("safety_buffer_km", 0.5)),
            "river_max_dist_biomass_km": float(rivers.get("max_dist_biomass_km", 30.0)),
            "road_max_dist_km":          float(roads.get("max_dist_km", 15.0)),
            "pop_density_threshold":     float(pop.get("density_threshold", 300.0)),
        }

    # ------------------------------------------------------------------
    # Properties — read-only access to settings sections
    # ------------------------------------------------------------------

    @property
    def land_suitability(self) -> Dict[int, Dict[str, float]]:
        """
        ESA WorldCover suitability scores keyed by class ID.

        Returns
        -------
        Dict[int, Dict[str, float]]
            ``{esa_class: {"solar": float, "wind": float, "biomass": float}}``
        """
        raw = self._params.get("land_suitability", {})
        return {
            int(k): {
                "solar":   float(v["solar"]),
                "wind":    float(v["wind"]),
                "biomass": float(v["biomass"]),
            }
            for k, v in raw.items()
        }

    @property
    def raw_path(self) -> Path:
        """Raw data directory (env ``GEOWORLD_RAW_DATA`` → settings.yaml → default)."""
        override = os.getenv("GEOWORLD_RAW_DATA")
        if override:
            return Path(override)
        return self.base_dir / self._settings.get("paths", {}).get(
            "raw_data", "data/raw"
        )

    @property
    def processed_path(self) -> Path:
        """Processed data directory from settings.yaml."""
        return self.base_dir / self._settings.get("paths", {}).get(
            "processed_data", "data/processed"
        )

    @property
    def logs_path(self) -> Path:
        """Logs directory — created on first access."""
        p = self.base_dir / self._settings.get("paths", {}).get(
            "logs_dir", "outputs/logs"
        )
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def system(self) -> Dict[str, Any]:
        """Full settings.yaml dictionary."""
        return self._settings

    @property
    def geospatial(self) -> Dict[str, Any]:
        """``geospatial`` section of settings.yaml."""
        return self._settings.get("geospatial", {})

    @property
    def credentials(self) -> Dict[str, str]:
        """API credentials from environment variables."""
        return {
            "terrascope_username": os.getenv("TERRASCOPE_USERNAME", ""),
            "terrascope_password": os.getenv("TERRASCOPE_PASSWORD", ""),
        }

    @property
    def visualization(self) -> Dict[str, Any]:
        """``visualization`` section of settings.yaml."""
        return self._settings.get("visualization", {})

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def get_lcoe_params(self, code: str) -> Dict[str, LCOETechParams]:
        """
        Return merged LCOETechParams for all technologies.

        Args:
            code: ISO-3166-alpha-3 country code.

        Returns:
            ``{"solar": LCOETechParams, "wind": LCOETechParams,
               "biomass": LCOETechParams}``
        """
        code = code.upper().strip()
        country_lcoe = (
            self._params.get("countries", {}).get(code, {}).get("lcoe", {})
        )
        solar, wind, biomass = self._build_lcoe_trio(code, country_lcoe)
        return {"solar": solar, "wind": wind, "biomass": biomass}

    def get_capacity_factor(self, code: str, tech: str) -> Optional[float]:
        """
        Return the country-specific capacity factor or ``None``.

        Precedence: parameters.json country block → ``None``.
        Returning ``None`` signals that the calling module should apply
        its own technology default.

        Args:
            code: ISO-3166-alpha-3 country code.
            tech: Technology name (``solar``, ``wind``, ``biomass``).

        Returns:
            ``float`` if defined; ``None`` otherwise.
        """
        code = code.upper().strip()
        tech = tech.lower().strip()
        raw  = (
            self._params.get("countries", {})
            .get(code, {})
            .get(tech, {})
            .get("capacity_factor")
        )
        if raw is not None:
            cf = float(raw)
            logger.debug("[%s/%s] capacity_factor=%.3f (country)", code, tech, cf)
            return cf
        logger.debug("[%s/%s] No country CF — module default applies.", code, tech)
        return None

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_summary(self, p: CountryParams) -> None:
        """Emit a concise, reproducible parameter summary after loading."""
        sep = "-" * 52
        logger.info(sep)
        logger.info("  %s (%s)", p.country_name, p.country_code)
        logger.info(
            "  Solar  : LUF=%.2f  thr=%.2f  CF=%.3f  density=%.1f MW/km²",
            p.solar.land_use_factor, p.solar.threshold,
            p.solar.capacity_factor, p.solar.power_density_mw_km2,
        )
        logger.info(
            "  Wind   : LUF=%.3f  thr=%.2f  CF=%.3f  density=%.1f MW/km²",
            p.wind.land_use_factor, p.wind.threshold,
            p.wind.capacity_factor, p.wind.power_density_mw_km2,
        )
        logger.info(
            "  Biomass: LUF=%.2f  thr=%.2f  CF=%.3f  density=%.1f MW/km²",
            p.biomass.land_use_factor, p.biomass.threshold,
            p.biomass.capacity_factor, p.biomass.power_density_mw_km2,
        )
        logger.info(
            "  OWA    : default=%s", p.owa.default_scenario,
        )
        logger.info(
            "  Abatement: carbon_price=%.1f USD/tCO2e  "
            "penetration=%.0f%%  existing_renew=%.0f%%",
            p.abatement.carbon_price_usd_tco2e,
            p.abatement.penetration_factor * 100,
            p.abatement.existing_renewable_share * 100,
        )
        logger.info(
            "  Criteria: river=%.1f km  road=%.1f km  slope=%d°",
            p.river_max_dist_km, p.road_max_dist_km, p.slope_threshold_deg,
        )
        logger.info(sep)