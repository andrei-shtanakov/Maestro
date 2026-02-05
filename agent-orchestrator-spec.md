# AI Agent Orchestrator — Техническое задание

**Проект:** Agent Orchestrator  
**Версия документа:** 0.1  
**Дата:** 2025-02-05  
**Автор:** Andrei / Claude  

---

## 1. Описание проекта

### 1.1 Проблема

При работе над сложными задачами разработки часто возникает потребность параллельно запускать несколько AI-кодинг-агентов (Claude Code, Codex, Aider и др.), которые работают над разными частями одного проекта. Сейчас это требует ручной координации: разработчик сам следит за зависимостями между задачами, вручную запускает агентов, проверяет результаты и решает, что запускать следующим.

### 1.2 Решение

Система-оркестратор (daemon/scheduler), которая:

- принимает описание задач в виде направленного ациклического графа (DAG) с зависимостями;
- отслеживает состояние задач и автоматически определяет, какие задачи готовы к выполнению;
- запускает подходящего AI-агента в headless-режиме или оповещает о доступной задаче;
- мониторит выполнение: таймауты, ошибки, ретраи;
- предоставляет единый интерфейс координации для всех агентов (MCP + REST API).

### 1.3 Целевые пользователи

- Разработчики, использующие AI-кодинг-агенты в повседневной работе.
- DevOps/SRE, автоматизирующие рутинные задачи обслуживания кодовой базы.
- Тимлиды, распределяющие задачи между AI-агентами и людьми.

---

## 2. User Stories

### US-1: Запуск параллельных задач
**Как** разработчик,  
**я хочу** описать набор задач с зависимостями в YAML-файле и запустить оркестратор одной командой,  
**чтобы** агенты автоматически выполняли задачи в правильном порядке без моего участия.

**Критерии приёмки:**
- Оркестратор парсит YAML с описанием задач и зависимостей.
- Задачи без зависимостей запускаются сразу (с учётом лимита параллелизма).
- Задачи с зависимостями запускаются автоматически после завершения всех предшественников.
- В терминале видно текущий статус каждой задачи.

### US-2: Мониторинг и нотификации
**Как** разработчик,  
**я хочу** получать уведомления о завершении задач, ошибках и доступных задачах,  
**чтобы** вмешиваться только когда это необходимо.

**Критерии приёмки:**
- При изменении статуса задачи отправляется уведомление (desktop notification / Telegram / лог-файл — настраивается).
- При ошибке агента оркестратор пытается перезапустить задачу (настраиваемое число ретраев).
- При таймауте задача помечается как failed, зависимые задачи блокируются.

### US-3: Координация через MCP
**Как** пользователь Claude Code,  
**я хочу** подключить координационный MCP-сервер к своей сессии,  
**чтобы** агент мог сам брать задачи, отчитываться о прогрессе и читать результаты других агентов.

**Критерии приёмки:**
- MCP-сервер предоставляет tools: `get_available_tasks`, `claim_task`, `update_status`, `get_task_result`, `post_message`, `read_messages`.
- Claude Code может подключиться через `claude mcp add`.
- Агент видит контекст из завершённых зависимостей при получении задачи.

### US-4: Поддержка разных типов агентов
**Как** разработчик,  
**я хочу** указать для каждой задачи предпочтительного агента (Claude Code, Codex, Aider),  
**чтобы** оркестратор автоматически запускал нужный инструмент.

**Критерии приёмки:**
- Поддерживаются как минимум: Claude Code (`claude --print`), Codex CLI (`codex`), Aider (`aider`).
- Для агентов без headless-режима (например, Cursor) оркестратор создаёт announce — оповещение о доступной задаче.
- Добавление нового типа агента требует написания только одного spawner-модуля.

### US-5: Валидация результатов
**Как** разработчик,  
**я хочу** чтобы после выполнения задачи оркестратор проверял результат (компиляция, тесты, линтинг),  
**чтобы** в зависимые задачи передавался только валидный код.

**Критерии приёмки:**
- Для каждой задачи можно указать validation-команду (например, `make test`, `pytest`, `npm run lint`).
- Если валидация провалилась — задача считается failed, запускается ретрай с контекстом ошибки.
- Результат валидации сохраняется в логах задачи.

