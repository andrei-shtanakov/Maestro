# Maestro — Structured Task Backlog

**Дата:** 2026-04-05
**Источник:** `06-roadmap.md`

---

## TL;DR

1. **27 задач** суммарно: 8 Quick Wins (P0), 15 Medium Effort (P0–P2), 11 Strategic (P1–P3).
2. **Критический path**: T-09 (BasePollLoop) → T-10 (retry) → T-11 (timeout) → T-19 (Event System) → T-26 (Heartbeats). Блокер всего — T-09.
3. **~180–260 часов** суммарного effort. Quick Wins — ~14h, Medium — ~80–120h, Strategic — ~90–130h.
4. **Параллельных потоков — 4**: Core, DX, Reliability, Agent Interface. Максимальная утилизация при 2+ разработчиках.
5. **Внешние блокеры**: Arbiter API spec (T-20), ATP API spec (T-21) — без них задачи не завершить.

---

## Сводная таблица

| ID | Title | Priority | Labels | Effort (h) | Dependencies |
|------|-------|----------|--------|-------------|--------------|
| T-01 | Add jitter to retry backoff | P0 | `core`, `scheduler` | 1–2 | — |
| T-02 | Add flock on PID file | P0 | `core`, `infra` | 1–2 | — |
| T-03 | Raise max_concurrent cap to 100 | P0 | `core`, `scheduler` | 1 | — |
| T-04 | Add `maestro config validate` CLI | P0 | `core`, `dx` | 2–3 | — |
| T-05 | Increase shutdown grace period to 5–10s | P0 | `core`, `agents` | 1–2 | — |
| T-06 | Formalize spawner entry points in pyproject.toml | P0 | `agents`, `infra` | 1 | — |
| T-07 | Add DEBUG logging to silent except blocks | P0 | `core`, `dx` | 1 | — |
| T-08 | Remove duplicate cycle detection from models.py | P0 | `core`, `scheduler` | 1–2 | T-04 (рекомендуется) |
| T-09 | Extract BasePollLoop from scheduler/orchestrator | P0 | `core`, `scheduler` | 16–24 | — |
| T-10 | Unify retry logic + persist to DB | P0 | `core`, `scheduler` | 8–12 | T-09 |
| T-11 | Add timeout to orchestrator mode | P0 | `core`, `worktrees` | 4–6 | T-09 |
| T-12 | Implement cascade failure propagation (BLOCKED) | P0 | `core`, `scheduler` | 8–12 | — |
| T-13 | Parse result_summary from agent JSON logs | P0 | `agents`, `scheduler` | 6–8 | — |
| T-14 | Fix blocking async calls | P0 | `core`, `infra` | 2–3 | — |
| T-15 | Orphaned worktree cleanup on startup | P0 | `worktrees`, `infra` | 8–12 | — |
| T-16 | Split COWORK_CONTEXT.md into ARCHITECTURE + ROADMAP | P0 | `docs` | 4–6 | — |
| T-17 | Add `maestro diagnose` CLI command | P0 | `dx`, `infra` | 8–12 | — |
| T-18 | E2e tests with AnnounceSpawner | P0 | `testing` | 8–12 | — |
| T-19 | Implement Event System (asyncio.Queue EventBus) | P1 | `core`, `scheduler` | 16–24 | T-09 |
| T-20 | Implement RoutingStrategy + Arbiter adapter | P1 | `arbiter`, `agents` | 12–20 | T-09, T-06 |
| T-21 | Implement ValidationStrategy + ATP integration | P2 | `atp`, `testing` | 12–20 | ⚠️ ATP API spec |
| T-22 | Agent-specific concurrency pools | P2 | `scheduler`, `agents` | 8–12 | — |
| T-23 | Pass structured metadata to agents via env vars | P1 | `agents`, `scheduler` | 4–6 | — |
| T-24 | Unify logging style across modules | P2 | `core`, `dx` | 8–12 | — |
| T-25 | Generate JSON Schema for YAML configs | P2 | `dx`, `docs` | 2–3 | — |
| T-26 | Implement heartbeat mechanism | P1 | `agents`, `core` | 16–24 | T-23, T-19 |
| T-27 | WORKFLOW.md injection into agent prompt | P1 | `agents`, `dx` | 8–12 | — |
| T-28 | Prometheus metrics + /metrics endpoint | P2 | `infra`, `dx` | 8–12 | — |
| T-29 | Pre-merge conflict detection | P2 | `worktrees`, `infra` | 8–12 | — |
| T-30 | Conditional tasks (when/skip_if) | P3 | `scheduler`, `core` | 16–24 | T-12 |
| T-31 | Define push vs pull coordination model | P3 | `core`, `scheduler` | 16–24 | T-09 |
| T-32 | DB per project (multi-project support) | P3 | `core`, `infra` | 12–20 | — |
| T-33 | Extract `_spawn_with_log()` helper in base spawner | P2 | `agents`, `core` | 2–3 | — |
| T-34 | Support `${VAR:-default}` in env var substitution | P2 | `core`, `dx` | 2–3 | — |

