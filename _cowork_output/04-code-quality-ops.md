# Maestro — Качество кода и операционная зрелость

**Дата:** 2026-04-05
**Базовые документы:** `_cowork_output/01-architecture-map.md`, `02-dag-scheduler-analysis.md`, `03-agent-integration.md`
**Анализируемые файлы:** весь пакет `maestro/` (33 файла) + `tests/` (27 файлов)

---

## TL;DR

1. **Type hints и Pydantic** — отличное покрытие (~98%), иерархия исключений чистая. `Any` используется уместно. Код значительно выше среднего для Python-проекта на этой стадии.
2. **Async correctness** — обнаружены **3 блокирующих вызова в async**: sync `open("w")` в `orchestrator.py:350`, sync `process.wait()` в `scheduler.py:731,817`. Помечено `noqa` только для `Path.exists()`.
3. **Тестирование** — ~940 тестовых функций в 27 файлах, target coverage 80%. DAG и database покрыты хорошо, но **нет stress-тестов**, **нет race condition тестов**, **нет e2e**. Тесты для timeout и signal handling отсутствуют.
4. **Observability** — JSONL event logging (18 типов событий) отлично реализован, но **стандартный logging несогласован** между модулями, **нет Prometheus-метрик**, **нет distributed tracing**. Определить "зависшую задачу" можно только через DB-запрос + tail log file.
5. **Error Recovery** — recovery.py корректно восстанавливает orphaned tasks, но **нет flock для PID file** (double-start возможен), **partial failure блокирует downstream навечно**, **retry backoff теряется при restart**, **idempotency нарушена** (BranchExistsError/WorkspaceExistsError при повторном запуске).

---

## 1. Code Quality

### 1.1 Дублирование кода

#### 🔴 Scheduler и Orchestrator — ~40% дублирования (high impact)

**Файлы:** `scheduler.py` (903 строки) vs `orchestrator.py` (646 строк)

Дублированные паттерны:

| Паттерн | Scheduler | Orchestrator |
|---|---|---|
| Poll loop (while not shutdown → resolve ready → spawn → monitor → sleep) | строки 249-290 | строки 177-225 |
| Signal handler setup (SIGTERM/SIGINT) | строки 787-803 | строки 591-607 |
| Shutdown cleanup (terminate → sleep 0.5s → kill → wait) | строки 805-848 | строки 609-646 |
| Process termination | 7 вхождений terminate/kill | 4 вхождения |

**Рекомендация:** Извлечь `BasePollLoop` с pluggable strategies: `resolve_ready()`, `spawn()`, `monitor()`, `on_success()`, `on_failure()`. Снизит дублирование на ~40%, упростит добавление третьего режима.

#### 🟡 Spawners — дублированный fd management (medium)

**Файлы:** `spawners/claude_code.py:61-78`, `codex.py:63-76`, `aider.py:74-83`, `announce.py:62-75`

Все 4 spawner'а повторяют идентичный паттерн:
```python
fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
try:
    process = subprocess.Popen([...], cwd=workdir, stdout=fd, stderr=subprocess.STDOUT)
finally:
    os.close(fd)
```

**Рекомендация:** Добавить helper метод в `AgentSpawner` (base.py): `_spawn_with_log(cmd, workdir, log_file) -> Popen`.

#### 🟡 Retry handling — _handle_validation_failure и _handle_task_failure (medium)

**Файл:** `scheduler.py:584-656` и `scheduler.py:657-713`

Два метода с ~80% идентичным кодом (retry decision, state transition, logging). Различие только в типе ошибки (validation vs execution).

**Рекомендация:** Извлечь `_handle_failure(task_id, error_msg, failure_type: str)`.

### 1.2 Мёртвый код

🟢 **Минимален (<1%).** Единственный маркер: `tests/test_git_worktree.py:316` — один TODO.

