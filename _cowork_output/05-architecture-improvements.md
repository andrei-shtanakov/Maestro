# Maestro — Архитектурные улучшения

**Дата:** 2026-04-05
**Базовые документы:** `01-architecture-map.md`, `02-dag-scheduler-analysis.md`, `03-agent-integration.md`, `04-code-quality-ops.md`
**Анализируемые файлы:** весь пакет `maestro/` (33 файла), `COWORK_CONTEXT.md`, `pyproject.toml`

---

## TL;DR

1. **Maestro Core** нуждается в извлечении `BasePollLoop` из scheduler/orchestrator (~40% дублирования), event system на базе `asyncio.Queue` вместо polling, и формализации plugin architecture через entry points. DAG scheduler как отдельная библиотека — преждевременно.
2. **Связь с Arbiter** — сейчас **полностью отсутствует** в коде (0 импортов). Нужен thin adapter layer с JSON Schema контрактом и fallback на статический routing. Arbiter должен быть опциональным by design.
3. **Связь с ATP** — встраивать в pipeline как `ValidationStrategy`, не как внешний шаг. Текущий `Validator` уже имеет правильный интерфейс — нужно расширить его до strategy pattern.
4. **Developer Experience** — CLI хорош, но не хватает `maestro diagnose`, `maestro config validate`, живой документации (JSON Schema для YAML). Onboarding затруднён разрывом между COWORK_CONTEXT.md (aspirational) и реальным кодом.
5. **Масштабирование** — при 50+ задачах упрутся в polling O(n), SQLite write contention, отсутствие agent-specific concurrency pools. Distributed mode не нужен сейчас, но нужна подготовка: абстракция storage + queue.

---

## 1. Maestro Core

### 1.1 Компоненты для рефакторинга

#### 🔴 A. Извлечение `BasePollLoop` из scheduler.py + orchestrator.py

**Текущее:** Два файла (~903 + 646 строк) с ~40% идентичной логики: poll loop, signal handling, shutdown cleanup, process termination. Различия — в деталях (sync vs async subprocess, DAG vs inline dependency check, retry с backoff vs без).

**Целевое:** Общий `BasePollLoop` (ABC) с pluggable strategies.

```python
# maestro/loop.py (~200 строк)
class BasePollLoop(ABC):
    """Абстрактный poll loop с общим lifecycle."""

    async def run(self) -> Stats:
        self._setup_signals()
        try:
            await self._main_loop()
        finally:
            await self._cleanup()

    async def _main_loop(self):
        while not self._shutdown_requested:
            completed = await self._get_completed_ids()
            if await self._all_complete(): break
            ready = await self._resolve_ready(completed)
            await self._spawn_ready(ready)
            await self._monitor_running()
            await self._wait_or_shutdown(self._poll_interval)

    # Strategies (переопределяются в потомках):
    @abstractmethod
    async def _resolve_ready(self, completed: set[str]) -> list[str]: ...
    @abstractmethod
    async def _spawn_one(self, item_id: str) -> None: ...
    @abstractmethod
    async def _on_success(self, item_id: str) -> None: ...
    @abstractmethod
    async def _on_failure(self, item_id: str, error: str) -> None: ...

# maestro/scheduler.py → class Scheduler(BasePollLoop)
# maestro/orchestrator.py → class Orchestrator(BasePollLoop)
```

**Шаги миграции:**
1. Создать `maestro/loop.py` с `BasePollLoop` + `_wait_or_shutdown()` (заменяет дублированный `asyncio.wait_for + suppress`)
2. Извлечь общий signal handler setup и shutdown cleanup
3. Постепенно мигрировать scheduler → наследование от BasePollLoop
4. Аналогично orchestrator
5. Удалить дублированный код

**Trade-offs:**
- (+) Снижение дублирования ~40%, единая точка для фиксов (timeout, signal handling)
- (+) Проще добавить третий режим (e.g. "hybrid mode")
- (−) Дополнительный уровень абстракции, сложнее дебажить конкретный loop
- (−) Template method pattern может стать хрупким при расхождении режимов

**Рекомендация: делать.** Дублирование уже привело к расхождениям (retry с backoff в scheduler, без backoff в orchestrator; timeout в scheduler, отсутствует в orchestrator). Общий loop устранит эту категорию багов.

---

