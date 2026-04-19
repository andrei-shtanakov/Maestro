# Maestro — Архитектурная карта проекта

**Дата:** 2026-04-05
**Версия:** 0.1.0
**Python:** ≥3.12

---

## TL;DR

1. Maestro — оркестратор AI-агентов с двумя режимами: **Task Scheduler** (единый процесс, DAG задач) и **Multi-Process Orchestrator** (git worktree изоляция, spec-runner).
2. Ядро проекта — **33 Python-файла** в пакете `maestro/` (включая `__init__.py` подпакетов), покрытых **29 тестовыми файлами**. Архитектура чистая: модели без внутренних зависимостей, чёткое разделение слоёв.
3. Интеграция с **Arbiter** (policy-based routing) **заявлена в COWORK_CONTEXT.md, но не реализована** в коде — ни одного импорта/вызова Arbiter не обнаружено.
4. **WORKFLOW.md injection** описан в контексте, но **не реализован** — spawners строят промпт только из полей задачи (title, prompt, scope, retry context).
5. Код чистый: всего **1 TODO** в тестах, **0 FIXME/HACK**, все `pass` в except-блоках легитимны.

---

## 1. Структура проекта

```
maestro/                          # Основной пакет (22 файла)
├── __init__.py                   # Facade: экспорт ~40 публичных символов
├── models.py                     # Pydantic-модели (893 строки) — ядро домена
├── config.py                     # YAML-парсинг + env var substitution
├── database.py                   # SQLite async (aiosqlite), WAL mode
├── dag.py                        # DAG: топологическая сортировка, cycle detection
├── scheduler.py                  # Главный цикл Task Scheduler
├── orchestrator.py               # Главный цикл Multi-Process Orchestrator
├── cli.py                        # Typer CLI: 9 команд
├── git.py                        # Git операции: branches, worktrees, merge
├── workspace.py                  # Worktree lifecycle для orchestrator mode
├── decomposer.py                 # Декомпозиция проекта через Claude CLI
├── pr_manager.py                 # GitHub PR через gh CLI
├── validator.py                  # Post-task validation (subprocess)
├── retry.py                      # Exponential backoff + retry context
├── recovery.py                   # Crash recovery (orphaned tasks)
├── cost_tracker.py               # Token/cost parsing из логов агентов
├── event_log.py                  # JSONL event logging
├── spawners/                     # Подсистема запуска агентов
│   ├── __init__.py
│   ├── base.py                   # ABC AgentSpawner
│   ├── claude_code.py            # Claude Code spawner
│   ├── codex.py                  # OpenAI Codex spawner
│   ├── aider.py                  # Aider spawner
│   ├── announce.py               # Notification-only spawner
│   └── registry.py               # Реестр с auto-discovery (entry points)
├── coordination/                 # API координации агентов
│   ├── __init__.py
│   ├── mcp_server.py             # FastMCP server (claim/update/messages)
│   └── rest_api.py               # FastAPI REST mirror + zadachi endpoints
├── dashboard/                    # Web UI
│   ├── __init__.py
│   └── app.py                    # FastAPI dashboard + SSE + Mermaid.js DAG
└── notifications/                # Уведомления
    ├── __init__.py
    ├── base.py                   # ABC NotificationChannel
    ├── desktop.py                # macOS (osascript) / Linux (notify-send)
    └── manager.py                # Multi-channel dispatch

tests/                            # 29 тестовых файлов
├── conftest.py
├── test_async.py, test_cli.py, test_config.py
├── test_cost_tracker.py, test_dag.py, test_dashboard.py
├── test_database.py, test_decomposer.py, test_event_log.py
├── test_git.py, test_git_worktree.py
├── test_mcp_server.py, test_messages.py, test_models.py
├── test_notifications.py, test_orchestrator.py, test_package.py
├── test_pr_manager.py, test_recovery.py
├── test_rest_api.py, test_rest_zadachi.py, test_retry.py
├── test_scheduler.py, test_spawner_registry.py, test_spawners.py
├── test_validator.py, test_workspace.py

examples/
├── tasks.yaml                    # Пример конфига для Task Scheduler
└── project.yaml                  # Пример конфига для Orchestrator

spec/                             # Спецификации проекта
├── requirements.md, design.md, tasks.md
├── .executor-state.json, .executor-progress.txt
├── .task-history.log
└── .executor-logs/               # 48 лог-файлов от spec-runner
```