---

## Детали задач

---

### T-01: Add jitter to retry backoff

**Priority:** P0 · **Labels:** `core`, `scheduler` · **Effort:** 1–2h · **Dependencies:** —

**Description:**
Добавить ±30% random jitter в формулу `base_delay × 2^n` в `RetryManager.calculate_delay()`. Без jitter при массовом сбое все retry приходят синхронно (thundering herd), что при 10+ параллельных задачах приводит к каскадному отказу.

**Acceptance Criteria:**

- [ ] `RetryManager.calculate_delay()` включает случайный jitter ±30%
- [ ] Unit-тест: при N вызовах с одинаковыми параметрами возвращаются разные значения
- [ ] Max delay увеличивается не более чем на 30% от текущего

**Файлы:** `maestro/retry.py:31-42`

---

### T-02: Add flock on PID file

**Priority:** P0 · **Labels:** `core`, `infra` · **Effort:** 1–2h · **Dependencies:** —

**Description:**
Использовать `fcntl.flock(LOCK_EX | LOCK_NB)` при записи PID file. Два одновременных `maestro run` → конкурентные writes в SQLite, двойной spawn задач. Flock даёт чёткую ошибку при повторном запуске.

**Acceptance Criteria:**

- [ ] При запуске maestro создаётся PID file с exclusive lock
- [ ] Повторный запуск → понятная ошибка "Maestro already running (PID: …)"
- [ ] Lock автоматически снимается при завершении процесса
- [ ] Тест: попытка повторного lock → `BlockingIOError`

**Файлы:** `maestro/cli.py:48-103`

**⚠️ Ограничение:** POSIX only (fcntl). Не работает на Windows.

---

### T-03: Raise max_concurrent cap to 100

**Priority:** P0 · **Labels:** `core`, `scheduler` · **Effort:** 1h · **Dependencies:** —

**Description:**
Изменить `max_concurrent: int = Field(default=3, ge=1, le=10)` → `le=100`. Текущий cap = 10 — узкое горло при 50+ задачах.

**Acceptance Criteria:**

- [ ] `le=100` в модели `ProjectConfig` и `OrchestratorConfig`
- [ ] Документировать рекомендации по fd exhaustion (ulimit) и SQLite write contention при высоком concurrency
- [ ] Unit-тест: конфиг с `max_concurrent: 50` валиден

**Файлы:** `maestro/models.py`

---

### T-04: Add `maestro config validate` CLI

**Priority:** P0 · **Labels:** `core`, `dx` · **Effort:** 2–3h · **Dependencies:** —

**Description:**
Новая CLI команда: загрузить YAML через `load_config()` / `load_orchestrator_config()`, показать ошибки или "Config valid". Без запуска задач. Позволяет проверять конфиг в CI/CD.

**Acceptance Criteria:**

- [ ] `maestro config validate <path>` — загружает YAML, выводит ошибки Pydantic
- [ ] При валидном конфиге: exit 0 + "✓ Config valid (N tasks)"
- [ ] При невалидном: exit 1 + human-readable ошибки
- [ ] Поддержка обоих режимов (scheduler config, orchestrator config)
- [ ] Создаёт DAG и проверяет на циклы

**Файлы:** `maestro/cli.py` (+50 строк)

---

### T-05: Increase shutdown grace period to 5–10s

**Priority:** P0 · **Labels:** `core`, `agents` · **Effort:** 1–2h · **Dependencies:** —

**Description:**
Заменить `asyncio.sleep(0.5)` между SIGTERM и SIGKILL на configurable значение (default 5s). Агенты получают время для graceful shutdown (сохранение state, коммит).

**Acceptance Criteria:**

- [ ] `shutdown_grace_seconds` параметр в конфиге (default: 5)
- [ ] SIGTERM → wait `shutdown_grace_seconds` → SIGKILL
- [ ] Изменено в scheduler.py и orchestrator.py
- [ ] Unit-тест: grace period берётся из конфига