### US-6: Dashboard (веб-интерфейс)
**Как** разработчик,  
**я хочу** видеть визуальное представление DAG задач с текущими статусами,  
**чтобы** быстро оценивать прогресс и находить проблемы.

**Критерии приёмки:**
- Веб-страница показывает граф задач с цветовой кодировкой статусов.
- Для каждой задачи доступен лог агента в реальном времени (tail -f).
- Можно вручную перезапустить failed-задачу из UI.

---

## 3. Функциональные требования

### 3.1 Task Engine

| ID | Требование | Приоритет |
|----|------------|-----------|
| FR-01 | Парсинг описания задач из YAML-файла | Must |
| FR-02 | Построение и валидация DAG (обнаружение циклов) | Must |
| FR-03 | Автоматический переход задач PENDING → READY при выполнении зависимостей | Must |
| FR-04 | Контроль максимального числа параллельно работающих агентов | Must |
| FR-05 | Поддержка ретраев с экспоненциальной задержкой | Should |
| FR-06 | Таймауты на уровне задачи | Must |
| FR-07 | Передача контекста (результатов зависимостей) в промпт агента | Must |
| FR-08 | Ручной запуск / остановка / ретрай задач через CLI и API | Should |
| FR-09 | Проверка пересечения scope для параллельных задач (warning при загрузке DAG) | Must |

### 3.2 Agent Spawner

| ID | Требование | Приоритет |
|----|------------|-----------|
| FR-10 | Запуск Claude Code в headless-режиме (`claude --print`) | Must |
| FR-11 | Запуск Codex CLI в auto-approve режиме | Should |
| FR-12 | Запуск Aider в non-interactive режиме | Should |
| FR-13 | Announce-режим для агентов без headless-поддержки | Should |
| FR-14 | Плагинная архитектура: добавление нового агента = один Python-модуль | Must |
| FR-15 | Захват stdout/stderr каждого агента в отдельный лог-файл | Must |
| FR-16 | Передача переменных окружения и рабочей директории агенту | Must |
| FR-17 | Git: автоматическое создание ветки перед запуском агента | Must |
| FR-18 | Git: rebase на base_branch перед запуском | Should |
| FR-19 | Git: auto-push после завершения задачи | Should |

### 3.3 Coordination API

| ID | Требование | Приоритет |
|----|------------|-----------|
| FR-20 | MCP-сервер с tools для координации (claim, status, messages) | Must |
| FR-21 | REST API (FastAPI) с теми же эндпоинтами | Must |
| FR-22 | Общая SQLite БД как backend для обоих интерфейсов | Must |
| FR-23 | Межагентные сообщения (post/read messages) | Should |
| FR-24 | Event stream (SSE) для real-time уведомлений | Could |

### 3.4 Нотификации

| ID | Требование | Приоритет |
|----|------------|-----------|
| FR-30 | Desktop notifications (notify-send) | Should |
| FR-31 | Telegram-бот уведомления | Could |
| FR-32 | Логирование событий в файл | Must |
| FR-33 | Настраиваемые хуки (webhook URL) | Could |

### 3.5 Валидация и качество

| ID | Требование | Приоритет |
|----|------------|-----------|
| FR-40 | Post-task validation commands | Should |
| FR-41 | Автоматический ретрай с контекстом ошибки при провале валидации | Should |
| FR-42 | Git-интеграция: автокоммит результатов задачи в ветку | Could |
| FR-43 | Парсинг token usage из логов агентов, запись в task_costs | Should |
| FR-44 | Суммарный cost-отчёт при завершении всех задач | Should |

---

## 4. Нефункциональные требования

| ID | Требование | Значение |
|----|------------|----------|
| NFR-01 | Язык реализации | Python 3.11+ |
| NFR-02 | Зависимости | Минимальные: FastAPI, uvicorn, FastMCP, SQLite (stdlib), pyyaml |
| NFR-03 | Платформа | Linux (основная), macOS (совместимость) |
| NFR-04 | Развёртывание | Одна команда: `pip install` + `agent-orch run tasks.yaml` |
| NFR-05 | Хранение состояния | SQLite — файловая БД, без внешних сервисов |
| NFR-06 | Восстановление после сбоя | При перезапуске оркестратор восстанавливает состояние из БД |
| NFR-07 | Тестируемость | Unit-тесты для DAG-логики, интеграционные тесты с mock-агентами |
| NFR-08 | Расширяемость | Новый тип агента = один Python-файл, реализующий интерфейс AgentSpawner |

