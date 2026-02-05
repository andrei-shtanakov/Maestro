# Tasks

> Задачи с приоритетами, зависимостями и трассировкой к требованиям

## Легенда

**Приоритет:**
- 🔴 P0 — Critical, блокирует релиз
- 🟠 P1 — High, нужно для полноценного использования
- 🟡 P2 — Medium, улучшение опыта
- 🟢 P3 — Low, nice to have

**Статус:**
- ⬜ TODO
- 🔄 IN PROGRESS
- ✅ DONE
- ⏸️ BLOCKED

**Оценка:**
- Указывается в днях (d) или часах (h)

---

## Definition of Done (для КАЖДОЙ задачи)

> ⚠️ Задача НЕ считается завершённой без выполнения этих пунктов:

- [ ] **Unit tests** — покрытие ≥80% нового кода
- [ ] **Tests pass** — `uv run pytest` проходит
- [ ] **Type check** — `pyrefly check` без ошибок
- [ ] **Lint** — `uv run ruff check .` без ошибок
- [ ] **Docs updated** — docstrings для публичных API

---

## Project Setup

### TASK-000: Project Scaffolding
🔴 P0 | ✅ DONE | Est: 0.5d

**Description:**
Создать структуру проекта и настроить зависимости.

**Checklist:**
- [x] Создать директории: `maestro/`, `maestro/spawners/`, `maestro/coordination/`, `maestro/notifications/`, `tests/`, `examples/`
- [x] `__init__.py` файлы во всех пакетах
- [x] `pyproject.toml` с зависимостями: pydantic, fastapi, uvicorn, fastmcp, pyyaml, click/typer, aiosqlite
- [x] Dev dependencies: pytest, pytest-asyncio, pytest-cov, ruff
- [x] `uv sync` для создания виртуального окружения
- [x] `.gitignore` для Python проекта

**Tests:**
- [x] `uv run python -c "import maestro"` работает

**Traces to:** [NFR-000]
**Depends on:** —
**Blocks:** [TASK-100]

---

## Testing Tasks (обязательные)

### TASK-100: Test Infrastructure Setup
🔴 P0 | ✅ DONE | Est: 1d

**Description:**
Настроить тестовую инфраструктуру проекта.

**Checklist:**
- [x] pytest + pytest-asyncio setup
- [x] Coverage reporting (pytest-cov)
- [x] conftest.py with fixtures
- [x] pyrefly init for type checking
- [x] ruff configuration in pyproject.toml

**Traces to:** [NFR-000]
**Depends on:** [TASK-000]
**Blocks:** All other tasks

---

## Milestone 1: MVP

### TASK-001: Pydantic Models
🔴 P0 | ✅ DONE | Est: 1d

**Description:**
Создать Pydantic модели для Task, TaskConfig, ProjectConfig и связанных типов.

**Checklist:**
- [x] TaskStatus enum с валидными переходами
- [x] TaskConfig модель для YAML parsing
- [x] Task модель для runtime state
- [x] ProjectConfig с defaults и git settings
- [x] Валидация через Pydantic validators

**Tests:**
- [x] Unit: model validation, status transitions
- [x] Unit: serialization/deserialization

**Traces to:** [REQ-001], [DESIGN-001], [DESIGN-003]
**Depends on:** [TASK-100]
**Blocks:** [TASK-002], [TASK-003]

---

### TASK-002: Config Parser
🔴 P0 | ✅ DONE | Est: 1d

**Description:**
Парсер YAML-конфигурации с поддержкой defaults и env variables.

**Checklist:**
- [x] YAML loading с PyYAML
- [x] Defaults merging (project-level → task-level)
- [x] Environment variable substitution (${VAR})
- [x] Schema validation через Pydantic
- [x] Error messages с указанием позиции

**Tests:**
- [x] Unit: valid config parsing
- [x] Unit: defaults merging
- [x] Unit: env variable substitution
- [x] Unit: validation errors

**Traces to:** [REQ-001], [DESIGN-001]
**Depends on:** [TASK-001]
**Blocks:** [TASK-004]

---

### TASK-003: SQLite Database Layer
🔴 P0 | ✅ DONE | Est: 1d

**Description:**
Database layer для хранения состояния задач.

**Checklist:**
- [x] SQLite connection с WAL mode
- [x] Schema creation/migration
- [x] CRUD операции для tasks
- [x] CRUD для task_dependencies
- [x] Atomic status updates с WHERE clause
- [x] Query by status

