---
id: docs-tasks
type: tracker
status: active
created: 2026-08-18
updated: 2026-08-19
updated_by: claude-code
version: 2
depends_on: [archive-backlog-full, archive-reorg-plan]
linked_by: [docs-readme, mem-11-onboarding, docs-claude-docs-rules]
scope: "Único rastreador vivo de trabalho aberto do projeto (BLOCKER/REFACTOR/QI/INVAR/Campaign/GAP/DOC); não contém narrativa longa nem decisões arquiteturais — essas ficam em archive/ e DECISIONS.md."
---

# TASKS.md — Rastreador vivo único

Todo trabalho aberto do projeto vive **somente aqui**. Não criar rastreador paralelo (ver `CLAUDE-DOCS-RULES.md`). Detalhes completos (evidência, linhas de código, validação) ficam em `archive/` e `analysis/` — este arquivo só linka, nunca copia narrativa longa.

**Status possíveis**: `open` · `partial` · `blocked` (aguardando outro ID) · `pending-decision` (aguardando decisão humana, não é falta de trabalho técnico).

---

## 🔴 Alta prioridade

| ID | Descrição | Status | Evidência |
|---|---|---|---|
| BLOCKER-010 | Fase 6 sempre reconstrói Potential/LCOE do disco, nunca usa o objeto vivo. Escopo já investigado e fechado; falta decisão de paridade de shape + implementação. Pré-requisito da arquitetura "Fase 6 = agregador" (ver `DECISIONS.md` D8/D9). | open | `archive/backlog-full-2026-08.md` §BLOCKER-010 |

---

## 🟡 Média prioridade