**Файлы:** `maestro/scheduler.py:727,813-814`, `maestro/orchestrator.py:622-637`

---

### T-06: Formalize spawner entry points in pyproject.toml

**Priority:** P0 · **Labels:** `agents`, `infra` · **Effort:** 1h · **Dependencies:** —

**Description:**
Зарегистрировать 4 built-in spawner'а через `[project.entry-points."maestro.spawners"]`. Plugin architecture становится explicit вместо implicit directory scan.

**Acceptance Criteria:**

- [ ] `[project.entry-points."maestro.spawners"]` в pyproject.toml
- [ ] 4 spawners зарегистрированы: claude_code, codex, aider, announce
- [ ] Registry использует entry points для discovery
- [ ] `uv run maestro --list-spawners` работает

**Файлы:** `pyproject.toml`

---

### T-07: Add DEBUG logging to silent except blocks

**Priority:** P0 · **Labels:** `core`, `dx` · **Effort:** 1h · **Dependencies:** —

**Description:**
Заменить `except OSError: pass` на `except OSError as e: logger.debug(...)` в 4 местах. Silent error swallowing затрудняет диагностику.

**Acceptance Criteria:**

- [ ] Все 4 silent except-блока логируют exception на уровне DEBUG
- [ ] Формат: `logger.debug("Failed to ...: %s", e)`
- [ ] Поведение при exception не изменяется (по-прежнему не reraise)

**Файлы:** `maestro/scheduler.py:733`, `maestro/orchestrator.py:427`, `maestro/validator.py:228`, `maestro/pr_manager.py:164`

---

### T-08: Remove duplicate cycle detection from models.py

**Priority:** P0 · **Labels:** `core`, `scheduler` · **Effort:** 1–2h · **Dependencies:** T-04 (рекомендуется)

**Description:**
Убрать `validate_no_cyclic_dependencies()` из Pydantic model_validator в `models.py`. Оставить единственную проверку в `DAG.__init__()` (алгоритм Кана). Два алгоритма дают разные cycle paths → путаница.

**Acceptance Criteria:**

- [ ] `validate_no_cyclic_dependencies()` удалён из models.py
- [ ] `DAG.__init__()` — единственное место проверки циклов
- [ ] `maestro config validate` (T-04) создаёт DAG, поэтому циклы всё равно обнаружатся
- [ ] Тесты обновлены: ожидаемые сообщения об ошибках из DAG, не из models

**Файлы:** `maestro/models.py:403-441`, `tests/test_models.py`

---

### T-09: Extract BasePollLoop from scheduler/orchestrator

**Priority:** P0 · **Labels:** `core`, `scheduler` · **Effort:** 16–24h · **Dependencies:** —

**Description:**
Создать `maestro/loop.py` с абстрактным `BasePollLoop` (ABC): общий poll loop, signal handling, shutdown cleanup, process termination. `Scheduler` и `Orchestrator` наследуются от него. ~40% дублирования между scheduler.py (903 строки) и orchestrator.py (646 строк).

**Acceptance Criteria:**

- [ ] `BasePollLoop` ABC в `maestro/loop.py` (~200 строк)
- [ ] Общая логика: poll loop, signal handling, graceful shutdown, process termination
- [ ] `Scheduler` и `Orchestrator` наследуются от `BasePollLoop`
- [ ] Нет дублирования: retry, timeout, shutdown — реализованы один раз
- [ ] Все существующие тесты проходят
- [ ] Composition over inheritance: стратегии через конструктор (не template method)

**Файлы:** Новый `maestro/loop.py`, рефакторинг `maestro/scheduler.py`, `maestro/orchestrator.py`

**⚠️ Главный разблокирующий фактор** — от него зависят T-10, T-11, T-19, T-26, T-31.

---

### T-10: Unify retry logic + persist to DB

**Priority:** P0 · **Labels:** `core`, `scheduler` · **Effort:** 8–12h · **Dependencies:** T-09

**Description:**
Подключить `RetryManager` к orchestrator (сейчас не используется). Добавить `next_retry_at` column в DB. При failure записывать в DB вместо in-memory dict. При `_resolve_ready()` фильтровать по `next_retry_at <= now()`.

**Acceptance Criteria:**

- [ ] `RetryManager` используется в обоих режимах (scheduler + orchestrator)
- [ ] DB migration: `next_retry_at` column в tasks/zadachi таблицах
- [ ] Retry state переживает restart (persistent, не in-memory)
- [ ] `_resolve_ready()` учитывает `next_retry_at`
- [ ] Backward compatibility: старые DB без `next_retry_at` работают