`__init__.py` экспортирует ~58 символов. Некоторые (`DAGNode`, `ScopeWarning`, `RunningTask`) не переиспользуются внутри пакета, но могут быть полезны для внешних потребителей. `BaseSpawner` в `scheduler.py:53` помечен как deprecated re-export, но всё ещё экспортируется.

### 1.3 Сложность (god-methods)

| Метод | Файл:строки | Строк | Проблема |
|---|---|---|---|
| `_spawn_task()` | `scheduler.py:319-419` | 100 | 7+ условных блоков: валидация workdir, retry delay, status transitions, context building, spawning |
| `_spawn_zadacha()` | `orchestrator.py:284-379` | 95 | Смешивает workspace creation, spec generation, config setup, process spawning |
| `update_task_status()` | `database.py:610-699` | 90 | Динамическое построение SQL с 14 условными clause |

**Рекомендация:** Разбить каждый на 3-4 подметода с единой ответственностью.

### 1.4 Type hints и Pydantic

🟢 **Отличное покрытие:**

| Модуль | Покрытие | Примечание |
|---|---|---|
| `models.py` | 100% | Pydantic v2, 14 моделей, 39 валидаторов |
| `database.py` | ~99% | Единственное исключение: `**extra_fields: Any` (kwargs — уместно) |
| `scheduler.py` | ~98% | Полная типизация public/private API |
| `orchestrator.py` | ~97% | Полная типизация |
| `git.py` | ~98% | Все методы типизированы |

Использование `Any` ограничено 3 случаями — все оправданны (kwargs и гибкие конфиг-значения).

### 1.5 Error handling

#### 🟢 Иерархия исключений — чистая и полезная

```
SchedulerError → TaskTimeoutError
DatabaseError → TaskNotFoundError, TaskAlreadyExistsError, ConcurrentModificationError,
                DependencyNotFoundError, MessageNotFoundError,
                ZadachaNotFoundError, ZadachaAlreadyExistsError
GitError → BranchExistsError, BranchNotFoundError, RemoteError,
           RebaseConflictError, NotARepositoryError, MergeConflictError, WorktreeError
OrchestratorError
ConfigError (с path, line, column)
ValidationError, ValidationTimeoutError
CycleError (с cycle_path)
```

#### 🟡 Silent error swallowing — обоснованное, но без логирования (medium)

| Файл | Строка | Контекст |
|---|---|---|
| `scheduler.py` | 733 | `except OSError: pass` — "Process may have already exited" |
| `orchestrator.py` | 427 | `except (json.JSONDecodeError, OSError): pass` — "State file may be partially written" |
| `validator.py` | 228 | `except OSError: pass` — "Process already terminated" |
| `pr_manager.py` | 164 | `except (TimeoutExpired, FileNotFoundError): pass` — graceful gh fallback |

Все случаи документированы комментарием, но лучше логировать на DEBUG:
```python
except OSError as e:
    logger.debug("Expected error during cleanup: %s", e)
```

#### 🟢 Нет bare `except:` или `except BaseException:` блоков

### 1.6 Async correctness

#### 🔴 Блокирующие вызовы в async-контексте (high impact)

| # | Файл | Строка | Проблема | Severity |
|---|---|---|---|---|
| 1 | `orchestrator.py` | 350 | `stdout=log_file.open("w")` — sync file open в async функции | 🔴 High |
| 2 | `scheduler.py` | 731 | `running_task.process.wait()` — sync wait блокирует event loop | 🔴 High |
| 3 | `scheduler.py` | 817 | `running_task.process.wait()` — аналогично, в shutdown | 🟡 Medium |
| 4 | `scheduler.py` | 379-381 | `workdir.exists()` / `workdir.is_dir()` — помечено `# noqa: ASYNC240` | 🟢 Low |

**Рекомендации:**
- #1: Использовать `os.open()` + `os.close()` (как в spawners)
- #2, #3: Заменить на `await loop.run_in_executor(None, process.wait)` или перейти на `asyncio.subprocess`

#### 🟢 aiosqlite используется корректно

`database.py` (1568 строк) — все DB-операции через `aiosqlite`, правильный context manager для транзакций, WAL mode включён. Нет `sqlite3.connect()` в async-контексте.