---

## 5. Архитектура

### 5.1 Компоненты

```
agent-orchestrator/
├── agent_orch/
│   ├── __init__.py
│   ├── cli.py                  # CLI: run, status, retry, stop
│   ├── config.py               # Парсинг YAML, валидация
│   ├── models.py               # Task, Status, AgentType dataclasses
│   ├── dag.py                  # DAG: построение, валидация, topological sort
│   ├── db.py                   # SQLite: CRUD операции, миграции
│   ├── scheduler.py            # Основной цикл: resolve → spawn → monitor
│   ├── spawners/
│   │   ├── __init__.py
│   │   ├── base.py             # ABC AgentSpawner
│   │   ├── claude_code.py      # Claude Code headless
│   │   ├── codex.py            # Codex CLI
│   │   ├── aider.py            # Aider
│   │   └── announce.py         # Announce-only (для Cursor и т.д.)
│   ├── coordination/
│   │   ├── __init__.py
│   │   ├── mcp_server.py       # FastMCP coordination tools
│   │   └── rest_api.py         # FastAPI endpoints
│   ├── notifications/
│   │   ├── __init__.py
│   │   ├── desktop.py
│   │   ├── telegram.py
│   │   └── webhook.py
│   └── dashboard/
│       ├── app.py              # FastAPI + static files
│       └── static/             # HTML/JS для DAG-визуализации
├── tests/
│   ├── test_dag.py
│   ├── test_scheduler.py
│   ├── test_spawners.py
│   └── test_coordination.py
├── examples/
│   ├── simple-two-tasks.yaml
│   ├── multi-branch-refactor.yaml
│   └── mixed-agents.yaml
├── pyproject.toml
└── README.md
```

### 5.2 Схема данных (SQLite)

```sql
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    branch TEXT,                     -- auto-generated as agent/<id> if empty
    workdir TEXT NOT NULL,
    agent_type TEXT NOT NULL DEFAULT 'claude_code',
    status TEXT NOT NULL DEFAULT 'pending',
    assigned_to TEXT,               -- agent instance id
    scope TEXT,                     -- JSON array of file/dir globs ["src/auth/*", "tests/auth/*"]
    priority INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 2,
    retry_count INTEGER DEFAULT 0,
    timeout_minutes INTEGER DEFAULT 30,
    requires_approval BOOLEAN DEFAULT FALSE,
    validation_cmd TEXT,            -- post-task validation command
    result_summary TEXT,            -- краткий итог от агента
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
    to_agent TEXT,                   -- NULL = broadcast
    message TEXT NOT NULL,
    read BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE agent_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    event TEXT NOT NULL,             -- started, progress, completed, failed, timeout
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE task_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0.0,
    attempt INTEGER DEFAULT 1,       -- номер попытки (для ретраев)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

-- Индексы для частых запросов
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_messages_to_agent ON messages(to_agent, read);
CREATE INDEX idx_agent_logs_task ON agent_logs(task_id);
```

### 5.3 Формат задач (YAML)

