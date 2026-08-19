---
id: docs-sprint
type: tracker
status: active
created: 2026-08-18
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [archive-session-log]
linked_by: [docs-readme]
scope: "Status atual em uma linha, foco da sessão, regras de coordenação e log condensado das últimas 5 sessões; histórico completo vive em archive/session-log.md, não aqui."
---

# SPRINT.md

> Gerado/atualizado pelo Claude Code a partir do estado real do repositório. Em caso de dúvida, este arquivo e `docs/TASKS.md` são a fonte de verdade, não o histórico da conversa.

---

## Status

**2026-08-18** — INVAR-002 fechado (commit `a9ff465`); próximo item: **INVAR-003**. `docs/` reorganizado nesta mesma data (ver `archive/00-project-state-and-reorg-plan.md` para o registro do reorg anterior, substituído por este).

## Foco atual

Invariant Validation Project (INVAR-003 em diante) e a decisão de escopo pendente do BLOCKER-010 (paridade de shape via `recover_potential_from_disk()` vs. marcador `_source`) — ver `TASKS.md` 🔴/🟡.

---

## Regras de coordenação de sessão

- Antes de qualquer commit, rodar `git status`/`git diff` e confirmar que não há trabalho pendente de outra sessão na árvore.
- Nunca rodar duas sessões simultaneamente na mesma árvore de trabalho sem `git worktree` separado.
- Mostrar diff antes de escrever, sempre.
- Commit local apenas, sem push.
- Código/commits/docs técnicos em inglês.
- Ferramenta de apoio: `scripts/session_lock.py acquire/release --session <nome>`.

---

## Log — últimas 5 sessões

Histórico completo (todas as sessões anteriores) → `archive/session-log.md`.

1. **2026-08-18** — Fechado INVAR-002 (commit `a9ff465`): `resolution_tolerance` confirmado sem efeito downstream; `logger.warning` adicionado para chave ausente; 2 testes, 82/82 passando.
2. **2026-08-18** — Fechado INVAR-001 (commit `25c7ec4`): `ConfigLoader.has_country()` adicionado; `except Exception` genérico removido de `DataAuditor.run()`; 2 testes, 80/80 passando.
3. **2026-08-18** — Invariant Validation Project registrado (16 itens, INVAR-001–016); BLOCKER-020 consolidado em INVAR-004. Só documentação, nenhum arquivo `src/` tocado.
4. **2026-08-18** — Investigação de escopo do BLOCKER-010 concluída (sem código alterado): causa raiz rastreada via `git log -S` até commit `153a1cc`; `_normalize_potential`/`_normalize_lcoe` já aceitam `.model_dump()` sem mudança; fix confinado a `main.py`, pronto para implementação direta.
5. **2026-08-18** — Removido caminho de dict cru para `country_params` (commits `de87ceb`, `6f3bfbc`) — 5 funções agora exigem `CountryParams` validado; Campaign #1 OAT sweep de `concentration` fechado (commit `2204109`), achado de heterogeneidade aberto como GAP-005.