#### 🔴 B. Унификация retry logic

**Текущее:** Scheduler использует `RetryManager` с exponential backoff (`retry.py`). Orchestrator — `zadacha.can_retry()` без backoff (retry немедленно, `orchestrator.py:533-575`). Backoff теряется при restart (in-memory `_retry_ready_times` в `scheduler.py:453-476`).

**Целевое:** Единый `RetryPolicy` с backoff + jitter, сохранение `next_retry_at` в DB.

```python
# maestro/retry.py (расширение)
@dataclass
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 5.0
    max_delay: float = 300.0
    jitter_factor: float = 0.3  # ← НОВОЕ: ±30%

    def next_delay(self, attempt: int) -> float:
        delay = self.base_delay * (2 ** attempt)
        jitter = random.uniform(0, delay * self.jitter_factor)
        return min(delay + jitter, self.max_delay)

# В database.py: новое поле
# ALTER TABLE tasks ADD COLUMN next_retry_at TEXT;
# ALTER TABLE zadachi ADD COLUMN next_retry_at TEXT;
```

**Шаги миграции:**
1. Добавить jitter в `RetryManager.calculate_delay()` (3 строки кода)
2. Добавить `next_retry_at` column в DB schema
3. При failure: записать `next_retry_at` в DB вместо in-memory dict
4. При `_resolve_ready()`: фильтровать по `next_retry_at <= now()`
5. Подключить RetryManager к orchestrator (сейчас не используется)

**Trade-offs:**
- (+) Устраняет thundering herd, единая retry policy для обоих режимов
- (+) Backoff переживает restart
- (−) DB migration (minor)

**Рекомендация: делать в первую очередь.** Jitter — 3 строки кода, high impact.

---

#### 🟡 C. God-methods decomposition

**Текущее:** `_spawn_task()` в `scheduler.py:319-419` (100 строк, 7+ условных блоков), `_spawn_zadacha()` в `orchestrator.py:284-379` (95 строк), `update_task_status()` в `database.py:610-699` (90 строк).

**Целевое:** Каждый разбит на 3-4 подметода.

**Шаги:** Стандартный extract method refactoring. Не описываю детально — это механическая работа.

**Trade-offs:**
- (+) Читаемость, testability
- (−) Больше indirection

---

### 1.2 Event System (pub/sub) вместо прямых вызовов

**Текущее:** Polling loop: `sleep → scan all tasks → spawn/monitor → repeat`. Каждая итерация — O(V) scan + DB queries. Нет event-driven реакции на завершение процесса. `event_log.py` — write-only JSONL, не event bus.

**Целевое:** Гибридная модель: `asyncio.Queue` для internal events + polling как fallback.

```python
# maestro/events.py
@dataclass
class Event:
    type: str  # "process_completed", "task_claimed", "shutdown"
    payload: dict

class EventBus:
    def __init__(self):
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Callable) -> None: ...
    async def publish(self, event: Event) -> None: ...
    async def process_events(self, timeout: float = 1.0) -> None: ...

# Использование в BasePollLoop:
async def _wait_process(self, task_id, process):
    await process.wait()
    await self._bus.publish(Event("process_completed", {"task_id": task_id, "rc": process.returncode}))

async def _main_loop(self):
    while not self._shutdown_requested:
        await self._bus.process_events(timeout=self._poll_interval)
        # Fallback polling для missed events:
        if self._tick_counter % 10 == 0:  # каждые 10 итераций
            await self._full_state_reconciliation()
```

**Шаги миграции:**
1. Создать `events.py` с `EventBus` (~100 строк)
2. Интегрировать в `BasePollLoop._main_loop()` — заменить sleep на `process_events(timeout=)`
3. Для каждого spawned процесса — запустить `_wait_process()` task (async)
4. При получении event — targeted state update вместо full scan
5. Оставить periodic reconciliation как safety net

**Trade-offs:**
- (+) O(1) реакция на завершение вместо O(n) polling
- (+) Фундамент для будущих расширений: heartbeats, MCP callbacks, webhook triggers
- (+) Естественная интеграция с существующим `event_log.py` (EventBus → EventLog)
- (−) Усложняет debugging (async event flow vs линейный poll)
- (−) Нужно handle missed events (process completed between polls) — поэтому fallback reconciliation
- (−) ~300 строк нового кода + refactor обоих loops

