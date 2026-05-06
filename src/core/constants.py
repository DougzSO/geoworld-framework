"""
constants.py
============
Scientific constants and reference values used across multiple modules.

Capacity Factor Ceilings
-------------------------
No plant operates at 100% capacity due to maintenance and curtailment.
Practical ceilings by technology (state of the art, 2024):
  - Solar PV:       ~35% (desert tracking with DC/AC oversizing)
  - Wind onshore:   ~55% (constant-wind regimes)
  - Wind offshore:  >60% (modern turbines, North Sea class)
  - Biomass:        ~92% (limited by maintenance and fuel supply)

AHP Scale Calibration
---------------------
Calibrated for CR <= 0.10 with up to 9 criteria.

Key References
--------------
    Saaty (1980). The Analytic Hierarchy Process. McGraw-Hill.
    Yager (1988). IEEE Trans SMC 18(1):183–190.
    IRENA (2024). Renewable Power Generation Costs in 2023.
    Wiser et al. (2021). Nat Energy 6:555–565.
    Franek & Kresta (2014). AHP inconsistency.
    ESA WorldCover (Zanaga et al., 2021). doi:10.5281/zenodo.5571936.
"""

from __future__ import annotations

import copy
from typing import Dict, List

# ===========================================================================
# Technology metadata
# ===========================================================================

TECH_ORDER: List[str] = ["solar", "wind", "biomass"]

TECH_META: Dict[str, Dict] = {
    "solar": {
        "label": "Solar PV",
        "short": "Solar",
        "color": "#F9A825",
        "rgb": (0.976, 0.627, 0.145),
        "dom_id": 1,
        "bg": "#FEF9C3",
        "cmap": "YlOrRd",
    },
    "wind": {
        "label": "Wind Onshore",
        "short": "Wind",
        "color": "#1565C0",
        "rgb": (0.086, 0.392, 0.753),
        "dom_id": 2,
        "bg": "#DBEAFE",
        "cmap": "Blues",
    },
    "biomass": {
        "label": "Biomass / Bioenergy",
        "short": "Biomass",
        "color": "#2E7D32",
        "rgb": (0.180, 0.490, 0.196),
        "dom_id": 3,
        "bg": "#DCFCE7",
        "cmap": "YlGn",
    },
}

TECH_LABELS: Dict[str, str] = {k: v["label"] for k, v in TECH_META.items()}

# ===========================================================================
# Wind resource modelling
# ===========================================================================

WIND_SIGMOID: Dict[str, float] = {
    "midpoint": 280.0,
    "steepness": 0.010,
    "max_cf": 0.45,
}

WIND_HEIGHT_KEYS: List[str] = ["200m", "100m", "50m"]
WIND_AHP_MATRIX: List[List[float]] = [
    [1.0, 3.0, 5.0],
    [1 / 3, 1.0, 3.0],
    [1 / 5, 1 / 3, 1.0],
]

# ===========================================================================
# Solar thermal losses by climate zone
# ===========================================================================

SOLAR_LOSSES: Dict[str, Dict[str, float]] = {
    "tropical": {"coeff": 0.005, "ref_temp": 35.0},
    "temperate": {"coeff": 0.0035, "ref_temp": 25.0},
    "continental": {"coeff": 0.003, "ref_temp": 20.0},
}

# ===========================================================================
# AHP — Analytic Hierarchy Process (Saaty, 1980)
# ===========================================================================

AHP_RANDOM_INDEX: Dict[int, float] = {
    1: 0.00, 2: 0.00, 3: 0.58, 4: 0.90, 5: 1.12,
    6: 1.24, 7: 1.32, 8: 1.41, 9: 1.45, 10: 1.49,
    11: 1.51, 12: 1.53, 13: 1.56, 14: 1.57, 15: 1.59,
}

AHP_CR_THRESHOLD: float = 0.10

AHP_SCALE_TABLE: Dict[str, Dict[int, int]] = {
    "subtle": {0: 1, 1: 2, 2: 3, 3: 4, 4: 4, 5: 5, 6: 5, 7: 6, 8: 6},
    "moderate": {0: 1, 1: 3, 2: 4, 3: 5, 4: 6, 5: 7, 6: 7, 7: 8, 8: 9},
    "strong": {0: 1, 1: 4, 2: 6, 3: 7, 4: 8, 5: 9, 6: 9, 7: 9, 8: 9},
}