---

## 2. Тестирование

### 2.1 Общая картина

| Метрика | Значение |
|---|---|
| Тестовых файлов | 27 |
| Тестовых функций | ~940 |
| Async тестов (`@pytest.mark.anyio`) | ~289 |
| Integration тестов | ~68 |
| Target coverage | 80% (pyproject.toml) |
| Branch coverage | Включена |

**Покрытие по модулям:**

| Модуль | Тестов | Тип | Оценка |
|---|---|---|---|
| `models.py` | 83 | Unit | ✅ Отлично |
| `database.py` | 60 | Unit + Async | ✅ Отлично |
| `cli.py` | 58 | Unit + Mock | ✅ Хорошо |
| `dag.py` | 49 | Unit | ✅ Отлично |
| `decomposer.py` | 49 | Unit + Mock | ✅ Хорошо |
| `git.py` + `git_worktree.py` | 41 + 21 | Integration (реальный git) | ✅ Хорошо |
| `scheduler.py` | ~30 | Unit + MockSpawner | 🟡 Среднее |
| `orchestrator.py` | ~25 | Unit + Mock | 🟡 Среднее |
| `rest_api.py` + `rest_zadachi.py` | 37 + ~20 | Unit | 🟡 Среднее |

### 2.2 Как тестируется DAG scheduler

**`test_dag.py`** (789 строк, 49 тестов) — **отлично покрыт:**
- `TestDAGConstruction` (13): пустой DAG, одна задача, цепи A→B→C, diamond A→B,C→D
- `TestCycleDetection` (7): прямые/косвенные циклы, многокомпонентные графы, атрибуты CycleError
- `TestTopologicalSort` (8): порядок для цепей, diamond, независимых задач
- `TestReadyTasks` (12): готовность с учётом зависимостей, приоритизация
- `TestScopeOverlapWarning` (11): перекрытие паттернов, wildcards, зависимые задачи

**`test_scheduler.py`** (~30 тестов) — **среднее покрытие:**
- MockSpawner с controllable return_code и delay_seconds
- Тестируется: ready task resolution, concurrency limiting (max_concurrent=1,2), spawner error handling
- 🔴 **НЕ тестируется:** timeout handling (TaskTimeoutError), signal handling (SIGTERM), race conditions, stress (100+ задач)

### 2.3 Как тестируется git worktree management

**`test_git_worktree.py`** (21 тест) + **`test_workspace.py`** (23 теста) — **хорошо:**

- 🟢 Используется **реальный git repo** через fixture `git_repo` (conftest.py:74-124)
- 🟢 Тестируется: create/remove worktree, workspace creation/cleanup, error handling (BranchExistsError, WorkspaceExistsError)
- 🔴 **НЕ тестируется:** orphaned worktrees cleanup, recovery после crash, dirty state worktree при restart

### 2.4 Как тестируется интеграция с Arbiter

🔴 **Никак** — Arbiter интеграция не реализована в коде (см. `03-agent-integration.md`).

### 2.5 Слабые места тестирования

| Проблема | Impact | Рекомендация |
|---|---|---|
| Нет e2e тестов (YAML → spawn → validate → DONE) | 🔴 High | Добавить 1-2 e2e с AnnounceSpawner |
| Нет stress-тестов (100+ задач в DAG) | 🟡 Medium | Параметризованный тест с генерацией large DAG |
| Нет race condition тестов | 🟡 Medium | Concurrent DB writes, parallel task completion |
| Нет тестов signal handling | 🟡 Medium | Send SIGTERM → проверить cleanup |
| Почти нет параметризации (`@pytest.mark.parametrize`) | 🟡 Medium | Добавить для валидаторов и edge cases |
| Потенциально flaky: `test_dashboard.py` (sleep 0.5s) | 🟢 Low | Заменить на event-driven ожидание |

---

## 3. Observability

### 3.1 Логирование

#### 🟢 JSONL Event Logging — отлично

