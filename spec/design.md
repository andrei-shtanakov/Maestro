# Design Specification

> Архитектура, API, схемы данных и ключевые решения для Maestro

## 1. Обзор архитектуры

### 1.1 Принципы

| Принцип | Описание |
|---------|----------|
| Plugin-first | Новый агент = один Python-модуль, минимум boilerplate |
| API-driven | Все компоненты общаются через API, не файловую систему |
| Fail-safe | Crash → recover из SQLite, no data loss |
| Single-process | MVP работает в одном процессе, готов к масштабированию |

### 1.2 Высокоуровневая диаграмма

```
┌─────────────────────────────────────────────────────────────────┐
│                          MAESTRO                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────┐   ┌───────────┐   ┌──────────┐   ┌──────────────┐ │
│  │  CLI    │──►│  Config   │──►│   DAG    │──►│  Scheduler   │ │
│  │         │   │  Parser   │   │ Builder  │   │  (asyncio)   │ │
│  └─────────┘   └───────────┘   └──────────┘   └──────┬───────┘ │
│                                                       │         │
│       ┌───────────────────────────────────────────────┤         │
│       │                                               │         │
│       ▼                                               ▼         │
│  ┌─────────┐   ┌───────────┐   ┌──────────┐   ┌────────────┐  │
│  │ Spawner │   │  Spawner  │   │ Spawner  │   │   SQLite   │  │
│  │ Claude  │   │   Codex   │   │  Aider   │   │     DB     │  │
│  └────┬────┘   └─────┬─────┘   └────┬─────┘   └────────────┘  │
│       │              │              │                │         │
│       ▼              ▼              ▼                │         │
│  ┌─────────────────────────────────────┐            │         │
│  │        subprocess.Popen             │            │         │
│  │   (agent processes in branches)     │            │         │
│  └─────────────────────────────────────┘            │         │
│                                                      │         │
│  ┌───────────────────────────────────────────────────┘         │
│  │                                                              │
│  ▼                                                              │
│  ┌─────────────┐        ┌─────────────┐                        │
│  │  MCP Server │        │  REST API   │                        │
│  │  (FastMCP)  │        │  (FastAPI)  │                        │
│  └─────────────┘        └─────────────┘                        │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**Traces to:** [REQ-001], [REQ-002], [REQ-020], [REQ-021]

---

## 2. Компоненты

### DESIGN-001: Config Parser

#### Описание
Парсит YAML-файл с описанием задач, валидирует схему, разрешает переменные окружения.

#### Interface
```python
from pydantic import BaseModel

class TaskConfig(BaseModel):
    id: str
    title: str
    prompt: str
    agent_type: str = "claude_code"
    scope: list[str] = []
    depends_on: list[str] = []
    timeout_minutes: int = 30
    max_retries: int = 2
    validation_cmd: str | None = None
    requires_approval: bool = False

class ProjectConfig(BaseModel):
    project: str
    repo: str
    max_concurrent: int = 3
    tasks: list[TaskConfig]
    git: GitConfig | None = None
    notifications: NotificationConfig | None = None

def load_config(path: Path) -> ProjectConfig:
    """Load and validate YAML config."""
    ...
```

#### Конфигурация
```yaml
project: feature-auth-jwt
repo: /path/to/repo
max_concurrent: 3

defaults:
  timeout_minutes: 30
  max_retries: 2
  agent_type: claude_code

tasks:
  - id: task-1
    title: "Task title"
    prompt: |
      Multi-line prompt
    scope: ["src/module/*"]
    depends_on: [task-0]
```

**Traces to:** [REQ-001]

---

### DESIGN-002: DAG Builder

#### Описание
Строит направленный ациклический граф из задач, обнаруживает циклы, определяет порядок выполнения.

#### Interface
```python
from dataclasses import dataclass

@dataclass
class DAGNode:
    task_id: str
    dependencies: set[str]
    dependents: set[str]

class DAG:
    def __init__(self, tasks: list[TaskConfig]) -> None:
        """Build DAG from task configs. Raises CycleError if cycles detected."""
        ...

    def get_ready_tasks(self, completed: set[str]) -> list[str]:
        """Return task IDs that are ready to run."""
        ...

    def topological_sort(self) -> list[str]:
        """Return tasks in execution order."""
        ...

    def check_scope_overlaps(self) -> list[ScopeWarning]:
        """Check for overlapping scopes in parallel tasks."""
        ...