---

## 2. Карта модулей

### 2.1 `models.py` — Доменные модели (893 строки)

**Назначение:** Единственный источник истины для всех структур данных. Pydantic v2 модели с кастомными валидаторами.

**Публичный API:**

| Класс/Enum | Строки | Описание |
|---|---|---|
| `TaskStatus` (StrEnum) | 17-69 | 9 состояний, `valid_transitions()`, `is_terminal()` |
| `AgentType` (StrEnum) | 72-79 | CLAUDE_CODE, CODEX, AIDER, ANNOUNCE |
| `TaskConfig` (BaseModel) | 81-153 | Конфигурация задачи из YAML |
| `Task` (BaseModel) | 156-296 | Runtime-состояние, `transition_to()`, `from_config()` |
| `GitConfig` (BaseModel) | 299-313 | base_branch, auto_push, branch_prefix |
| `NotificationConfig` (BaseModel) | 316-334 | Desktop, telegram, webhook |
| `DefaultsConfig` (BaseModel) | 337-346 | Дефолты для задач |
| `ProjectConfig` (BaseModel) | 349-479 | Полная конфигурация (scheduler mode) |
| `TaskCost` (BaseModel) | 482-501 | Token usage + cost |
| `Message` (BaseModel) | 504-523 | Inter-agent messaging |
| `WorkspaceType` (StrEnum) | 531-534 | WORKTREE |
| `ZadachaStatus` (StrEnum) | 537-585 | 10 состояний (orchestrator mode) |
| `ZadachaConfig` (BaseModel) | 588-650 | Конфигурация задачи (orchestrator mode) |
| `Zadacha` (BaseModel) | 653-751 | Runtime-состояние (orchestrator mode) |
| `SpecRunnerConfig` (BaseModel) | 754-801 | Конфиг spec-runner, `to_executor_config()` |
| `OrchestratorConfig` (BaseModel) | 804-893 | Полная конфигурация (orchestrator mode) |

**Внутренние зависимости:** Нет (leaf module)
**Внешние зависимости:** `pydantic`, `re`, `datetime`, `enum`

---

### 2.2 `config.py` — Парсинг конфигурации (316 строк)

**Назначение:** Загрузка YAML, подстановка `${ENV_VARS}`, валидация через Pydantic.

**Публичный API:**

| Функция/Класс | Сигнатура |
|---|---|
| `ConfigError` | `__init__(message, path=None, line=None, column=None)` |
| `resolve_env_vars()` | `(value: Any, path: Path \| None) -> Any` |
| `load_config()` | `(path: Path \| str) -> ProjectConfig` |
| `load_orchestrator_config()` | `(path: Path \| str) -> OrchestratorConfig` |
| `load_config_from_string()` | `(content: str, path: Path \| None) -> ProjectConfig` |

**Внутренние зависимости:** `models` (ProjectConfig, OrchestratorConfig)
**Внешние зависимости:** `yaml`, `pydantic`

---

### 2.3 `database.py` — Persistence layer (1568 строк)

**Назначение:** Async SQLite с WAL mode. CRUD для tasks, zadachi, messages, costs. Optimistic concurrency control.

**Ключевые методы `Database`:**

- **Tasks:** `create_task()`, `get_task()`, `get_all_tasks()`, `update_task()`, `delete_task()`, `update_task_status()`, `get_tasks_by_status()`, `get_tasks_by_statuses()`
- **Dependencies:** `add_dependency()`, `remove_dependency()`, `get_task_dependencies()`, `get_dependent_tasks()`, `get_all_dependencies()`
- **Messages:** `save_message()`, `get_message()`, `get_messages_for_agent()`, `mark_message_read()`, `mark_messages_read()`, `delete_message()`
- **Costs:** `save_task_cost()`, `get_task_costs()`, `get_all_costs()`, `get_cost_summary()`
- **Zadachi:** `create_zadacha()`, `get_zadacha()`, `get_all_zadachi()`, `update_zadacha_status()`, `get_zadachi_by_status()`, `delete_zadacha()`

**Exceptions:** `DatabaseError`, `TaskNotFoundError`, `TaskAlreadyExistsError`, `ConcurrentModificationError`, `DependencyNotFoundError`, `MessageNotFoundError`, `ZadachaNotFoundError`, `ZadachaAlreadyExistsError`

