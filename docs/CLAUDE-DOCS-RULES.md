---
id: docs-claude-docs-rules
type: reference
status: active
created: 2026-08-18
updated: 2026-08-18
updated_by: claude-code
version: 1
depends_on: [docs-tasks, docs-decisions, docs-sprint]
linked_by: [docs-readme]
scope: "Regras de manutenção obrigatórias para docs/ — o que fazer ao terminar uma task, tomar uma decisão, ou terminar uma sessão, e o que nunca fazer. Não é narrativa nem tracker — é a política que governa os outros arquivos."
---

# CLAUDE-DOCS-RULES.md — Regras de manutenção de `docs/`

> **Nota**: este arquivo será mesclado ao `CLAUDE.md` da estação de trabalho principal. Até lá, vale como a política vigente para qualquer sessão (humana ou IA) que edite `docs/`.

Esta reorganização (2026-08-18) existiu porque o mesmo fato vivia em 3–5 arquivos diferentes e uma quantidade real de trabalho aberto não tinha dono nem número de rastreio. As regras abaixo existem para que isso não se repita.

---

## OBRIGATÓRIO ao terminar qualquer task

1. Marcar a task como concluída em `TASKS.md` (mover para a seção ✅).
2. Atualizar o campo `updated:` do frontmatter do(s) arquivo(s) tocado(s) para a data de hoje.
3. Incrementar `version:` em 1 no frontmatter do(s) arquivo(s) tocado(s).

## OBRIGATÓRIO ao tomar uma decisão arquitetural

- Adicionar uma nova entrada a `DECISIONS.md` (nunca sobrescrever uma entrada existente — se uma decisão substitui outra, a antiga ganha `Status: Superseded by D<N>`, não é apagada nem editada).

## OBRIGATÓRIO ao final de toda sessão

- Adicionar 1 linha ao log de sessão em `SPRINT.md` (seção "Log — últimas 5 sessões"). Se isso ultrapassar 5 entradas, mover a mais antiga para `archive/session-log.md` antes de adicionar a nova.

---

## PROIBIDO, sempre

- Criar um novo arquivo `.md` em `docs/` sem perguntar antes.
- Escrever o mesmo fato em mais de um arquivo — escrever uma vez, linkar nos demais. Se notar um fato duplicado, corrigir na hora (deixar um dono, trocar os outros por link), não adicionar um terceiro lugar.
- Editar qualquer arquivo com `status: frozen` ou `status: archived` no frontmatter (isso cobre toda `analysis/` e toda `archive/`). Se um achado desses arquivos precisar de atualização de status, essa atualização vai em `TASKS.md`/`DECISIONS.md`, nunca como edição do arquivo congelado.
- Registrar uma task em qualquer lugar que não seja `TASKS.md` — nem em comentário de código, nem em outro `.md`, nem em `memory/`.

---

## Convenção de frontmatter (todo arquivo de `docs/`)

```yaml
---
id: <ID-ÚNICO>
type: tracker | reference | frozen | archive | index
status: active | frozen | archived
created: <data original, ou hoje se desconhecida>
updated: <hoje>
updated_by: claude-code
version: 1
depends_on: [...]
linked_by: [...]
scope: "<1 frase: o que este arquivo É e o que ele NÃO é>"
---
```