```

**Traces to:** [REQ-002], [REQ-004]

---

### DESIGN-003: Task State Machine

#### Описание
Управляет жизненным циклом задачи, валидирует переходы между статусами.

#### State Diagram
```
                  ┌──────────────────────────────────────┐
                  │                                      │
                  ▼                                      │
PENDING ──► READY ──► RUNNING ──► VALIDATING ──► DONE   │
               │        │            │                   │
               │        │            │ validation failed │
               │        │            ▼                   │
               │        └──────► FAILED ──► (retry?) ───┘
               │                   │
               │                   │ max retries exceeded
               │                   ▼
               │              NEEDS_REVIEW ──► (manual) ──► READY
               │                   │
               │                   │ abandoned by user
               │                   ▼
               │              ABANDONED
               │
               │ requires_approval=true
               ▼
          AWAITING_APPROVAL ──► (approved) ──► RUNNING
```

#### Interface
```python
from enum import Enum

class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    VALIDATING = "validating"
    DONE = "done"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    ABANDONED = "abandoned"

class TaskStateMachine:
    VALID_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
        TaskStatus.PENDING: {TaskStatus.READY},
        TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL},
        TaskStatus.AWAITING_APPROVAL: {TaskStatus.RUNNING, TaskStatus.ABANDONED},
        TaskStatus.RUNNING: {TaskStatus.VALIDATING, TaskStatus.FAILED},
        TaskStatus.VALIDATING: {TaskStatus.DONE, TaskStatus.FAILED},
        TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.NEEDS_REVIEW},
        TaskStatus.NEEDS_REVIEW: {TaskStatus.READY, TaskStatus.ABANDONED},
    }

    def can_transition(self, from_status: TaskStatus, to_status: TaskStatus) -> bool:
        ...

    def transition(self, task_id: str, to_status: TaskStatus) -> None:
        """Validate and execute state transition."""
        ...
```

**Traces to:** [REQ-003]

---

### DESIGN-010: Agent Spawner (Base)

#### Описание
Абстрактный базовый класс для всех spawner'ов. Определяет интерфейс запуска агентов.

#### Interface
```python
from abc import ABC, abstractmethod
from subprocess import Popen

class AgentSpawner(ABC):
    """Base class for agent spawners."""

    @property
    @abstractmethod
    def agent_type(self) -> str:
        """Unique identifier for this agent type."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this agent is installed and available."""
        ...

    @abstractmethod
    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
    ) -> Popen[bytes]:
        """Spawn agent process. Returns subprocess handle."""
        ...

    def build_prompt(self, task: Task, context: str) -> str:
        """Build prompt with task details and dependency context."""
        return f"""Task: {task.title}

{task.prompt}

Context from completed dependencies:
{context}

Scope (files you can modify):
{', '.join(task.scope) or 'any'}
"""
```

**Traces to:** [REQ-011]

---

### DESIGN-011: Claude Code Spawner

#### Описание
Spawner для Claude Code в headless-режиме.

#### Interface
```python
class ClaudeCodeSpawner(AgentSpawner):
    agent_type = "claude_code"

    def is_available(self) -> bool:
        return shutil.which("claude") is not None

    def spawn(
        self,
        task: Task,
        context: str,
        workdir: Path,
        log_file: Path,
    ) -> Popen[bytes]:
        prompt = self.build_prompt(task, context)
        return Popen(
            ["claude", "--print", "--output-format", "json", "-p", prompt],
            cwd=workdir,
            stdout=log_file.open("w"),
            stderr=subprocess.STDOUT,
        )
```

**Traces to:** [REQ-010]

---

### DESIGN-012: Git Manager

#### Описание
Управляет git-операциями: создание веток, rebase, push.

#### Interface
```python
class GitManager:
    def __init__(self, repo_path: Path, base_branch: str = "main") -> None:
        ...

    def create_task_branch(self, task_id: str) -> str:
        """Create and checkout agent/<task_id> branch."""
        branch = f"agent/{task_id}"
        subprocess.run(["git", "checkout", "-b", branch], cwd=self.repo_path, check=True)
        return branch

    def rebase_on_base(self) -> None:
        """Rebase current branch on base_branch."""
        ...

    def push(self, branch: str) -> None:
        """Push branch to origin."""
        ...

    def get_current_branch(self) -> str:
        ...
```

**Traces to:** [REQ-012]

---

### DESIGN-020: MCP Server

#### Описание
FastMCP сервер для координации агентов через MCP protocol.

#### Interface
```python
from fastmcp import FastMCP

mcp = FastMCP("maestro-coordination")

@mcp.tool()
def get_available_tasks(agent_id: str) -> list[dict]:
    """Get list of READY tasks available for claiming."""
    ...