# ===========================================================================
# Capacity factor ceiling
# ===========================================================================

CF_ABS_CEILING: float = 0.60

# ===========================================================================
# ESA WorldCover land-cover classes
# ===========================================================================

ESA_CLASS_NAMES: Dict[int, str] = {
    10: "Tree cover",
    20: "Shrubland",
    30: "Grassland",
    40: "Cropland",
    50: "Built-up",
    60: "Bare / Sparse vegetation",
    70: "Snow and ice",
    80: "Permanent water bodies",
    90: "Herbaceous wetland",
    95: "Mangroves",
    100: "Moss and lichen",
}

ESA_EXCLUDED_CLASSES: set[int] = {50, 70, 80, 90, 95}
ESA_NATIVE_RESOLUTION_DEG: float = 8.333e-05

# ===========================================================================
# Geographic conversion
# ===========================================================================

KM_PER_DEG_LAT: float = 111.32

# ===========================================================================
# NoData sentinels
# ===========================================================================

NODATA_FLOAT: float = -9999.0
NODATA_INT: int = 0
NODATA_UINT8: int = 255
MASK_FILL: float = -9999.0

# ===========================================================================
# Criterion: proximity to existing power plants
# ===========================================================================

PROXIMITY_DECAY_SIGMA_KM: float = 10.0
PROXIMITY_SMOOTH_SIGMA_PX: float = 2.0

# ===========================================================================
# Criterion: protected areas (WDPA / IUCN)
# ===========================================================================

IUCN_SCORES: Dict[str, float] = {
    "ia": 0.00,
    "ib": 0.00,
    "ii": 0.10,
    "iii": 0.25,
    "iv": 0.30,
    "v": 0.45,
    "vi": 0.55,
    "not reported": 0.25,
    "not applicable": 0.45,
    "not assigned": 0.25,
    "not_reported": 0.25,
    "not_applicable": 0.45,
    "not_assigned": 0.25,
}

IUCN_SCORE_DEFAULT: float = 0.25
IUCN_FREE_SCORE: float = 1.00

# ===========================================================================
# Terrain — Topographic Roughness Index (TRI)
# ===========================================================================

TRI_THRESHOLD: float = 50.0

# ===========================================================================
# Biomass
# ===========================================================================

BIOMASS_SMOOTH_SIGMA_DEFAULT: float = 1.0

# ===========================================================================
# LCOE — Levelized Cost of Energy
# ===========================================================================

LCOE_REFERENCE_YEAR: int = 2024

LCOE_BENCHMARK_USD_MWH: Dict[str, Dict[str, float]] = {
    "solar": {"p25": 33.0, "median": 48.0, "p75": 71.0},
    "wind": {"p25": 38.0, "median": 55.0, "p75": 87.0},
    "biomass": {"p25": 71.0, "median": 98.0, "p75": 142.0},
}

# ===========================================================================
# Default technology parameters (lowest-priority fallback)
# ===========================================================================

DEFAULT_TECH_PARAMS: Dict[str, Dict] = {
    "solar": {
        "thresholds": {
            "optimistic": 0.50,
            "balanced": 0.60,
            "conservative": 0.70,
        },
        "land_use_factor": 0.20,
        "power_density_mw_km2": 40.0,
        "capacity_factor": 0.195,
        "hours_year": 8760,
        "resource_unit": "kWh/m2/yr (GHI)",
        "color": "#F9A825",
        "label": "Solar PV",
        "cmap": "YlOrRd",
    },
    "wind": {
        "thresholds": {
            "optimistic": 0.40,
            "balanced": 0.50,
            "conservative": 0.60,
        },
        "land_use_factor": 0.030,
        "power_density_mw_km2": 6.5,
        "capacity_factor": 0.274,
        "hours_year": 8760,
        "resource_unit": "W/m2 (wind power density)",
        "color": "#1565C0",
        "label": "Wind Onshore",
        "cmap": "YlGnBu",
    },
    "biomass": {
        "thresholds": {
            "optimistic": 0.40,
            "balanced": 0.50,
            "conservative": 0.60,
        },
        "land_use_factor": 0.30,
        "power_density_mw_km2": 2.0,
        "capacity_factor": 0.750,
        "hours_year": 8760,
        "resource_unit": "ton DM/ha/yr",
        "color": "#2E7D32",
        "label": "Biomass / Bioenergy",
        "cmap": "YlGn",
    },
}


