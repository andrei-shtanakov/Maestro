# Maestro — Глубокий анализ DAG-планировщика

**Дата:** 2026-04-05
**Базовый документ:** `_cowork_output/01-architecture-map.md`
**Анализируемые файлы:** `dag.py` (427 строк), `scheduler.py` (903), `orchestrator.py` (646), `config.py` (316), `models.py` (893), `retry.py` (98), `recovery.py` (185), `workspace.py` (183)

---

## TL;DR

1. DAG реализован корректно через алгоритм Кана (cycle detection + topological sort), но **дублирует** проверку циклов — и в `models.py` (Pydantic validator), и в `dag.py` (при конструировании). Orchestrator mode **вообще не использует DAG-класс**, а делает inline-проверку `depends_on ⊆ completed`.
2. Scheduling algorithm — **простой polling loop** с `get_ready_tasks()` + priority sort. Нет priority queue, нет resource-aware scheduling (CPU/memory), нет динамической репланировки. `max_concurrent` — единственный лимитер.
3. Execution model — **asyncio + subprocess** (Popen в scheduler, `create_subprocess_exec` в orchestrator). Прогресс отслеживается только через `poll()` exit code. Race conditions предотвращаются git worktree изоляцией, но **в scheduler mode все задачи работают в одной директории** — scope overlap — лишь warning, не enforcement.
4. По сравнению с Airflow/Prefect/Temporal, Maestro проще и целенаправленнее (AI agent orchestration), но ему критически не хватает: **dynamic DAG**, **event-driven scheduling**, **resource quotas**, **dead letter queue**, **observability** (metrics/traces).
5. Retry реализован с exponential backoff (`5s × 2^n`, cap 300s), но **без jitter** — при массовых сбоях все retry пойдут синхронно (thundering herd).

---

## 1. Построение графа

### 1.1 Трансляция tasks.yaml → DAG

**Цепочка вызовов:**
```
cli.py: run_command(config_path)
  → config.py: load_config(path)
    → yaml.safe_load() + resolve_env_vars()
    → ProjectConfig(**resolved_config)         # Pydantic validation
      → validate_unique_task_ids()             # models.py:382
      → validate_dependencies_exist()          # models.py:392
      → validate_no_cyclic_dependencies()      # models.py:403 — DFS cycle detection
      → apply_defaults_to_tasks()              # models.py:444
  → dag.py: DAG(config.tasks)
    → _build_graph()                           # Два прохода: nodes → edges
    → _detect_cycles()                         # Алгоритм Кана (BFS)
```

**Структура YAML → модель:**

| YAML-поле | Pydantic-модель | DAG-элемент |
|---|---|---|
| `tasks[].id` | `TaskConfig.id` | `DAGNode.task_id` |
| `tasks[].depends_on` | `TaskConfig.depends_on` | `DAGNode.dependencies` (edge: dep → task) |
| `tasks[].scope` | `TaskConfig.scope` | Используется в `check_scope_overlaps()` |
| `tasks[].priority` | `TaskConfig.priority` | Sort key в `get_ready_tasks()` |
| `max_concurrent` | `ProjectConfig.max_concurrent` | `SchedulerConfig.max_concurrent` |

**🔴 Проблема: двойная валидация циклов (high impact на maintainability)**

Циклы проверяются **дважды** разными алгоритмами:

1. `models.py:403-441` — DFS с recursion stack (в Pydantic model_validator)
2. `dag.py:109-145` — Алгоритм Кана (BFS in-degree reduction)

Обе проверки корректны, но:
- DFS в models.py проходит по `depends_on` (dependency → node), DFS в dag.py — тоже
- Если Pydantic validator сработал, DAG constructor **гарантированно не найдёт цикл** — проверка избыточна
- Разные алгоритмы → разные cycle paths в ошибках → путаница при отладке

**Рекомендация:** Удалить `validate_no_cyclic_dependencies()` из models.py, оставить единственную проверку в `DAG.__init__()`. Pydantic validators должны проверять **синтаксис** (unique IDs, existing deps), а `DAG` — **семантику** (ацикличность).

### 1.2 Валидация циклических зависимостей