```yaml
# tasks.yaml
project: feature-auth-jwt
repo: /home/user/projects/myapp
max_concurrent: 3

defaults:
  timeout_minutes: 30
  max_retries: 2
  agent_type: claude_code

git:
  auto_branch: true                 # auto-create agent/<task-id> branches
  base_branch: main                 # rebase onto this before starting
  integration_branch: integrate/auth-jwt  # optional, for related tasks
  auto_push: true                   # push after task completion

notifications:
  desktop: true
  telegram:
    enabled: false
    token: "${TELEGRAM_BOT_TOKEN}"
    chat_id: "${TELEGRAM_CHAT_ID}"

tasks:
  - id: prepare-interfaces
    title: "Extract auth interfaces (contract-first)"
    prompt: |
      Extract auth interfaces to prepare for parallel work:
      1. Create src/auth/interfaces.py with AuthProvider ABC
      2. Create src/auth/tokens.py (empty, interface only)
      3. Ensure existing code still works after refactoring.
    scope: ["src/auth/interfaces.py", "src/auth/tokens.py", "src/auth/__init__.py"]
    validation_cmd: "python -m pytest tests/auth/ -x"

  - id: refactor-auth
    title: "Implement JWT auth provider"
    prompt: |
      Implement JWTAuthProvider following the AuthProvider interface.
      Key files: src/auth/tokens.py, src/auth/jwt_provider.py
    scope: ["src/auth/tokens.py", "src/auth/jwt_provider.py", "src/auth/config.py"]
    depends_on: [prepare-interfaces]
    validation_cmd: "python -m pytest tests/auth/ -x"

  - id: update-api-tests
    title: "Update API integration tests"
    prompt: |
      Update all API integration tests to work with the new JWT auth.
      Use the result summary from the auth refactoring task for context.
    scope: ["tests/api/*", "tests/fixtures/*"]
    depends_on: [refactor-auth]
    validation_cmd: "python -m pytest tests/api/ -x"

  - id: update-docs
    title: "Update API documentation"
    prompt: |
      Update API documentation in docs/ to reflect JWT auth changes.
      Include migration guide for existing API clients.
    scope: ["docs/*"]
    depends_on: [refactor-auth]
    agent_type: codex               # эту задачу отдаём Codex

  - id: final-integration
    title: "Final integration test"
    prompt: |
      Run the full test suite. Fix any remaining failures.
      Ensure all changes across auth, API tests, and docs are consistent.
    depends_on: [update-api-tests, update-docs]
    requires_approval: true         # ручное подтверждение перед финальным этапом
    validation_cmd: "python -m pytest --tb=short"
```

### 5.4 State Machine задачи

```
                  ┌──────────────────────────────────────┐
                  │                                      │
                  ▼                                      │
PENDING ──→ READY ──→ RUNNING ──→ VALIDATING ──→ DONE   │
               │        │            │                   │
               │        │            │ validation failed │
               │        │            ▼                   │
               │        └──────→ FAILED ──→ (retry?) ───┘
               │                   │
               │                   │ max retries exceeded
               │                   ▼
               │              NEEDS_REVIEW ──→ (manual) ──→ READY
               │                   │
               │                   │ abandoned by user
               │                   ▼
               │              ABANDONED
               │
               │ requires_approval=true
               ▼
          AWAITING_APPROVAL ──→ (approved) ──→ RUNNING
```

Статусы:
- **PENDING** — ждёт завершения зависимостей.
- **READY** — все зависимости выполнены, задача готова к запуску.
- **AWAITING_APPROVAL** — задача готова, но требует ручного подтверждения (`requires_approval: true`).
- **RUNNING** — агент запущен и работает.
- **VALIDATING** — агент завершил работу, выполняется `validation_cmd`.
- **DONE** — задача успешно завершена и провалидирована.
- **FAILED** — ошибка выполнения или валидации (может быть ретрай).
- **NEEDS_REVIEW** — ретраи исчерпаны, требуется ручное вмешательство.
- **ABANDONED** — пользователь решил не продолжать задачу.

---

## 6. Этапы разработки

### Этап 1: Ядро (MVP) — ~3-4 дня

**Цель:** Минимально работающий оркестратор, который парсит YAML, строит DAG и запускает Claude Code в headless-режиме.

**Задачи:**
1. `models.py` — dataclasses Task, Status, AgentType
2. `config.py` — парсинг YAML, валидация схемы
3. `dag.py` — построение графа, topological sort, обнаружение циклов
4. `db.py` — SQLite схема, CRUD, миграции
5. `scheduler.py` — основной цикл (resolve → spawn → monitor)
6. `spawners/claude_code.py` — запуск `claude --print`
7. `cli.py` — команда `agent-orch run tasks.yaml`

**Промежуточный результат:**
```bash
agent-orch run examples/simple-two-tasks.yaml
# Оркестратор запускает Task 1, ждёт завершения, запускает Task 2
# Логи в /var/log/agent-orch/
```