| ID | Descrição | Status | Evidência |
|---|---|---|---|
| BLOCKER-005 | Metade de validação: falta validador load-time garantindo que `thresholds` tem as 3 chaves de cenário. Vetor de dict cru já removido, risco ativo baixo. | partial | `archive/backlog-full-2026-08.md` §BLOCKER-005 |
| REFACTOR-004 | `run()` da Sensitivity (Fase 8) ainda não dividido em `_run_sa1()`…`_run_sa6()`. Extração dos métodos SA1-6 já feita. | partial | `archive/backlog-full-2026-08.md` §REFACTOR-004 |
| REFACTOR-006 | Achado residual: Fase 8 é a única sem `output_model` Pydantic no `result.pkl` — investigado, não corrigido. | open | `archive/backlog-full-2026-08.md` §REFACTOR-006 |
| QI-001 (gap) | `exclusion.py` e `normalization.py` seguem sem nenhum teste. | open | `archive/backlog-full-2026-08.md` §QI-001, `memory/06-risk-areas.md` |
| QI-003 | Script de validação de config/schema no startup — não existe ainda. | open | `archive/backlog-full-2026-08.md` §QI-003 |
| QI-004 | Testes para `raster_io.find_raster_by_base_name()` e acessores de `params_helpers.py`. | open | `archive/backlog-full-2026-08.md` §QI-004 |
| INVAR-003 | `grid_aligner.py:910,960,968-969` — fallback silencioso (P1), latente, baixa severidade. | open | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| INVAR-004 | `criteria_builder.py` `ParamsLike`+`_param()`, ~12 call sites — absorve BLOCKER-020. | open | `archive/backlog-full-2026-08.md` §Invariant Validation Project, §BLOCKER-020 |
| INVAR-005 | `suitability_builder.py:140,144,162,167-169,190-191` — fallback silencioso (P1), latente. | open | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| INVAR-006 | `suitability_builder.py:339` — fallback silencioso (P1), latente, baixa severidade. | open | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| INVAR-007 | `potential_calculator.py:439` — mesmo site do BLOCKER-005 (não fecha aquele item). | open | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| INVAR-008 | `lcoe_calculator.py:527-560` (`_resolve_threshold`) — fallback silencioso (P1), ativo. | open | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| INVAR-009 | `results_writer.py:162-211` (`_normalize_potential`/`_normalize_lcoe`) — múltiplos formatos aceitos (P3). | blocked (BLOCKER-010) | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| INVAR-010 | `results_writer.py:213-229+` (`_normalize_abatement`) — múltiplos formatos aceitos (P3). | blocked (BLOCKER-010) | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| INVAR-011 | `data_recovery.py` (`recover_*_from_disk`) — múltiplos formatos aceitos (P3). | blocked (BLOCKER-010) | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| INVAR-012 | `ghg_abatement_calculator.py:946` — múltiplos formatos aceitos (P3), ativo. | open | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| INVAR-013 | `ghg_abatement_calculator.py:1031-1054` — fallback silencioso (P1), ativo. | open | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| INVAR-014 | `sensitivity_analyzer.py:230-278` (`_resolve_tech_params`) — P1+P3, ativo. | open | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| INVAR-015 | `sensitivity_analyzer.py:484-496` — fallback silencioso (P1), ativo. | open | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| INVAR-016 | `sensitivity_analyzer.py:418` — fallback silencioso (P1), latente, menor. | open | `archive/backlog-full-2026-08.md` §Invariant Validation Project |
| Campaign-02 | `proximity_plants` migrado sem justificativa externa documentada; precisa comparação com literatura/normas de siting. | pending-decision | `archive/backlog-full-2026-08.md` §Sensitivity & Config Migration Campaign, linha 2 |
| Campaign-04 | `normalize_percentile` default `p_low=5.0,p_high=95.0` (`normalization.py:26-27`) nunca sobrescrito por país. | open | `archive/backlog-full-2026-08.md` Campaign, linha 4 |
| Campaign-05 | `IUCN_SCORES` mapping (`constants.py:177-194`) — bloqueia decisão final de `protected_areas`. | open | `archive/backlog-full-2026-08.md` Campaign, linha 5 |
| Campaign-06 | `_MIN_RESOURCE_COVERAGE=0.05` (`lcoe_calculator.py:112`). | open | `archive/backlog-full-2026-08.md` Campaign, linha 6 |
| Campaign-07 | `src_cv<0.01`, fallback biomassa constante (`lcoe_calculator.py:1021`). | open | `archive/backlog-full-2026-08.md` Campaign, linha 7 |
| Campaign-08 | Deltas de cenário `±0.10` (`settings.yaml` `potential.scenarios`). | open | `archive/backlog-full-2026-08.md` Campaign, linha 8 |
| Campaign-09 | Pesos `0.6×slope + 0.4×TRI` em `compute_terrain_score` (`criteria_builder.py:216`). | open | `archive/backlog-full-2026-08.md` Campaign, linha 9 |
| Campaign-10 | `TRI_THRESHOLD=50.0` (`constants.py:200`), acoplado à Campaign-09. | open | `archive/backlog-full-2026-08.md` Campaign, linha 10 |
| Campaign-11 | Percentis hardcoded sem config: roads/grid, proximity_plants, seismic (`criteria_builder.py:258,349,589`). | open | `archive/backlog-full-2026-08.md` Campaign, linha 11 |
| Campaign-12 | Variações `sa4_lcoe_uncertainty` (capex/opex/cf) — desbloqueado (BLOCKER-017/018 done). | open | `archive/backlog-full-2026-08.md` Campaign, linha 12 |
| Campaign-13 | Corte "robust" SA-1, `rho>=0.95` — desbloqueado (BLOCKER-017/018 done). | open | `archive/backlog-full-2026-08.md` Campaign, linha 13 |
| GAP-001 | `mask_source` (Fase 5/LCOE) computado e logado, mas descartado antes de persistir — nunca recebeu número BLOCKER. | open | `archive/00-project-state-and-reorg-plan.md` Parte 4b, `analysis/analysis-lcoe_calculator.md` §2a |

---

## 🟢 Baixa prioridade