**Рекомендация: делать, но ПОСЛЕ BasePollLoop extraction.** EventBus — natural extension BasePollLoop. Без BasePollLoop придётся внедрять в оба loop'а отдельно — удвоение работы.

**Альтернатива (проще):** Не делать полноценный EventBus, а просто заменить `process.poll()` на `await process.wait()` в asyncio tasks. Это даёт event-driven completion detection без полноценного pub/sub. Рекомендую начать с этого.

---

### 1.3 DAG Scheduler как отдельная библиотека

**Текущее:** `dag.py` (427 строк) — чистый модуль, зависит только от `models.TaskConfig`. Алгоритм Кана, topological sort, scope overlap detection.

**Вопрос:** Стоит ли выделить в отдельный пакет (PyPI)?

**Аргументы ЗА:**
- Чистые границы (единственная зависимость — `TaskConfig`)
- Переиспользуемость (AppForge, spec-runner, другие orchestrators)
- Отдельное версионирование

**Аргументы ПРОТИВ:**
- `get_ready_tasks()` принимает `set[str]` — generic, но `check_scope_overlaps()` завязан на `TaskConfig.scope` (domain-specific)
- DAG — 427 строк. Отдельный пакет для 427 строк — overhead (CI, releases, versioning, dependency management)
- Orchestrator **не использует** DAG-класс (inline `depends_on ⊆ completed`). Если выделить — orchestrator всё равно не будет зависеть
- Нет внешних потребителей прямо сейчас

**Рекомендация: НЕ выделять сейчас.** Преждевременная абстракция. Вместо этого:
1. Убрать дублирование: orchestrator должен использовать `DAG.get_ready_tasks()` вместо inline check
2. Убрать `validate_no_cyclic_dependencies()` из `models.py` (дублирует `DAG._detect_cycles()`)
3. Когда появится второй потребитель (e.g. AppForge) — тогда выделять

---

### 1.4 Plugin Architecture для агентов

**Текущее:** `SpawnerRegistry` (`registry.py`, 365 строк) уже поддерживает:
- Ручную регистрацию: `registry.register(spawner)`
- Entry points: `discover_entry_points(group="maestro.spawners")`
- Directory scanning: `discover_from_directory(path)`
- Fallback spawner для unknown types

**Это уже plugin architecture.** Вопрос — насколько формализовать.

**Целевое (рекомендация — эволюционный подход):**

**Фаза 1 (сейчас):** Формализовать entry points в `pyproject.toml`:
```toml
[project.entry-points."maestro.spawners"]
claude_code = "maestro.spawners.claude_code:ClaudeCodeSpawner"
codex = "maestro.spawners.codex:CodexSpawner"
aider = "maestro.spawners.aider:AiderSpawner"
announce = "maestro.spawners.announce:AnnounceSpawner"
```

Это ничего не ломает, но делает явным то, что сейчас находится через directory scan.

**Фаза 2 (при появлении внешних spawners):** Plugin metadata protocol:
```python
class AgentSpawner(ABC):
    # Существующие методы +

    @property
    def capabilities(self) -> dict:
        """Declarative capabilities for routing."""
        return {"languages": [], "tools": [], "max_concurrent": None}

    @property
    def version(self) -> str:
        return "0.1.0"
```

**Фаза 3 (если нужна изоляция):** Spawners как отдельные pip-пакеты (`maestro-spawner-claude`, `maestro-spawner-aider`). Это нужно только если spawners начнут тянуть тяжёлые зависимости.

**Trade-offs:**
- Фаза 1: (+) 5 строк в pyproject.toml, (−) ничего
- Фаза 2: (+) Фундамент для Arbiter routing, (−) breaking change в ABC
- Фаза 3: (+) Полная изоляция зависимостей, (−) значительный operational overhead

**Рекомендация: Фаза 1 сейчас, Фаза 2 при интеграции Arbiter.**

---

## 2. Связь Maestro ↔ Arbiter

### 2.1 Текущая связанность

**Ответ: НИКАКОЙ связанности.** В коде Maestro **нет ни одного упоминания** Arbiter. `grep -ri "arbiter" maestro/` — 0 результатов. Arbiter описан в COWORK_CONTEXT.md как aspirational design, но не реализован.