**Файлы:** `maestro/retry.py`, `maestro/database.py`, `maestro/orchestrator.py`

---

### T-11: Add timeout to orchestrator mode

**Priority:** P0 · **Labels:** `core`, `worktrees` · **Effort:** 4–6h · **Dependencies:** T-09

**Description:**
Добавить elapsed time check в `_monitor_running()` orchestrator'а аналогично scheduler. Зависший spec-runner процесс будет убит по timeout.

**Acceptance Criteria:**

- [ ] Orchestrator проверяет elapsed time каждую итерацию poll loop
- [ ] Зависший процесс → SIGTERM → grace period → SIGKILL → FAILED
- [ ] `timeout_minutes` из config используется orchestrator'ом
- [ ] Тест: mock процесс, timeout → killed

**Файлы:** `maestro/orchestrator.py:381-397`

---

### T-12: Implement cascade failure propagation (BLOCKED)

**Priority:** P0 · **Labels:** `core`, `scheduler` · **Effort:** 8–12h · **Dependencies:** —

**Description:**
Если зависимость в terminal-failure (NEEDS_REVIEW, retries исчерпаны), downstream задачи → BLOCKED. Добавить `skip_on_failure: bool` в TaskConfig. Сейчас scheduler зависает в бесконечном poll loop.

**Acceptance Criteria:**

- [ ] Новый статус `BLOCKED` (или `ABANDONED`) в `TaskStatus`
- [ ] Upstream в NEEDS_REVIEW → все downstream автоматически BLOCKED
- [ ] `skip_on_failure: bool` в TaskConfig (default: false)
- [ ] Dashboard и CLI отображают BLOCKED-задачи
- [ ] Scheduler корректно завершается, когда все оставшиеся задачи BLOCKED
- [ ] Тест: diamond dependency, upstream fails → downstream blocked

**Файлы:** `maestro/models.py`, `maestro/scheduler.py:657-785`, `maestro/dag.py`

---

### T-13: Parse result_summary from agent JSON logs

**Priority:** P0 · **Labels:** `agents`, `scheduler` · **Effort:** 6–8h · **Dependencies:** —

**Description:**
После завершения Claude Code — парсить JSON log, извлекать `result` field как `result_summary`. Для Codex/Aider — последние N строк stdout. Сейчас result_summary = "Task completed successfully" — бесполезно для downstream.

**Acceptance Criteria:**

- [ ] Claude Code: result из JSON log → `result_summary`
- [ ] Codex/Aider: последние 20 строк stdout → `result_summary`
- [ ] Fallback на "Task completed successfully" при ошибке парсинга
- [ ] `result_summary` доступен через MCP/REST API
- [ ] Тест: mock JSON log → parsed result

**Файлы:** `maestro/scheduler.py:546-561`, `maestro/cost_tracker.py`

---

### T-14: Fix blocking async calls

**Priority:** P0 · **Labels:** `core`, `infra` · **Effort:** 2–3h · **Dependencies:** —

**Description:**
Заменить sync `process.wait()` на `await loop.run_in_executor()`, sync file open на async-safe вызовы. 3 блокирующих вызова в async-контексте могут заблокировать event loop.

**Acceptance Criteria:**

- [ ] Все sync blocking calls заменены на async-safe эквиваленты
- [ ] `process.wait()` → `await asyncio.create_subprocess_*` или `run_in_executor`
- [ ] Sync file I/O → `os.open()` + `os.close()` или aiofiles
- [ ] Тест: event loop не блокируется при длительных операциях

**Файлы:** `maestro/orchestrator.py:350`, `maestro/scheduler.py:731,817`

---

### T-15: Orphaned worktree cleanup on startup

**Priority:** P0 · **Labels:** `worktrees`, `infra` · **Effort:** 8–12h · **Dependencies:** —

**Description:**
При startup orchestrator: `git worktree prune` + reset dirty worktrees. Добавить `workspace_mgr.cleanup_stale()`. При переиспользовании: `git checkout -- .` + `git clean -fd`. Worktrees не удаляются при crash → dirty state при restart.

**Acceptance Criteria:**

- [ ] Startup: `git worktree prune` автоматически
- [ ] `cleanup_stale()` метод в WorkspaceManager
- [ ] Переиспользование worktree: reset dirty state
- [ ] `--clean-workspaces` flag или confirmation перед `git clean -fd`
- [ ] Recovery: маппинг worktree → zadacha из DB
- [ ] Тест: создать dirty worktree → startup → clean state

