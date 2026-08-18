# CURRENT-SPRINT.md

> Este arquivo é gerado/atualizado pelo Claude Code a partir do estado real do repositório. Qualquer resumo colado em uma conversa de chat é uma cópia de um instante específico — em caso de dúvida, este arquivo e `docs/BACKLOG.md` são a fonte de verdade, não o histórico da conversa.

---

## 1. Status em uma linha

**2026-08-18** — última novidade: Campaign #1 (`concentration`, Dirichlet SA-2) fechada — OAT sweep completo para PRT/BRA × solar/wind/biomass × concentration∈{10,20,40} (commit `2204109`, `scratchpad/oat_sa2_concentration_sensitivity.py`), confirmando que o prototype original citado em BLOCKER-016 nunca foi commitado e é irrecuperável; a sweep reproduz exatamente o ~2.5x de BRA/solar (17.9%→45.4%), mas achou que isso não é universal (PRT/wind ~16.3x na mesma faixa) — aberto como novo achado não-priorizado em BACKLOG.md ("Campaign #1 (follow-up)"), pendente de triagem do Douglas. Na mesma sessão, commit `8dcc063` (decisão manual do usuário, não relacionado à Campaign #1) removeu artefatos de saída obsoletos pré-maio/2026 de `outputs/{ZAF,IND,RUS,CHN,EGY}/` e 20 logs antigos de `outputs/logs/`; `outputs/PRT/`, `outputs/BRA/` e `outputs/reports/` não foram tocados. Antes disso: fechamento de duas lacunas encontradas na auditoria que gerou este arquivo (BLOCKER-019 registrado para o crash real de Transport; `SUMMARY.md` corrigido por completo — não só `sensitivity_analyzer.py`, 15 módulos estavam com LOC desatualizado e 3 módulos novos não tinham entrada; REFACTOR-004 corrigido de "sem status" para "Partial"); enxugamento de `sensitivity_analyzer.py` (6 commits, validado) + migração do Bloco 1 da Sensitivity & Config Migration Campaign (`common_exclusions`/`_SLOPE_OFFSET_DEG` → `parameters.json`) + utilitários de coordenação da campanha (`scripts/session_lock.py`, `scripts/validate_run_checksum.py`).

---

## 2. Decisões arquiteturais fixas