@mcp.tool()
def claim_task(agent_id: str, task_id: str) -> dict:
    """Atomically claim a task. Returns task details or error."""
    ...

@mcp.tool()
def update_status(
    agent_id: str,
    task_id: str,
    status: str,
    result_summary: str | None = None,
) -> dict:
    """Update task status and optionally add result summary."""
    ...

@mcp.tool()
def get_task_result(task_id: str) -> dict:
    """Get result of completed task (for dependency context)."""
    ...

@mcp.tool()
def post_message(from_agent: str, message: str, to_agent: str | None = None) -> dict:
    """Post inter-agent message. to_agent=None for broadcast."""
    ...

@mcp.tool()
def read_messages(agent_id: str) -> list[dict]:
    """Read unread messages for agent."""
    ...
```

**Traces to:** [REQ-020]

---

### DESIGN-021: REST API

#### Описание
FastAPI REST API с теми же эндпоинтами что и MCP.

#### API
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | /tasks | List all tasks with status |
| GET | /tasks/available | List READY tasks |
| GET | /tasks/{id} | Get task details |
| POST | /tasks/{id}/claim | Claim task (body: {agent_id}) |
| PUT | /tasks/{id}/status | Update status (body: {status, result_summary}) |
| GET | /tasks/{id}/result | Get task result |
| POST | /messages | Post message |
| GET | /messages/{agent_id} | Get messages for agent |
| GET | /health | Health check |

#### Interface
```python
from fastapi import FastAPI

app = FastAPI(title="Maestro API", version="1.0.0")

@app.get("/tasks/available")
async def get_available_tasks(agent_id: str) -> list[TaskResponse]:
    ...

@app.post("/tasks/{task_id}/claim")
async def claim_task(task_id: str, body: ClaimRequest) -> TaskResponse:
    ...
```

**Traces to:** [REQ-021]

---

### DESIGN-030: Validator

#### Описание
Выполняет post-task validation команды.

#### Interface
```python
class Validator:
    async def validate(self, task: Task, workdir: Path) -> ValidationResult:
        """Run validation_cmd and return result."""
        if not task.validation_cmd:
            return ValidationResult(success=True, output="No validation configured")

        proc = await asyncio.create_subprocess_shell(
            task.validation_cmd,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()

        return ValidationResult(
            success=proc.returncode == 0,
            output=stdout.decode(),
        )
```

**Traces to:** [REQ-030]

---

### DESIGN-031: Retry Manager

#### Описание
Управляет retry-логикой с экспоненциальной задержкой.

#### Interface
```python
class RetryManager:
    def __init__(self, base_delay: float = 5.0, max_delay: float = 300.0) -> None:
        self.base_delay = base_delay
        self.max_delay = max_delay

    def get_delay(self, retry_count: int) -> float:
        """Calculate exponential backoff delay."""
        delay = self.base_delay * (2 ** retry_count)
        return min(delay, self.max_delay)

    def should_retry(self, task: Task) -> bool:
        """Check if task should be retried."""
        return task.retry_count < task.max_retries

    def build_retry_context(self, task: Task, error: str) -> str:
        """Build context for retry prompt."""
        return f"""
Previous attempt failed with error:
{error}

Please fix the issue and try again.
Attempt {task.retry_count + 1} of {task.max_retries}.
"""
```

**Traces to:** [REQ-031]

---

### DESIGN-032: State Recovery

#### Описание
Восстановление состояния после аварийного перезапуска.

#### Interface
```python
class StateRecovery:
    def __init__(self, db: Database) -> None:
        self.db = db

    def recover(self) -> RecoveryResult:
        """Recover state from database after crash."""
        # RUNNING tasks -> READY (will be restarted)
        running_tasks = self.db.get_tasks_by_status(TaskStatus.RUNNING)
        for task in running_tasks:
            self.db.update_status(task.id, TaskStatus.READY)

        # VALIDATING tasks -> READY
        validating_tasks = self.db.get_tasks_by_status(TaskStatus.VALIDATING)
        for task in validating_tasks:
            self.db.update_status(task.id, TaskStatus.READY)

        return RecoveryResult(
            recovered_tasks=len(running_tasks) + len(validating_tasks),
            done_tasks=len(self.db.get_tasks_by_status(TaskStatus.DONE)),
        )
```

**Traces to:** [REQ-032]

---

### DESIGN-013: Spawner Registry

#### Описание
Автоматическое обнаружение и регистрация spawner-плагинов.

#### Interface
```python
class SpawnerRegistry:
    """Registry for agent spawners with auto-discovery."""

    def __init__(self) -> None:
        self._spawners: dict[str, type[AgentSpawner]] = {}
        self._discover_spawners()

    def _discover_spawners(self) -> None:
        """Auto-discover spawner classes in spawners package."""
        ...

    def register(self, spawner_class: type[AgentSpawner]) -> None:
        """Register a spawner class."""
        ...

    def get_spawner(self, agent_type: str) -> AgentSpawner:
        """Get spawner instance by agent type."""
        ...

    def list_available(self) -> list[str]:
        """List available agent types."""
        ...
```

**Traces to:** [REQ-011]

---

### DESIGN-040: Notification Manager

#### Описание
Управляет отправкой уведомлений через разные каналы.

#### Interface
```python
from abc import ABC, abstractmethod

class NotificationChannel(ABC):
    """Base class for notification channels."""

    @abstractmethod
    async def send(self, title: str, message: str, level: str = "info") -> bool:
        """Send notification. Returns True if successful."""
        ...

class DesktopNotifier(NotificationChannel):
    """Desktop notifications (macOS/Linux)."""

    async def send(self, title: str, message: str, level: str = "info") -> bool:
        ...

class NotificationManager:
    """Manages multiple notification channels."""

    def __init__(self) -> None:
        self._channels: list[NotificationChannel] = []

    def add_channel(self, channel: NotificationChannel) -> None:
        ...

    async def notify(self, title: str, message: str, level: str = "info") -> None:
        """Send notification to all channels."""
        ...
```

**Traces to:** [REQ-040]

---

### DESIGN-050: Dashboard

#### Описание
Веб-интерфейс для мониторинга с DAG визуализацией и real-time обновлениями.

#### Interface
```python
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

app = FastAPI(title="Maestro Dashboard")

@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    """Serve dashboard HTML with Mermaid.js DAG visualization."""
    ...

@app.get("/events")
async def events(db_path: str) -> StreamingResponse:
    """SSE endpoint for real-time task status updates."""
    ...

@app.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, db_path: str) -> dict:
    """Retry a failed task from the dashboard."""
    ...