Maestro полностью standalone: `agent_type` задаётся статически в YAML, `pyproject.toml` не содержит arbiter-зависимостей.

### 2.2 Целевая архитектура

**Текущее:** Static routing — `agent_type` hardcoded в tasks.yaml.

**Целевое:** Optional dynamic routing через thin adapter.

```python
# maestro/routing.py (~150 строк) — НОВЫЙ ФАЙЛ
from abc import ABC, abstractmethod

class RoutingStrategy(ABC):
    """Стратегия выбора агента для задачи."""

    @abstractmethod
    async def select_agent(self, task: Task, available_agents: list[str]) -> str: ...

class StaticRouting(RoutingStrategy):
    """Текущее поведение: agent_type из YAML."""
    async def select_agent(self, task, available) -> str:
        return task.agent_type

class ArbiterRouting(RoutingStrategy):
    """Роутинг через Arbiter MCP server."""
    def __init__(self, arbiter_url: str, timeout: float = 5.0): ...

    async def select_agent(self, task, available) -> str:
        try:
            result = await self._mcp_call("route_task", {
                "task_id": task.id,
                "title": task.title,
                "scope": task.scope,
                "priority": task.priority,
                "available_agents": available,
            })
            return result["agent_type"]
        except (ConnectionError, TimeoutError):
            logger.warning("Arbiter unavailable, falling back to static routing")
            return task.agent_type  # Graceful fallback

# В scheduler.py:
class Scheduler(BasePollLoop):
    def __init__(self, ..., routing: RoutingStrategy = StaticRouting()):
        self._routing = routing

    async def _spawn_one(self, task_id):
        task = await self._db.get_task(task_id)
        agent_type = await self._routing.select_agent(task, list(self._spawners.keys()))
        spawner = self._spawners[agent_type]
        ...
```

### 2.3 Контракт: JSON Schema vs Protobuf

**Текущее:** Нет формального контракта (Arbiter не интегрирован).

**Целевое:** JSON Schema.

**Почему JSON Schema, а не Protobuf:**

| Критерий | JSON Schema | Protobuf |
|---|---|---|
| Совместимость с MCP | ✅ Нативно (MCP = JSON-RPC) | ❌ Нужен adapter |
| Совместимость с Pydantic | ✅ `model_json_schema()` генерирует автоматически | 🟡 Через `protobuf-to-pydantic` |
| Совместимость с REST API | ✅ Нативно (FastAPI + OpenAPI) | ❌ Нужен gRPC gateway |
| Overhead | Минимален для AI agent orchestration | Избыточен для ~10 RPS |
| Arbiter (Rust) | `serde_json` + `jsonschema` | `prost` (хорошо, но другая экосистема) |

**Рекомендация: JSON Schema.**

```python
# Генерация контракта из Pydantic:
from maestro.models import Task

schema = Task.model_json_schema()
# → Сохранить как maestro/schemas/route_request.json
# → Arbiter валидирует входящие запросы по этой схеме
```

**Шаги миграции:**
1. Создать `maestro/routing.py` с `RoutingStrategy` ABC + `StaticRouting`
2. Внедрить в Scheduler через dependency injection (default: `StaticRouting`)
3. Сгенерировать JSON Schema из Pydantic models → `maestro/schemas/`
4. Создать `ArbiterRouting` с MCP call + fallback
5. Конфиг: `routing: {type: static}` или `routing: {type: arbiter, url: "..."}`

### 2.4 Arbiter как опциональный компонент

**Принцип:** Arbiter должен быть **enhancer, не dependency**.

**Механизмы обеспечения опциональности:**
1. `RoutingStrategy` ABC с `StaticRouting` default → Maestro работает без Arbiter из коробки
2. `ArbiterRouting` с graceful fallback на `task.agent_type` при connection failure
3. `arbiter` не в `dependencies`, а в `optional-dependencies`:
   ```toml
   [project.optional-dependencies]
   arbiter = ["maestro-arbiter-client>=0.1"]
   ```
4. Runtime check: `if arbiter_url in config: use ArbiterRouting else: use StaticRouting`

**Trade-offs:**
- (+) Zero-cost abstraction: StaticRouting — passthrough, без overhead
- (+) Graceful degradation: Arbiter падает → fallback на static
- (−) Routing strategy абстракция может стать leaky, если Arbiter нуждается в rich metadata (22-dim feature vector), которое StaticRouting не генерирует

