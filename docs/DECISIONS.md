---
id: docs-decisions
type: reference
status: active
created: 2026-08-18
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [archive-backlog-full]
linked_by: [docs-readme, mem-09-decisions, mem-03-pipeline]
scope: "Log append-only de decisões arquiteturais (D1, D2, D3...); nunca editado retroativamente e não é um rastreador de tarefas."
---

# DECISIONS.md — Log de decisões arquiteturais

**Append-only.** Nunca editar ou apagar uma entrada existente — só adicionar uma nova ao final, mesmo que ela substitua/reverta uma anterior (nesse caso, a entrada antiga ganha `Status: Superseded by D<N>`, não é removida). Formato por entrada: **ID | Data | Decisão | Racional | Consequências** (compacto — para o histórico completo/citações originais, ver `archive/backlog-full-2026-08.md`, seção "Appendix: Technical Decisions", de onde D1–D7 foram migradas verbatim).

---

### D1 — Separação estrita `settings.yaml` (operacional) vs. `parameters.json` (científico)
**Data**: não documentada (era do refactor v2.0). **Decisão**: todo parâmetro científico/tecnológico vive exclusivamente em `parameters.json`; `settings.yaml` governa só infraestrutura, paths, resoluções, visualização e flags de skip. **Racional**: versões anteriores misturavam parâmetros financeiros de LCOE dentro de `settings.yaml`; o refactor v2.0 separou para que mudar *onde* o pipeline roda nunca risque mudar *o que* ele computa. **Consequências**: um pesquisador só precisa tocar um arquivo por mudança científica; regra é só de convenção, sem guarda de runtime (ver `memory/06-risk-areas.md`).

### D2 — Pydantic v2 (`schemas.py`) substitui `models.py`
**Data**: não documentada. **Decisão**: todo contrato de entrada/saída/config de fase consolidado em um módulo Pydantic v2 único. **Racional**: `schemas.py`'s próprio docstring: "Autoridade única de modelos de dados — substitui `models.py`, removido". **Consequências**: segurança de tipo e validação nas fronteiras de persistência; `models.py` não existe mais na árvore atual (confirmado).

### D3 — Extração de AHP/TOPSIS/OWA/exclusão para `src/utils/`
**Data**: não documentada (v2.0). **Decisão**: lógica MCDA extraída de `suitability_builder.py` para módulos standalone, technology-agnostic. **Racional**: reuso fora da fase de suitability (`grid_aligner.py`, Fase 2a, já reusa `ahp.py`). **Consequências**: primitivas reutilizáveis e testáveis independentemente (teste real só chegou com QI-001, bem depois).

### D4 — TOPSIS como superfície primária de suitability; OWA secundária
**Data**: não documentada; hardened por BLOCKER-006 (fixed). **Decisão**: TOPSIS é o input padrão da Fase 4; OWA (3 cenários) existe mas `use_owa=True` está implementado e deliberadamente não plugado no orquestrador. **Racional**: análise formal de incerteza de peso já é coberta pela Fase 8 (SA-1 OAT, SA-2 Monte Carlo Dirichlet seed=42) com base estatística — rodar OWA nos 3 cenários fixos duplicaria essa análise de forma mais crua, a ~4x custo de processamento/disco. TOPSIS é método estabelecido na literatura de siting (Hwang & Yoon, 1981) e já em uso para todo país processado. **Consequências**: toda descoberta de raster de suitability (`find_suitability_tif()`) tenta TOPSIS primeiro, sempre — BLOCKER-006 corrigiu uma divergência real onde `results_writer.py` tentava OWA-balanced primeiro, contradizendo esta decisão na prática. **Status**: Active (caminho TOPSIS); caminho OWA "In migration"/não ativado.

### D5 — Estilização de mapa e relatório de texto centralizados
**Data**: não documentada (v2.0). **Decisão**: todo mapa raster renderiza via `GeoWorldStyler.render_raster_map()`; todo relatório de texto de fase constrói via `build_phase_report()`. **Racional**: eliminar `_plot_*`/formatação de texto duplicados por fase. **Consequências**: consistência visual/textual entre as 9 fases — importante para um conjunto de figuras que deve ler como uma tese/publicação coerente. Novas fases devem usar essas funções, não escrever plotting/formatação bespoke.

### D6 — Escopo de abatimento GHG limitado ao setor elétrico
**Data**: não documentada. **Decisão**: todas as figuras/cálculos da Fase 7 modelam só substituição no setor de geração elétrica; qualquer "CO₂ nacional total" mostrado para contexto é um denominador maior e claramente diferente. **Racional**: evitar o erro analítico comum de implicar descarbonização economy-wide a partir de um modelo de substituição só-elétrico. **Consequências**: extensão da Fase 7 para outros setores exige módulo novo, explicitamente escopado (Transporte já é separado, na Fase 9).

### D7 — Ausência de suite de testes automatizada
**Data**: não documentada (inferida). **Status**: **Superseded** — `tests/unit/` existe desde QI-001 (82 testes em 2026-08-18). Mantida aqui só como registro histórico da decisão/ausência original, per regra append-only — não editar a entrada, só marcar superseded.

---

### D8 — `results_writer.py` (Fase 6) é o agregador final de resultados; nunca recalcula
**Data**: sessão de consolidação de documentação (2026-08-18, `archive/00-project-state-and-reorg-plan.md` Parte 7). **Decisão**: toda fase futura do GeoWorld deve, ao terminar, alimentar sua saída final na Fase 6 — ponto único de geração/teste/auditoria de resultado primário. **Condição vinculante**: só funciona se a Fase 6 **agregar, nunca recomputar** — lê valores já calculados/persistidos pela fase que os possui, nunca reabre um raster ou re-deriva uma estatística que outra fase já produziu. Essa é exatamente a distinção que separa um "agregador final" seguro do padrão que causou BLOCKER-001/002/003/009. **Consequências**: promove BLOCKER-010 (`TASKS.md`) de "deferido, alto risco" a **pré-requisito** desta arquitetura — ver D9.

### D9 — BLOCKER-010 é pré-requisito da arquitetura "Fase 6 = agregador"
**Data**: 2026-08-18. **Decisão**: toda nova fase adicionada sob o design D8 reproduzirá o mesmo padrão frágil de reconstrução-do-disco (que já causou 4 bugs numéricos confirmados) até BLOCKER-010 ser corrigido. Investigação de escopo concluída (causa raiz, raio de impacto, classificação de tamanho); implementação em si permanece fora de escopo até sessão dedicada. **Racional**: ver D8. **Consequências**: BLOCKER-010 tratado como Alta prioridade em `TASKS.md`, não como "um item entre muitos".

### D10 — Rastreador de tarefas único, nunca paralelo
**Data**: 2026-08-18 (consolidação `docs/`, commit `6643074` tornou `refactoring-roadmap.md`/`BACKLOG.md` o tracker canônico; nesta reorganização o papel passa para `TASKS.md`). **Decisão**: `docs/TASKS.md` é o único rastreador vivo de trabalho pendente do projeto — nenhuma outra lista de tarefas paralela deve ser criada em nenhum arquivo. **Racional**: a auditoria de 2026-08-18 encontrou o mesmo fato/tarefa narrado em 3–5 arquivos diferentes por falta dessa regra (ver diagnóstico da varredura de `docs/`). **Consequências**: ver `CLAUDE-DOCS-RULES.md`, regra "Registrar task em qualquer lugar fora de `TASKS.md` é proibido".