| ID | Descrição | Status | Evidência |
|---|---|---|---|
| BLOCKER-012 | Transport (Fase 9) tem o mesmo bug de dupla persistência do BLOCKER-011; dormente. | open | `archive/backlog-full-2026-08.md` §BLOCKER-012 |
| BLOCKER-014 | Fetch ao vivo da OWID em Abatement quebra determinismo bit-a-bit de validação. | open | `archive/backlog-full-2026-08.md` §BLOCKER-014 |
| BLOCKER-015 | Skip-check da Fase 1 (Audit) olha para o diretório errado. | open | `archive/backlog-full-2026-08.md` §BLOCKER-015 |
| BLOCKER-019 | Transport crasha em `country_params.solar_capacity_factor` (attr plano vs. aninhado) — causa raiz do `skip_transport: true`. | open | `archive/backlog-full-2026-08.md` §BLOCKER-019 |
| REFACTOR-003 | Dividir `data_fetcher.py` por dataset. | open | `archive/backlog-full-2026-08.md` §REFACTOR-003 |
| REFACTOR-008 | Separar compositing PIL e decorações de `map_styling.py`. | open | `archive/backlog-full-2026-08.md` §REFACTOR-008 |
| REFACTOR-009 | Extrair lógica de siting de hubs de `transport_decarbonization_calculator.py`. | open | `archive/backlog-full-2026-08.md` §REFACTOR-009 |
| REFACTOR-010 | Mover valores hardcoded remanescentes das Fases 3-6 para config. | open | `archive/backlog-full-2026-08.md` §REFACTOR-010 |
| QI-002 | Teste de integração end-to-end com país sintético. | open | `archive/backlog-full-2026-08.md` §QI-002 |
| QI-005 | Gerar tabela de LOC do `SUMMARY.md` via script em vez de edição manual. | open | `archive/backlog-full-2026-08.md` §QI-005 |
| GAP-002 | `_LCOEView.mean_lcoe` tolera 4 formatos de dict distintos — sintoma do mesmo problema raiz do BLOCKER-010, não resolvível isoladamente. | blocked (BLOCKER-010) | `archive/00-project-state-and-reorg-plan.md` Parte 4c |
| GAP-003 | `transport_decarbonization_calculator.py` escreve GeoDataFrame como CSV puro, perdendo fidelidade espacial; dormente com Fase 9 desligada. | open | `archive/00-project-state-and-reorg-plan.md` Parte 4d |
| GAP-004 | `write_criteria_summary()` e outros escritores de relatório não usam a convenção compartilhada `build_phase_report()`. | open | `archive/00-project-state-and-reorg-plan.md` Parte 4f |
| GAP-005 | Campaign #1 (follow-up): `concentration` (SA-2) tem sensibilidade muito heterogênea entre país×tech (PRT/wind ~16,3x vs. BRA/solar ~2,5x); precisa triagem do Douglas. | pending-decision | `archive/backlog-full-2026-08.md` §Sensitivity Campaign, "Row 1" |
| DOC-001 | `CITATION.cff` (`v1.0.0`) desatualizado vs. `README.md` (`v2.0.0`); `zenodo.json` não tem campo de versão. | open | `memory/01-overview.md` |
| DOC-002 | `src/visualization/` sem `__init__.py`, ao contrário de todo outro pacote `src/*` — intencional ou esquecimento, não confirmado. | open | `memory/02-architecture.md` |
| DOC-003 | Nenhum caminho de execução CI/cluster confirmado ausente fora do repo (pipeline processa ~18GB/run). | open | `memory/05-environment.md` |
| DOC-004 | `requirements.txt` é um `pip freeze` completo não curado, não uma lista de dependências diretas. | open | `memory/05-environment.md` |
| DOC-005 | Formato exato esperado por `--batch country_list.txt` nunca verificado contra o parsing real de `main.py`. | open | `memory/10-scripts-and-commands.md` |
| DOC-006 | Significado das tags de changelog inline (`T1_06`, `T2_09`, `BUG_02`, `DUP_22`) não confirmado — sistema externo ou notação pessoal do autor. | open | `memory/08-conventions.md` |
| DOC-007 | Equações do modelo de demanda hidrogênio/EV em `transport_decarbonization_calculator.py` (2237 linhas) nunca lidas por completo. | open | `memory/04-algorithms.md` |
| DOC-008 | `LICENSE` ausente no repositório apesar de `README.md`/`CITATION.cff`/`zenodo.json` afirmarem MIT. | open | `memory/06-risk-areas.md` |

---

## ✅ Concluído nas últimas 2 semanas

- **INVAR-002** — `resolution_tolerance` confirmado sem efeito downstream; `logger.warning` adicionado; 2 testes, 82/82 passando. (2026-08-18)
- **INVAR-001** — `ConfigLoader.has_country()` adicionado; `except Exception` genérico removido de `DataAuditor.run()`; 2 testes, 80/80 passando. (2026-08-18)
- **BLOCKER-020** — fechado, absorvido por INVAR-004 (registro, não fix). (2026-08-18)
- **Campaign #1 (sweep)** — OAT de `concentration` (SA-2) fechado para PRT/BRA × 3 techs; decisão de valor final permanece aberta como GAP-005. (2026-08-18)
- **BLOCKER-010 (investigação de escopo)** — causa raiz, raio de impacto e classificação de tamanho confirmados; implementação segue aberta acima. (2026-08-18)
- **Remoção do dict-input cru de `country_params`** — 5 funções agora exigem `CountryParams` validado; caminho morto em produção confirmado por busca completa. (2026-08-18)
- **`sensitivity_analyzer.py` enxugamento** — 6 commits: extração `sensitivity_math.py`, remoção de fallback silencioso de import, remoção de campo órfão, nova saída `sa2_distribution_summary`. (2026-08-18)
- **Bloco 1 — Task 2 (migração)** — `common_exclusions`/`_SLOPE_OFFSET_DEG` migrados para `parameters.json`, validado byte-idêntico. (2026-08-18)