**Файлы:** `maestro/workspace.py`, `maestro/orchestrator.py:284-372`, `maestro/recovery.py`

---

### T-16: Split COWORK_CONTEXT.md into ARCHITECTURE + ROADMAP

**Priority:** P0 · **Labels:** `docs` · **Effort:** 4–6h · **Dependencies:** —

**Description:**
COWORK_CONTEXT.md описывает как реализованное, так и нереализованное (Arbiter, WORKFLOW.md, ATP, agents.toml) без разделения. Новый разработчик ищет код, которого нет.

**Acceptance Criteria:**

- [ ] `ARCHITECTURE.md` — только реализованный код, с ссылками на файлы
- [ ] `ROADMAP.md` — planned/in-progress фичи, с маркировкой статуса
- [ ] Каждое описание указывает: ✅ implemented / 🚧 in progress / 📋 planned
- [ ] Ссылки из CLAUDE.md обновлены

**Файлы:** `COWORK_CONTEXT.md` → `ARCHITECTURE.md` + `ROADMAP.md`

---

### T-17: Add `maestro diagnose` CLI command

**Priority:** P0 · **Labels:** `dx`, `infra` · **Effort:** 8–12h · **Dependencies:** —

**Description:**
CLI команда для диагностики: stuck tasks (RUNNING дольше timeout), orphaned worktrees, DB integrity, stale PID file, disk space.

**Acceptance Criteria:**

- [ ] `maestro diagnose` выводит список проверок с результатами
- [ ] Проверки: stuck tasks, orphaned worktrees, DB integrity, stale PID, disk space
- [ ] Exit code: 0 = all OK, 1 = issues found
- [ ] JSON output (`--json`) для автоматизации
- [ ] Тест: mock stuck task → diagnose обнаруживает

**Файлы:** `maestro/cli.py` (+100 строк)

---

### T-18: E2e tests with AnnounceSpawner

**Priority:** P0 · **Labels:** `testing` · **Effort:** 8–12h · **Dependencies:** —

**Description:**
2–3 end-to-end теста: YAML → spawn (AnnounceSpawner) → validate → DONE. Полный lifecycle без внешних зависимостей.

**Acceptance Criteria:**

- [ ] E2e тест: linear pipeline (A → B → C), все DONE
- [ ] E2e тест: diamond DAG, parallel execution, все DONE
- [ ] E2e тест: failure + retry, итоговый DONE или NEEDS_REVIEW
- [ ] Время выполнения каждого теста < 10s
- [ ] Интеграция в `pytest` (маркер `@pytest.mark.e2e`)

**Файлы:** Новый `tests/test_e2e.py`

---

### T-19: Implement Event System (asyncio.Queue EventBus)

**Priority:** P1 · **Labels:** `core`, `scheduler` · **Effort:** 16–24h · **Dependencies:** T-09

**Description:**
Гибридная модель: `asyncio.Queue` для internal events + polling fallback. Заменить `process.poll()` на `await process.wait()` в asyncio tasks. Фундамент для heartbeats, MCP callbacks, webhook triggers.

**Acceptance Criteria:**

- [ ] `EventBus` в `maestro/events.py` на базе `asyncio.Queue`
- [ ] События: `ProcessCompleted`, `TaskClaimed`, `Shutdown`, `Heartbeat`
- [ ] Подписка через `bus.subscribe(event_type, handler)`
- [ ] Polling fallback: reconciliation для missed events
- [ ] Интеграция с `BasePollLoop` (T-09)
- [ ] Тест: emit event → handler вызван

**Файлы:** Новый `maestro/events.py` (~200 строк), рефакторинг `maestro/loop.py`

---

### T-20: Implement RoutingStrategy + Arbiter adapter

**Priority:** P1 · **Labels:** `arbiter`, `agents` · **Effort:** 12–20h · **Dependencies:** T-09, T-06

**Description:**
`RoutingStrategy` ABC → `StaticRouting` (текущее поведение) + `ArbiterRouting` (MCP call + fallback). Конфиг: `routing: {type: static|arbiter, url: "..."}`.

**Acceptance Criteria:**

- [ ] `RoutingStrategy` ABC в `maestro/routing.py`
- [ ] `StaticRouting` — текущее поведение, default
- [ ] `ArbiterRouting` — MCP call + graceful fallback на static при ошибке
- [ ] Конфиг: `routing.type: static|arbiter`, `routing.url`
- [ ] Тест: static routing, mock arbiter routing

