# Maestro — AI Agent Orchestrator

## Назначение

Оркестратор для параллельной координации AI coding agents (Claude Code, Codex, Aider).
Распределяет задачи между агентами, изолирует их рабочие пространства через git worktrees,
управляет жизненным циклом задач через DAG-based scheduling, и интегрируется с Arbiter
для интеллектуального роутинга.

## Стек и зависимости

- **Язык**: Python (основной), интеграция с Rust-based Arbiter через MCP
- **Конфигурация задач**: tasks.yaml (DAG-описание задач)
- **Конфигурация агентов**: agents.toml (capabilities, constraints)
- **Per-repo настройки**: .maestro/workflow.yml + .maestro/WORKFLOW.md
- **Workspace isolation**: git worktrees
- **Сборка**: pyproject.toml

## Архитектурные принципы

1. **DAG-based Scheduling** — задачи описываются как направленный ациклический граф
   с зависимостями, что позволяет параллельное выполнение независимых задач
2. **Agent Agnostic** — оркестратор не привязан к конкретному агенту,
   работает через унифицированный интерфейс (Claude Code, Codex, Aider)
3. **Workspace Isolation** — каждый агент работает в изолированном git worktree,
   предотвращая конфликты при параллельной работе над одним репо
4. **Policy-Driven Routing** — выбор агента делегируется Arbiter (MCP server),
   который использует Decision Tree inference + 10 safety invariants

## Основные компоненты

### Ядро оркестратора
- **MaestroOrchestrator** — главный класс, управляет жизненным циклом задач
- **DAG Scheduler** — разбор tasks.yaml, построение графа зависимостей,
  определение задач для параллельного запуска
- **WorkflowLoader** — загрузка per-repo конфигурации из .maestro/
- **WorkspaceManager** — создание/cleanup git worktrees
- **ValidationRunner** — запуск тестов после завершения задачи

### Конфигурация
```
tasks.yaml          # DAG задач (task_id, depends_on, scope, description)
agents.toml         # Capabilities агентов (languages, tools, cost)
.maestro/
├── workflow.yml    # Runtime конфиг (max_parallel, timeout, retry, states)
└── WORKFLOW.md     # Агентная политика (промпт, инжектируется в system prompt)
```

### Интеграция с экосистемой
- **Arbiter** (Rust, MCP server) — роутинг задач к агентам,
  22-dim feature vector, budget tracking, scope isolation
- **ATP Platform** — верификация результатов агентов (опциональная интеграция)
- **spec-runner** — альтернативный execution mode через markdown specs
- **AppForge** — Claude Code skill, использующий паттерны Maestro
  для multi-agent app development

### Workflow States (Symphony-inspired)
```
active:   [in_progress, retrying]
terminal: [done, cancelled, failed]
handoff:  human_review
```

## Потоки данных

```
tasks.yaml → DAG Scheduler → [parallel tasks]
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
              WorkspaceManager  WorkspaceManager  WorkspaceManager
              (git worktree)    (git worktree)    (git worktree)
                    │               │               │
                    ▼               ▼               ▼
              Arbiter/Router → Agent Selection → Agent Execution
              (MCP call)     (Claude/Codex/     (в изолированном
                              Aider)             worktree)
                    │               │               │
                    ▼               ▼               ▼
              ValidationRunner → State Transition → Cleanup/PR
```

## Текущее состояние и направления развития

- WorkflowLoader + WORKFLOW.md паттерн (адаптация Symphony) — реализован
- Интеграция с Arbiter через MCP — в процессе / частичная
- DAG scheduler — ядро работает, расширение для complex dependencies
- Handoff states и human_review flow — дизайн готов
- ATP-интеграция для post-task validation — спроектирована

## Связанные проекты

| Проект | Роль | Связь с Maestro |
|--------|------|-----------------|
| Arbiter | Policy engine (Rust/MCP) | Роутинг задач к агентам |
| ATP Platform | Agent testing | Верификация результатов |
| spec-runner | Task execution | Альтернативный runner для markdown specs |
| AppForge | Claude Code skill | Использует паттерны Maestro |

## Ограничения для Cowork

- **НЕ** удаляй и не модифицируй файлы проекта без явного запроса
- **НЕ** трогай `.git/`, `__pycache__/`, `.venv/`, `node_modules/`
- **НЕ** модифицируй `.maestro/` конфиги в целевых репозиториях
- Все выходные файлы сохраняй в `_cowork_output/`