**Внутренние зависимости:** `models`
**Внешние зависимости:** `aiosqlite`, `json`, `sqlite3`

---

### 2.4 `dag.py` — Граф зависимостей (426 строк)

**Назначение:** Построение DAG, cycle detection (алгоритм Кана), определение готовых задач, предупреждения о пересекающихся scope.

**Публичный API:**

| Класс/Метод | Описание |
|---|---|
| `CycleError(cycle_path)` | Исключение с путём цикла |
| `ScopeWarning` (dataclass) | task_ids, overlapping_patterns, suggestion |
| `DAGNode` (dataclass) | task_id, dependencies, dependents |
| `DAG(tasks: list[TaskConfig])` | Построение графа |
| `DAG.topological_sort()` | Топологическая сортировка |
| `DAG.get_ready_tasks(completed)` | Задачи без неудовлетворённых зависимостей |
| `DAG.check_scope_overlaps()` | Предупреждения о пересечении scope |

**Внутренние зависимости:** `models` (TaskConfig)
**Внешние зависимости:** `collections`, `dataclasses`, `fnmatch`

---

### 2.5 `scheduler.py` — Task Scheduler (903 строки)

**Назначение:** Главный async-цикл режима `maestro run`. Управляет lifecycle задач: spawn → monitor → validate → retry/done.

**Публичный API:**

| Класс | Ключевые элементы |
|---|---|
| `SchedulerError`, `TaskTimeoutError` | Исключения |
| `RunningTask` (dataclass) | task, process, started_at, log_file |
| `SchedulerConfig` (dataclass) | max_concurrent, poll_interval, workdir, log_dir |
| `BaseSpawner` (ABC) | `agent_type`, `spawn()`, `is_available()` |
| `Scheduler` | `run()`, `shutdown()`, `is_running`, `running_count` |
| `create_scheduler_from_config()` | Factory function |

**Внутренние зависимости:** `dag`, `database`, `models`, `notifications`, `retry`, `validator`
**Внешние зависимости:** `asyncio`, `subprocess`, `signal`

---

### 2.6 `orchestrator.py` — Multi-Process Orchestrator (646 строк)

**Назначение:** Главный async-цикл режима `maestro orchestrate`. Декомпозиция → worktree → spec-runner → monitor → PR.

**Публичный API:**

| Класс | Ключевые элементы |
|---|---|
| `OrchestratorError` | Базовое исключение |
| `RunningZadacha` (dataclass) | zadacha, process, started_at, workspace_path, log_file |
| `OrchestratorStats` (dataclass) | total, completed, failed, prs_created, start_time |
| `Orchestrator` | `run() -> OrchestratorStats`, `shutdown()` |

**Внутренние зависимости:** `database`, `decomposer`, `models`, `pr_manager`, `workspace`
**Внешние зависимости:** `asyncio`, `json`, `signal`, `subprocess`

---

### 2.7 `cli.py` — CLI интерфейс (948 строк)

**CLI команды:**

| Команда | Описание |
|---|---|
| `maestro run <config.yaml>` | Запуск Task Scheduler |
| `maestro status [--db]` | Статус задач |
| `maestro retry <task-id>` | Повтор задачи |
| `maestro stop` | Остановка через PID file |
| `maestro approve <task-id>` | Одобрение задачи |
| `maestro orchestrate <config.yaml>` | Запуск Multi-Process Orchestrator |
| `maestro zadachi [--db]` | Статус zadachi |
| `maestro workspaces [--workspace-base]` | Список worktrees |

**Точка входа:** `main()` → `app()` (Typer). `pyproject.toml`: `maestro = "maestro.cli:main"`

**Внутренние зависимости:** `config`, `dag`, `database`, `decomposer`, `git`, `models`, `orchestrator`, `pr_manager`, `workspace` + re-exports через `maestro.__init__`
**Внешние зависимости:** `typer`, `rich`, `asyncio`, `signal`

---

### 2.8 `git.py` — Git операции (551 строка)

**Назначение:** Все git-операции: branches, worktrees, merge, rebase, push.

**Branch operations:** `create_task_branch(task_id)`, `checkout(branch)`, `rebase_on_base()`, `push(branch)`, `auto_commit(task_id, title)`, `branch_exists(branch)`, `get_branch_list()`

