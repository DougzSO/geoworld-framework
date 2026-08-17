# schemas.py
"""
schemas.py
==========
Type-safe contracts for the GeoWorld Framework pipeline.

Autoridade única de modelos de dados — substitui models.py, que foi removido.
Todos os contratos de fases (Criteria, Suitability, Potential, LCOE, Pipeline)
vivem aqui em Pydantic v2 nativo.

Design Principles
-----------------
1. Immutability   : Modelos de configuração usam frozen=True.
2. Validation     : Restrições científicas aplicadas na construção.
3. Compatibility  : Dict-style access via __getitem__ / get() para módulos
                    legados ainda não migrados para acesso por atributo.
4. Single aliases : biomass.collection_factor == biomass.land_use_factor —
                    resolvido uma vez no ConfigLoader, nunca duplicado.
5. Persistence    : Modelos de resultado validam o que é PERSISTIDO (pickle),
                    não o dict intermediário em memória (que contém ndarray /
                    GeoDataFrame / Affine — tipos fora da fronteira Pydantic).

Hierarquia de configuração (maior → menor precedência)
------------------------------------------------------
  parameters.json (bloco do país)
    → parameters.json (abatement_defaults)
      → constants.DEFAULT_TECH_PARAMS (baseline global de último recurso)

Schemas de configuração
-----------------------
  AlignedLayers    Phase 2a — caminhos de rasters espacialmente alinhados.
  TechParams       Parâmetros compartilhados por solar e eólica.
  SolarParams      TechParams + pesos de camadas de recurso.
  BiomassParams    TechParams + collection_factor + tabela de rendimento.
  OWAParams        Pesos OWA com validação de soma e ordem.
  LCOETechParams   Parâmetros econômicos LCOE por tecnologia.
  AbatementParams  Parâmetros de abatimento GHG e substituição térmica.
  CountryParams    Contrato completo do país — substitui Dict de get_country.

Schemas de resultado (o que é persistido em pickle)
---------------------------------------------------
  CriteriaResult      Phase 2b — critérios normalizados e metadados.
  AHPWeights          Pesos AHP e razão de consistência.
  SuitabilityError    Caso de exclusão total numa tecnologia (BUG_01).
  SuitabilityStats    Estatísticas por tecnologia da Phase 3 (caso sucesso).
  SuitabilityResult   Phase 3 — resultado completo de aptidão (T1_06).
  ScenarioResult      Resultado de um cenário de potencial.
  TechPotentialResult Resultado por tecnologia da Phase 4.
  PotentialResult     Phase 4 — potencial instalável e geração.
  SupplyCurveSummary  Resumo escalar da curva de oferta (4 valores).
  LCOEStats           Estatísticas LCOE por tecnologia.
  LCOETechResult      Resultado persistido por tecnologia da Phase 5.
  LCOEResult          Phase 5 — resultado completo LCOE.
  PipelineState       Estado de execução do pipeline (fases concluídas).

Changelog
---------
  BUG_01 (fix) : SuitabilityStats tinha tif_path / map_path / weights_json
                 como obrigatórios, o que quebrava o caso de exclusão total
                 (builder retorna dict de erro sem esses campos). Criado
                 SuitabilityError como modelo separado; SuitabilityResult.techs
                 passa a ser Dict[str, Union[SuitabilityStats, SuitabilityError]].
                 Consumidores devem usar isinstance() para distinguir os casos.

  BUG_03 (fix) : CriteriaResult._n_criteria_must_match_dict usava
                 field_validator, que é frágil à reordenação de campos em
                 Pydantic v2. Migrado para model_validator(mode="after").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# ===========================================================================
# AlignedLayers — Phase 2a output
# ===========================================================================


class AlignedLayers(BaseModel):
    """
    Caminhos de rasters alinhados produzidos pelo GridAligner (Phase 2a).

    Todos os rasters compartilham CRS, resolução e extensão espacial.
    Camadas ausentes são None; consumidores devem guardar com ``if layer``.
    """

    elevation:  Optional[Path] = None
    slope:      Optional[Path] = None
    solar:      Optional[Path] = None
    wind:       Optional[Path] = None
    land_cover: Optional[Path] = None
    population: Optional[Path] = None
    roads:      Optional[Path] = None
    plants:     Optional[Path] = None
    lakes:      Optional[Path] = None
    rivers:     Optional[Path] = None
    seismic:    Optional[Path] = None
    grid:       Optional[Path] = None

    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    # Dict-style compatibility
    def __getitem__(self, key: str) -> Optional[Path]:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def to_dict(self) -> Dict[str, Optional[Path]]:
        return {f: getattr(self, f) for f in self.model_fields}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AlignedLayers":
        """Constrói a partir do dict bruto de GridAligner.run(); descarta chaves desconhecidas."""
        known = set(cls.model_fields)
        return cls(**{k: v for k, v in d.items() if k in known})


# ===========================================================================
# TechParams — parâmetros compartilhados de tecnologia
# ===========================================================================


class TechParams(BaseModel):
    """
    Parâmetros científicos compartilhados por solar FV e eólica.

    Attributes
    ----------
    land_use_factor       Fração de área apta efetivamente implantada [0–1].
    power_density_mw_km2  Capacidade instalada por área (MW/km²).
    threshold             Score mínimo TOPSIS para implantação [0–1].
    capacity_factor       Eficiência anual de geração [0–1].
    cf_ceiling            Limite superior do CF (clamp antes do LCOE) [0–1].
                          Evita hardcodes em lcoe_calculator.py (T6_15).
    cf_floor              Limite inferior do CF (clamp antes do LCOE) [0–1].
    """

    land_use_factor:      float = Field(..., ge=0.0, le=1.0)
    power_density_mw_km2: float = Field(..., gt=0.0)
    threshold:            float = Field(..., ge=0.0, le=1.0)
    capacity_factor:      float = Field(..., ge=0.0, le=1.0)
    cf_ceiling:           float = Field(default=0.95, ge=0.0, le=1.0)
    cf_floor:             float = Field(default=0.0,  ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _cf_bounds_consistent(self) -> "TechParams":
        if self.cf_floor > self.cf_ceiling:
            raise ValueError(
                f"cf_floor ({self.cf_floor}) > cf_ceiling ({self.cf_ceiling})."
            )
        return self

    model_config = {"frozen": True}


# ===========================================================================
# SolarParams — extensões solar
# ===========================================================================


class SolarParams(TechParams):
    """
    Parâmetros solar FV estendendo TechParams com pesos de camadas de recurso.

    Os três pesos (pvout, ghi, dni) devem somar 1.0. A configuração padrão
    (pvout=1, ghi=0, dni=0) usa PVOUT exclusivamente.
    """

    pvout_weight: float = Field(default=1.0, ge=0.0, le=1.0)
    ghi_weight:   float = Field(default=0.0, ge=0.0, le=1.0)
    dni_weight:   float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "SolarParams":
        total = self.pvout_weight + self.ghi_weight + self.dni_weight
        if abs(total - 1.0) > 0.01:
            raise ValueError(
                f"Solar resource weights must sum to 1.0, got {total:.3f} "
                f"(pvout={self.pvout_weight}, ghi={self.ghi_weight}, "
                f"dni={self.dni_weight})."
            )
        return self

    model_config = {"frozen": True}


# ===========================================================================
# BiomassParams — extensões biomassa
# ===========================================================================


class BiomassParams(TechParams):
    """
    Parâmetros biomassa estendendo TechParams.

    ``collection_factor`` é o alias canônico de ``land_use_factor`` no
    contexto de biomassa (fração do resíduo disponível coletada). Ambos os
    nomes são armazenados para compatibilidade com call sites legados.

    ``yield_by_land_cover`` mapeia códigos ESA WorldCover (strings) a
    rendimento em t MS/ha/ano. Classes 90 e 95 devem ser zero (conservação).
    """

    collection_factor:   float = Field(..., ge=0.0, le=1.0)
    yield_by_land_cover: Dict[str, float] = Field(default_factory=dict)

    @field_validator("yield_by_land_cover")
    @classmethod
    def _no_wetland_yield(cls, v: Dict[str, float]) -> Dict[str, float]:
        """ESA classes 90 e 95 devem ter rendimento zero (restrição de conservação)."""
        forbidden = {"90": "Herbaceous wetland", "95": "Mangroves"}
        for code, name in forbidden.items():
            if float(v.get(code, 0.0)) > 0.0:
                raise ValueError(
                    f"Biomass yield for ESA class {code} ({name}) must be "
                    f"0.0 (conservation constraint), got {v[code]}."
                )
        return v

    model_config = {"frozen": True}


# ===========================================================================
# OWAParams — pesos OWA por cenário
# ===========================================================================


class OWAParams(BaseModel):
    """
    Pesos OWA (Ordered Weighted Averaging) por cenário.

    Cada tupla tem três valores ordenados melhor→médio→pior critério.
    Pesos devem somar 1.0 e ser não-crescentes.
    """

    default_scenario: str = Field(default="balanced")
    optimistic:       Tuple[float, float, float]
    balanced:         Tuple[float, float, float]
    conservative:     Tuple[float, float, float]

    @field_validator("optimistic", "balanced", "conservative")
    @classmethod
    def _valid_weights(cls, v: Tuple[float, float, float]) -> Tuple[float, float, float]:
        total = sum(v)
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"OWA weights must sum to 1.0, got {total:.3f} for {v}.")
        if not all(v[i] >= v[i + 1] for i in range(len(v) - 1)):
            raise ValueError(f"OWA weights must be non-increasing (best→worst), got {v}.")
        return v

    def for_scenario(self, scenario: str) -> Tuple[float, float, float]:
        """Retorna a tupla de pesos para o cenário nomeado."""
        mapping = {
            "optimistic":   self.optimistic,
            "balanced":     self.balanced,
            "conservative": self.conservative,
        }
        if scenario not in mapping:
            raise KeyError(
                f"Unknown OWA scenario '{scenario}'. Valid: {list(mapping)}"
            )
        return mapping[scenario]

    model_config = {"frozen": True}


# ===========================================================================
# LCOETechParams — parâmetros econômicos LCOE
# ===========================================================================


class LCOETechParams(BaseModel):
    """
    Parâmetros econômicos para cálculo do LCOE por tecnologia.

    Attributes
    ----------
    capex_usd_kw     CAPEX (USD/kW instalado).
    opex_usd_kw_yr   OPEX anual (USD/kW/ano).
    lifetime_years   Vida econômica do projeto (anos).
    discount_rate    Taxa de desconto real (fração, ex.: 0.06 = 6%/ano).
    """

    capex_usd_kw:   float = Field(..., gt=0.0)
    opex_usd_kw_yr: float = Field(..., ge=0.0)
    lifetime_years: int   = Field(..., gt=0)
    discount_rate:  float = Field(..., gt=0.0, lt=1.0)

    model_config = {"frozen": True}


# ===========================================================================
# AbatementParams — parâmetros de abatimento GHG
# ===========================================================================


class AbatementParams(BaseModel):
    """
    Parâmetros de abatimento GHG e substituição da frota térmica.

    Semântica de penetração
    -----------------------
    penetration_factor       = META de participação renovável na grade total.
    existing_renewable_share = participação renovável atual (baseline).
    Volume substituível      = max(0, penetration - existing) * grid_total_gwh
    """

    carbon_price_usd_tco2e:  float = Field(default=75.0, ge=0.0)
    penetration_factor:       float = Field(default=0.60, ge=0.0, le=1.0)
    existing_renewable_share: float = Field(default=0.0,  ge=0.0, le=1.0)
    grid_total_gwh:           float = Field(default=0.0,  ge=0.0)
    lcoe_threshold_usd_mwh:  float = Field(default=60.0, gt=0.0)
    thermal_types:            List[str]        = Field(
        default_factory=lambda: ["coal", "gas", "oil"]
    )
    emission_factors:         Dict[str, float] = Field(
        default_factory=lambda: {"coal": 820.0, "gas": 490.0, "oil": 750.0}
    )
    thermal_cf:               Dict[str, float] = Field(
        default_factory=lambda: {"coal": 0.55, "gas": 0.45, "oil": 0.35}
    )
    thermal_marginal_cost:    Dict[str, float] = Field(
        default_factory=lambda: {"coal": 35.0, "gas": 48.0, "oil": 90.0}
    )
    thermal_capacity_mw:      Dict[str, float] = Field(default_factory=dict)

    model_config = {"frozen": True}


# ===========================================================================
# CountryParams — contrato completo do país
# ===========================================================================


class CountryParams(BaseModel):
    """
    Conjunto validado de parâmetros de um país.

    Retornado por ``ConfigLoader.get_country()`` e consumido por todas as
    fases downstream. Substitui o antigo ``Dict[str, Any]``.

    Acesso por atributo (params.solar.land_use_factor) é preferido em código
    novo. Acesso dict-style (params.get("solar_capacity_factor")) é suportado
    para compatibilidade retroativa durante a migração dos módulos.
    """

    # Identidade
    country_code: str = Field(..., min_length=3, max_length=3)
    country_name: str = Field(..., min_length=1)

    # Tecnologia
    solar:   SolarParams
    wind:    TechParams
    biomass: BiomassParams

    # MCDA
    owa: OWAParams

    # Economia
    lcoe_solar:   LCOETechParams
    lcoe_wind:    LCOETechParams
    lcoe_biomass: LCOETechParams

    # Abatimento
    abatement: AbatementParams

    # Terreno / geometria
    slope_threshold_deg:    int  = Field(..., ge=0, le=90)
    protected_as_exclusion: bool = True
    forest_as_exclusion:    bool = True
    use_mainland_only:      bool = True

    # Distâncias de critérios (km)
    river_max_dist_km:         float = Field(default=5.0,   gt=0.0)
    river_safety_buffer_km:    float = Field(default=0.5,   ge=0.0)
    river_max_dist_biomass_km: float = Field(default=30.0,  gt=0.0)
    road_max_dist_km:          float = Field(default=15.0,  gt=0.0)
    pop_density_threshold:     float = Field(default=300.0, ge=0.0)

    # Portões de exclusão rígida da Fase 3 (globais, não per-country --
    # ver Bloco 1 em docs/BACKLOG.md). lakes_exclusion deliberadamente NÃO
    # tem campo aqui: compute_lakes_exclusion() só emite {0.0, 1.0}, então
    # qualquer valor em (0, 1) é matematicamente equivalente -- não há
    # parâmetro real para migrar.
    protected_areas_threshold:  float = Field(default=0.99, ge=0.0, le=1.0)
    proximity_plants_threshold: float = Field(default=0.01, ge=0.0, le=1.0)
    slope_offset_solar_deg:     float = Field(default=5.0,  ge=0.0, le=90.0)
    slope_offset_wind_deg:      float = Field(default=10.0, ge=0.0, le=90.0)
    slope_offset_biomass_deg:   float = Field(default=20.0, ge=0.0, le=90.0)

    # Tabela de aptidão por cobertura do solo
    land_suitability: Dict[int, Dict[str, float]] = Field(default_factory=dict)

    model_config = {"frozen": True, "validate_assignment": True}

    # ------------------------------------------------------------------
    # Mapa de aliases de chave plana (construído uma vez, nível de classe)
    # ------------------------------------------------------------------

    _FLAT_KEY_MAP: Dict[str, Tuple[str, str]] = {
        # Solar
        "solar_land_use_factor":      ("solar", "land_use_factor"),
        "solar_power_density_mw_km2": ("solar", "power_density_mw_km2"),
        "solar_threshold":            ("solar", "threshold"),
        "solar_capacity_factor":      ("solar", "capacity_factor"),
        "solar_cf_ceiling":           ("solar", "cf_ceiling"),
        "solar_cf_floor":             ("solar", "cf_floor"),
        "solar_pvout_weight":         ("solar", "pvout_weight"),
        "solar_ghi_weight":           ("solar", "ghi_weight"),
        "solar_dni_weight":           ("solar", "dni_weight"),
        # Wind
        "wind_land_use_factor":         ("wind", "land_use_factor"),
        "wind_capacity_density_mw_km2": ("wind", "power_density_mw_km2"),
        "wind_power_density_mw_km2":    ("wind", "power_density_mw_km2"),
        "wind_threshold":               ("wind", "threshold"),
        "wind_capacity_factor":         ("wind", "capacity_factor"),
        "wind_cf_ceiling":              ("wind", "cf_ceiling"),
        "wind_cf_floor":                ("wind", "cf_floor"),
        # Biomass
        "biomass_land_use_factor":      ("biomass", "land_use_factor"),
        "biomass_collection_factor":    ("biomass", "collection_factor"),
        "biomass_power_density_mw_km2": ("biomass", "power_density_mw_km2"),
        "biomass_threshold":            ("biomass", "threshold"),
        "biomass_capacity_factor":      ("biomass", "capacity_factor"),
        "biomass_cf_ceiling":           ("biomass", "cf_ceiling"),
        "biomass_cf_floor":             ("biomass", "cf_floor"),
        "biomass_yield_by_land_cover":  ("biomass", "yield_by_land_cover"),
        # OWA
        "owa_default_scenario": ("owa", "default_scenario"),
        # Abatement
        "abatement_carbon_price_usd_tco2e":   ("abatement", "carbon_price_usd_tco2e"),
        "abatement_penetration_factor":        ("abatement", "penetration_factor"),
        "abatement_existing_renewable_share":  ("abatement", "existing_renewable_share"),
        "abatement_grid_total_gwh":            ("abatement", "grid_total_gwh"),
        "abatement_lcoe_threshold_usd_mwh":   ("abatement", "lcoe_threshold_usd_mwh"),
        "abatement_thermal_types":             ("abatement", "thermal_types"),
        "abatement_emission_factors":          ("abatement", "emission_factors"),
        "abatement_thermal_cf":                ("abatement", "thermal_cf"),
        "abatement_thermal_marginal_cost":     ("abatement", "thermal_marginal_cost"),
        "abatement_thermal_capacity_mw":       ("abatement", "thermal_capacity_mw"),
    }

    # ------------------------------------------------------------------
    # Acesso dict-style (compatibilidade retroativa)
    # ------------------------------------------------------------------

    def __getitem__(self, key: str) -> Any:
        if key in self.model_fields:
            return getattr(self, key)
        if key in self._FLAT_KEY_MAP:
            obj_name, attr_name = self._FLAT_KEY_MAP[key]
            return getattr(getattr(self, obj_name), attr_name)
        if key.startswith("owa_scenario_"):
            scenario = key[len("owa_scenario_"):]
            weights = getattr(self.owa, scenario, None)
            if weights is not None:
                return ",".join(str(w) for w in weights)
        if key == "lcoe_country_params":
            return {
                "solar":   self.lcoe_solar.model_dump(),
                "wind":    self.lcoe_wind.model_dump(),
                "biomass": self.lcoe_biomass.model_dump(),
            }
        if key == "_owa_weights":
            return {
                "optimistic":   self.owa.optimistic,
                "balanced":     self.owa.balanced,
                "conservative": self.owa.conservative,
            }
        if key == "_biomass_yields":
            return {int(k): v for k, v in self.biomass.yield_by_land_cover.items()}
        if key == "_land_suitability":
            return self.land_suitability
        raise KeyError(f"Key '{key}' not found in CountryParams.")

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: str) -> bool:
        try:
            self[key]
            return True
        except KeyError:
            return False

    def keys(self):
        yield from self.model_fields
        yield from self._FLAT_KEY_MAP
        yield from (f"owa_scenario_{s}" for s in ("optimistic", "balanced", "conservative"))
        yield from ("lcoe_country_params", "_owa_weights", "_biomass_yields", "_land_suitability")

    def to_flat_dict(self) -> Dict[str, Any]:
        """Converte para o formato flat dict do legado get_country()."""
        return {key: self[key] for key in self.keys()}


# ===========================================================================
# Schemas de resultado — Phase 2b (Criteria)
# ===========================================================================


class CriteriaResult(BaseModel):
    """
    Contrato validado do artefato persistido pela Phase 2b (CriteriaBuilder).

    Valida o ``result_data`` que CriteriaBuilder.run() entrega ao
    artifact_mgr.save_result(). NÃO valida o dict em memória com numpy arrays.

    ``criteria``  →  Dict[str, np.ndarray]; tipado como Any pois Pydantic não
                     valida arrays numpy. Pickle lida com a serialização.
    ``fig_dir``   →  Diretório de PNGs. Nomeado fig_dir (não png_dir) para
                     corresponder ao caminho base / "figures" do CriteriaBuilder.

    Invariante: n_criteria == len(criteria), validado em construção.

    Changelog
    ---------
    BUG_03 (fix): Migrado de field_validator para model_validator(mode="after")
                  para garantir que a validação cruzada funcione independentemente
                  da ordem de declaração dos campos.
    """

    country_code: str
    criteria:     Dict[str, Any]   # name → np.ndarray; pickle only
    tif_dir:      Path
    fig_dir:      Path
    report_dir:   Path
    n_criteria:   int
    timestamp:    str

    # BUG_03: model_validator garante acesso a todos os campos,
    # independentemente da ordem de declaração.
    @model_validator(mode="after")
    def _n_criteria_must_match_dict(self) -> "CriteriaResult":
        if self.n_criteria != len(self.criteria):
            raise ValueError(
                f"n_criteria={self.n_criteria} does not match "
                f"len(criteria)={len(self.criteria)}. "
                "Compute n_criteria from the final dict, not an intermediate count."
            )
        return self

    model_config = {"arbitrary_types_allowed": True}


# ===========================================================================
# Schemas de resultado — Phase 3 (Suitability) — T1_06
# ===========================================================================


class AHPWeights(BaseModel):
    """
    Pesos AHP com razão de consistência para uma tecnologia e intensidade.

    Usado em SuitabilityStats; não persistido isoladamente.
    """
    tech:       str
    intensity:  str
    lambda_max: float
    ci:         float
    cr:         float
    cr_ok:      bool
    weights:    Dict[str, float]


class SuitabilityError(BaseModel):
    """
    Regista o caso de exclusão total para uma tecnologia na Phase 3.

    BUG_01 (fix): antes desta classe, SuitabilityStats tinha tif_path /
    map_path / weights_json como obrigatórios. O builder pode retornar um
    dict de erro sem esses campos (quando todos os pixels são excluídos por
    restrições). Tentar construir SuitabilityStats a partir desse dict lançava
    ValidationError silencioso em runtime.

    Uso
    ---
    Em SuitabilityResult.techs, cada valor pode ser SuitabilityStats (sucesso)
    ou SuitabilityError (falha). Consumidores devem distinguir com isinstance():

        for tech, result in suitability_result.techs.items():
            if isinstance(result, SuitabilityError):
                logger.warning("Tech %s excluded: %s", tech, result.reason)
                continue
            # resultado válido: result.tif_path garantido

    O campo ``kind`` é um discriminador literal que permite ao Pydantic
    desserializar o Union corretamente sem ambiguidade.
    """
    kind:    Literal["error"] = "error"
    tech:    str
    reason:  str
    weights: Optional[Dict[str, float]] = None

    model_config = {"frozen": True}


class SuitabilityStats(BaseModel):
    """
    Estatísticas de aptidão para uma única tecnologia (caso sucesso).

    Parte de SuitabilityResult.techs[tech] — descreve a distribuição espacial
    do score TOPSIS final, mas NÃO contém o array (descartado antes da
    persistência para economizar espaço; apenas o GeoTIFF é mantido).

    O campo ``kind`` é um discriminador literal que permite ao Pydantic
    desserializar o Union[SuitabilityStats, SuitabilityError] corretamente.

    BUG_01 (fix): tif_path / map_path / weights_json são obrigatórios aqui
    (caso sucesso), mas a distinção é feita no nível do Union — SuitabilityError
    é usado quando esses caminhos não existem.
    """
    kind:         Literal["ok"] = "ok"
    n_total:      int
    n_excluded:   int
    n_valid:      int
    pct_excluded: float
    mean:         float
    std:          float
    p10:          float
    p50:          float
    p90:          float
    pct_high:     float
    CR:           float
    cr_ok:        bool
    tif_path:     Path
    map_path:     Path
    weights_json: Path

    @model_validator(mode="after")
    def _n_valid_consistency(self) -> "SuitabilityStats":
        if self.n_valid + self.n_excluded > self.n_total:
            raise ValueError(
                f"n_valid({self.n_valid}) + n_excluded({self.n_excluded}) "
                f"> n_total({self.n_total})"
            )
        return self

    model_config = {"frozen": True}


# Tipo anotado para consumidores — distingue sucesso de falha sem isinstance nu.
SuitabilityOutcome = Union[SuitabilityStats, SuitabilityError]


class SuitabilityResult(BaseModel):
    """
    Phase 3 — resultado completo de aptidão (apenas paths e metadados).

    T1_06: Modelo adicionado para validar a saída de SuitabilityBuilder.run().

    Arrays numpy NÃO são persistidos neste modelo (são descartados antes da
    serialização para economizar espaço). O raster completo vive em
    SuitabilityStats.tif_path; este modelo contém apenas estatísticas escalares
    e metadados de rastreabilidade.

    BUG_01 (fix): techs agora é Dict[str, SuitabilityOutcome] em vez de
    Dict[str, SuitabilityStats]. Tecnologias com exclusão total são registadas
    como SuitabilityError em vez de causarem ValidationError.

    Helpers
    -------
    successful_techs  →  Dict[str, SuitabilityStats] (apenas sucessos)
    failed_techs      →  Dict[str, SuitabilityError] (apenas falhas)
    n_successful      →  int
    n_failed          →  int
    """
    country_code:  str
    techs:         Dict[str, SuitabilityOutcome]
    timestamp:     str
    elapsed_total: float

    @property
    def successful_techs(self) -> Dict[str, SuitabilityStats]:
        """Retorna apenas as tecnologias com resultado válido."""
        return {
            t: r for t, r in self.techs.items()
            if isinstance(r, SuitabilityStats)
        }

    @property
    def failed_techs(self) -> Dict[str, SuitabilityError]:
        """Retorna apenas as tecnologias com exclusão total."""
        return {
            t: r for t, r in self.techs.items()
            if isinstance(r, SuitabilityError)
        }

    @property
    def n_successful(self) -> int:
        return len(self.successful_techs)

    @property
    def n_failed(self) -> int:
        return len(self.failed_techs)


# ===========================================================================
# Schemas de resultado — Phase 4 (Potential)
# ===========================================================================


class ScenarioResult(BaseModel):
    """Resultado de um cenário de potencial (optimistic/balanced/conservative)."""
    threshold:       float
    n_pixels:        int
    area_km2:        float
    area_eff_km2:    float
    capacity_mw:     float
    capacity_gw:     float
    generation_gwh:  float
    generation_twh:  float


class TechPotentialResult(BaseModel):
    """Resultado por tecnologia da Phase 4."""
    tech:      str
    label:     str
    scenarios: Dict[str, ScenarioResult]
    params:    Dict[str, Any]


class PotentialResult(BaseModel):
    """Phase 4 — potencial instalável e geração."""
    country_code:  str
    timestamp:     str
    techs:         Dict[str, TechPotentialResult]
    elapsed_total: float
    timings:       Dict[str, float]


# ===========================================================================
# Schemas de resultado — Phase 5 (LCOE)
# ===========================================================================


class SupplyCurveSummary(BaseModel):
    """
    Resumo escalar da curva de oferta, espelhando exatamente
    tech_serial["supply_curve_summary"] em LCOECalculator.run().
    Omitido (None) quando supply_curve.empty == True.
    """
    n_points:        int
    min_lcoe:        float
    max_lcoe:        float
    max_capacity_gw: float

    @field_validator("max_lcoe")
    @classmethod
    def _max_gte_min(cls, v: float, info: Any) -> float:
        min_lcoe = (info.data or {}).get("min_lcoe")
        if min_lcoe is not None and v < min_lcoe:
            raise ValueError(f"max_lcoe({v}) < min_lcoe({min_lcoe})")
        return v


class LCOEStats(BaseModel):
    """
    Estatísticas LCOE por tecnologia.

    Dois formatos possíveis dependendo de lcoe_valid.size > 0:

    Caminho sucesso (11 chaves)
        base_lcoe, mean, p10, p25, median, p75, p90, n_pixels, crf,
        base_cf, source.

    Caminho degenerado (todos os pixels NaN — 3 chaves)
        Apenas crf, base_cf, source.

    Campos de distribuição são Optional para forçar consumidores a tratar
    o caso degenerado explicitamente.

    p25/median/p75 (BLOCKER-002): computed directly from the same
    lcoe_valid pixel population as mean/p10/p90 — not approximated from
    p10/p90 interpolation or derived from a coarser population.
    """
    crf:      float
    base_cf:  float
    source:   str   # "resource_tif" | "suitability_proxy" | "suitability_proxy (resource constant)"

    base_lcoe: Optional[float] = None
    mean:      Optional[float] = None
    p10:       Optional[float] = None
    p25:       Optional[float] = None
    median:    Optional[float] = None
    p75:       Optional[float] = None
    p90:       Optional[float] = None
    n_pixels:  Optional[int]   = None

    @property
    def is_degenerate(self) -> bool:
        """True quando todos os pixels LCOE eram NaN (falha de cobertura)."""
        return self.n_pixels is None


class LCOETechResult(BaseModel):
    """
    Resultado persistido por tecnologia, espelhando tech_serial dentro de
    serializable_results["techs"][tech] em LCOECalculator.run().

    Campos omitidos intencionalmente (não persistidos):
      lcoe_map     (np.ndarray) — descartado antes da persistência.
      supply_curve (pd.DataFrame) — substituído por supply_curve_summary.

    ``transform`` é uma 6-tupla de floats (a, b, c, d, e, f) — serialização
    padrão de rasterio.Affine. O validator aceita tanto o objeto Affine quanto
    uma sequência de 6 elementos.

    TODO (Lote 2): Mover a conversão Affine → tuple para lcoe_calculator.py
                   antes de chamar save_result(). Schemas não devem fazer
                   transformações, apenas validar tipos esperados.
    """
    tech:                  str
    label:                 str
    params:                Dict[str, Any]
    stats:                 LCOEStats
    transform:             Optional[Tuple[float, float, float, float, float, float]] = None
    crs:                   Optional[str] = None
    supply_curve_summary:  Optional[SupplyCurveSummary] = None

    @field_validator("transform", mode="before")
    @classmethod
    def _coerce_affine(cls, v: Any) -> Any:
        """
        Aceita rasterio.Affine e converte para tuple (temporário).

        TODO (Lote 2): A conversão deveria ocorrer no lcoe_calculator.py
                       antes da serialização, não no validator do schema.
        """
        if v is None:
            return None
        if hasattr(v, "a") and hasattr(v, "f"):   # rasterio.Affine
            return (v.a, v.b, v.c, v.d, v.e, v.f)
        return tuple(v)


class LCOEResult(BaseModel):
    """
    Resultado persistido da Phase 5 (LCOECalculator).

    Divergência de nomenclatura preservada: o código-fonte usa a chave
    "country" (não "country_code") em serializable_results.
    """
    country_code:  str = Field(alias="country")
    timestamp:     str
    techs:         Dict[str, LCOETechResult]
    elapsed_total: float

    model_config = {"populate_by_name": True}


# ===========================================================================
# PipelineState — estado de execução
# ===========================================================================


class PipelineState(BaseModel):
    """Estado de execução do pipeline (fases concluídas + manifests)."""
    country_code:     str
    phases:           Dict[str, Dict[str, Any]] = {}
    last_updated:     str
    pipeline_version: str = "1.0"