**Файл:** `event_log.py` (249 строк)

- **Формат:** JSONL (одна JSON-строка на событие)
- **18 типов событий:** scheduler_started/stopped, task lifecycle (created, ready, started, completed, failed, retrying, needs_review, approved, abandoned), validation (started, passed, failed), git (branch_created, committed, pushed)
- **Correlation ID:** `task_id` во всех событиях
- **Structured details:** длительность, agent_type, error, retry_count, commit_hash

#### 🟡 Стандартный logging — несогласован (medium)

14 модулей используют `logging.getLogger(__name__)`, но:

| Проблема | Файлы | Рекомендация |
|---|---|---|
| Разный стиль: прямой `logging.info()` vs `self._logger.info()` | `scheduler.py` vs `orchestrator.py` | Унифицировать: везде `self._logger` |
| Нет task_id в обычных логах (только в event_log) | Все модули | Добавить `[task_id]` prefix или ContextVar |
| event_log и logging не синхронизированы | `event_log.py`, `scheduler.py` | Дублировать критические события в оба канала |
| `retry.py` — логгер объявлен, но не используется | `retry.py` | Удалить или начать использовать |

### 3.2 Метрики

#### 🟢 Cost tracking — реализован

**Файл:** `cost_tracker.py` (311 строк)

- Token usage (input/output) для Claude Code (JSON парсинг)
- Pricing по моделям (hardcoded: claude $3/$15 per 1M tokens)
- REST API: `/tasks/{id}/costs`, `/costs/summary`

#### 🔴 Операционные метрики — отсутствуют (high impact)

Нет:
- Prometheus / StatsD / OpenMetrics
- Histogram: время выполнения задач (p50, p95, p99)
- Counter: tasks_completed_total{status, agent_type}
- Gauge: running_tasks_count, queue_depth
- Success/failure rate по agent типам
- `/metrics` endpoint

**Рекомендация:** Добавить `prometheus_client` с базовыми метриками и `/metrics` endpoint в REST API.

### 3.3 Трейсинг

🔴 **Минимален:**
- `task_id` как единственный correlation ID
- Нет distributed tracing (OpenTelemetry)
- Нет Request ID middleware в REST API
- Нет сквозного trace: YAML → spawn → agent → validate → PR

### 3.4 Диагностика "задача зависла"

**Доступные инструменты:**

| Инструмент | Как использовать | Ограничение |
|---|---|---|
| Timeout в scheduler | `scheduler.py:504-510` — проверяет elapsed vs timeout_minutes | ❌ Orchestrator **не имеет timeout** |
| `maestro status --db` | CLI показывает текущие статусы | Не показывает elapsed time |
| REST `/health` | Простой healthcheck API + DB | Не показывает stuck tasks |
| Dashboard SSE | Real-time статусы через Mermaid.js | Нет alert'ов |
| Event log grep | `grep task_id logs/events.jsonl` | Ручной, нет тулинга |
| Log file tail | `tail -f logs/{task_id}.log` | Нет streaming в scheduler |

**🔴 Отсутствуют:**
- Heartbeat от агентов ("я жив")
- Watchdog для stuck detection (кроме timeout в scheduler)
- `maestro diagnose` команда
- Alert rules / PagerDuty integration
- Progress bar (% выполнения внутри task)

---

## 4. Error Recovery

### 4.1 Crash recovery

#### 🟢 StateRecovery — корректная реализация

**Файл:** `recovery.py` (185 строк)

- Обнаруживает orphaned RUNNING/VALIDATING задачи
- Переводит в FAILED → READY для повторной попытки
- Вызывается при `--resume` через CLI

#### 🔴 Нет flock на PID file — double-start возможен (high impact)

**Файл:** `cli.py:48-103`

```python
PID_FILE = DEFAULT_DB_DIR / "maestro.pid"

def _write_pid_file(pid: int) -> None:
    PID_FILE.write_text(str(pid))  # НЕ АТОМНО, нет flock
```