- `results_writer.py` = agregador final de resultados, nunca recalcula. → [`docs/00-project-state-and-reorg-plan.md`, Part 7](00-project-state-and-reorg-plan.md#part-7--decision-on-results_writerpys-role-resolved)
- BLOCKER-010 é pré-requisito da arquitetura "Phase 6 = agregador", mas permanece **fora de escopo** até sessão dedicada explícita. → [`docs/BACKLOG.md`, entrada BLOCKER-010](BACKLOG.md)
- `settings.yaml` = operacional / meta-parâmetros de método estatístico; `parameters.json` = científico/geoespacial. → [`docs/memory/07-configuration.md`](memory/07-configuration.md)
- `docs/BACKLOG.md` é o rastreador vivo único de BLOCKER/REFACTOR/QI/Campaign — nunca criar rastreador paralelo. → [`docs/BACKLOG.md`](BACKLOG.md)

---

## 3. Regras de coordenação

- Antes de qualquer commit, rodar `git status`/`git diff` e confirmar que não há trabalho pendente de outra sessão na árvore.
- Nunca rodar duas sessões simultaneamente na mesma árvore de trabalho sem `git worktree` separado.
- Mostrar diff antes de escrever, sempre.
- Commit local apenas, sem push.
- Código/commits/docs técnicos em inglês.

*(Ferramenta de apoio à primeira regra: `scripts/session_lock.py acquire/release --session <nome>`, usada pela Sensitivity & Config Migration Campaign.)*

---

## 4. Tarefas pendentes

### Alta prioridade

- [ ] **BLOCKER-010** — Phase 6 sempre reconstrói Potential/LCOE do disco, nunca usa o objeto vivo. Pré-requisito da arquitetura "results_writer = agregador" (`main.py`, `results_writer.py`). → [BACKLOG.md#BLOCKER-010](BACKLOG.md)
- [ ] **BLOCKER-005 (metade de validação)** — proteção contra `thresholds` incompleto em `parameters.json` funciona hoje "por acidente" (estrutura de `build_tech_params()`), não por um validador real (`schemas.py`/`config_loader.py`). → [BACKLOG.md#QI-003, "Fragility note"](BACKLOG.md)

### Média prioridade

- [ ] **Campaign #5** — `IUCN_SCORES` mapping (`constants.py:177-194`) — bloqueia a decisão final de `protected_areas` (já migrado, mas marcado não-final). → [BACKLOG.md, Campaign table row 5](BACKLOG.md)
- [ ] **Campaign #1 (follow-up)** — SA-2's `concentration` sensitivity é muito heterogênea entre pares país×tech (PRT/wind ~16.3x vs. BRA/solar ~2.5x e BRA/wind ~2.1x, mesma faixa {10,20,40}); `concentration=20` é o único default hardcoded para todos os pares. Sem prioridade definida — precisa de triagem do Douglas (calibração por país/tech, ou apenas ressalva documentada na metodologia da tese). → [BACKLOG.md, Campaign table row 1, "Row 1" write-up]
- [ ] **Campaign #2 (follow-up)** — `proximity_plants` migrado sem justificativa externa documentada; precisa de comparação com literatura/normas de distância de siting. → [BACKLOG.md, Campaign table row 2, veredito]
- [ ] **Campaign #4** — `normalize_percentile` default `p_low=5.0, p_high=95.0` (solar/wind/biomass resource), nunca sobrescrito por país (`normalization.py:26-27`). → [BACKLOG.md, Campaign table row 4]
- [ ] **Campaign #6** — `_MIN_RESOURCE_COVERAGE=0.05` (`lcoe_calculator.py:112`). → [BACKLOG.md, Campaign table row 6]
- [ ] **Campaign #7** — `src_cv < 0.01`, fallback de recurso de biomassa constante (`lcoe_calculator.py:1021`). → [BACKLOG.md, Campaign table row 7]
- [ ] **Campaign #8** — deltas de cenário `±0.10` (optimistic/conservative), `configs/settings.yaml` `potential.scenarios`. → [BACKLOG.md, Campaign table row 8]
- [ ] **Campaign #9** — pesos `0.6×slope + 0.4×TRI` em `compute_terrain_score` (`criteria_builder.py:216`). → [BACKLOG.md, Campaign table row 9]
- [ ] **Campaign #10** — `TRI_THRESHOLD=50.0` (`constants.py:200`), acoplado ao item acima. → [BACKLOG.md, Campaign table row 10]
- [ ] **Campaign #11** — percentis hardcoded sem nenhum caminho de config: roads/grid, proximity_plants, seismic (`criteria_builder.py:258,349,589`). → [BACKLOG.md, Campaign table row 11]
- [ ] **Campaign #12** — variações do `sa4_lcoe_uncertainty` (capex/opex/cf), desbloqueado desde que BLOCKER-017/018 landaram (`sensitivity_analyzer.py:573-575`). → [BACKLOG.md, Campaign table row 12]
- [ ] **Campaign #13** — corte "robust" do SA-1, `rho >= 0.95`, desbloqueado (`sensitivity_analyzer.py:362`). → [BACKLOG.md, Campaign table row 13]
- [ ] **REFACTOR-004 (status: Partial)** — extração de SA1-6 para `sensitivity_math.py` feita (commit `350c80c`); `run()` ainda **não** foi dividido em `_run_sa1()`…`_run_sa6()` (confirmado por grep: nenhum método desse nome existe em `sensitivity_analyzer.py`). Status corrigido em BACKLOG.md nesta passada (antes não tinha `**Status**` nenhum). → [BACKLOG.md#REFACTOR-004](BACKLOG.md)
- [ ] **REFACTOR-006 (achado residual)** — Phase 8 (`sensitivity_analyzer.py`) é a única fase sem `output_model` Pydantic no `result.pkl` (`main.py` passa `output_model=None`); investigado, não corrigido. → [BACKLOG.md#REFACTOR-006, "Status update"](BACKLOG.md)
- [ ] **Gap não numerado (Part 4b)** — `mask_source` (Phase 5/LCOE) é computado e logado, mas descartado antes de persistir; nunca recebeu número de BLOCKER/REFACTOR. → [00-project-state-and-reorg-plan.md#4b](00-project-state-and-reorg-plan.md)
- [ ] **QI-001 (gap)** — `exclusion.py` e `normalization.py` seguem sem nenhum teste (`tests/unit/` não tem `test_exclusion.py`/`test_normalization.py`, confirmado). → [BACKLOG.md#QI-001](BACKLOG.md)
- [ ] **QI-004** — testes para `raster_io.find_raster_by_base_name()` e os acessores de `params_helpers.py` introduzidos por BLOCKER-003/006/007. → [BACKLOG.md#QI-004](BACKLOG.md)
- [ ] **QI-003** — script de validação de config/schema no startup (`config_validator.py` não existe ainda). → [BACKLOG.md#QI-003](BACKLOG.md)

### Baixa prioridade

- [ ] **BLOCKER-019** — Transport (Phase 9) crasha de verdade em `country_params.solar_capacity_factor` (atributo flat vs. `CountryParams.solar.capacity_factor` aninhado), em `run()` L402-404 e `_log_parameter_dashboard()` L1461-1462; é a causa raiz de `skip_transport: true`. Registrado nesta passada (antes só existia em texto solto). Baixa prioridade — Transport fica dormente por decisão, não por esquecimento. → [BACKLOG.md#BLOCKER-019](BACKLOG.md)
- [ ] **BLOCKER-012** — Transport (Phase 9) tem o mesmo bug de dupla persistência do BLOCKER-011; dormente enquanto `skip_transport: true`. → [BACKLOG.md#BLOCKER-012](BACKLOG.md)
- [ ] **BLOCKER-014** — fetch ao vivo da OWID em Abatement quebra determinismo bit-a-bit de validação. → [BACKLOG.md#BLOCKER-014](BACKLOG.md)
- [ ] **BLOCKER-015** — skip-check da Fase 1 (Audit) olha para o diretório errado. → [BACKLOG.md#BLOCKER-015](BACKLOG.md)
- [ ] **REFACTOR-003** — dividir `data_fetcher.py` por dataset. → [BACKLOG.md#REFACTOR-003](BACKLOG.md)
- [ ] **REFACTOR-008** — separar compositing PIL e decorações de `map_styling.py`. → [BACKLOG.md#REFACTOR-008](BACKLOG.md)
- [ ] **REFACTOR-009** — extrair lógica de siting de hubs de `transport_decarbonization_calculator.py`. → [BACKLOG.md#REFACTOR-009](BACKLOG.md)
- [ ] **REFACTOR-010** — mover valores hardcoded remanescentes das Fases 3-6 para config (sobrepõe parcialmente com a Campaign acima). → [BACKLOG.md#REFACTOR-010](BACKLOG.md)
- [ ] **QI-002** — teste de integração end-to-end com país sintético. → [BACKLOG.md#QI-002](BACKLOG.md)
- [ ] **Gap não numerado (Part 4c)** — `_LCOEView.mean_lcoe` tolera 4 formatos de dict distintos; sintoma do mesmo problema raiz do BLOCKER-010, não resolvível isoladamente. → [00-project-state-and-reorg-plan.md#4c](00-project-state-and-reorg-plan.md)
- [ ] **Gap não numerado (Part 4d)** — `transport_decarbonization_calculator.py` escreve um GeoDataFrame como CSV puro, perdendo fidelidade espacial; dormente com Phase 9 desligada. → [00-project-state-and-reorg-plan.md#4d](00-project-state-and-reorg-plan.md)
- [ ] **Gap não numerado (Part 4f)** — `write_criteria_summary()` e outros escritores de relatório não usam a convenção compartilhada `build_phase_report()`. → [00-project-state-and-reorg-plan.md#4f](00-project-state-and-reorg-plan.md)
- [ ] **QI-005** — gerar a tabela de LOC do `SUMMARY.md` via script (`wc -l` automatizado por módulo) em vez de edição manual; registrado nesta passada depois do `SUMMARY.md` ficar desatualizado pela segunda vez após um refactor estrutural — padrão recorrente, não incidente isolado. Não implementado ainda. → [BACKLOG.md#QI-005](BACKLOG.md)
- [ ] **Doc stale** — `src/visualization/` continua sem `__init__.py` (confirmado); nota ⚠️ em `02-architecture.md` segue sem resolução. → [docs/memory/02-architecture.md](memory/02-architecture.md)

---

## Já resolvido, não repetir (verificado nesta passada)

Para não reabrir o que já foi checado: BLOCKER-001–004, 006–009, 011, 013, 016–018 = done; REFACTOR-001, 002, 005, 007 = done; Campaign #1's OAT sweep (`concentration`, SA-2) = done — mas só o teste, a decisão de valor final segue aberta (ver "Campaign #1 (follow-up)" acima); a verificação da Part 4a (dupla persistência em Suitability/Abatement/Sensitivity) já foi feita — nenhum dos três perde dado, só Transport (BLOCKER-012) perde; a reorganização da Part 6 do reorg-plan já foi aplicada (`docs/BACKLOG.md`, `docs/analysis/`, `docs/README.md` existem); as staleness da Part 5 em `06-risk-areas.md`, `08-conventions.md`, `07-configuration.md` e `D7` já foram corrigidas.

**Corrigido nesta passada**: `SUMMARY.md` teve **todos** os números de LOC conferidos contra `wc -l` real (não só `sensitivity_analyzer.py`) — 15 módulos estavam desatualizados (`config_loader.py`, `constants.py`, `schemas.py`, `artifact_manager.py`, `grid_aligner.py`, `suitability_builder.py`, `sensitivity_analyzer.py`, `transport_decarbonization_calculator.py`, `raster_io.py`, `data_recovery.py`, `params_helpers.py`, `map_styling.py`, `reporting.py`, `dashboard_panels.py`, `settings.yaml`, `parameters.json`) e 3 módulos novos não tinham entrada nenhuma (`sensitivity_math.py`, `sensitivity_plots.py`, `transport_plots.py`) — todos corrigidos/adicionados. Duas alegações de "maior módulo do código" ficaram falsas com os números corretos (`sensitivity_analyzer.py` não é mais o maior processor, `transport_decarbonization_calculator.py` não é mais o maior módulo do repo) e foram removidas do texto.

**Também nesta passada**: commit `2204109` fechou a Campaign #1 — sweep OAT de `concentration` (SA-2) para PRT/BRA × solar/wind/biomass, ver BACKLOG.md e o novo item "Campaign #1 (follow-up)" acima. Separadamente, commit `8dcc063` (decisão manual do usuário, sem relação com a Campaign #1) removeu artefatos de saída obsoletos pré-maio/2026 de `outputs/{ZAF,IND,RUS,CHN,EGY}/` e 20 logs antigos de `outputs/logs/`; `outputs/PRT/`, `outputs/BRA/` e `outputs/reports/` não foram tocados por nenhum dos dois commits.