---

## 3. Связь Maestro ↔ ATP

### 3.1 Post-task validation через ATP

**Текущее:** `validator.py` (298 строк) — запуск `validation_cmd` как subprocess. Захватывает stdout/stderr, timeout 300s. Возвращает `ValidationResult(success, output, error)`.

Точка вызова: `scheduler.py` → после процесса с exit code 0 → `VALIDATING` → `validator.validate()` → `DONE` или `FAILED`.

ATP Platform упоминается в COWORK_CONTEXT.md как "верификация результатов агентов", но **не интегрирована**.

**Целевое:** `ValidationStrategy` pattern, где ATP — одна из стратегий.

```python
# maestro/validator.py (расширение)
class ValidationStrategy(ABC):
    @abstractmethod
    async def validate(self, task: Task, workdir: Path) -> ValidationResult: ...

class CommandValidator(ValidationStrategy):
    """Текущее поведение: запуск validation_cmd как subprocess."""
    async def validate(self, task, workdir) -> ValidationResult:
        # ... существующий код из Validator._run_validation() ...

class ATPValidator(ValidationStrategy):
    """Валидация через ATP Platform API."""
    def __init__(self, atp_url: str, timeout: float = 120.0):
        self._url = atp_url

    async def validate(self, task, workdir) -> ValidationResult:
        # 1. Собрать diff (git diff HEAD~1)
        # 2. Отправить на ATP: POST /validate {task_id, diff, scope, agent_type}
        # 3. Получить результат: {passed: bool, issues: [...], suggestions: [...]}
        # 4. Вернуть ValidationResult

class CompositeValidator(ValidationStrategy):
    """Цепочка: сначала CommandValidator, потом ATP."""
    def __init__(self, validators: list[ValidationStrategy]):
        self._validators = validators

    async def validate(self, task, workdir) -> ValidationResult:
        for v in self._validators:
            result = await v.validate(task, workdir)
            if not result.success:
                return result
        return ValidationResult(success=True, output="All validations passed")
```

### 3.2 Встраивание в pipeline vs внешний шаг

**Вариант A: Встроить в pipeline (рекомендую)**

```yaml
# tasks.yaml
defaults:
  validation:
    - type: command
      cmd: "uv run pytest"
    - type: atp
      url: "http://localhost:8080"
      checks: [style, security, tests]
```

**Плюсы:** Единый lifecycle (VALIDATING → validators → DONE/FAILED), retry при validation failure, результат ATP виден в dashboard.

**Вариант B: Внешний шаг (post-pipeline)**

ATP запускается после всего pipeline как отдельный CI/CD step.

**Плюсы:** Не усложняет Maestro, ATP может валидировать весь результат целиком (не отдельные задачи).

**Рекомендация: Вариант A для per-task validation, Вариант B для project-level validation.** Они не исключают друг друга.

**Шаги миграции:**
1. Рефакторить `Validator` → `CommandValidator(ValidationStrategy)`
2. Добавить `CompositeValidator` для chaining
3. Конфиг: `validation: [{type: command, cmd: "..."}, {type: atp, url: "..."}]`
4. `ATPValidator` — реализовать при наличии ATP API spec
5. Добавить `validation_results` в Task model (structured, не только text)

**Trade-offs:**
- (+) Per-task ATP validation с retry = fast feedback loop для агентов
- (+) ValidationStrategy pattern позволяет добавлять validators без изменения core
- (−) ATP-зависимость в hot path — если ATP медленный, замедляет весь pipeline
- (−) Нужен timeout и circuit breaker для ATP calls

---

## 4. Developer Experience

### 4.1 CLI: что не хватает

**Текущие команды (8):** `run`, `status`, `retry`, `stop`, `approve`, `orchestrate`, `zadachi`, `workspaces`

#### 🔴 Отсутствующие команды (high impact)

| Команда | Назначение | Сложность |
|---|---|---|
| `maestro config validate <file>` | Валидация YAML без запуска. Сейчас ошибки видны только при `run` | Low — вызвать `load_config()` + print errors |
| `maestro diagnose [--db]` | Диагностика: stuck tasks, orphaned worktrees, DB integrity | Medium — query DB + `git worktree list` + process check |
| `maestro logs <task-id>` | Показать логи конкретной задачи (tail -f или cat) | Low — `log_dir / task_id / *.log` |
| `maestro clean [--worktrees] [--db]` | Очистка: orphaned worktrees, stale DB entries | Medium — `git worktree prune` + DB cleanup |

