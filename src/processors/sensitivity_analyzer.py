"""
sensitivity_analyzer.py — Phase 8: Sensitivity Analysis
==========================================================
Sensitivity analysis for the GeoWorld pipeline targeting Q1 publications.

SA-1 · OAT Weight Sensitivity: Perturbs AHP weights (±10 to 30%). Spearman ρ.
SA-2 · Monte Carlo AHP: Dirichlet distributions on weights (decision robustness
       under the framework's real scenario threshold -- see METRIC_013 below).
SA-3 · Threshold Sweep: Area elasticity vs. spatial suitability constraints.
SA-4 · LCOE Uncertainty: Triangular Monte Carlo for CAPEX, OPEX, CF.
SA-5 · Sobol Global Sensitivity: GHG Abatement indices (S1 and ST).
SA-6 · Potential Sensitivity: Parameter elasticities (Power Density, CF).

References:
  - Saltelli et al. (2008). Global Sensitivity Analysis.
  - Malczewski (1999). GIS and Multicriteria Decision Analysis.

Changelog
---------
  BUG_02 (fix) : Matching de tecnologias em _load_suitability_from_disk e
                 _load_weights_from_disk usava `in` (substring), o que tornava
                 o matching ambíguo se um nome de tecnologia fosse substring de
                 outro (ex: "wind" dentro de "windoffshore"). Corrigido para
                 comparação exata por stem do ficheiro.

  BUG_05 (fix) : _lcoe_params_for_tech acessava country_params.lcoe, atributo
                 inexistente em CountryParams. Corrigido para aceder
                 country_params.lcoe_solar / lcoe_wind / lcoe_biomass
                 diretamente, com fallback para cfg dict.

  IMM_09 (fix) : Ambos os métodos de carregamento de pesos já usavam rglob
                 (parcialmente corrigido em sessão anterior). O matching exato
                 por stem completa a correção.

  DUP_21 (ref) : Refatorado _format_report para usar reporting.build_phase_report.
                 Substitui formatação manual (~80 linhas) por estrutura
                 hierárquica com ReportSection e subsections.

  BUG_06 (fix) : _load_suitability_from_disk localizava o TIF de suitability
                 via rglob(f"*{tech}_suitability*.tif"), wildcard que também
                 casa com os GeoTIFFs OWA ({code}_{tech}_suitability_owa_
                 {scenario}.tif). Em produção (ver PRT), next() sobre esse
                 glob pode devolver um raster OWA em vez do TOPSIS esperado,
                 dependendo da ordem de iteração do filesystem. Corrigido
                 para nome exato esperado, com fallback por stem exato.

  METRIC_013 (change): SA-2's stable_fraction (fraction of pixels whose 90%
                 CI band on the raw TOPSIS score is narrower than 0.10)
                 replaced with a threshold-crossing metric: among pixels apt
                 under the base AHP weights, what fraction of the 1000
                 Dirichlet weight samples keep that pixel above the real
                 scenario threshold. A prototype (scratchpad/
                 threshold_crossing_prototype.py, validated against the
                 real pipeline's own logged stable_fraction numbers before
                 trusting the new metric) showed stable_fraction couldn't
                 distinguish PRT from BRA on wind (4.4% vs. 4.3%) while the
                 threshold-crossing metric revealed a real inversion (PRT
                 wind: 72.1% of apt pixels in the 25-75% boundary zone,
                 barely 2.3% decisive; BRA wind: 53.1% decisive). The old
                 metric answered "is the score numerically stable"; the
                 framework's actual output is a threshold-based apt/not-apt
                 decision, which is what this metric now measures directly.

  DOC_01 (fix) : This module docstring had SA-5/SA-6 swapped (labeled SA-5
                 as potential-parameter sensitivity and SA-6 as Sobol) --
                 the actual functions (sa5_sobol_ghg, sa6_potential_
                 sensitivity) and docs/memory/04-algorithms.md always had it
                 the other way around. Corrected here; no code affected.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import rasterio

from src.core.schemas import CountryParams
from src.utils.map_styling import GeoWorldStyler
from src.utils.raster_io import find_suitability_tif
from src.utils.reporting import ReportSection, build_phase_report
from src.utils.sensitivity_math import (
    _balanced_threshold,
    _build_ghg_function_from_abatement,
    _sfmt,
    sa1_oat_weight_sensitivity,
    sa2_monte_carlo_weights,
    sa3_threshold_sweep,
    sa4_lcoe_uncertainty,
    sa5_sobol_ghg,
    sa6_potential_sensitivity,
)

# sensitivity_plots is a first-party, version-controlled sibling module, not
# an optional dependency -- an ImportError here means the package is broken,
# not a runtime condition to degrade gracefully from. The previous
# try/except silently set every plot_* function to None on failure, which
# meant a broken import would surface later as a confusing per-SA "failed"
# log line (plot_sa1_tornado(...) raising TypeError: 'NoneType' object is
# not callable) instead of a clear import error at startup. Fail fast.
from src.utils.sensitivity_plots import (
    TECH_LABEL,
    plot_sa1_tornado,
    plot_sa1_heatmap,
    plot_sa2_cv,
    plot_sa3_threshold,
    plot_sa4_lcoe,
    plot_sa5_sobol,
    plot_sa6_potential,
    plot_dashboard,
)

logger = logging.getLogger("geoworld.processors.SensitivityAnalyzer")

# ── SA-4 fallback defaults (used only when CountryParams has no LCOE config) ─
_SA4_DEFAULTS: Dict[str, Dict[str, float]] = {
    "solar":   {"capex": 850,  "opex": 15,  "life": 25, "dr": 0.06, "cf": 0.18},
    "wind":    {"capex": 1400, "opex": 40,  "life": 25, "dr": 0.06, "cf": 0.28},
    "biomass": {"capex": 2500, "opex": 100, "life": 30, "dr": 0.07, "cf": 0.75},
}

# ── Mapa canônico: tech → atributo LCOETechParams em CountryParams ──────────
# Evita strings hardcoded em _lcoe_params_for_tech (BUG_05).
_LCOE_ATTR: Dict[str, str] = {
    "solar":   "lcoe_solar",
    "wind":    "lcoe_wind",
    "biomass": "lcoe_biomass",
}


# TOPSIS core, data loaders, and SA-1 through SA-6 (pure functions, no
# SensitivityAnalyzer instance state) now live in src/utils/sensitivity_math.py
# -- imported above. See that module's docstring for the extraction rationale.


class SensitivityAnalyzer:
    """Orchestrates Phase 8 evaluations (SA-1 to SA-6) with unified reporting."""

    def __init__(self, cfg: Any, outputs_dir: Path) -> None:
        self.cfg         = cfg
        self.outputs_dir = Path(outputs_dir)
        self.styler = GeoWorldStyler(
            cfg.system.get("visualization", {}),
            global_dpi=cfg.system.get("pipeline", {}).get("map_dpi_export", 150),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _lcoe_params_for_tech(
        self,
        tech: str,
        country_params: Optional[CountryParams],
    ) -> Dict[str, float]:
        """
        Return LCOE parameters for *tech*, preferring CountryParams.

        BUG_05 (fix): A versão anterior acessava country_params.lcoe (dict),
        que não existe em CountryParams. O acesso correto é via os atributos
        tipados lcoe_solar / lcoe_wind / lcoe_biomass (LCOETechParams).
        """
        fallback = dict(_SA4_DEFAULTS.get(tech, _SA4_DEFAULTS["solar"]))

        if country_params is not None:
            # Acesso direto ao atributo tipado (LCOETechParams)
            lcoe_attr = _LCOE_ATTR.get(tech)
            if lcoe_attr:
                try:
                    tech_lcoe = getattr(country_params, lcoe_attr, None)
                    if tech_lcoe is not None:
                        # capacity_factor vem do TechParams da tecnologia,
                        # não do LCOETechParams (que não tem esse campo).
                        tech_obj = getattr(country_params, tech, None)
                        cf = (
                            float(tech_obj.capacity_factor)
                            if tech_obj is not None
                            else fallback["cf"]
                        )
                        return {
                            "capex": float(tech_lcoe.capex_usd_kw),
                            "opex":  float(tech_lcoe.opex_usd_kw_yr),
                            "life":  int(tech_lcoe.lifetime_years),
                            "dr":    float(tech_lcoe.discount_rate),
                            "cf":    cf,
                        }
                except Exception as exc:
                    logger.debug(
                        "[_lcoe_params_for_tech] CountryParams access failed "
                        "for tech=%s: %s", tech, exc,
                    )

        # Legacy cfg dict (fallback quando CountryParams não disponível)
        try:
            lc = (
                self.cfg.system
                .get("lcoe", {})
                .get("technologies", {})
                .get(tech, {})
            )
            if lc:
                return {
                    "capex": float(lc.get("capex_usd_kw",
                                          lc.get("base_capex_usd_kw", fallback["capex"]))),
                    "opex":  float(lc.get("opex_usd_kw_yr",
                                          lc.get("base_opex_usd_kw_yr", fallback["opex"]))),
                    "life":  int(  lc.get("lifetime_years",   fallback["life"])),
                    "dr":    float(lc.get("discount_rate",    fallback["dr"])),
                    "cf":    float(lc.get("capacity_factor",  fallback["cf"])),
                }
        except Exception:
            pass

        logger.debug(
            "[_lcoe_params_for_tech] Using hardcoded defaults for tech=%s.", tech
        )
        return fallback

    def _resolve_tech_params(
        self,
        tech: str,
        country_params: Optional[CountryParams],
        pot_results: Any,
    ) -> Tuple[float, float, float, float]:
        """Resolve (power_density, land_use_factor, capacity_factor, threshold).

        `threshold` is always sourced from Phase 4's persisted balanced-
        scenario result via `_balanced_threshold()` (BLOCKER-017 fix) --
        `CountryParams` has no `suitability_threshold` attribute, so the
        previous inline lookup silently fell back to a hardcoded default
        on every call, before the (correct) `pot_results`-derived value
        below was ever reached. `power_density`/`land_use_factor`/
        `capacity_factor` resolution is unchanged.
        """
        tp    = self.cfg.system.get("potential", {}).get("technologies", {}).get(tech, {})
        pd_mw = float(tp.get("power_density_mw_km2",  30.0))
        luf   = float(tp.get("land_use_factor",        0.20))
        cf    = float(tp.get("capacity_factor_max",    0.22))

        if country_params is not None:
            try:
                tech_obj = getattr(country_params, tech, None)
                if tech_obj is not None:
                    pd_mw = float(getattr(tech_obj, "power_density_mw_km2", pd_mw))
                    luf   = float(getattr(tech_obj, "land_use_factor",       luf))
                    cf    = float(getattr(tech_obj, "capacity_factor",        cf))
                    return pd_mw, luf, cf, _balanced_threshold(tech, pot_results)
            except AttributeError:
                pass

        if pot_results is not None:
            try:
                if hasattr(pot_results, "techs"):
                    tech_obj = pot_results.techs.get(tech)
                elif isinstance(pot_results, dict):
                    tech_obj = pot_results.get("techs", {}).get(tech)
                else:
                    tech_obj = None
                if tech_obj is not None:
                    pp = (
                        tech_obj.params if hasattr(tech_obj, "params")
                        else tech_obj.get("params", {})
                    )
                    if pp:
                        pd_mw = float(pp.get("power_density_mw_km2", pd_mw))
                        luf   = float(pp.get("land_use_factor",       luf))
                        cf    = float(pp.get("capacity_factor",        cf))
            except Exception:
                pass

        return pd_mw, luf, cf, _balanced_threshold(tech, pot_results)

    @staticmethod
    def _match_tech_from_stem(stem: str, country_code: str) -> Optional[str]:
        """
        Determina a tecnologia a partir do stem do ficheiro de pesos.

        BUG_02 (fix): A versão anterior usava `in` para matching de substring,
        o que tornava o matching ambíguo se um nome de tecnologia fosse
        substring de outro (ex: "wind" dentro de "windoffshore").

        Esta implementação usa igualdade exata no stem completo, tentando
        primeiro o padrão com country_code (ex: "PRT_solar_weights") e depois
        o padrão sem prefixo (ex: "solar_weights"). Retorna None se não
        reconhecer nenhuma tecnologia.

        Args:
            stem:         Stem do ficheiro (nome sem extensão).
            country_code: Código ISO-3 do país (ex: "PRT").

        Returns:
            Nome canônico da tecnologia ("solar", "wind", "biomass") ou None.
        """
        for tech in ("solar", "wind", "biomass"):
            if stem == f"{country_code}_{tech}_weights" or stem == f"{tech}_weights":
                return tech
        return None

    def _load_suitability_from_disk(
        self,
        country_code: str,
        criteria_dir: Path,
    ) -> Dict[str, Any]:
        """
        Load suitability results from disk when Phase 3 was previously executed.

        Uses recursive search (rglob) for both TIFs and JSON files under the
        suitability/ directory, so it works regardless of subfolder layout.

        BUG_02 (fix): matching de tecnologias agora usa _match_tech_from_stem,
        que compara o stem completo do ficheiro — sem ambiguidade de substring.
        IMM_09 (fix): rglob já estava presente; o matching exato completa a correção.
        """
        techs: Dict[str, Any] = {}
        suitability_dir = self.outputs_dir / country_code / "suitability"

        # BLOCKER-017/018 item 4 (docs/BACKLOG.md): reuse _load_weights_from_disk()
        # instead of duplicating its rglob + parsing logic here. Confirmed this is
        # NOT the BUG_07/BLOCKER-010 pattern (preferring disk over a live object
        # that already has the answer) -- SuitabilityStats never carries weights
        # in memory, only a weights_json path, so disk is the only real source
        # either way; the two loaders were just independently re-implementing the
        # same read.
        weights_by_tech: Dict[str, Dict[str, float]] = self._load_weights_from_disk(
            country_code
        )

        for tech in ("solar", "wind", "biomass"):
            # 1. Localizar TIF de suitability — resolver centralizado
            # (raster_io.find_suitability_tif, BLOCKER-006), que tenta o
            # nome exato do TOPSIS primeiro e cai para um rglob por stem
            # exato (nunca um wildcard `*suitability*` ambíguo que também
            # casaria com os GeoTIFFs OWA — mesma classe de bug que BUG_02
            # corrigiu para os ficheiros de pesos). allow_owa_fallback=False
            # porque este carregamento de pesos nunca usou OWA.
            suit_tif = find_suitability_tif(
                suitability_dir, tech, country_code, allow_owa_fallback=False
            )
            if suit_tif is None:
                logger.debug("[%s] Suitability TIF not found.", tech)
                continue

            logger.info(
                "[%s] TIF found: %s", tech,
                suit_tif.relative_to(self.outputs_dir),
            )

            # 2. Pesos já carregados por _load_weights_from_disk() acima.
            weights: Dict[str, float] = dict(weights_by_tech.get(tech, {}))

            # 3. Fallback: pesos iguais a partir dos critérios disponíveis
            if not weights:
                logger.info(
                    "[%s] Weights JSON not found — inferring criteria from %s",
                    tech, criteria_dir,
                )
                crit_names: List[str] = []
                for tif_path in sorted(criteria_dir.glob("*.tif")):
                    name = tif_path.stem
                    for prefix in (
                        f"{country_code}_{tech}_",
                        f"{country_code}_",
                        f"{tech}_",
                    ):
                        if name.startswith(prefix):
                            name = name[len(prefix):]
                            break
                    crit_names.append(name)
                if crit_names:
                    eq = 1.0 / len(crit_names)
                    weights = {n: eq for n in crit_names}
                    logger.warning(
                        "[%s] Using %d criteria with equal weights (fallback).",
                        tech, len(weights),
                    )

            if weights:
                techs[tech] = {"weights": weights, "error": False}
            else:
                logger.warning("[%s] No weights available — skipping technology.", tech)

        if not techs:
            logger.warning("No technologies found with valid data on disk.")
            return {}

        return {"techs": techs}

    def _load_weights_from_disk(
        self,
        country_code: str,
    ) -> Dict[str, Dict[str, float]]:
        """
        Carrega os pesos AHP do disco para cada tecnologia.

        BUG_02 (fix): matching agora usa _match_tech_from_stem (exato),
        eliminando o risco de ambiguidade por substring entre tecnologias.

        Returns:
            Dict[tech_name, weights_dict]
        """
        weights_by_tech: Dict[str, Dict[str, float]] = {}
        suitability_dir = self.outputs_dir / country_code / "suitability"

        for wf in suitability_dir.rglob("*_weights.json"):
            tech = self._match_tech_from_stem(wf.stem, country_code)
            if tech is None or tech in weights_by_tech:
                # Ignora ficheiros não reconhecidos; primeira ocorrência vence.
                continue
            try:
                raw = json.loads(wf.read_text(encoding="utf-8"))
                if "weights" in raw and isinstance(raw["weights"], dict):
                    weights = {str(k): float(v) for k, v in raw["weights"].items()}
                else:
                    weights = {
                        str(k): float(v) for k, v in raw.items()
                        if isinstance(v, (int, float))
                    }
                if weights:
                    weights_by_tech[tech] = weights
                    logger.info(
                        "[%s] Weights loaded from %s (%d criteria)",
                        tech, wf.name, len(weights),
                    )
                else:
                    logger.warning(
                        "[%s] No valid weights found in %s", tech, wf.name
                    )
            except Exception as exc:
                logger.warning(
                    "[%s] Failed to read weights JSON %s: %s", tech, wf.name, exc
                )

        return weights_by_tech

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        country_code: str,
        suitability_results: Optional[Dict[str, Any]],
        criteria_dir: Path,
        country_name: str = "",
        country_params: Optional[CountryParams] = None,
        pot_results: Any = None,
        abat_result: Optional[Dict[str, Any]] = None,
        run_sa1: bool = True,
        run_sa2: bool = True,
        run_sa3: bool = True,
        run_sa4: bool = True,
        run_sa5: bool = False,
        run_sa6: bool = True,
        n_mc_samples: int = 1000,
        sa5_ghg_function: Optional[Callable] = None,
        sa5_base_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute sensitivity analysis suite (SA-1 to SA-6).

        Args:
            country_code:        ISO-3166 alpha-3 country code.
            suitability_results: Phase 3 output (Pydantic model or dict).
            criteria_dir:        Directory with normalized criteria TIFs.
            country_name:        Human-readable country name for reports.
            country_params:      Typed CountryParams schema instance.
            pot_results:         Phase 4 output (PotentialResult or dict).
            abat_result:         Phase 7 output dict (for SA-5 GHG function).
            run_sa1..run_sa6:    Flags to enable/disable individual SA modules.
            n_mc_samples:        Monte Carlo sample count (SA-2 and SA-4).
            sa5_ghg_function:    Pre-built GHG function (overrides auto-build).
            sa5_base_params:     Parameter bounds for SA-5 Sobol.

        Returns:
            Nested dict: {tech: {sa1: {...}, sa2: {...}, ...}, sa5_sobol: {...}}.
        """
        # ── 0. Normalize suitability_results ─────────────────────────────
        if suitability_results is not None:
            if hasattr(suitability_results, "model_dump"):
                suitability_results = suitability_results.model_dump()
            elif hasattr(suitability_results, "dict"):
                suitability_results = suitability_results.dict()
            elif not isinstance(suitability_results, dict):
                try:
                    suitability_results = dict(suitability_results)
                except Exception:
                    suitability_results = {}
        else:
            suitability_results = {}

        # ── ENRICH: Load weights from disk (always) ──────────────────────
        weights_from_disk = self._load_weights_from_disk(country_code)
        if weights_from_disk:
            techs = suitability_results.get("techs", {})
            for tech, wdict in weights_from_disk.items():
                if tech not in techs:
                    techs[tech] = {}
                techs[tech]["weights"] = wdict
            suitability_results["techs"] = techs
            logger.info(
                "Weights enriched from disk for %d technologies.",
                len(weights_from_disk),
            )
        else:
            logger.warning("No weights found on disk for any technology.")

        # ── 1. Validate inputs ────────────────────────────────────────────
        criteria_dir = Path(criteria_dir)
        if not criteria_dir.exists():
            logger.error("Criteria directory not found: %s", criteria_dir)
            return {}

        tifs = sorted(criteria_dir.glob("*.tif"))
        if tifs:
            logger.info("  Input criteria: %d TIF files found", len(tifs))
            for tif in tifs[:5]:
                logger.info("    - %s", tif.name)
            if len(tifs) > 5:
                logger.info("    ... and %d more", len(tifs) - 5)
        else:
            logger.warning("  No criteria TIF files found in %s", criteria_dir)

        # Log CountryParams origin for traceability
        if country_params is not None:
            try:
                solar_cf = country_params.solar.capacity_factor
                wind_cf  = country_params.wind.capacity_factor
                bio_cf   = country_params.biomass.capacity_factor
            except AttributeError:
                solar_cf = wind_cf = bio_cf = 0.0
            logger.info(
                "  [CountryParams] Loaded for %s — "
                "solar CF=%.3f | wind CF=%.3f | biomass CF=%.3f",
                country_params.country_code, solar_cf, wind_cf, bio_cf,
            )
        else:
            logger.warning(
                "  [CountryParams] Not provided — "
                "falling back to cfg.system dict for all parameters."
            )

        out_dir = self.outputs_dir / country_code / "sensitivity"
        out_dir.mkdir(parents=True, exist_ok=True)

        logger.info("=" * 62)
        logger.info("SENSITIVITY ANALYZER — Phase 8 (%s)", country_code)
        logger.info("Output: %s", out_dir)
        logger.info("=" * 62)

        results_sa: Dict[str, Any] = {}
        t0_total = time.perf_counter()

        # ── 2. Load suitability from disk if not in memory ────────────────
        if not suitability_results or not suitability_results.get("techs"):
            logger.info(
                "Suitability results not available in memory. "
                "Attempting to load from disk..."
            )
            suitability_results = self._load_suitability_from_disk(
                country_code, criteria_dir
            )
            if not suitability_results or not suitability_results.get("techs"):
                logger.error(
                    "No suitability data found. "
                    "Run Phase 3 (SuitabilityBuilder) first."
                )
                return {}

        # ── 3. Reference grid ─────────────────────────────────────────────
        ref_tif = next(criteria_dir.glob("*.tif"), None)
        if not ref_tif:
            logger.error(
                "No criteria TIF found. Run Phase 2b (CriteriaBuilder) first."
            )
            return {}

        with rasterio.open(str(ref_tif)) as src:
            height    = src.height
            width     = src.width
            transform = src.transform

        logger.info("Reference grid: %d×%d pixels", height, width)
        logger.info(
            "Technologies to analyze: %s",
            list(suitability_results.get("techs", {}).keys()),
        )

        # ── 4. SA-5 GHG function (built once) ─────────────────────────────
        if run_sa5 and sa5_ghg_function is None and abat_result:
            sa5_ghg_function, sa5_base_params = _build_ghg_function_from_abatement(
                abat_result
            )
            if sa5_ghg_function:
                logger.info("[SA-5] GHG function built from Phase 7 abatement results.")
            else:
                logger.warning(
                    "[SA-5] Could not build GHG function "
                    "(abatement result empty or CO₂=0)."
                )

        # ══════════════════════════════════════════════════════════════════
        # Main per-technology loop
        # ══════════════════════════════════════════════════════════════════
        for tech, tech_data in suitability_results.get("techs", {}).items():
            if tech_data.get("error"):
                logger.warning("[%s] Marked with error — skipping.", tech)
                continue

            weights: Dict[str, float] = tech_data.get("weights", {})
            if not weights:
                logger.warning("[%s] No AHP weights available — skipping.", tech)
                continue

            logger.info(
                "[%s] Starting SA suite (%d criteria)...", tech, len(weights)
            )
            results_sa[tech] = {}

            # ── Resolve tech params once per technology ──────────────────
            pd_mw, luf, cf, base_thr = self._resolve_tech_params(
                tech, country_params, pot_results
            )

            # ── SA-1 ──────────────────────────────────────────────────────
            if run_sa1:
                t0 = time.perf_counter()
                try:
                    logger.info("[%s] SA-1: OAT weight perturbation...", tech)
                    df1 = sa1_oat_weight_sensitivity(
                        criteria_dir, list(weights.keys()), weights, height, width,
                    )
                    df1.to_csv(
                        out_dir / f"{country_code}_{tech}_sa1_oat.csv", index=False
                    )
                    rb = df1.groupby("criterion")["spearman_rho"].min()
                    results_sa[tech]["sa1"] = {
                        "n_robust":       int((rb >= 0.95).sum()),
                        "n_sensitive":    len(weights) - int((rb >= 0.95).sum()),
                        "most_sensitive": str(rb.idxmin()),
                        "rho_min_global": float(rb.min()),
                        "elapsed_s":      round(time.perf_counter() - t0, 1),
                        "_df":            df1,
                    }
                    plot_sa1_tornado(
                        self.styler, df1, tech,
                        out_dir / f"{country_code}_{tech}_sa1_tornado.png",
                    )
                    plot_sa1_heatmap(
                        self.styler, df1, tech,
                        out_dir / f"{country_code}_{tech}_sa1_heatmap.png",
                    )
                    logger.info(
                        "[%s] SA-1 complete: rho_min=%.4f, most_sensitive=%s",
                        tech, rb.min(), rb.idxmin(),
                    )
                except Exception as exc:
                    logger.error("[%s] SA-1 failed: %s", tech, exc, exc_info=True)

            # ── SA-2 ──────────────────────────────────────────────────────
            if run_sa2:
                t0 = time.perf_counter()
                try:
                    sa2_threshold = _balanced_threshold(tech, pot_results)
                    logger.info(
                        "[%s] SA-2: Monte Carlo AHP (%d samples, thr=%.2f)...",
                        tech, n_mc_samples, sa2_threshold,
                    )
                    mc = sa2_monte_carlo_weights(
                        criteria_dir, list(weights.keys()), weights,
                        height, width, threshold=sa2_threshold,
                        n_samples=n_mc_samples,
                    )
                    results_sa[tech]["sa2"] = {
                        "threshold":          mc["threshold"],
                        "concentration":      mc["concentration"],
                        "n_apt_base":         mc["n_apt_base"],
                        "decisive_fraction":  mc["decisive_fraction"],
                        "boundary_fraction":  mc["boundary_fraction"],
                        "moderate_fraction":  mc["moderate_fraction"],
                        "mean_cv":            float(mc["cv"].mean()),
                        "mean_ci90_width":    float(mc["ci_width"].mean()),
                        "elapsed_s":          round(time.perf_counter() - t0, 1),
                        "_cv":                mc["cv"],
                    }
                    plot_sa2_cv(
                        self.styler, mc["cv"], mc["ci_width"], tech,
                        out_dir / f"{country_code}_{tech}_sa2_cv_dist.png",
                    )
                    logger.info(
                        "[%s] SA-2 complete: decisive=%.1f%% boundary=%.1f%% "
                        "moderate=%.1f%% (thr=%.2f, n_apt=%d)",
                        tech,
                        mc["decisive_fraction"] * 100,
                        mc["boundary_fraction"] * 100,
                        mc["moderate_fraction"] * 100,
                        mc["threshold"], mc["n_apt_base"],
                    )
                except Exception as exc:
                    logger.error("[%s] SA-2 failed: %s", tech, exc, exc_info=True)

            # ── SA-3 ──────────────────────────────────────────────────────
            if run_sa3:
                t0 = time.perf_counter()
                try:
                    from src.utils.utils import compute_pixel_area_geodesic as _paf

                    suit_tif = find_suitability_tif(
                        self.outputs_dir / country_code / "suitability" / "tif",
                        tech, country_code, allow_owa_fallback=False,
                    )
                    if suit_tif is None:
                        logger.warning(
                            "[%s] SA-3: Suitability TIF not found — skipping.",
                            tech,
                        )
                    else:
                        logger.info(
                            "[%s] SA-3: threshold sweep "
                            "(pd=%.1f luf=%.3f cf=%.3f)...",
                            tech, pd_mw, luf, cf,
                        )
                        df3 = sa3_threshold_sweep(
                            suit_tif, _paf, transform, width, height,
                            power_density_mw_km2=pd_mw,
                            land_use_factor=luf,
                            capacity_factor=cf,
                        )
                        df3.to_csv(
                            out_dir / f"{country_code}_{tech}_sa3_threshold_sweep.csv",
                            index=False,
                        )
                        results_sa[tech]["sa3"] = {
                            "base_threshold": base_thr,
                            "elapsed_s":      round(time.perf_counter() - t0, 1),
                            "_df":            df3,
                        }
                        plot_sa3_threshold(
                            self.styler, df3, tech, base_thr,
                            out_dir / f"{country_code}_{tech}_sa3_curve.png",
                        )
                        logger.info(
                            "[%s] SA-3 complete: %d thresholds evaluated",
                            tech, len(df3),
                        )
                except Exception as exc:
                    logger.error("[%s] SA-3 failed: %s", tech, exc, exc_info=True)

            # ── SA-4 ──────────────────────────────────────────────────────
            if run_sa4:
                t0 = time.perf_counter()
                try:
                    lc_p = self._lcoe_params_for_tech(tech, country_params)
                    logger.info(
                        "[%s] SA-4: LCOE MC (%d samples) | "
                        "CAPEX=%d OPEX=%d CF=%.3f r=%.1f%% n=%dyr",
                        tech, max(n_mc_samples, 10_000),
                        lc_p["capex"], lc_p["opex"], lc_p["cf"],
                        lc_p["dr"] * 100, lc_p["life"],
                    )
                    df4 = sa4_lcoe_uncertainty(
                        base_capex_usd_kw=lc_p["capex"],
                        base_opex_usd_kw_yr=lc_p["opex"],
                        lifetime=int(lc_p["life"]),
                        discount_rate=lc_p["dr"],
                        capacity_factor=lc_p["cf"],
                        n_samples=max(n_mc_samples, 10_000),
                    )
                    df4.to_csv(
                        out_dir / f"{country_code}_{tech}_sa4_lcoe_mc.csv", index=False
                    )
                    results_sa[tech]["sa4"] = {
                        **df4.attrs.get("stats", {}),
                        "elapsed_s": round(time.perf_counter() - t0, 1),
                        "_df":       df4,
                    }
                    plot_sa4_lcoe(
                        self.styler, df4, tech,
                        out_dir / f"{country_code}_{tech}_sa4_lcoe_hist.png",
                    )
                    st = df4.attrs.get("stats", {})
                    logger.info(
                        "[%s] SA-4 complete: P50=$%.1f/MWh CI90=$%.1f",
                        tech, st.get("lcoe_p50", 0), st.get("ci90_width", 0),
                    )
                except Exception as exc:
                    logger.error("[%s] SA-4 failed: %s", tech, exc, exc_info=True)

            # ── SA-6 ──────────────────────────────────────────────────────
            if run_sa6:
                t0 = time.perf_counter()
                try:
                    from src.utils.utils import compute_pixel_area_geodesic as _paf

                    suit_tif = find_suitability_tif(
                        self.outputs_dir / country_code / "suitability" / "tif",
                        tech, country_code, allow_owa_fallback=False,
                    )
                    if suit_tif is None:
                        logger.warning(
                            "[%s] SA-6: Suitability TIF not found — skipping.",
                            tech,
                        )
                    else:
                        logger.info(
                            "[%s] SA-6: potential parameter sensitivity "
                            "(pd=%.1f luf=%.3f cf=%.3f thr=%.2f)...",
                            tech, pd_mw, luf, cf, base_thr,
                        )
                        df6 = sa6_potential_sensitivity(
                            suit_tif, _paf, transform, width, height,
                            pd_mw, luf, cf, base_thr,
                        )
                        df6.to_csv(
                            out_dir / f"{country_code}_{tech}_sa6_potential_oat.csv",
                            index=False,
                        )
                        results_sa[tech]["sa6"] = {
                            "base_gw":   df6.attrs.get("base_gw",  0),
                            "base_twh":  df6.attrs.get("base_twh", 0),
                            "elapsed_s": round(time.perf_counter() - t0, 1),
                            "_df":       df6,
                        }
                        plot_sa6_potential(
                            self.styler, df6, tech,
                            out_dir / f"{country_code}_{tech}_sa6_potential.png",
                        )
                        logger.info(
                            "[%s] SA-6 complete: base=%.1f GW",
                            tech, df6.attrs.get("base_gw", 0),
                        )
                except Exception as exc:
                    logger.error("[%s] SA-6 failed: %s", tech, exc, exc_info=True)

            # ── Dashboard ─────────────────────────────────────────────────
            try:
                plot_dashboard(
                    self.styler,
                    results_sa,
                    tech,
                    country_name or country_code,
                    out_dir / f"{country_code}_{tech}_dashboard.png",
                )
                logger.info("[%s] Dashboard saved.", tech)
            except Exception as exc:
                logger.error("[%s] Dashboard failed: %s", tech, exc, exc_info=True)

        # ══════════════════════════════════════════════════════════════════
        # SA-5: Sobol GHG (cross-technology, runs once)
        # ══════════════════════════════════════════════════════════════════
        if run_sa5 and sa5_ghg_function and sa5_base_params:
            logger.info("[SA-5] Sobol global sensitivity (GHG abatement)...")
            try:
                df5 = sa5_sobol_ghg(sa5_base_params, sa5_ghg_function, n_samples=1024)
                if df5 is not None:
                    df5.to_csv(
                        out_dir / f"{country_code}_sa5_sobol_ghg.csv", index=False
                    )
                    results_sa["sa5_sobol"] = {
                        "dominant": str(df5.iloc[0]["parameter"]),
                        "_df":      df5,
                    }
                    plot_sa5_sobol(
                        self.styler, df5, out_dir / f"{country_code}_sa5_sobol_barplot.png"
                    )
                    logger.info(
                        "[SA-5] Sobol complete: dominant=%s (ST=%.3f)",
                        df5.iloc[0]["parameter"], df5.iloc[0]["ST"],
                    )
            except Exception as exc:
                logger.error("[SA-5] Execution failed: %s", exc, exc_info=True)
        elif run_sa5:
            logger.warning(
                "[SA-5] SA-5 enabled but no valid ghg_function "
                "(abatement result empty or Phase 7 not executed)."
            )

        # ── Final report ──────────────────────────────────────────────────
        elapsed_total = time.perf_counter() - t0_total
        report = self._format_report(
            results_sa, country_code, country_name, elapsed_total
        )
        (out_dir / f"{country_code}_sensitivity_report.txt").write_text(
            report, encoding="utf-8"
        )

        # ── Verify outputs ────────────────────────────────────────────────
        expected_files = [f"{country_code}_sensitivity_report.txt"]
        for tech in [t for t in results_sa if t != "sa5_sobol"]:
            expected_files.extend([
                f"{country_code}_{tech}_sa1_tornado.png",
                f"{country_code}_{tech}_sa1_heatmap.png",
                f"{country_code}_{tech}_sa2_cv_dist.png",
                f"{country_code}_{tech}_sa3_curve.png",
                f"{country_code}_{tech}_sa4_lcoe_hist.png",
                f"{country_code}_{tech}_sa6_potential.png",
                f"{country_code}_{tech}_dashboard.png",
            ])
        missing = [
            f for f in expected_files
            if not (out_dir / f).exists() and f.endswith(".png")
        ]
        if missing:
            logger.warning("  Missing plots: %s", ", ".join(missing[:5]))
        else:
            logger.info("  All expected sensitivity outputs generated.")

        # ── Persist artefacts ─────────────────────────────────────────────
        from src.io.artifact_manager import ArtifactManager
        artifact_mgr = ArtifactManager(self.outputs_dir, country_code)
        phase_dir    = artifact_mgr.phase_dir("sensitivity")

        serializable: Dict[str, Any] = {}
        for tech, td in results_sa.items():
            if tech == "sa5_sobol":
                serializable[tech] = {
                    "dominant":       td.get("dominant", ""),
                    "_df_available":  "_df" in td,
                }
                continue
            serializable[tech] = {}
            for sa_name, sa_data in td.items():
                if sa_name.startswith("_"):
                    continue
                if isinstance(sa_data, dict):
                    serializable[tech][sa_name] = {
                        k: v for k, v in sa_data.items()
                        if not k.startswith("_")
                        and isinstance(v, (str, int, float, bool, type(None)))
                    }
                else:
                    serializable[tech][sa_name] = sa_data

        artifact_mgr.save_result(phase_dir, serializable, serializer="pickle")
        files = {
            str(p.relative_to(phase_dir)): str(p)
            for p in out_dir.rglob("*") if p.is_file()
        }
        artifact_mgr.save_manifest(
            phase_dir,
            "sensitivity",
            files=files,
            parameters={
                "country":                  country_code,
                "run_sa1":                  run_sa1,
                "run_sa2":                  run_sa2,
                "run_sa3":                  run_sa3,
                "run_sa4":                  run_sa4,
                "run_sa5":                  run_sa5,
                "run_sa6":                  run_sa6,
                "n_mc_samples":             n_mc_samples,
                "country_params_available": country_params is not None,
            },
        )

        logger.info("[%s] Phase 8 completed in %.1fs.", country_code, elapsed_total)
        logger.info(
            "[%s] Outputs: %d CSVs, %d plots",
            country_code,
            len(list(out_dir.glob("*.csv"))),
            len(list(out_dir.glob("*.png"))),
        )

        return results_sa

    # ─────────────────────────────────────────────────────────────────────
    # Format report (DUP_21 — refatorado para usar reporting)
    # ─────────────────────────────────────────────────────────────────────

    def _format_report(
        self,
        rs: Dict[str, Any],
        code: str,
        country_name: str,
        elapsed: float,
    ) -> str:
        """
        Gera o relatório de sensibilidade usando o módulo unificado reporting.

        DUP_21: Substitui a formatação manual (~80 linhas) por uma estrutura
        hierárquica com ReportSection e subsections.
        """
        sections: List[ReportSection] = []

        for tech, td in rs.items():
            if tech == "sa5_sobol":
                continue

            sa1 = td.get("sa1", {})
            sa2 = td.get("sa2", {})
            sa3 = td.get("sa3", {})
            sa4 = td.get("sa4", {})
            sa6 = td.get("sa6", {})

            tech_section = ReportSection(
                title=f"{tech.upper()} [{TECH_LABEL.get(tech, tech)}]"
            )

            # SA-1
            rows1 = [
                ("Robust criteria (ρ >= 0.95)", str(sa1.get('n_robust', '—'))),
                ("Sensitive criteria (ρ < 0.95)", str(sa1.get('n_sensitive', '—'))),
                ("Global minimum ρ", _sfmt(sa1.get('rho_min_global'), '.4f')),
                ("Most sensitive criterion", sa1.get('most_sensitive', '—')),
            ]
            tech_section.subsections.append(
                ReportSection(title="SA-1 · OAT Weight Sensitivity", rows=rows1)
            )

            # SA-2
            decisive_pct = sa2.get('decisive_fraction', 0) * 100
            boundary_pct = sa2.get('boundary_fraction', 0) * 100
            moderate_pct = sa2.get('moderate_fraction', 0) * 100
            rows2 = [
                ("Threshold used", _sfmt(sa2.get('threshold'), '.2f')),
                ("Apt pixels (base weights)", str(sa2.get('n_apt_base', '—'))),
                ("Decisive (>95% or <5% crossing)", f"{_sfmt(decisive_pct, '.1f', '0.0')}%"),
                ("Boundary (25-75% crossing)", f"{_sfmt(boundary_pct, '.1f', '0.0')}%"),
                ("Moderate (rest)", f"{_sfmt(moderate_pct, '.1f', '0.0')}%"),
                ("Mean CV Spatial Baseline", _sfmt(sa2.get('mean_cv'), '.4f')),
                ("Mean CI90 width globally", _sfmt(sa2.get('mean_ci90_width'), '.4f')),
            ]
            tech_section.subsections.append(
                ReportSection(title="SA-2 · Monte Carlo AHP", rows=rows2)
            )

            # SA-3
            rows3 = [
                ("Base threshold (balanced)", _sfmt(sa3.get('base_threshold'), '.2f')),
            ]
            tech_section.subsections.append(
                ReportSection(title="SA-3 · Threshold Sweep", rows=rows3)
            )

            # SA-4
            rows4 = [
                ("LCOE P50 (Median Proxy)", f"{_sfmt(sa4.get('lcoe_p50'), '.1f')} USD/MWh"),
                ("Confidence Interval (90%)", f"{_sfmt(sa4.get('ci90_width'), '.1f')} USD/MWh Spread"),
                ("Standard Deviation", f"{_sfmt(sa4.get('lcoe_std'), '.1f')} USD/MWh"),
            ]
            tech_section.subsections.append(
                ReportSection(title="SA-4 · LCOE Uncertainty", rows=rows4)
            )

            # SA-6
            rows6 = [
                ("Base potential evaluated", f"{_sfmt(sa6.get('base_gw'), '.1f')} GW"),
                ("Base generation estimated", f"{_sfmt(sa6.get('base_twh'), '.1f')} TWh/yr"),
            ]
            tech_section.subsections.append(
                ReportSection(title="SA-6 · Potential Parameter Sensitivity", rows=rows6)
            )

            sections.append(tech_section)

        # ── SA-5 section ──────────────────────────────────────────────────
        if "sa5_sobol" in rs:
            sa5 = rs["sa5_sobol"]
            df5 = sa5.get("_df")
            rows5 = [
                ("Dominant parameter (ST proxy)", sa5.get('dominant', '—')),
            ]
            if df5 is not None and not df5.empty:
                rows5.append(("", ""))  # separador
                for _, r5 in df5.iterrows():
                    rows5.append(
                        (f"{r5['parameter']} (ST)", _sfmt(r5['ST'], '.4f'))
                    )
            sections.append(
                ReportSection(
                    title="SA-5 · Sobol Global Sensitivity (GHG Abatement Function)",
                    rows=rows5
                )
            )

        return build_phase_report(
            title="SENSITIVITY ANALYSIS REPORT",
            country_name=country_name,
            country_code=code,
            timestamp=date.today().isoformat(),
            sections=sections,
            timings={},
            elapsed_total=elapsed,
            width=72,
        )