Нет `os.kill(pid, 0)` для проверки живости процесса. Нет `fcntl.flock()`. Два scheduler'а могут запуститься одновременно → конкурентные writes в SQLite.

**Рекомендация:** Добавить `fcntl.flock(LOCK_EX | LOCK_NB)` на PID file.

### 4.2 State persistence

🟢 SQLite + WAL mode — транзакции атомарны, crash-safe.

🟡 **Нет WAL checkpoint при shutdown** (`database.py:316-319`) — просто `close()` без `PRAGMA wal_checkpoint(RESTART)` и `PRAGMA optimize`. WAL-файл может расти.

🟡 **Retry backoff теряется при restart** — `_retry_ready_times` хранится in-memory (`scheduler.py:453-476`). При `--resume` задача перезапустится без backoff delay.

**Рекомендация:** Сохранять `next_retry_at: datetime` в DB.

### 4.3 Partial failure (3 из 5 задач успешны)

#### 🔴 Downstream блокируется навечно при upstream failure (high impact)

**Файл:** `scheduler.py:657-712, 769-785`

Сценарий:
```
Task-A: NEEDS_REVIEW (failed, retries исчерпаны)
Task-B: depends_on=[Task-A], status=READY
Task-C: depends_on=[Task-B], status=PENDING

_all_tasks_complete() → Task-A: NEEDS_REVIEW (continue/skip)
                      → Task-B: READY (не terminal → return False)
Но get_ready_tasks() никогда не вернёт Task-B (зависимость A не в completed).

Scheduler зависает в бесконечном poll loop.
```

**Рекомендация:** Добавить cascade failure propagation:
1. Если зависимость в terminal-failure → downstream автоматически в ABANDONED или BLOCKED
2. Или: `skip_on_failure: bool` в TaskConfig для graceful degradation

### 4.4 Idempotency

#### 🔴 Повторный запуск нарушен (high impact)

| Сценарий | Файл:строка | Поведение | Проблема |
|---|---|---|---|
| Branch уже существует | `git.py:186-209` | `BranchExistsError` | Scheduler падает |
| Workspace уже существует | `workspace.py:55-87` | `WorkspaceExistsError` | Orchestrator падает |

При crash + restart: branch/worktree от предыдущего run остаются → повторный запуск невозможен без ручной очистки.

**Mitigation в orchestrator** (`orchestrator.py:296-299`): если worktree уже есть, переиспользует. Но без `git reset --hard` / `git clean -fd` — грязный state.

**Рекомендация:** Сделать idempotent: если branch/workspace существует — переиспользовать с очисткой state.

### 4.5 Signal handling

🟡 **SIGTERM/SIGINT обрабатываются** (`scheduler.py:787-848`, `orchestrator.py:591-646`), но:

| Проблема | Файл:строка | Impact |
|---|---|---|
| Grace period 0.5s — слишком мал для агента | `scheduler.py:813-814` | 🟡 Medium |
| `process.wait()` — sync call в async | `scheduler.py:817` | 🔴 High |
| Нет `os.killpg()` — child processes остаются orphaned | `scheduler.py:723-733` | 🟡 Medium |
| Worktrees не удаляются при shutdown | `orchestrator.py:609-646` | 🟡 Medium |

---

## 5. Configuration Management

### 5.1 Hardcoded values

| Значение | Файл:строка | Текущее | Рекомендация | Impact |
|---|---|---|---|---|
| Retry base_delay | `retry.py:31` | 5.0s | Сделать configurable | 🟡 Medium |
| Retry max_delay | `retry.py:42` | 300.0s | Сделать configurable | 🟡 Medium |
| Validation timeout | `validator.py:107` | 300s | Добавить в defaults конфига | 🟡 Medium |
| PR manager timeout | `pr_manager.py:107,158` | 60s / 30s | Configurable | 🟢 Low |
| Cost pricing | `cost_tracker.py:26-31` | $3/$15 per 1M | Configurable или из API | 🟡 Medium |
| SpecRunner test_command | `models.py:778` | "uv run pytest" | Предполагает uv + pytest | 🟢 Low |
| SpecRunner lint_command | `models.py:783` | "uv run ruff check ." | Предполагает uv + ruff | 🟢 Low |
| Shutdown grace period | `scheduler.py:813` | 0.5s | Увеличить до 5-10s | 🟡 Medium |