#### 🟡 Улучшения существующих (medium)

| Улучшение | Текущее | Целевое |
|---|---|---|
| `status` elapsed time | Показывает только status | Показывать `RUNNING (3m 42s)` |
| `status` failure reason | Нет | Показывать `FAILED: validation timeout (retry 2/3)` |
| `status --watch` | Нет (one-shot) | Polling с `rich.live` обновлением |
| `run --dry-run` | Нет | Показать DAG + execution plan без запуска |

**Шаги:** Каждая команда — отдельный PR, 50-100 строк в `cli.py`. `config validate` и `diagnose` — первый приоритет.

### 4.2 Документация: приоритеты

#### 🔴 Критический разрыв: COWORK_CONTEXT.md vs реальность

COWORK_CONTEXT.md описывает:
- Arbiter integration (22-dim feature vector, MCP) — **не реализовано**
- WorkflowLoader + WORKFLOW.md injection — **не реализовано**
- `agents.toml` parsing — **не реализовано**
- ATP integration — **не реализовано**

Новый разработчик, прочитав COWORK_CONTEXT.md, будет искать код, которого нет. Это серьёзная проблема onboarding.

**Рекомендация:** Разделить на два документа:
1. `ARCHITECTURE.md` — описание **текущего** состояния (что реализовано)
2. `ROADMAP.md` — описание **целевого** состояния (что планируется)

#### Приоритеты документации

| # | Документ | Impact | Сложность |
|---|---|---|---|
| 1 | Разделение COWORK_CONTEXT.md → ARCHITECTURE.md + ROADMAP.md | 🔴 High | Low |
| 2 | JSON Schema для tasks.yaml и project.yaml (auto-gen из Pydantic) | 🔴 High | Low |
| 3 | `docs/adr/` — ADR для ключевых решений (два режима, SQLite, polling) | 🟡 Medium | Medium |
| 4 | `CONTRIBUTING.md` — как добавить нового spawner, как запустить тесты | 🟡 Medium | Low |
| 5 | Примеры конфигов с комментариями (examples/ расширить) | 🟡 Medium | Low |

### 4.3 Onboarding

**Текущее состояние: 4/10.**

Что хорошо: CLAUDE.md с командами, README.md, чистый код с docstrings.

Что плохо:
- COWORK_CONTEXT.md обманывает о состоянии проекта
- Нет `CONTRIBUTING.md`
- Нет `docs/getting-started.md`
- Нет пояснения двух режимов и когда какой использовать
- Нет примера "запусти за 5 минут"

**Целевое: 7/10.**

**Шаги:**
1. `CONTRIBUTING.md`: как добавить spawner (entry points), как запустить тесты
2. `docs/getting-started.md`: установка → `examples/tasks.yaml` → `maestro run` → `maestro status`
3. Инлайн-комментарии в `examples/tasks.yaml` и `examples/project.yaml`
4. Разделить COWORK_CONTEXT.md (см. выше)

---

## 5. Масштабирование

### 5.1 При 50+ параллельных задачах

#### Что сломается

| Компонент | Проблема | Порог | Severity |
|---|---|---|---|
| **Polling O(n)** | `get_ready_tasks()` — O(V) scan всех nodes каждую секунду. 50 задач OK, 500 — заметный CPU | ~200 tasks | 🟡 Medium |
| **SQLite write contention** | WAL mode позволяет concurrent reads, но **single writer**. 50 параллельных task status updates → contention | ~30 concurrent writes | 🔴 High |
| **`max_concurrent` cap = 10** | `ProjectConfig.max_concurrent` валидируется как 1-10 (`models.py`). При 50 задачах — узкое горло | Hard limit | 🔴 High |
| **Agent-specific concurrency** | Нет `max_per_agent_type`. 10 Claude Code + 0 Codex, хотя оптимально 5+5 | При гетерогенных задачах | 🟡 Medium |
| **Process FD exhaustion** | Каждый spawned process = 1 Popen + 1 log file FD. 50 processes → 100+ FDs | ulimit -n (usually 1024) | 🟢 Low |
| **Retry thundering herd** | 50 задач fail одновременно → все retry через 5s (нет jitter) | При массовом failure | 🔴 High |