**Тесты:**
- Unit: DAG-валидация (циклы, missing deps), state transitions.
- Integration: mock-агент (echo "done"), проверка последовательности выполнения.

---

### Этап 2: Coordination API — ~2-3 дня

**Цель:** MCP-сервер и REST API, позволяющие агентам самостоятельно координироваться.

**Задачи:**
1. `coordination/mcp_server.py` — FastMCP tools:
   - `get_available_tasks(agent_id)` → список READY задач
   - `claim_task(agent_id, task_id)` → атомарный claim с проверкой конфликтов
   - `update_status(agent_id, task_id, status, result_summary)` → обновление статуса
   - `get_task_result(task_id)` → результат завершённой задачи
   - `post_message(from_agent, to_agent, message)` → межагентное сообщение
   - `read_messages(agent_id)` → чтение входящих сообщений
2. `coordination/rest_api.py` — те же эндпоинты через FastAPI
3. Запуск MCP и REST как часть оркестратора (или standalone)

**Промежуточный результат:**
```bash
# Терминал 1: оркестратор + API
agent-orch run tasks.yaml --api-port 8080 --mcp

# Терминал 2: Claude Code подключается к MCP
claude mcp add coordination -- agent-orch mcp-serve

# Или через curl:
curl localhost:8080/tasks/available
curl -X POST localhost:8080/tasks/refactor-auth/claim -d '{"agent_id": "claude-1"}'
```

**Тесты:**
- MCP tools: claim conflict (два агента берут одну задачу).
- REST API: CRUD, concurrency, error handling.

---

### Этап 3: Мульти-агенты и нотификации — ~2-3 дня

**Цель:** Поддержка разных типов агентов, система нотификаций.

**Задачи:**
1. `spawners/base.py` — ABC AgentSpawner с интерфейсом:
   ```python
   class AgentSpawner(ABC):
       @abstractmethod
       def spawn(self, task: Task, context: str) -> subprocess.Popen | None: ...
       @abstractmethod
       def is_available(self) -> bool: ...  # проверка, установлен ли агент
   ```
2. `spawners/codex.py` — Codex CLI spawner
3. `spawners/aider.py` — Aider spawner
4. `spawners/announce.py` — announce-only для неавтоматизируемых агентов
5. `notifications/desktop.py` — notify-send
6. `notifications/telegram.py` — Telegram bot API
7. `notifications/webhook.py` — generic webhook
8. Конфигурация нотификаций в YAML

**Промежуточный результат:**
```yaml
# tasks.yaml с разными агентами
tasks:
  - id: task-1
    agent_type: claude_code   # автозапуск
  - id: task-2
    agent_type: codex          # автозапуск другим агентом
  - id: task-3
    agent_type: manual         # только оповещение
```

**Тесты:**
- Spawner registry: авто-определение доступных агентов.
- Fallback: если предпочтительный агент недоступен.
- Notification delivery: mock-тесты для каждого канала.

---

### Этап 4: Валидация, ретраи, устойчивость — ~2 дня

**Цель:** Production-ready поведение: валидация результатов, умные ретраи, восстановление после сбоев.

**Задачи:**
1. Post-task validation: запуск `validation_cmd`, анализ exit code
2. Retry с контекстом ошибки: при ретрае в промпт добавляется предыдущая ошибка
3. Экспоненциальная задержка между ретраями
4. Graceful shutdown: SIGTERM → дождаться текущих задач → сохранить состояние
5. Recovery: при перезапуске оркестратор восстанавливает состояние, RUNNING задачи → READY

**Промежуточный результат:**
```bash
# Задача с валидацией
agent-orch run tasks.yaml
# Task 1 завершается, pytest fails → retry с контекстом → pytest passes → DONE

# Аварийный перезапуск
kill -TERM $(pidof agent-orch)
agent-orch run tasks.yaml --resume
# Восстанавливает состояние, продолжает с прерванного места
```

**Тесты:**
- Validation failure → retry с правильным контекстом.
- Max retries exceeded → ABANDONED, зависимые задачи блокируются.
- Kill + resume: состояние корректно восстанавливается.