**Алгоритм в dag.py (Кана):**
```python
def _detect_cycles(self):
    in_degree = {id: len(node.dependencies) for id, node in self._nodes.items()}
    queue = deque(nodes with in_degree == 0)

    while queue:
        node = queue.popleft()
        processed_count += 1
        for dependent in node.dependents:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    if processed_count != len(nodes):
        cycle_path = self._find_cycle_path()  # DFS для читаемого сообщения
        raise CycleError(cycle_path)
```

**Оценка:** Корректный O(V+E). Кан обнаруживает **существование** цикла, затем `_find_cycle_path()` (DFS) находит конкретный путь для сообщения об ошибке — хороший паттерн.

**🟡 Нюанс (medium):** `_find_cycle_path()` ходит по `dependencies` (обратные рёбра), что означает: путь формируется как "task-a зависит от task-b зависит от task-c зависит от task-a". Это **корректно для зависимостей**, но может выглядеть контринтуитивно, если пользователь мыслит в терминах "task-a → task-b → task-c → task-a" (стрелка = "блокирует"). В CycleError сообщение `a -> b -> c -> a` используется в обоих направлениях.

### 1.3 Поддержка conditional tasks (if/when)

**🔴 Не поддерживается (high impact).**

В `TaskConfig` и `DAG` нет:
- `condition` / `when` / `if` полей
- Skip logic (пропуск задачи по условию)
- Dynamic task generation (задачи, создающие другие задачи)
- Trigger-based execution (webhook/event triggers)

Все задачи статически определены в YAML, все обязательны к выполнению. `ABANDONED` — единственный способ "пропустить", но он ручной (`cli.py approve`).

**Где это критично:** Если агент X обнаружил, что changes не нужны (e.g. "файл уже в нужном состоянии"), downstream задачи всё равно запустятся. Нет `skip_if_no_changes` логики.

**Рекомендация:** Добавить хотя бы:
```yaml
tasks:
  - id: lint-fix
    when: "exit_code != 0"  # exit code предыдущей зависимости
    depends_on: [lint-check]
```

### 1.4 Обработка ошибок парсинга

**Хорошо реализовано:**

`config.py` обрабатывает 5 типов ошибок:

| Ошибка | Обработка | Файл:строка |
|---|---|---|
| Файл не найден | `ConfigError("Configuration file not found")` | config.py:193 |
| Не файл | `ConfigError("Path is not a file")` | config.py:196 |
| Невалидный YAML | `_format_yaml_error()` → `ConfigError` с line/column | config.py:202-203 |
| Пустой файл | `ConfigError("Configuration file is empty")` | config.py:209 |
| Не mapping | `ConfigError("Configuration must be a YAML mapping")` | config.py:212-215 |
| Pydantic validation | `_format_validation_error()` — single/multiple errors | config.py:221-224 |
| Undefined env var | `ConfigError("Environment variable '{name}' is not defined")` | config.py:113-115 |

**🟢 Позитив:** `ConfigError` хранит `path`, `line`, `column` — отличные диагностические сообщения.

**🟡 Нюанс:** `resolve_env_vars()` не поддерживает значения по умолчанию (`${VAR:-default}`) и mandatory check (`${VAR:?error}`). Regex `\$\{([A-Za-z_][A-Za-z0-9_]*)\}` — строгий, но не поддерживает nested vars.

---

## 2. Scheduling Algorithm

### 2.1 Алгоритм

**Категория:** Polling-based topological scheduler с priority sort.

```
while not shutdown:
    completed_ids = db.get_tasks_by_status(DONE)      # DB query каждую итерацию
    if all_tasks_complete(): break

    ready_ids = dag.get_ready_tasks(completed_ids)     # O(V) scan всех nodes
    ready_ids = [id for id in ready_ids if id not in running]  # Filter running

    for task_id in ready_ids[:available_slots]:         # Spawn up to max_concurrent
        spawn_task(task_id)

    monitor_running_tasks()                             # poll() each process
    sleep(poll_interval)                                # Default: 1.0s
```

**Complexity per iteration:** O(V) для `get_ready_tasks` + O(R) для monitoring, где V = total tasks, R = running tasks.

### 2.2 Определение задач для параллельного запуска

`DAG.get_ready_tasks(completed)` (`dag.py:218-243`):
1. Для каждой node проверяет: `node.dependencies <= completed` (set subset check)
2. Фильтрует уже completed
3. Сортирует по `priority` (descending)