### 5.2 Валидация конфигов

🟢 **Хорошо реализовано:**
- Pydantic validators: unique IDs, dependency existence, cycle detection, telegram field pairing
- `ConfigError` с `path`, `line`, `column` — отличная диагностика
- `resolve_env_vars()` с рекурсивной подстановкой
- `apply_defaults_to_tasks()` — merge defaults в tasks

🟡 **Отсутствует:**
- JSON Schema для IDE autocomplete и внешней валидации
- Поддержка `${VAR:-default}` (значения по умолчанию для env vars)
- Поддержка `.env` файлов (python-dotenv)
- `agents.toml` — упоминается в COWORK_CONTEXT.md, но **не парсится** нигде в коде

### 5.3 Чувствительные данные

🟢 **Sensitive values НЕ логируются** — проверено во всех модулях.

🟡 **Проблемы:**

| Проблема | Файл:строка | Рекомендация | Impact |
|---|---|---|---|
| Telegram token в plain text YAML | `models.py:320` | Использовать `${TELEGRAM_TOKEN}` (уже возможно) | 🟡 Medium |
| Webhook URL может содержать API key | `models.py:322-323` | Документировать best practice: env vars | 🟡 Medium |
| API keys передаются агентам через env vars процесса | Spawners | ✅ Корректно — наследуют env | 🟢 OK |
| Нет encryption at rest для config | config.py | Для production: secrets manager | 🟡 Medium |

---

## 6. Сводная таблица всех находок

### 🔴 High Impact

| # | Категория | Файл:строки | Проблема | Решение |
|---|---|---|---|---|
| 1 | Async | `orchestrator.py:350` | Sync `log_file.open("w")` в async | Использовать `os.open()` |
| 2 | Async | `scheduler.py:731,817` | Sync `process.wait()` блокирует event loop | `await loop.run_in_executor()` |
| 3 | Recovery | `cli.py:48-103` | Нет flock на PID file — double-start | Добавить `fcntl.flock()` |
| 4 | Recovery | `scheduler.py:657-785` | Partial failure блокирует downstream навечно | Cascade failure / BLOCKED status |
| 5 | Recovery | `git.py:186-209`, `workspace.py:55-87` | Idempotency нарушена при restart | Переиспользовать existing branch/workspace |
| 6 | Observability | Весь проект | Нет Prometheus/операционных метрик | Добавить `prometheus_client` |
| 7 | Testing | `tests/` | Нет e2e, stress, race condition тестов | Добавить критические сценарии |
| 8 | Duplication | `scheduler.py` + `orchestrator.py` | ~40% дублирования poll loop/shutdown | Извлечь `BasePollLoop` |

### 🟡 Medium Impact

| # | Категория | Файл:строки | Проблема | Решение |
|---|---|---|---|---|
| 9 | Observability | `scheduler.py`, `orchestrator.py` | Разный стиль logging между модулями | Унифицировать на `self._logger` |
| 10 | Observability | event_log.py + logging | Два канала логирования не связаны | Синхронизировать критические события |
| 11 | Recovery | `scheduler.py:453-476` | Retry backoff теряется при restart (in-memory) | Сохранять `next_retry_at` в DB |
| 12 | Recovery | `scheduler.py:813` | Grace period 0.5s слишком мал | Увеличить до 5-10s |
| 13 | Recovery | `scheduler.py:723-733` | Нет `os.killpg()` — orphaned child processes | Kill process group |
| 14 | Config | `retry.py:31-42` | base_delay/max_delay hardcoded | Параметризовать через config |
| 15 | Config | `cost_tracker.py:26-31` | Hardcoded pricing может устареть | Configurable или из API |
| 16 | Config | `config.py` | Нет поддержки `${VAR:-default}` | Расширить regex |
| 17 | Testing | `spawners/*.py` | fd management дублирование | Helper метод в base.py |
| 18 | Observability | `orchestrator.py` | Нет timeout для процессов (зависнет навечно) | Добавить elapsed check |
| 19 | Recovery | `database.py:316-319` | Нет WAL checkpoint при shutdown | `PRAGMA wal_checkpoint(RESTART)` |