**Worktree operations:** `create_worktree(path, branch)`, `create_worktree_existing_branch(path, branch)`, `remove_worktree(path, force)`, `list_worktrees()`, `prune_worktrees()`

**Merge operations:** `merge_branch(source, target, no_ff)`, `delete_branch(branch, force)`

**9 exception types:** `GitError`, `GitNotFoundError`, `BranchExistsError`, `BranchNotFoundError`, `RemoteError`, `RebaseConflictError`, `NotARepositoryError`, `MergeConflictError`, `WorktreeError`

**Внутренние зависимости:** Нет (leaf module)
**Внешние зависимости:** `subprocess`, `shutil`

---

### 2.9 `workspace.py` — Workspace management (183 строки)

**Публичный API:** `create_workspace(zadacha_id, branch) -> Path`, `setup_spec_runner(workspace_path, config)`, `cleanup_workspace(zadacha_id, force)`, `get_workspace_path(zadacha_id)`, `workspace_exists(zadacha_id)`, `list_workspaces()`, `cleanup_all()`

**Внутренние зависимости:** `git` (GitManager, WorktreeError)
**Внешние зависимости:** `yaml`, `shutil`

---

### 2.10 `decomposer.py` — Декомпозиция проекта (440 строк)

**Публичный API:** `decompose(project_description) -> list[ZadachaConfig]`, `generate_spec(zadacha, workspace_path)`, `validate_non_overlap(zadachi) -> list[ScopeOverlapWarning]`

**Внутренние зависимости:** `models` (ZadachaConfig)
**Внешние зависимости:** `subprocess`, `json`, `fnmatch`

---

### 2.11 `pr_manager.py` — GitHub PR (189 строк)

**Публичный API:** `is_available() -> bool`, `push_branch(branch)`, `create_pr(branch, title, body, base_branch) -> str`, `push_and_create_pr(...) -> str`

**Внутренние зависимости:** `git` (GitManager, RemoteError)
**Внешние зависимости:** `subprocess`, `shutil`

---

### 2.12 `spawners/` — Подсистема запуска агентов

#### `base.py` (102 строки) — ABC AgentSpawner
`agent_type` (abstract), `is_available()` (abstract), `spawn(task, workdir, log_file) -> Popen` (abstract), `build_prompt(task) -> str`

#### Реализации:
| Spawner | CLI команда | Ключевые флаги |
|---|---|---|
| `claude_code.py` | `claude --print --output-format json -p "{prompt}"` | stdout → log_file |
| `codex.py` | `codex --quiet --approval-mode auto-edit "{prompt}"` | |
| `aider.py` | `aider --yes-always --no-auto-commits --message "{prompt}" [files]` | scope → positional args |
| `announce.py` | `echo "{prompt}" > log_file` | Notification-only |

#### `registry.py` (364 строки) — SpawnerRegistry
`register(spawner)`, `get_spawner(agent_type, fallback)`, `set_fallback(spawner)`, `discover_entry_points()` (group: `maestro.spawners`), `discover_from_directory(path)`, `create_default_registry()`

---

### 2.13 `coordination/` — API координации

#### `mcp_server.py` (759 строк) — FastMCP server

MCP Tools: `get_available_tasks()`, `claim_task(task_id, agent_id)`, `update_status(task_id, status)`, `get_task_result(task_id)`, `post_message(from, to, msg)`, `read_messages(agent_id)`, `mark_messages_read(ids)`

Singleton: `get_server()` с `asyncio.Lock`

#### `rest_api.py` (1101 строк) — FastAPI REST

REST endpoints mirroring MCP + дополнительные: `/health`, `/tasks/*`, `/messages/*`, `/tasks/{id}/costs`, `/costs/summary`, `/zadachi/*`, `/zadachi/{id}/callback`

---

### 2.14 `dashboard/app.py` — Web Dashboard (322 строки)

Endpoints: `/dashboard` (HTML+Mermaid.js), `/api/dag` (JSON граф), `/api/tasks/stream` (SSE), `/api/tasks/{id}/retry` (POST), `/api/tasks/{id}/logs` (GET)

---

### 2.15 `notifications/` — Уведомления

| Класс | Описание |
|---|---|
| `NotificationEvent` (StrEnum) | 6 типов событий |
| `Notification` (dataclass) | `from_task()`, `format_title()`, `format_body()` |
| `NotificationChannel` (ABC) | `channel_type`, `is_available()`, `send()` |
| `DesktopNotifier` | macOS: `osascript`, Linux: `notify-send` |
| `NotificationManager` | Multi-channel dispatch |