#### Решения

**Текущее → Целевое:**

1. **`max_concurrent`: 10 → configurable**
   ```python
   # models.py — убрать верхний лимит или поднять до 100
   max_concurrent: int = Field(default=3, ge=1, le=100)
   ```

2. **Agent pools:**
   ```yaml
   # tasks.yaml
   concurrency:
     global: 20
     per_agent:
       claude_code: 5
       codex: 3
       aider: 10
   ```

3. **SQLite → write batching:**
   ```python
   # database.py — batch status updates
   async def batch_update_statuses(self, updates: list[tuple[str, TaskStatus]]):
       async with self._db.execute("BEGIN"):
           for task_id, status in updates:
               await self._update_single(task_id, status)
   ```

4. **Jitter (описано в 1.2 выше)**

5. **Event-driven loop (описано в 1.2 выше)** — при 50+ задачах polling O(n) per second = 50+ DB queries/sec. Event-driven = query only on event.

### 5.2 При 10+ репозиториях одновременно

#### Что сломается

| Компонент | Проблема | Severity |
|---|---|---|
| **Single SQLite DB** | Все задачи из всех репо — в одной DB. Нет namespace/isolation | 🔴 High |
| **Disk space (worktrees)** | 10 репо × 5 worktrees/repo × 5GB/repo = 250GB | 🔴 High |
| **Single scheduler process** | Один event loop обслуживает все репо. Если один repo's git операция зависает — блокирует всё | 🟡 Medium |
| **Branch naming collisions** | `feature/{zadacha_id}` — если два репо имеют задачу с одинаковым ID | 🟡 Medium |
| **Config management** | Нет "multi-project" конфига. Каждый `maestro run` — отдельный process | 🟢 Low |

#### Решения

**Текущее → Целевое:**

1. **DB per project:**
   ```python
   # Текущее: один maestro.db
   # Целевое: {project_name}/maestro.db или namespace в tables
   db_path = project_dir / ".maestro" / "maestro.db"
   ```

2. **Worktree quota:**
   ```yaml
   orchestrator:
     max_worktrees: 10
     max_disk_gb: 50
     cleanup_on_success: true  # Уже есть
     cleanup_on_failure: false  # Для debug
   ```

3. **Multi-project orchestrator (Phase 2):**
   ```yaml
   # maestro-multi.yaml
   projects:
     - name: backend
       repo: /path/to/backend
       config: tasks.yaml
     - name: frontend
       repo: /path/to/frontend
       config: tasks.yaml
   ```

   Каждый project — отдельный `Scheduler` instance в своём `asyncio.Task`. Shared event bus для координации.

### 5.3 Distributed mode (несколько машин)

**Текущее:** Single-process, single-machine. SQLite — local file.

**Нужен ли distributed mode?**

**Сейчас — НЕТ.** AI agent orchestration ограничен:
- API rate limits (Claude: ~60 RPM, Codex: varies)
- Одна машина легко тянет 10-20 параллельных агентов
- Git worktrees требуют local filesystem

**Когда понадобится:**
- 100+ параллельных агентов (маловероятно при текущих API лимитах)
- Разные агенты на разных машинах (GPU для local models)
- Geo-distributed teams с разными access patterns

**Подготовка без реализации:**

```python
# Абстракция storage:
class StateStore(ABC):
    """Абстрагирует persistence layer."""
    @abstractmethod
    async def get_task(self, task_id: str) -> Task: ...
    @abstractmethod
    async def update_status(self, task_id: str, status: TaskStatus) -> None: ...

class SQLiteStore(StateStore):
    """Текущая реализация."""
    ...

# При необходимости distributed:
class PostgresStore(StateStore): ...
class RedisStore(StateStore): ...
```

**Trade-offs:**
- (+) Подготовка без overhead: `StateStore` ABC — ~50 строк, `SQLiteStore` — wrapper над текущим `Database`
- (−) Premature abstraction risk: если distributed mode никогда не понадобится
- (−) PostgresStore/RedisStore требуют значительных отличий (transactions, locking)