# ===========================================================================
# Technology parameter builder (three-tier merge)
# ===========================================================================

def _set_nested(d: dict, dotted_key: str, value: float) -> None:
    """
    Set a value in a dict using a dotted key path.
    
    Args:
        d: Dictionary to modify
        dotted_key: Key path using dot notation (e.g., 'thresholds.balanced')
        value: Value to set
    """
    parts = dotted_key.split(".", 1)
    if len(parts) == 2:
        d[parts[0]][parts[1]] = value
    else:
        d[parts[0]] = value


def build_tech_params(
    cfg_system: Dict,
    country_params: Dict | None = None,
) -> Dict[str, Dict]:
    """
    Build the technology parameter dictionary with three-tier precedence.

    Precedence (highest to lowest):
      1. ``country_params`` — per-country values from ``parameters.json``
      2. ``cfg_system``     — global overrides from ``settings.yaml``
      3. ``DEFAULT_TECH_PARAMS`` — hardcoded scientific baseline

    This function is consumed by both PotentialCalculator (Phase 4) and
    LCOECalculator (Phase 5) to ensure consistent ``land_use_factor``,
    ``power_density``, and ``capacity_factor`` across phases.

    Args:
        cfg_system: The ConfigLoader.system dictionary (full settings.yaml)
        country_params: Per-country flat parameter dictionary from
                       ConfigLoader.get_country()

    Returns:
        Merged parameters for solar, wind, and biomass
    """
    params = copy.deepcopy(DEFAULT_TECH_PARAMS)

    techs_cfg = (
        cfg_system.get("potential", {}).get("technologies", {})
        if cfg_system
        else {}
    )

    _YAML_MAPPING = [
        ("land_use_factor", "land_use_factor"),
        ("power_density_mw_km2", "power_density_mw_km2"),
        ("capacity_factor_max", "capacity_factor"),
        ("base_threshold", "thresholds.balanced"),
    ]

    for tech in TECH_ORDER:
        tc = techs_cfg.get(tech, {})
        for src_key, dst_key in _YAML_MAPPING:
            if src_key in tc:
                _set_nested(params[tech], dst_key, float(tc[src_key]))

    if country_params:
        _COUNTRY_MAPPING: Dict[str, list] = {
            "solar": [
                ("solar_power_density_mw_km2", "power_density_mw_km2"),
                ("solar_land_use_factor", "land_use_factor"),
                ("solar_threshold", "thresholds.balanced"),
            ],
            "wind": [
                ("wind_capacity_density_mw_km2", "power_density_mw_km2"),
                ("wind_land_use_factor", "land_use_factor"),
                ("wind_threshold", "thresholds.balanced"),
            ],
            "biomass": [
                ("biomass_power_density_mw_km2", "power_density_mw_km2"),
                ("biomass_land_use_factor", "land_use_factor"),
                ("biomass_threshold", "thresholds.balanced"),
            ],
        }
        for tech, mappings in _COUNTRY_MAPPING.items():
            for src_key, dst_key in mappings:
                val = country_params.get(src_key)
                if val is not None:
                    _set_nested(params[tech], dst_key, float(val))

        for tech in TECH_ORDER:
            val = country_params.get(f"{tech}_capacity_factor")
            if val is not None:
                params[tech]["capacity_factor"] = float(val)

    scen_cfg = (cfg_system or {}).get("potential", {}).get("scenarios", {})
    opt_offset = float(
        scen_cfg.get("optimistic", {}).get("suitability_threshold", -0.10)
    )
    cons_offset = float(
        scen_cfg.get("conservative", {}).get("suitability_threshold", +0.10)
    )
    for tech in params:
        base = params[tech]["thresholds"]["balanced"]
        params[tech]["thresholds"]["optimistic"] = max(
            0.0, base + opt_offset
        )
        params[tech]["thresholds"]["conservative"] = min(
            0.99, base + cons_offset
        )

    return params