---

### 2.16 Вспомогательные модули

| Модуль | Строки | Назначение |
|---|---|---|
| `validator.py` | 298 | Запуск `validation_cmd` как subprocess, timeout, capture stdout/stderr |
| `retry.py` | 98 | Exponential backoff (`base_delay * 2^count`), `build_retry_context()` |
| `recovery.py` | 185 | Recovery orphaned RUNNING/VALIDATING → READY после crash |
| `cost_tracker.py` | 310 | Парсинг JSON логов Claude Code, расчёт стоимости |
| `event_log.py` | 248 | JSONL logging, 17 типов событий, global singleton |

---

## 3. Граф зависимостей между модулями

```
                    ┌─────────────────────────────────────────┐
                    │              cli.py                      │
                    │  (10 внутренних зависимостей — hub)      │
                    └──┬──┬──┬──┬──┬──┬──┬──┬──┬──────────────┘
                       │  │  │  │  │  │  │  │  │
          ┌────────────┘  │  │  │  │  │  │  │  └──────────┐
          ▼               │  │  │  │  │  │  │              ▼
    ┌──────────┐          │  │  │  │  │  │  │       ┌────────────┐
    │ config   │          │  │  │  │  │  │  │       │ orchestrator│
    └────┬─────┘          │  │  │  │  │  │  │       └──┬──┬──┬───┘
         │                │  │  │  │  │  │  │          │  │  │
         ▼                ▼  │  │  │  │  │  │          │  │  │
    ┌──────────┐   ┌────────┐│  │  │  │  │  │          │  │  │
    │ models   │◄──│database││  │  │  │  │  │          │  │  │
    └──────────┘   └────────┘│  │  │  │  │  │          │  │  │
         ▲                   │  │  │  │  │  │          │  │  │
         │            ┌──────┘  │  │  │  │  │          │  │  │
         │            ▼         │  │  │  │  │          │  │  │
         │     ┌──────────┐    │  │  │  │  │          │  │  │
         ├─────│   dag     │    │  │  │  │  │          │  │  │
         │     └──────────┘    │  │  │  │  │          │  │  │
         │                     ▼  │  │  │  │          │  │  │
         │            ┌───────────┐│  │  │  │          │  │  │
         │            │ scheduler ││  │  │  │          │  │  │
         │            └───────────┘│  │  │  │          │  │  │
         │                        │  │  │  │          │  │  │
         │                        ▼  │  │  │          ▼  │  │
         │                 ┌────────┐│  │  │   ┌──────────┐│  │
         ├─────────────────│ git    ││  │  │   │workspace ││  │
         │                 └────────┘│  │  │   └──────────┘│  │
         │                           │  │  │               │  │
         │                           ▼  │  │               ▼  │
         │                  ┌───────────┐│  │     ┌───────────┐│
         ├──────────────────│decomposer ││  │     │pr_manager ││
         │                  └───────────┘│  │     └───────────┘│
         │                               │  │                  │
         │            spawners/          │  │                  │
         │     ┌─────────────────────┐   │  │                  │
         ├─────│ base → claude_code  │   │  │                  │
         ├─────│     → codex         │   │  │                  │
         ├─────│     → aider         │   │  │                  │
         ├─────│     → announce      │   │  │                  │
         │     │ registry            │   │  │                  │
         │     └─────────────────────┘   │  │                  │
         │                               ▼  │                  │
         │     coordination/       ┌────────┐│                 │
         ├─────────────────────────│mcp_srvr││                 │
         │                        └────────┘│                  │
         │                                  ▼                  │
         │                         ┌──────────┐                │
         ├─────────────────────────│ rest_api │                │
         │                         └──────────┘                │
         │                                                     │
         │     notifications/                                  │
         │     ┌──────────────────────┐                        │
         ├─────│ base → desktop       │                        │
         │     │ manager              │                        │
         │     └──────────────────────┘                        │
         │                                                     │
         │     Leaf modules (нет внутренних зависимостей):     │
         │     ┌──────────────────────────────┐                │
         │     │ validator, event_log, git     │                │
         │     └──────────────────────────────┘                │
         │                                                     │
         │     Зависят только от models:                       │
         │     ┌──────────────────────────────┐                │
         └─────│ retry, recovery, cost_tracker│                │
               └──────────────────────────────┘
```