**Результат:** Список task IDs, готовых к запуску. Scheduler берёт первые `available_slots = max_concurrent - len(running)`.

**🔴 Проблема: нет awareness о "failed dependencies" (high impact)**

`get_ready_tasks()` проверяет только `dependencies <= completed`. Если зависимость в `FAILED` или `NEEDS_REVIEW`, dependent задача **никогда не станет ready** — она просто "зависнет" в `PENDING` навечно. Scheduler не обнаруживает deadlock.

`_all_tasks_complete()` (`scheduler.py:769-785`) считает завершёнными `DONE` и `ABANDONED`, а `NEEDS_REVIEW` пропускает через `continue`. Это создаёт **скрытый баг**: если задача A в `NEEDS_REVIEW`, а задача B зависит от A — scheduler **считает всё завершённым** (loop completes → returns True), но задача B остаётся в `PENDING` навсегда. Scheduler молча завершается, не выполнив всю работу.

**Рекомендация:** Добавить `propagate_failure()` — если зависимость в terminal-failure состоянии и все retries исчерпаны, downstream задачи должны автоматически переходить в `ABANDONED` или `BLOCKED`. Альтернативно: `NEEDS_REVIEW` не должен считаться terminal для `_all_tasks_complete()`, если есть dependent задачи в non-terminal state.

### 2.3 Resource-aware scheduling

**🔴 Не реализовано (high impact для production use).**

| Возможность | Статус |
|---|---|
| `max_concurrent` (глобальный лимит) | ✅ Есть (`ProjectConfig.max_concurrent`, 1-10) |
| CPU/memory limits per task | ❌ Нет |
| Agent-specific concurrency | ❌ Нет (нельзя сказать "max 1 Codex + 2 Claude одновременно") |
| Cost budget | ❌ Нет (cost_tracker парсит, но не ограничивает) |
| Rate limiting per API | ❌ Нет |
| Disk space check | ❌ Нет (worktrees могут заполнить диск) |

**Рекомендация:** Минимально — добавить `max_per_agent_type: {claude_code: 2, codex: 1}` в config.

### 2.4 Обработка ситуации "зависимость упала"

**Текущее поведение:**

```
Task A (FAILED, retries exhausted) → NEEDS_REVIEW
Task B (depends_on: [A]) → остаётся PENDING навсегда
```

`get_ready_tasks()` никогда не вернёт B, потому что A не в `completed` set. Scheduler не имеет логики "каскадного отказа".

Для orchestrator mode (`orchestrator.py:227-260`) ситуация **аналогичная** — `_resolve_ready()` проверяет `z.depends_on ⊆ completed_ids`, и failed задача блокирует downstream.

**🔴 Это deadlock по дизайну** — scheduler loop не завершится, пока оператор вручную не approves/abandons зависимость через CLI.

---

## 3. Execution Model

### 3.1 asyncio + subprocess

| Режим | Event loop | Process spawning | Мониторинг |
|---|---|---|---|
| Scheduler (`maestro run`) | `asyncio` (single thread) | `subprocess.Popen` (sync) | `process.poll()` (sync, non-blocking) |
| Orchestrator (`maestro orchestrate`) | `asyncio` (single thread) | `asyncio.create_subprocess_exec` (async) | `process.returncode` check |

**🟡 Несогласованность (medium):** Scheduler использует sync `Popen` (блокирует event loop на spawn), а Orchestrator — async subprocess. Причина: spawners (claude_code, codex, aider) имеют sync `spawn()` API, который возвращает `Popen`. Для orchestrator spawn идёт напрямую через asyncio.

**Потенциальная проблема:** `Popen()` в scheduler — sync call. Если агент долго стартует (e.g. `claude` CLI загружает модель), event loop блокируется. На практике `Popen` возвращается мгновенно (fork+exec), так что блокировка минимальна, но это архитектурный долг.

### 3.2 Отслеживание прогресса

**Scheduler mode:**
- **Грубый:** Только `process.poll()` → exit code (0 = success, else = failure)
- Нет streaming stdout/stderr
- Нет промежуточных статусов ("50% done")
- Log file пишется spawner'ом, но scheduler **не читает** его до завершения