**⚠️ Внешний блокер:** Arbiter API spec нужен для полноценной реализации `ArbiterRouting`.

**Файлы:** Новый `maestro/routing.py` (~150 строк), `maestro/scheduler.py`, `maestro/config.py`

---

### T-21: Implement ValidationStrategy + ATP integration

**Priority:** P2 · **Labels:** `atp`, `testing` · **Effort:** 12–20h · **Dependencies:** ⚠️ ATP API spec

**Description:**
Рефакторить `Validator` → `CommandValidator(ValidationStrategy)`. `CompositeValidator` для chaining. `ATPValidator` для ATP Platform API.

**Acceptance Criteria:**

- [ ] `ValidationStrategy` ABC в `maestro/validator.py`
- [ ] `CommandValidator` — текущее поведение
- [ ] `CompositeValidator` — chain нескольких стратегий
- [ ] `ATPValidator` stub с timeout + circuit breaker
- [ ] Конфиг: `validation: [{type: command, cmd: "..."}, {type: atp, url: "..."}]`

**⚠️ Внешний блокер:** ATP API spec нужен для завершения.

**Файлы:** `maestro/validator.py`, `maestro/scheduler.py`

---

### T-22: Agent-specific concurrency pools

**Priority:** P2 · **Labels:** `scheduler`, `agents` · **Effort:** 8–12h · **Dependencies:** —

**Description:**
Per-agent limits вместо одного `max_concurrent`: `concurrency: {global: 20, per_agent: {claude_code: 5, codex: 3, aider: 10}}`.

**Acceptance Criteria:**

- [ ] Конфиг `concurrency.per_agent` (dict agent_type → int)
- [ ] `concurrency.global` — общий cap (обратная совместимость с `max_concurrent`)
- [ ] Per-agent counter check в `_spawn_ready()`
- [ ] Тест: 3 claude_code tasks + limit 2 → только 2 параллельно

**Файлы:** `maestro/models.py`, `maestro/scheduler.py`, `maestro/orchestrator.py`

---

### T-23: Pass structured metadata to agents via env vars

**Priority:** P1 · **Labels:** `agents`, `scheduler` · **Effort:** 4–6h · **Dependencies:** —

**Description:**
При spawn передавать env vars: `MAESTRO_TASK_ID`, `MAESTRO_CALLBACK_URL`, `MAESTRO_TIMEOUT_MINUTES`, `MAESTRO_RETRY_COUNT`. Агенты смогут интегрироваться с координацией.

**Acceptance Criteria:**

- [ ] Base spawner устанавливает 4 env vars при spawn
- [ ] Все spawners наследуют env vars
- [ ] Env vars документированы в README / ARCHITECTURE
- [ ] Тест: spawn → env vars присутствуют в subprocess environment

**Файлы:** `maestro/spawners/base.py`, все spawners

---

### T-24: Unify logging style across modules

**Priority:** P2 · **Labels:** `core`, `dx` · **Effort:** 8–12h · **Dependencies:** —

**Description:**
Везде `self._logger` вместо модульного `logger`. Добавить `task_id` в log records (ContextVar). Синхронизировать event_log и standard logging.

**Acceptance Criteria:**

- [ ] Единый стиль: `self._logger = logging.getLogger(self.__class__.__name__)`
- [ ] `task_id` в каждом log record через ContextVar
- [ ] event_log и standard logging синхронизированы
- [ ] Формат: `[timestamp] [level] [module] [task_id] message`

**Файлы:** Все модули `maestro/`

---

### T-25: Generate JSON Schema for YAML configs

**Priority:** P2 · **Labels:** `dx`, `docs` · **Effort:** 2–3h · **Dependencies:** —

**Description:**
Сгенерировать JSON Schema из `ProjectConfig.model_json_schema()`. IDE autocomplete для YAML, внешняя валидация.

**Acceptance Criteria:**

- [ ] `maestro/schemas/scheduler.json` и `maestro/schemas/orchestrator.json`
- [ ] Auto-generated из Pydantic models
- [ ] CLI команда `maestro schema generate` для обновления
- [ ] `$schema` ссылка в example YAML файлах

**Файлы:** Новый `maestro/schemas/`, обновить `examples/`

---

### T-26: Implement heartbeat mechanism

**Priority:** P1 · **Labels:** `agents`, `core` · **Effort:** 16–24h · **Dependencies:** T-23, T-19

**Description:**
Agent периодически сигнализирует "жив" через REST callback. Пропущенный heartbeat > threshold → kill + retry. Отличает "агент думает" от "агент завис".