### 🟢 Low Impact

| # | Категория | Файл:строки | Проблема | Решение |
|---|---|---|---|---|
| 20 | Code | `scheduler.py:733` и др. | Silent error swallowing без logging | Добавить `logger.debug()` |
| 21 | Code | `scheduler.py:319-419` | God-method _spawn_task (100 строк) | Разбить на подметоды |
| 22 | Config | `models.py:778,783` | SpecRunner предполагает uv + pytest + ruff | Документировать / autodetect |
| 23 | Config | Весь проект | Нет JSON Schema для YAML конфигов | Сгенерировать из Pydantic моделей |
| 24 | Testing | `test_dashboard.py` | Потенциально flaky (sleep 0.5s) | Event-driven ожидание |

---

## 7. Матрица операционной зрелости

| Категория | Оценка | Комментарий |
|---|---|---|
| **Type Safety** | ⭐⭐⭐⭐⭐ | 98% type hints, Pydantic v2, чистая иерархия исключений |
| **Code Structure** | ⭐⭐⭐⭐ | Чистое разделение слоёв, но ~40% дублирования scheduler/orchestrator |
| **Error Handling** | ⭐⭐⭐⭐ | Кастомные исключения, обоснованные pass-блоки. Нет bare except |
| **Async Correctness** | ⭐⭐⭐ | В основном корректно, но 3 блокирующих вызова |
| **Test Coverage** | ⭐⭐⭐⭐ | ~940 тестов, 80% target. Хорошо для DAG/DB, слабо для scheduler/e2e |
| **Logging** | ⭐⭐⭐ | JSONL event log отлично, стандартный logging несогласован |
| **Metrics** | ⭐⭐ | Только cost tracking. Нет операционных метрик |
| **Tracing** | ⭐ | Только task_id. Нет distributed tracing |
| **Crash Recovery** | ⭐⭐⭐ | recovery.py работает, но нет flock, backoff теряется |
| **Idempotency** | ⭐⭐ | Restart может сломаться на existing branches/worktrees |
| **Config Validation** | ⭐⭐⭐⭐ | Pydantic + ConfigError с позицией. Нет JSON Schema |
| **Secret Management** | ⭐⭐⭐ | Env vars поддерживаются, sensitive не логируются. Нет encryption |

**Общая оценка: B+** — код качественный, но операционная зрелость требует работы над observability, recovery и idempotency.

---

## Открытые вопросы к автору

1. **Double-start protection** — `fcntl.flock()` решит проблему, но нужно ли поддерживать Windows? Если нет — flock идеален.

2. **Cascade failure** — при NEEDS_REVIEW upstream, downstream задачи блокируются навечно. Какое поведение желательно: автоматический ABANDONED downstream, или новый статус BLOCKED, или `skip_on_failure` per task?

3. **Retry backoff persistence** — сохранять `next_retry_at` в DB или считаешь in-memory достаточным? При crash retry delay сбрасывается.

4. **Observability roadmap** — планируется ли Prometheus? Или достаточно event_log + dashboard для текущего масштаба?

5. **Idempotency strategy** — при restart с existing branch: checkout existing (может быть dirty), или force recreate (потеря partial work), или error (текущее поведение)?

6. **E2e тесты** — используется ли AnnounceSpawner для e2e? Он идеален: instant completion, no external deps. Или есть другой подход?

7. **agents.toml** — файл упоминается в COWORK_CONTEXT.md, но нигде не парсится. Это deprecated idea или planned feature?