**Orchestrator mode:**
- **Лучше:** Читает `.executor-state.json` из worktree (`orchestrator.py:399-427`)
- Парсит `tasks` dict → `{done}/{total} done`
- Сохраняет `subtask_progress` в DB
- Но: файл читается sync (`run_in_executor`), и `json.JSONDecodeError` тихо подавляется

**🟡 Рекомендация (medium):** Scheduler mode нуждается в аналогичном progress tracking. Варианты:
1. Парсить stdout/stderr log в реальном времени (tail)
2. Использовать MCP callback от агента → REST API `/tasks/{id}/callback`
3. Поллить размер log file как proxy метрику

### 3.3 Retry с backoff

**Реализация:** `retry.py` (98 строк) — простая и чистая.

```python
delay = base_delay * (2 ** retry_count)    # base_delay=5.0
delay = min(delay, max_delay)               # max_delay=300.0
```

| Attempt | Delay |
|---|---|
| 1 | 5s |
| 2 | 10s |
| 3 | 20s |
| 4 | 40s |
| 5 | 80s |
| 6 | 160s |
| 7+ | 300s (cap) |

**Retry flow в scheduler:**
```
Task RUNNING → exit code != 0 → _handle_task_failure()
  → should_retry(task)? (retry_count < max_retries)
    → YES: RUNNING → FAILED (save error) → READY (ready for re-spawn)
           _retry_delay_elapsed() checks backoff timer
    → NO:  RUNNING → FAILED → NEEDS_REVIEW
```

**🔴 Проблема: нет jitter (high impact при масштабировании)**

Формула `base × 2^n` без random jitter. Если 3 задачи упали одновременно (e.g. API rate limit), все три retry придут ровно через 5s → опять rate limit → все три через 10s → и так далее (thundering herd).

**Рекомендация:**
```python
import random
jitter = random.uniform(0, delay * 0.3)  # ±30% jitter
delay = min(base_delay * (2 ** retry_count) + jitter, max_delay)
```

**🟡 Нюанс:** Retry context injection хорошо реализован — `build_retry_context()` формирует текстовый блок с предыдущей ошибкой (truncated to 4000 chars) и передаёт агенту. Это **правильный паттерн** для AI agents — даёт агенту шанс "учиться" на ошибке.

**🟡 Нюанс:** Orchestrator mode (`orchestrator.py:533-575`) имеет retry через `zadacha.can_retry()`, но **не использует RetryManager** и **не имеет backoff** — retry происходит немедленно. Это несогласованность с scheduler mode.

### 3.4 Race conditions

#### Scheduler mode (shared workdir)

**🔴 Критическая проблема (high impact):**

В scheduler mode **все задачи работают в одном каталоге** (`SchedulerConfig.workdir`). Никакой изоляции нет. `scope` — это лишь **декларативная метка** для overlap warnings, но **не enforcement**.

```python
# scheduler.py:407-408
process = spawner.spawn(task, context, workdir, log_file, retry_context)
# workdir — один для всех задач!
```

Если два параллельных Claude Code процесса модифицируют один файл:
- Git конфликт при merge (если используются branches)
- Silent data corruption (если оба пишут в один файл без branches)
- Nondeterministic test failures

`DAG.check_scope_overlaps()` генерирует `ScopeWarning`, но:
1. Warning **не блокирует** запуск задач
2. Warning проверяет **glob patterns**, а не реальные файлы
3. `_patterns_overlap()` — heuristic, не точный (e.g. `src/*.py` и `src/auth/*.py` — false positive на overlap, хотя один nested)

#### Orchestrator mode (worktree isolation)

**✅ Правильный подход:**

Каждая zadacha работает в **своём git worktree** с отдельной branch:
```python
# workspace.py:55-87
workspace_path = self._workspace_base / zadacha_id
self._git.create_worktree(workspace_path, branch)
```

Это даёт filesystem-level изоляцию. Но:

**🟡 Merge conflicts отложены (medium):** Когда две zadachi завершаются и создают PR в main, merge conflicts обнаруживаются только на этапе PR merge (GitHub), а не в Maestro. Нет pre-merge validation.

**🟡 Shared .git directory:** Все worktrees делят один `.git` directory. Concurrent `git` operations в разных worktrees **безопасны** (git имеет internal locking), но heavy operations (repack, gc) могут замедлить все worktrees.