```

**Traces to:** [REQ-050]

---

### DESIGN-060: Cost Tracker

#### Описание
Парсинг token usage из логов агентов и расчёт стоимости.

#### Interface
```python
@dataclass
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

@dataclass
class CostReport:
    task_id: str
    agent_type: str
    usage: TokenUsage
    estimated_cost_usd: float

class CostTracker:
    """Track token usage and costs from agent logs."""

    # Pricing per million tokens
    PRICING: dict[str, dict[str, float]] = {
        "claude_code": {"input": 3.0, "output": 15.0},
        ...
    }

    def parse_log(self, log_content: str, agent_type: str) -> TokenUsage | None:
        """Parse token usage from agent log output."""
        ...

    def calculate_cost(self, usage: TokenUsage, agent_type: str) -> float:
        """Calculate estimated cost in USD."""
        ...

    async def save_cost(self, db: Database, report: CostReport) -> None:
        """Save cost report to database."""
        ...
```

**Traces to:** —

---

## 3. Схемы данных

### 3.1 Task

```python
from pydantic import BaseModel
from datetime import datetime

class Task(BaseModel):
    id: str
    title: str
    prompt: str
    branch: str | None = None
    workdir: str
    agent_type: str = "claude_code"
    status: TaskStatus = TaskStatus.PENDING
    assigned_to: str | None = None
    scope: list[str] = []
    priority: int = 0
    max_retries: int = 2
    retry_count: int = 0
    timeout_minutes: int = 30
    requires_approval: bool = False
    validation_cmd: str | None = None
    result_summary: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
```

### 3.2 Database Schema

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    branch TEXT,
    workdir TEXT NOT NULL,
    agent_type TEXT NOT NULL DEFAULT 'claude_code',
    status TEXT NOT NULL DEFAULT 'pending',
    assigned_to TEXT,
    scope TEXT,  -- JSON array
    priority INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 2,
    retry_count INTEGER DEFAULT 0,
    timeout_minutes INTEGER DEFAULT 30,
    requires_approval BOOLEAN DEFAULT FALSE,
    validation_cmd TEXT,
    result_summary TEXT,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE task_dependencies (
    task_id TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on),
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (depends_on) REFERENCES tasks(id)
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent TEXT NOT NULL,
    to_agent TEXT,  -- NULL = broadcast
    message TEXT NOT NULL,
    read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE agent_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    event TEXT NOT NULL,
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_messages_to_agent ON messages(to_agent, read);
```

---