**Acceptance Criteria:**

- [ ] REST endpoint: `POST /heartbeat` с task_id и timestamp
- [ ] Heartbeat threshold configurable (default: 60s)
- [ ] Пропущенный heartbeat → warning → kill → retry
- [ ] Dashboard: "last heartbeat" column
- [ ] Graceful degradation: агенты без heartbeat → fallback на timeout
- [ ] Тест: mock heartbeat → detected, missed → killed

**Файлы:** `maestro/coordination/rest_api.py`, `maestro/scheduler.py`, spawners

---

### T-27: WORKFLOW.md injection into agent prompt

**Priority:** P1 · **Labels:** `agents`, `dx` · **Effort:** 8–12h · **Dependencies:** —

**Description:**
`WorkflowLoader`: читать `.maestro/WORKFLOW.md`, инжектировать в промпт. Для Claude Code — альтернатива: `.claude/CLAUDE.md`. `max_workflow_size` в конфиге.

**Acceptance Criteria:**

- [ ] `WorkflowLoader` в `maestro/workflow.py`
- [ ] Читает `.maestro/WORKFLOW.md` из workdir
- [ ] Инжектирует содержимое в system prompt агента
- [ ] Claude Code: альтернативно записывает `.claude/CLAUDE.md`
- [ ] `max_workflow_size` (default: 50KB), truncation + warning при превышении
- [ ] Тест: mock WORKFLOW.md → prompt содержит его содержимое

**Файлы:** `maestro/spawners/base.py:67-101`, новый `maestro/workflow.py`

---

### T-28: Prometheus metrics + /metrics endpoint

**Priority:** P2 · **Labels:** `infra`, `dx` · **Effort:** 8–12h · **Dependencies:** —

**Description:**
`prometheus_client` с метриками: tasks_completed_total, running_tasks_count, queue_depth, task_duration_seconds. Endpoint `/metrics`.

**Acceptance Criteria:**

- [ ] `prometheus_client` — optional dependency
- [ ] Метрики: counter (completed/failed), gauge (running/queued), histogram (duration)
- [ ] Labels: `status`, `agent_type`
- [ ] `/metrics` endpoint в REST API
- [ ] Без `prometheus_client` — graceful fallback (no-op)
- [ ] Тест: mock task completion → counter incremented

**Файлы:** `maestro/coordination/rest_api.py`, новый `maestro/metrics.py`, `pyproject.toml`

---

### T-29: Pre-merge conflict detection

**Priority:** P2 · **Labels:** `worktrees`, `infra` · **Effort:** 8–12h · **Dependencies:** —

**Description:**
Перед PR creation: `git merge --no-commit --no-ff` dry-run. Если конфликт — warning, не создавать PR автоматически.

**Acceptance Criteria:**

- [ ] Dry-run merge перед PR creation
- [ ] Конфликт → warning в логах + статус NEEDS_REVIEW
- [ ] Нет конфликта → PR creation как обычно
- [ ] Optional (конфиг `pre_merge_check: true/false`)
- [ ] Тест: mock conflicting branches → detected

**Файлы:** `maestro/pr_manager.py`, `maestro/git.py`

---

### T-30: Conditional tasks (when/skip_if)

**Priority:** P3 · **Labels:** `scheduler`, `core` · **Effort:** 16–24h · **Dependencies:** T-12

**Description:**
Поля `when` / `skip_if` в TaskConfig. Условия: `exit_code`, `scope_changed`, `always`. Позволяют пропускать задачи.

**Acceptance Criteria:**

- [ ] `when` / `skip_if` поля в TaskConfig
- [ ] Условия: `exit_code == 0`, `scope_changed`, `always`
- [ ] Skipped задача → статус SKIPPED (не BLOCKED, не DONE)
- [ ] Downstream от SKIPPED выполняются нормально
- [ ] Тест: conditional skip → downstream runs

**Файлы:** `maestro/models.py`, `maestro/dag.py`, `maestro/scheduler.py`

---

### T-31: Define push vs pull coordination model

**Priority:** P3 · **Labels:** `core`, `scheduler` · **Effort:** 16–24h · **Dependencies:** T-09

**Description:**
MCP server и Scheduler — параллельные, не интегрированные. Определить модель: push (scheduler спаунит) или pull (агенты claim через MCP). ADR + реализация.

**Acceptance Criteria:**

- [ ] ADR документ с решением (push vs pull vs hybrid)
- [ ] При push: MCP server → read-only API
- [ ] При pull: scheduler → task queue, agents claim
- [ ] Optimistic locking при pull (предотвращение двойного claim)
- [ ] Migration path от текущей реализации