**Рекомендация: НЕ реализовывать distributed mode. НЕ делать абстракцию storage сейчас.** SQLite + WAL покрывает текущие и ближайшие потребности. Абстракция storage выглядит чисто, но на практике семантика SQL transactions, optimistic locking и WAL специфична для SQLite, и обёртка будет leaky.

Если distributed mode понадобится — проще будет перейти на message queue (Redis Streams, NATS) + PostgreSQL, чем пытаться абстрагировать текущий SQLite-based подход.

---

## 6. Сводная матрица рекомендаций

### По приоритету

| # | Рекомендация | Направление | Impact | Сложность | Зависимости |
|---|---|---|---|---|---|
| 1 | Jitter в retry | Core | 🔴 High | Low (3 строки) | Нет |
| 2 | Поднять `max_concurrent` cap | Масштабирование | 🔴 High | Low (1 строка) | Нет |
| 3 | Разделить COWORK_CONTEXT.md | DX | 🔴 High | Low | Нет |
| 4 | `maestro config validate` | DX | 🔴 High | Low (~50 строк) | Нет |
| 5 | Entry points в pyproject.toml | Core / Plugins | 🟡 Medium | Low (5 строк) | Нет |
| 6 | Извлечь `BasePollLoop` | Core | 🔴 High | Medium (~300 строк) | Нет |
| 7 | `maestro diagnose` | DX | 🔴 High | Medium (~100 строк) | Нет |
| 8 | `RoutingStrategy` + Arbiter adapter | Arbiter | 🔴 High | Medium (~150 строк) | #6 (лучше после BasePollLoop) |
| 9 | Унификация retry (scheduler ↔ orchestrator) | Core | 🔴 High | Medium | #6 |
| 10 | Agent-specific concurrency pools | Масштабирование | 🟡 Medium | Medium | Нет |
| 11 | `ValidationStrategy` pattern | ATP | 🟡 Medium | Medium (~200 строк) | Нет |
| 12 | Event-driven loop (`asyncio.Queue`) | Core | 🟡 Medium | High (~300 строк) | #6 |
| 13 | DB per project | Масштабирование | 🟡 Medium | Medium | Нет |
| 14 | JSON Schema для YAML configs | DX | 🟡 Medium | Low | Нет |
| 15 | DAG выделить в библиотеку | Core | 🟢 Low | High | Внешний потребитель |
| 16 | Distributed mode | Масштабирование | 🟢 Low | Very High | Реальная потребность |

### Рекомендуемый порядок внедрения

**Sprint 1 (Quick Wins, 1-2 дня):**
- #1 Jitter
- #2 Max concurrent cap
- #3 Разделить COWORK_CONTEXT.md
- #4 `config validate`
- #5 Entry points
- #14 JSON Schema

**Sprint 2 (Core Refactoring, 3-5 дней):**
- #6 BasePollLoop
- #9 Унификация retry
- #7 `maestro diagnose`

**Sprint 3 (Integration, 3-5 дней):**
- #8 RoutingStrategy + Arbiter
- #11 ValidationStrategy + ATP
- #10 Agent pools

**Sprint 4 (Scale, по необходимости):**
- #12 Event-driven loop
- #13 DB per project

---

## Открытые вопросы к автору

1. **BasePollLoop** — устраивает ли Template Method pattern? Или предпочтительнее composition (inject strategies через конструктор вместо наследования)?

2. **Arbiter** — актуален ли он? Если да, есть ли API spec или хотя бы список MCP tools, которые Arbiter будет предоставлять? Это определит дизайн `ArbiterRouting`.

3. **ATP** — есть ли API spec? Какие проверки ATP делает (lint, security, test coverage, code review)? Это определит структуру `ATPValidator`.

4. **Multi-repo** — реальный use case или теоретический? Если реальный — сколько репо, какого размера? Это определит приоритет DB per project и worktree quotas.

5. **`max_concurrent` cap = 10** — это осознанное ограничение (e.g. "больше 10 Claude Code одновременно бессмысленно из-за API rate limits") или произвольный лимит?

6. **COWORK_CONTEXT.md** — кто его поддерживает? Есть ли процесс обновления при изменениях в коде? Или это snapshot, который разошёлся с реальностью?

7. **Distributed mode** — есть ли сценарий, где одна машина недостаточна? Или текущий масштаб (5-10 параллельных агентов) покрывает потребности на обозримое будущее?