**Leaf-модули (нет внутренних зависимостей):** `models`, `git`, `validator`, `event_log`
**Hub-модули (много зависимостей):** `cli` (10), `scheduler` (6), `orchestrator` (5), `rest_api` (4)

---

## 4. Точки входа

| Точка входа | Тип | Файл | Описание |
|---|---|---|---|
| `maestro` CLI | `pyproject.toml` entry point | `cli.py:942` | Основная CLI через Typer |
| `main.py` | Python script | `main.py:5` | `from maestro.cli import main; main()` |
| REST API | FastAPI app | `rest_api.py` | `create_app_with_lifespan(db_path)` |
| MCP Server | FastMCP singleton | `mcp_server.py` | `get_server()` |
| Dashboard | FastAPI app | `dashboard/app.py` | `create_dashboard_app(db, log_dir)` |

---

## 5. Ключевые потоки

### 5.1 Парсинг tasks.yaml → DAG construction

```
cli.py: run_command(config_path)
  → config.py: load_config(path)
    → yaml.safe_load() + resolve_env_vars()
    → ProjectConfig.model_validate(data)
      → validate_unique_task_ids()
      → validate_dependencies_exist()
      → validate_no_cyclic_dependencies()  # Строит граф в памяти
      → apply_defaults_to_tasks()          # Мержит DefaultsConfig
  → dag.py: DAG(config.tasks)
    → _build_graph()                       # Строит nodes + edges
    → _detect_cycles()                     # Алгоритм Кана
    → topological_sort()                   # Определяет порядок
    → get_ready_tasks(completed=set())     # Начальные задачи
```

### 5.2 Выбор агента

```
tasks.yaml:
  - id: my-task
    agent_type: claude_code    # Жёстко задан в конфиге

scheduler.py: Scheduler.__init__(spawners={...})
  → spawners: dict[str, SpawnerProtocol]  # Из registry

scheduler.py: _spawn_task(task)
  → spawner = self._spawners.get(task.agent_type)
  → spawner.spawn(task, workdir, log_file)
```

**🔴 Arbiter integration:** Описана в COWORK_CONTEXT.md как "Policy-Driven Routing через MCP" с "22-dim feature vector, budget tracking, scope isolation". В коде **не реализована** — ни одного import/вызова Arbiter. Выбор агента статический из YAML.

### 5.3 Создание/удаление worktrees

```
# Создание (orchestrator.py:284 → workspace.py:55 → git.py:384)
Orchestrator._spawn_zadacha(zadacha_id)
  → workspace_mgr.create_workspace(zadacha_id, branch)
    → git_manager.create_worktree(path, branch)
      → subprocess: git worktree add {workspace_base}/{zadacha_id} -b {branch}
  → workspace_mgr.setup_spec_runner(path, config)
    → Пишет executor.config.yaml + spec/

# Удаление (orchestrator.py:453 → workspace.py:118 → git.py:426)
Orchestrator._handle_success(zadacha_id)
  → workspace_mgr.cleanup_workspace(zadacha_id, force=True)
    → git_manager.remove_worktree(path, force=True)
      → subprocess: git worktree remove --force {path}
    → shutil.rmtree(path)  # fallback
```

### 5.4 WORKFLOW.md injection в промпт агента

**🔴 НЕ РЕАЛИЗОВАНО.**

`AgentSpawner.build_prompt(task)` (файл `spawners/base.py:67-101`) строит промпт из:
- `task.title`
- `task.prompt`
- `task.scope` (список файлов)
- `retry_context` (при повторе: error + validation output)

`.maestro/WORKFLOW.md` упоминается в COWORK_CONTEXT.md как "инжектируется в system prompt", но ни один spawner его не читает.

---

## 6. Внешние зависимости

### Runtime (pip)

| Пакет | Версия | Где используется |
|---|---|---|
| `aiosqlite` | ≥0.22.1 | `database.py` |
| `fastapi` | ≥0.128.1 | `rest_api.py`, `dashboard/app.py` |
| `fastmcp` | ≥2.14.5 | `mcp_server.py` |
| `pydantic` | ≥2.12.5 | `models.py`, `config.py`, `event_log.py`, API models |
| `pyyaml` | ≥6.0.3 | `config.py`, `workspace.py` |
| `rich` | ≥14.3.2 | `cli.py` |
| `typer` | ≥0.21.1 | `cli.py` |
| `uvicorn` | ≥0.40.0 | REST/dashboard serving |