**Файлы:** `maestro/coordination/mcp_server.py`, `maestro/scheduler.py`

---

### T-32: DB per project (multi-project support)

**Priority:** P3 · **Labels:** `core`, `infra` · **Effort:** 12–20h · **Dependencies:** —

**Description:**
Вместо единого `maestro.db` — отдельная DB per project: `{project_dir}/.maestro/maestro.db`. Namespace isolation для multi-repo.

**Acceptance Criteria:**

- [ ] DB path: `{project_dir}/.maestro/maestro.db`
- [ ] `maestro status` без `--db` находит DB автоматически
- [ ] Migration: existing single DB → per-project DB (CLI команда)
- [ ] Backward compatibility: если `--db` указан явно — использовать его
- [ ] Тест: два проекта → две независимые DB

**Файлы:** `maestro/database.py`, `maestro/cli.py`, `maestro/config.py`

---

### T-33: Extract `_spawn_with_log()` helper in base spawner

**Priority:** P2 · **Labels:** `agents`, `core` · **Effort:** 2–3h · **Dependencies:** —

**Description:**
Извлечь дублированный fd management (os.open → Popen → os.close) из 4 spawners в базовый метод.

**Acceptance Criteria:**

- [ ] `_spawn_with_log(cmd, log_path)` в `AgentSpawner` base
- [ ] Все 4 spawners используют базовый метод
- [ ] Нет дублирования fd management
- [ ] Тесты не ломаются

**Файлы:** `maestro/spawners/base.py`, `claude_code.py`, `codex.py`, `aider.py`, `announce.py`

---

### T-34: Support `${VAR:-default}` in env var substitution

**Priority:** P2 · **Labels:** `core`, `dx` · **Effort:** 2–3h · **Dependencies:** —

**Description:**
Расширить regex в `resolve_env_vars()`: `${VAR:-fallback}` и `${VAR:?error}`. Shell-совместимый синтаксис.

**Acceptance Criteria:**

- [ ] `${VAR:-default}` → использовать default при отсутствии VAR
- [ ] `${VAR:?error msg}` → raise с error msg при отсутствии VAR
- [ ] Обратная совместимость: `${VAR}` работает как раньше
- [ ] Тесты на все 3 формата

**Файлы:** `maestro/config.py:100-130`

---

## Граф зависимостей

```
T-01 ─┐
T-02  │
T-03  │
T-04 ─┤── Этап 0: Quick Wins (параллельно, день 1)
T-05  │
T-06 ─┤
T-07  │
T-08 ─┘
       │
       ▼
T-09 (BasePollLoop) ──────────────────────────── 🔑 Главный разблокер
  │         │         │          │
  ▼         ▼         ▼          ▼
T-10 ──→ T-11      T-19 ──→ T-26    T-31
(retry)  (timeout) (events) (heartbeat) (push/pull)
                      │
                      ▼
                    T-20 (Arbiter routing)
                      │
                      └── T-06 (entry points)

T-12 (cascade) ──→ T-30 (conditional tasks)
T-23 (env vars) ──→ T-26 (heartbeat)

Независимые (параллельно):
T-13, T-14, T-15, T-16, T-17, T-18, T-22, T-24, T-25, T-27, T-28, T-29, T-32, T-33, T-34
```

---

## Параллельные потоки выполнения

| Поток | Задачи | Фокус |
|-------|--------|-------|
| **Core** | T-09 → T-10 → T-11 → T-19 → T-26 | Архитектура ядра |
| **DX** | T-16 → T-17 → T-25 → T-27 → T-28 | Developer experience |
| **Reliability** | T-12 → T-15 → T-18 → T-29 | Надёжность и тесты |
| **Agent Interface** | T-13 → T-23 → T-20 → T-06 | Интеграция агентов |

---

## Открытые вопросы к автору

1. **Arbiter API spec** — есть ли? Без него T-20 можно начать (StaticRouting + stub), но не завершить.
2. **ATP API spec** — есть ли? Блокирует T-21 полностью.
3. **Windows support** — нужен? Влияет на T-02 (flock → POSIX only).
4. **Push vs Pull** (T-31) — есть предпочтение? От этого зависит объём работы.
5. **Приоритет heartbeat** (T-26) — насколько критично отличать "думает" от "завис"? При <10 агентах timeout может быть достаточен.
6. **Multi-repo** (T-32) — реальный use case сейчас? Или на будущее?