**Tests:**
- [x] Unit: CRUD operations
- [x] Unit: atomic updates (concurrent access)
- [x] Integration: full lifecycle

**Traces to:** [REQ-003], [DESIGN-003]
**Depends on:** [TASK-001]
**Blocks:** [TASK-005], [TASK-020]

---

### TASK-004: DAG Builder
🔴 P0 | ✅ DONE | Est: 1d

**Description:**
Построение DAG из конфигурации, валидация, topological sort.

**Checklist:**
- [x] Graph construction из depends_on
- [x] Cycle detection (Kahn's algorithm)
- [x] Topological sort
- [x] get_ready_tasks() по completed set
- [x] Scope overlap detection

**Tests:**
- [x] Unit: simple DAG construction
- [x] Unit: cycle detection
- [x] Unit: topological sort
- [x] Unit: ready tasks calculation
- [x] Unit: scope overlap warning

**Traces to:** [REQ-002], [REQ-004], [DESIGN-002]
**Depends on:** [TASK-002]
**Blocks:** [TASK-005]

---

### TASK-005: Scheduler Core
🔴 P0 | ✅ DONE | Est: 2d

**Description:**
Основной scheduler loop: resolve ready tasks → spawn → monitor.

**Checklist:**
- [x] asyncio event loop
- [x] Ready task resolution через DAG
- [x] Concurrency limit (max_concurrent)
- [x] Task timeout handling
- [x] Process monitoring (Popen)
- [x] Status transitions на completion
- [x] Graceful shutdown (SIGTERM)

**Tests:**
- [x] Unit: ready task resolution
- [x] Unit: concurrency limiting
- [x] Integration: full execution with mock spawner
- [x] Integration: timeout handling
- [x] Integration: graceful shutdown

**Traces to:** [REQ-002], [REQ-003]
**Depends on:** [TASK-003], [TASK-004]
**Blocks:** [TASK-010]

---

### TASK-010: Claude Code Spawner
🔴 P0 | ✅ DONE | Est: 1d

**Description:**
Spawner для Claude Code в headless-режиме.

**Checklist:**
- [x] AgentSpawner ABC
- [x] ClaudeCodeSpawner implementation
- [x] is_available() проверка
- [x] spawn() с --print --output-format json
- [x] Prompt building с context
- [x] Log file capture

**Tests:**
- [x] Unit: prompt building
- [x] Unit: is_available check
- [x] Integration: spawn with mock (echo)

**Traces to:** [REQ-010], [REQ-011], [DESIGN-010], [DESIGN-011]
**Depends on:** [TASK-005]
**Blocks:** [TASK-012]

---

### TASK-011: Spawner Registry
🟠 P1 | ✅ DONE | Est: 0.5d

**Description:**
Auto-discovery и registry для spawner plugins.

**Checklist:**
- [x] SpawnerRegistry class
- [x] Auto-discovery через entry points или directory scan
- [x] get_spawner(agent_type) lookup
- [x] Fallback handling

**Tests:**
- [x] Unit: registry operations
- [x] Unit: spawner discovery

**Traces to:** [REQ-011], [DESIGN-011]
**Depends on:** [TASK-010]
**Blocks:** —

---

### TASK-012: Git Manager
🔴 P0 | ✅ DONE | Est: 1d

**Description:**
Git операции: создание веток, push, rebase.

**Checklist:**
- [x] GitManager class
- [x] create_task_branch(task_id)
- [x] checkout existing branch
- [x] rebase_on_base()
- [x] push()
- [x] get_current_branch()
- [x] Error handling для git failures

**Tests:**
- [x] Integration: branch creation (temp repo)
- [x] Integration: push (mock remote)
- [x] Unit: command building

**Traces to:** [REQ-012], [DESIGN-012]
**Depends on:** [TASK-010]
**Blocks:** [TASK-030]

---

### TASK-006: CLI Interface
🔴 P0 | ✅ DONE | Est: 1d

**Description:**
CLI с командами run, status, retry, stop.

**Checklist:**
- [x] Click/Typer setup
- [x] `maestro run tasks.yaml` command
- [x] `maestro status` command
- [x] `maestro retry <task-id>` command
- [x] `maestro stop` command
- [x] `--resume` flag для recovery
- [x] Pretty output (rich/click styling)

**Tests:**
- [x] Unit: command parsing
- [x] Integration: full flow

**Traces to:** [REQ-001], [REQ-003]
**Depends on:** [TASK-005]
**Blocks:** —

---

## Milestone 2: Coordination

### TASK-020: MCP Server
🔴 P0 | ✅ DONE | Est: 1d

**Description:**
FastMCP сервер для координации агентов.

**Checklist:**
- [x] FastMCP setup
- [x] get_available_tasks tool
- [x] claim_task tool (atomic)
- [x] update_status tool
- [x] get_task_result tool
- [x] Интеграция с database layer

**Tests:**
- [x] Unit: tool handlers
- [x] Integration: claim conflict
- [x] Integration: status updates

**Traces to:** [REQ-020], [DESIGN-020]
**Depends on:** [TASK-003], [TASK-005]
**Blocks:** [TASK-022]

---

### TASK-021: REST API
🟠 P1 | ✅ DONE | Est: 1d

**Description:**
FastAPI REST API с теми же эндпоинтами.

**Checklist:**
- [x] FastAPI app setup
- [x] /tasks endpoints
- [x] /tasks/{id}/claim endpoint
- [x] /tasks/{id}/status endpoint
- [x] /health endpoint
- [x] OpenAPI documentation

**Tests:**
- [x] Integration: API endpoints
- [x] Integration: concurrent claims

**Traces to:** [REQ-021], [DESIGN-021]
**Depends on:** [TASK-003], [TASK-005]
**Blocks:** —

---

### TASK-022: Inter-Agent Messages
🟡 P2 | ✅ DONE | Est: 0.5d

**Description:**
Система сообщений между агентами.

**Checklist:**
- [x] messages table in DB
- [x] post_message MCP tool
- [x] read_messages MCP tool
- [x] REST endpoints for messages
- [x] Broadcast support (to_agent=null)

**Tests:**
- [x] Unit: message CRUD
- [x] Integration: broadcast

**Traces to:** [REQ-020], [DESIGN-020]
**Depends on:** [TASK-020]
**Blocks:** —

---

### TASK-030: Post-Task Validation
🟠 P1 | ✅ DONE | Est: 1d

**Description:**
Валидация результатов через validation_cmd.

**Checklist:**
- [x] Validator class
- [x] validation_cmd execution
- [x] Timeout handling
- [x] Exit code → success/failure
- [x] Output capture для retry context
- [x] VALIDATING status integration

**Tests:**
- [x] Unit: validation execution
- [x] Integration: success/failure flows

**Traces to:** [REQ-030], [DESIGN-030]
**Depends on:** [TASK-012]
**Blocks:** [TASK-031]

---

### TASK-031: Retry Logic
🟠 P1 | ✅ DONE | Est: 0.5d

**Description:**
Retry с exponential backoff и контекстом ошибки.

**Checklist:**
- [x] RetryManager class
- [x] Exponential backoff calculation
- [x] Error context injection в prompt
- [x] max_retries limit
- [x] NEEDS_REVIEW transition

**Tests:**
- [x] Unit: backoff calculation
- [x] Unit: context building
- [x] Integration: retry flow

**Traces to:** [REQ-031], [DESIGN-031]
**Depends on:** [TASK-030]
**Blocks:** —

---

### TASK-032: State Recovery
🟠 P1 | ✅ DONE | Est: 0.5d

**Description:**
Восстановление состояния после crash.

**Checklist:**
- [x] StateRecovery class
- [x] RUNNING → READY transition
- [x] VALIDATING → READY transition
- [x] --resume CLI flag integration
- [x] Recovery statistics

**Tests:**
- [x] Integration: crash recovery simulation

**Traces to:** [REQ-032], [DESIGN-032]
**Depends on:** [TASK-003]
**Blocks:** —

---

## Milestone 3: Production Ready

### TASK-040: Desktop Notifications
🟡 P2 | ✅ DONE | Est: 0.5d

**Description:**
Desktop notifications через notify-send или macOS.

**Checklist:**
- [x] NotificationChannel ABC
- [x] DesktopNotifier implementation
- [x] Platform detection (Linux/macOS)
- [x] Configuration in YAML

**Tests:**
- [x] Unit: notification formatting
- [x] Manual: visual verification

**Traces to:** [REQ-040]
**Depends on:** [TASK-005]
**Blocks:** —

---

### TASK-041: Additional Spawners
🟡 P2 | ✅ DONE | Est: 1d

**Description:**
Spawners для Codex CLI и Aider.

**Checklist:**
- [x] CodexSpawner implementation
- [x] AiderSpawner implementation
- [x] AnnounceSpawner (notification-only)
- [x] Registry integration

**Tests:**
- [x] Unit: is_available checks
- [x] Integration: spawn with mock

**Traces to:** [REQ-011]
**Depends on:** [TASK-011]
**Blocks:** —

---

### TASK-050: Web Dashboard
🟡 P2 | ✅ DONE | Est: 2d

**Description:**
Веб-интерфейс для мониторинга.

**Checklist:**
- [x] FastAPI static serving
- [x] DAG visualization (Mermaid.js)
- [x] Real-time updates (SSE)
- [x] Task status colors
- [x] Retry button
- [x] Log viewer

**Tests:**
- [x] Integration: SSE events
- [x] Manual: visual verification

**Traces to:** [REQ-050]
**Depends on:** [TASK-021]
**Blocks:** —

---

### TASK-060: Cost Tracking
🟢 P3 | ✅ DONE | Est: 1d

**Description:**
Трекинг token usage и стоимости.

**Checklist:**
- [x] task_costs table
- [x] Log parsing для token counts
- [x] Cost calculation
- [x] Summary report

**Tests:**
- [x] Unit: log parsing
- [x] Unit: cost calculation

**Traces to:** —
**Depends on:** [TASK-010]
**Blocks:** —

---

## Dependency Graph

```
TASK-000 (Project Scaffolding)
    │
    └──► TASK-100 (Test Infrastructure)
             │
             ├──► TASK-001 (Pydantic Models)
    │        │
    │        ├──► TASK-002 (Config Parser)
    │        │        │
    │        │        └──► TASK-004 (DAG Builder)
    │        │                 │
    │        │                 └──► TASK-005 (Scheduler Core)
    │        │                          │
    │        │                          ├──► TASK-006 (CLI)
    │        │                          │
    │        │                          ├──► TASK-010 (Claude Spawner)
    │        │                          │        │
    │        │                          │        ├──► TASK-011 (Spawner Registry)
    │        │                          │        │        │
    │        │                          │        │        └──► TASK-041 (Additional Spawners)
    │        │                          │        │
    │        │                          │        └──► TASK-012 (Git Manager)
    │        │                          │                 │
    │        │                          │                 └──► TASK-030 (Validation)
    │        │                          │                          │
    │        │                          │                          └──► TASK-031 (Retry Logic)
    │        │                          │
    │        │                          ├──► TASK-020 (MCP Server)
    │        │                          │        │
    │        │                          │        └──► TASK-022 (Messages)
    │        │                          │
    │        │                          ├──► TASK-021 (REST API)
    │        │                          │        │
    │        │                          │        └──► TASK-050 (Dashboard)
    │        │                          │
    │        │                          └──► TASK-040 (Notifications)
    │        │
    │        └──► TASK-003 (Database Layer)
    │                 │
    │                 └──► TASK-032 (State Recovery)
    │
    └──► TASK-060 (Cost Tracking) — parallel track
```

---

## Summary by Milestone

### MVP
| Priority | Count | Est. Total |
|----------|-------|------------|
| 🔴 P0 | 10 | 11d |
| 🟠 P1 | 1 | 0.5d |
| **Total** | **11** | **~11.5d** |

### Coordination
| Priority | Count | Est. Total |
|----------|-------|------------|
| 🔴 P0 | 1 | 1d |
| 🟠 P1 | 4 | 3d |
| 🟡 P2 | 1 | 0.5d |
| **Total** | **6** | **~4.5d** |

### Production Ready
| Priority | Count | Est. Total |
|----------|-------|------------|
| 🟡 P2 | 3 | 3.5d |
| 🟢 P3 | 1 | 1d |
| **Total** | **4** | **~4.5d** |

---

## Risk Register

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| Claude Code headless mode нестабилен | High | Medium | Таймауты, ретраи, fallback на announce |
| Агент выходит за scope | Medium | High | Warning в prompt, post-validation |
| SQLite concurrency bottleneck | Low | Low | WAL mode, atomic updates |
| Git conflicts при merge | Medium | Medium | Scope validation, branch isolation |

---

## Notes

- Start with TASK-100 (test infrastructure) before any implementation
- MVP tasks are sequential due to strong dependencies
- Coordination milestone can start after TASK-005 (scheduler)
- Dashboard is optional for initial release