---

### Этап 5: Dashboard и UX — ~2-3 дня

**Цель:** Веб-интерфейс для мониторинга и управления.

**Задачи:**
1. `dashboard/app.py` — FastAPI + статика
2. DAG-визуализация (Mermaid или D3.js) с цветовой кодировкой статусов
3. Real-time обновление через SSE (Server-Sent Events)
4. Просмотр логов агентов в реальном времени
5. Кнопки управления: retry, cancel, force-complete
6. CLI-улучшения: `agent-orch status`, `agent-orch retry <task-id>`, `agent-orch logs <task-id>`

**Промежуточный результат:**
```bash
agent-orch run tasks.yaml --dashboard
# Открывается http://localhost:8080/dashboard
# Видно граф задач, статусы обновляются в реальном времени
```

---

### Этап 6: Graph-RAG интеграция и расширения — ~2-3 дня

**Цель:** Интеграция с уже разработанными инструментами (Graph-RAG MCP, Docling) для обогащения контекста агентов.

**Задачи:**
1. Интеграция с Graph-RAG: результаты задач сохраняются как документы в графе знаний
2. Агенты могут запрашивать контекст из графа через MCP tool `query_project_knowledge`
3. Docling: автоматическое конвертирование и индексация проектной документации
4. Генерация финального отчёта о выполненной работе (Docling → DOCX/PDF)
5. Template-система для промптов: переменные, условия, подстановка контекста

**Промежуточный результат:**
```yaml
# tasks.yaml с Graph-RAG
context_sources:
  - type: graph_rag
    mcp_server: "graph-rag-server"
  - type: docling
    documents: ["docs/architecture.pdf", "docs/api-spec.md"]

tasks:
  - id: implement-feature
    prompt: |
      Implement the feature described in the architecture doc.
      Query the knowledge graph for related decisions and constraints.
```

---

## 7. Инструменты и технологии

| Компонент | Технология | Обоснование |
|-----------|-----------|-------------|
| Язык | Python 3.11+ | Экосистема FastMCP, знакомый стек |
| Task engine | asyncio + subprocess | Нативный параллелизм, управление процессами |
| Хранение | SQLite | Zero-config, файловая БД, ACID |
| MCP | FastMCP | Нативная интеграция с Claude Code |
| REST API | FastAPI + uvicorn | Async, автодокументация OpenAPI |
| Dashboard | FastAPI + vanilla JS + Mermaid | Минимум зависимостей |
| Конфигурация | YAML (PyYAML + jsonschema) | Читаемость, валидация |
| CLI | click или typer | Удобный CLI с подкомандами |
| Тесты | pytest + pytest-asyncio | Стандарт для Python |
| Пакетирование | pyproject.toml + hatch/pip | Современный Python packaging |

---

## 8. Риски и допущения

### Риски

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Headless-режим агента работает нестабильно | Средняя | Высокое | Таймауты, ретраи, fallback на announce |
| Агент производит невалидный код | Высокая | Среднее | Post-validation, ретрай с контекстом ошибки |
| Race condition при параллельном claim задач | Низкая | Среднее | SQLite WAL mode, атомарные UPDATE с WHERE |
| Промпт-контекст слишком длинный | Средняя | Среднее | Суммаризация результатов зависимостей, лимит токенов |

### Допущения

- Claude Code поддерживает headless-режим через `claude --print` и возвращает exit code.
- Codex CLI и Aider поддерживают non-interactive режимы.
- Все агенты работают с одним и тем же git-репозиторием (возможно, разные ветки).
- Сеть не требуется для координации (всё локально, SQLite).

---

## 9. Метрики успеха

| Метрика | Целевое значение |
|---------|-----------------|
| Время от описания задач до начала работы первого агента | < 10 секунд |
| Процент задач, завершённых без ручного вмешательства | > 70% |
| Время добавления нового типа агента | < 1 час (один Python-файл) |
| Overhead оркестратора (CPU/RAM) | < 1% CPU, < 50MB RAM |
| Восстановление после аварийного перезапуска | < 5 секунд, 0 потерянных данных |

---

## 10. Принятые решения (бывшие открытые вопросы)