### Dev

| Пакет | Версия |
|---|---|
| `pytest` | ≥9.0.2 |
| `pytest-asyncio` | ≥1.3.0 |
| `pytest-cov` | ≥7.0.0 |
| `ruff` | ≥0.15.0 |
| `pyrefly` | ≥0.51.0 |
| `anyio` | ≥4.12.1 |

### System tools

| Инструмент | Где | Обязательный? |
|---|---|---|
| `git` | `git.py` | Да |
| `claude` | `spawners/claude_code.py`, `decomposer.py` | Да (для Claude Code) |
| `codex` | `spawners/codex.py` | Нет |
| `aider` | `spawners/aider.py` | Нет |
| `gh` | `pr_manager.py` | Для PR creation |
| `spec-runner` | `orchestrator.py` | Для orchestrator mode |
| `osascript` | `notifications/desktop.py` | macOS notifications |
| `notify-send` | `notifications/desktop.py` | Linux notifications |

---

## 7. Незавершённые модули и TODO/FIXME/HACK

### Найденные маркеры

| Файл | Строка | Содержание |
|---|---|---|
| `tests/test_git_worktree.py` | 316 | `TODO: merge_branch should also check stdout for CONFLICT.` |

**Больше TODO/FIXME/HACK/XXX в коде нет.**

### 🔴 Нереализованная функциональность (high impact)

1. **Arbiter integration** — COWORK_CONTEXT.md описывает policy-based routing через Arbiter MCP server (22-dim feature vector, budget tracking, scope isolation). В коде **нет ни одного упоминания** Arbiter. Выбор агента статический из YAML.

2. **WORKFLOW.md injection** — COWORK_CONTEXT.md описывает `WorkflowLoader` и `.maestro/WORKFLOW.md` для инжекции в system prompt агента. В коде **нет класса WorkflowLoader**, spawners не читают WORKFLOW.md.

3. **ATP Platform integration** — Упоминается как "верификация результатов агентов", в коде не используется.

### 🟡 Частично реализовано (medium impact)

4. **Telegram/Webhook notifications** — `NotificationConfig` имеет поля `telegram_token`, `telegram_chat_id`, `webhook_url`, но реализован только `DesktopNotifier`. Каналы telegram и webhook не имеют реализаций.

5. **Cost tracking для Codex/Aider** — `parse_log()` для Codex и Aider возвращает нулевой `TokenUsage`. Парсинг реализован только для Claude Code JSON.

6. **Dashboard static files** — HTML генерируется inline в коде. Static files mount зарегистрирован, но директория может быть пустой.

### 🟢 Легитимные pass-блоки (low impact)

| Файл | Строка | Контекст |
|---|---|---|
| `pr_manager.py` | 164 | `except (TimeoutExpired, FileNotFoundError): pass` — graceful gh CLI fallback |
| `database.py` | 173 | `except ValueError: pass` — fallback datetime parsing |
| `orchestrator.py` | 619 | `except OSError: pass` — process kill cleanup |
| `scheduler.py` | 819 | `except OSError: pass` — zombie process reap |

---

## Открытые вопросы к автору

1. **Arbiter integration** — планируется ли реализация? Как будет выглядеть интерфейс: MCP tool call из scheduler → Arbiter → agent_type? Или отдельный pre-processing step?

2. **WORKFLOW.md injection** — это осознанный пропуск или запланированная фича? Где должен загружаться файл — в `AgentSpawner.build_prompt()` или в отдельном `WorkflowLoader`?

3. **Telegram/Webhook notifiers** — планируется реализация? Модели готовы, нужны только `NotificationChannel` имплементации.

4. **Cost tracking для Codex/Aider** — есть ли формат логов, который можно парсить? Или это low priority?

5. **spec-runner** — это отдельный PyPI пакет? Почему его нет в dependencies `pyproject.toml`?

6. **Тесты для orchestrator mode** — `test_orchestrator.py` существует, но насколько полно покрыт flow decompose → spawn → monitor → PR?

7. **Dashboard** — Mermaid.js загружается из CDN или локально? Нужна ли поддержка offline?