#### Database concurrency

`database.py` использует WAL mode (Write-Ahead Logging) и `ConcurrentModificationError` с `expected_status` pattern:
```python
await self._db.update_task_status(
    task_id,
    TaskStatus.RUNNING,
    expected_status=TaskStatus.READY,  # Optimistic lock
)
```

Это **корректный** optimistic concurrency control для single-process scheduler. Но если два scheduler instances запущены одновременно (accidental double-start), WAL mode не предотвратит двойной spawn.

---

## 4. Сравнение с аналогами

### 4.1 Maestro vs Airflow

| Аспект | Maestro | Airflow |
|---|---|---|
| **DAG definition** | YAML (static) | Python code (dynamic, programmatic) |
| **Conditional tasks** | ❌ Нет | ✅ BranchOperator, ShortCircuit, trigger_rules |
| **Scheduling** | Polling loop (1s) | Event-driven + database-backed scheduler |
| **Concurrency** | `max_concurrent` (global) | Pool-based (per-task, per-DAG, per-cluster) |
| **Retry** | Exponential backoff, no jitter | Exponential backoff + jitter + retry_delay |
| **Observability** | SQLite + SSE dashboard | Rich UI, Prometheus metrics, structured logs |
| **Task types** | AI agents only | 50+ operators (Bash, Python, Docker, K8s, ...) |
| **State persistence** | SQLite | Postgres/MySQL |
| **Deployment** | Single process | Distributed (scheduler + workers + webserver) |
| **Dead letter** | NEEDS_REVIEW (manual) | Configurable callbacks, SLA alerts |