## 4. Ключевые решения (ADR)

### ADR-001: Git Branch per Task
**Status:** Accepted
**Date:** 2025-02-05

**Context:**
При параллельной работе нескольких агентов над одним репозиторием возникают конфликты.

**Decision:**
Каждая задача работает в своей ветке `agent/<task-id>`. После завершения — автоматический push.

**Rationale:**
- Изоляция изменений
- Простой откат
- Стандартный workflow (PR review)

**Consequences:**
- (+) Нет конфликтов при параллельной работе
- (+) История изменений прозрачна
- (-) Больше веток в репозитории
- (-) Нужен merge/rebase в конце

**Traces to:** [REQ-012]

---

### ADR-002: SQLite for State
**Status:** Accepted
**Date:** 2025-02-05

**Context:**
Нужно хранить состояние задач, логи, сообщения между агентами.

**Decision:**
Использовать SQLite как единое хранилище. WAL mode для concurrency.

**Rationale:**
- Zero-config deployment
- ACID гарантии
- Достаточная производительность для single-machine
- Готов к миграции на PostgreSQL при масштабировании

**Consequences:**
- (+) Простое развёртывание
- (+) Crash recovery из коробки
- (-) Один файл = один instance оркестратора

**Traces to:** [REQ-003], [REQ-032]

---

### ADR-003: Scope-based Conflict Prevention
**Status:** Accepted
**Date:** 2025-02-05

**Context:**
Параллельные задачи могут модифицировать одни и те же файлы.

**Decision:**
Каждая задача объявляет `scope` — список файлов/директорий. Оркестратор проверяет пересечения.

**Rationale:**
- Предотвращение лучше разрешения
- Явный контракт для агента
- Ранняя диагностика проблем

**Consequences:**
- (+) Меньше конфликтов
- (+) Агент знает границы своей работы
- (-) Требует дисциплины при описании задач
- (-) Не защищает от агента, нарушающего scope

**Traces to:** [REQ-004]

---

## 5. Directory Structure

```
maestro/
├── maestro/
│   ├── __init__.py
│   ├── cli.py                  # Typer CLI: run, status, retry, stop
│   ├── config.py               # YAML parsing, validation
│   ├── models.py               # Task, Status, pydantic models
│   ├── dag.py                  # DAG building, validation, topological sort
│   ├── database.py             # SQLite async CRUD, WAL mode
│   ├── scheduler.py            # Main asyncio loop: resolve → spawn → monitor
│   ├── git.py                  # Git operations (branch, push, rebase)
│   ├── validator.py            # Post-task validation (validation_cmd)
│   ├── retry.py                # Exponential backoff retry logic
│   ├── recovery.py             # State recovery after crash
│   ├── cost_tracker.py         # Token usage parsing, cost calculation
│   ├── spawners/
│   │   ├── __init__.py
│   │   ├── base.py             # AgentSpawner ABC
│   │   ├── registry.py         # SpawnerRegistry (auto-discovery)
│   │   ├── claude_code.py      # Claude Code headless
│   │   ├── codex.py            # Codex CLI
│   │   ├── aider.py            # Aider
│   │   └── announce.py         # Announce-only (notification)
│   ├── coordination/
│   │   ├── __init__.py
│   │   ├── mcp_server.py       # FastMCP tools
│   │   └── rest_api.py         # FastAPI endpoints
│   ├── notifications/
│   │   ├── __init__.py
│   │   ├── base.py             # NotificationChannel ABC
│   │   ├── manager.py          # NotificationManager
│   │   └── desktop.py          # Desktop notifications (macOS/Linux)
│   └── dashboard/
│       ├── __init__.py
│       ├── app.py              # FastAPI + SSE + static
│       └── static/             # HTML/JS (Mermaid.js DAG visualization)
├── tests/
│   ├── conftest.py
│   ├── test_models.py
│   ├── test_config.py
│   ├── test_database.py
│   ├── test_dag.py
│   ├── test_scheduler.py
│   ├── test_spawners.py
│   ├── test_spawner_registry.py
│   ├── test_git.py
│   ├── test_validator.py
│   ├── test_retry.py
│   ├── test_recovery.py
│   ├── test_cost_tracker.py
│   ├── test_mcp_server.py
│   ├── test_rest_api.py
│   ├── test_messages.py
│   ├── test_notifications.py
│   ├── test_dashboard.py
│   └── test_cli.py
├── spec/
│   ├── requirements.md
│   ├── design.md
│   └── tasks.md
├── examples/
├── pyproject.toml
├── CLAUDE.md
└── README.md
```