### 10.1 Git-стратегия

**Решение:** Отдельная ветка на каждую задачу + интеграционная ветка для связанных задач.

Схема веток:
```
main/master (защищённая, только через CI)
│
├── agent/<task-id>-<short>     ← короткоживущая ветка агента
├── agent/<task-id>-<short>     ← короткоживущая ветка агента
│
└── integrate/<feature>         ← если задачи связаны
    ├── ← merge из agent/* после CI
    └── → fast-forward в main когда всё зелёное
```

Правила:
- Каждый агент работает в своей ветке `agent/<task-id>-<short>`.
- Агент регулярно делает rebase на актуальный `main` (или на `integrate/<feature>` для связанных задач).
- Слияние: `--no-ff` merge / PR + обязательный CI.
- Оркестратор создаёт ветки автоматически перед запуском агента.

**Влияние на архитектуру:**
- Поле `branch` в задаче генерируется автоматически из `task.id`, если не указано явно.
- Оркестратор выполняет `git checkout -b agent/<task-id>` перед запуском спаунера.
- После завершения задачи — автоматический `git push` + опциональное создание PR.

### 10.2 Конфликты кода

**Решение:** Предотвращение важнее разрешения. Три уровня защиты:

**Уровень 1: Разрез по владению файлами/модулями**
- В описании задачи указывается `scope` — список файлов/директорий, которые задача может модифицировать.
- Оркестратор проверяет пересечения scope при загрузке DAG.
- Пересечение → предупреждение + предложение добавить зависимость или разделить задачу.

```yaml
tasks:
  - id: refactor-auth
    scope: ["src/auth/*", "src/middleware/auth.py"]
  - id: update-api
    scope: ["src/api/*", "tests/api/*"]
    # scope не пересекается → можно параллельно
```

**Уровень 2: Контракт-first**
- Для связанных задач первой задачей в DAG идёт «подготовка»: выделение интерфейса, добавление абстракции, разнесение кода по файлам.
- Последующие задачи работают по разным файлам, опираясь на контракт.

**Уровень 3: Feature flags / adapter**
- Один агент добавляет новый путь рядом со старым (флаг/стратегия).
- Другой агент мигрирует вызовы.
- Конфликтов меньше, откат проще.

**Принцип:** Не пытаться «смёржить всё в конце». Интеграция — часто и рано.

**Влияние на архитектуру:**
- Новое поле `scope: list[str]` в модели задачи.
- `dag.py` — проверка пересечения scope для параллельных задач (warning/error).
- В промпт агента добавляется: «Ты можешь модифицировать только файлы из scope: ...»

### 10.3 Трекинг стоимости

**Решение:** Да, желательно. Реализация:
- Парсинг логов агента для извлечения информации о токенах (Claude Code выводит usage в `--output-format json`).
- Сохранение в таблицу `task_costs` (input_tokens, output_tokens, estimated_cost).
- Суммарный отчёт при завершении всех задач.
- Приоритет: Should (этап 4-5).

### 10.4 Человек в петле

**Решение:** По умолчанию — нет. Manual approval только если работа остановилась (failed задача после исчерпания ретраев).

Реализация:
- Статус `NEEDS_REVIEW` — задача остановлена, ждёт ручного решения.
- Нотификация + команда `agent-orch approve <task-id>` / `agent-orch retry <task-id>`.
- Опциональный флаг `requires_approval: true` в описании задачи для критичных задач.

### 10.5 Масштаб: мультимашинность

**Решение:** Конечная цель — работа на нескольких машинах. Но не первоначальный этап.

Стратегия подготовки (чтобы не переписывать позже):
- Этапы 1-5: SQLite, всё локально. Но API — через REST (а не через прямой доступ к БД).
- Этап будущий: замена SQLite на PostgreSQL/Redis, добавление worker-нод.
- Архитектурное правило: все компоненты общаются через API, а не через файловую систему.

**Влияние на архитектуру:**
- Spawner не обращается к БД напрямую — только через coordination API.
- Логи агентов хранятся по ID задачи (готово к S3/shared storage в будущем).
- MCP-сервер и REST API stateless относительно процессов (вся информация в БД).