**Что Maestro делает лучше:**
- Нативная интеграция с AI coding agents (prompt injection, retry context)
- Git worktree isolation из коробки
- Scope overlap detection
- Гораздо проще в setup (single binary vs. Airflow's 5 services)
- Cost tracking для AI API calls

**Что стоит заимствовать из Airflow:**
- 🔴 **Trigger rules** (`all_success`, `one_success`, `all_failed`, `none_failed`) — определяют поведение downstream при failure upstream
- 🔴 **Task pools** — лимиты per-resource, а не только global
- 🟡 **XCom** — передача данных между задачами (у Maestro есть `result_summary` и `_build_dependency_context()`, но это текстовый blob)

### 4.2 Maestro vs Prefect

| Аспект | Maestro | Prefect |
|---|---|---|
| **DAG** | Explicit `depends_on` | Implicit (from data flow) + explicit |
| **Dynamic tasks** | ❌ | ✅ `.map()`, `.submit()`, runtime-created tasks |
| **State handling** | Custom state machine | Rich state machine + state handlers |
| **Concurrency** | Process-based | Thread/process/async, task runners |
| **Retry** | Global policy | Per-task configurable with custom logic |
| **Infrastructure** | Local only | Local, Docker, K8s, serverless |

**Что стоит заимствовать из Prefect:**
- 🔴 **State change hooks** — callbacks при переходах состояний (`on_failure`, `on_completion`), а не только notifications
- 🟡 **Automatic caching** — если input не изменился, skip execution
- 🟡 **Параметризованные runs** — запуск того же DAG с разными параметрами

### 4.3 Maestro vs Temporal

| Аспект | Maestro | Temporal |
|---|---|---|
| **Model** | DAG tasks | Durable workflows (long-running) |
| **Failure handling** | Retry + NEEDS_REVIEW | Automatic replay, saga pattern |
| **State** | SQLite snapshots | Event-sourced history |
| **Determinism** | Not guaranteed | Enforced (replay safety) |
| **Human-in-the-loop** | AWAITING_APPROVAL (basic) | Signals, queries, updates |

**Что стоит заимствовать из Temporal:**
- 🔴 **Heartbeats** — task периодически сигнализирует "я жив" (вместо poll-based timeout detection)
- 🔴 **Workflow replay** — полная воспроизводимость: те же inputs → те же outputs
- 🟡 **Saga pattern** — compensation actions при failure (e.g. "откати git branch если PR creation failed")

### 4.4 Maestro vs Makefile

| Аспект | Maestro | Makefile |
|---|---|---|
| **DAG** | YAML `depends_on` | Target dependencies |
| **Parallelism** | `max_concurrent` | `make -j N` |
| **Incremental** | ❌ Always re-run | ✅ File timestamps |
| **Retry** | Built-in exponential backoff | None (manual re-run) |
| **AI awareness** | Prompt, scope, agent type | None |

**Что стоит заимствовать из Make:**
- 🟡 **Incremental execution** — skip задачу если output files newer than input files. Для AI tasks: skip если scope files unchanged since last run.
- 🟡 **Phony targets** — tasks without outputs (для "announce" type)

### 4.5 Сводная матрица рекомендаций

| Паттерн | Источник | Impact | Сложность внедрения |
|---|---|---|---|
| Trigger rules (cascade failure/skip) | Airflow | 🔴 High | Medium (state machine extension) |
| Jitter в retry | Industry standard | 🔴 High | Low (3 строки кода) |
| Agent-specific concurrency pools | Airflow | 🔴 High | Medium (new config + tracking) |
| Heartbeat mechanism | Temporal | 🔴 High | High (protocol change) |
| State change hooks | Prefect | 🟡 Medium | Medium (observer pattern) |
| Incremental execution | Make | 🟡 Medium | Medium (file hash tracking) |
| Saga/compensation actions | Temporal | 🟡 Medium | High (new concept) |
| Dynamic task generation | Prefect | 🟢 Low priority | High (fundamental change) |

---

## 5. Архитектурные находки

### 5.1 Два разных scheduling engine

Scheduler и Orchestrator реализуют **одинаковую логику** (poll loop → resolve ready → spawn → monitor) **по-разному**:

| Аспект | Scheduler | Orchestrator |
|---|---|---|
| Ready resolution | `DAG.get_ready_tasks()` | Inline `z.depends_on ⊆ completed` |
| Process spawning | Sync `Popen` через spawners | Async `create_subprocess_exec` |
| Retry | `RetryManager` с backoff | `zadacha.can_retry()` без backoff |
| Progress tracking | Только exit code | `.executor-state.json` parsing |
| Workspace | Shared workdir | Isolated worktree per zadacha |
| Shutdown | `FAILED → READY` | `FAILED → READY` (аналогично) |

**🔴 Code duplication (high impact on maintainability):** Два файла по ~700-900 строк с очень похожей структурой. Рефакторинг в общий `BaseSchedulerLoop` с pluggable strategies снизил бы дублирование на ~40%.

### 5.2 Отсутствие event-driven architecture

Обе реализации используют **polling** (sleep → check all):
- Scheduler: `poll_interval = 1.0s`
- Orchestrator: `poll_interval = 2.0s`

Каждую итерацию: DB query для completed → full scan для ready → poll each process. При 100 задачах это незаметно, но при 1000+ — O(n) per second becomes noticeable.

**Альтернатива:** Event-driven с `asyncio.Queue`:
```python
# Process completion → event
async def _wait_process(self, task_id, process):
    await process.wait()
    await self._event_queue.put(("completed", task_id, process.returncode))

# Main loop
while True:
    event = await self._event_queue.get()
    handle(event)
```

---

## Открытые вопросы к автору

1. **Двойная проверка циклов** — это осознанное решение (defense in depth) или legacy? Если defense in depth, стоит ли добавить comment/docstring объясняющий почему?

2. **Orchestrator не использует DAG класс** — планируется ли унификация? Или zadachi всегда будут иметь "плоские" зависимости без глубокого DAG?

3. **Scheduler mode: shared workdir** — это by design (для простоты) или worktree isolation планируется и для scheduler mode? Если by design, нужно ли усилить scope enforcement (reject spawn если scope overlap с running task)?

4. **Deadlock при failed dependency** — это known issue? Есть ли план добавить cascade failure propagation?

5. **Retry в orchestrator без backoff** — это осознанный выбор (spec-runner имеет свой backoff?) или пропущенная интеграция с RetryManager?

6. **Event-driven scheduling** — рассматривалось ли? Текущий polling подход хорош для прототипа, но при росте числа задач может стать bottleneck.

7. **Cost budget enforcement** — `cost_tracker.py` парсит стоимость, но не ограничивает. Планируется ли `max_budget_usd` в конфиге с автоматической остановкой при превышении?
