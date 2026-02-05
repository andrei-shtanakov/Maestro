# Requirements Specification

> Maestro — AI Agent Orchestrator for parallel coding agent coordination

## 1. Контекст и цели

### 1.1 Проблема

При работе над сложными задачами разработки часто возникает потребность параллельно запускать несколько AI-кодинг-агентов (Claude Code, Codex, Aider и др.), которые работают над разными частями одного проекта. Сейчас это требует ручной координации: разработчик сам следит за зависимостями между задачами, вручную запускает агентов, проверяет результаты и решает, что запускать следующим.

### 1.2 Цели проекта

| ID | Цель | Метрика успеха |
|----|------|----------------|
| G-1 | Автоматизировать координацию AI-агентов | > 70% задач завершаются без ручного вмешательства |
| G-2 | Сократить время от описания задач до начала работы | < 10 секунд от запуска до первого агента |
| G-3 | Обеспечить простое добавление новых агентов | < 1 часа на интеграцию нового типа агента |
| G-4 | Минимизировать overhead оркестратора | < 1% CPU, < 50MB RAM |

### 1.3 Стейкхолдеры

| Роль | Интересы | Влияние |
|------|----------|---------|
| Разработчик | Автоматизация рутинных задач, параллельное выполнение | Высокое |
| DevOps/SRE | Автоматизация обслуживания кодовой базы | Среднее |
| Тимлид | Распределение задач между AI-агентами и людьми | Среднее |

### 1.4 Out of Scope

> ⚠️ Явно НЕ входит в проект

- ❌ Мультимашинная координация (только локальная работа в MVP)
- ❌ Собственные AI модели (используем существующие CLI агентов)
- ❌ Auto-merge в production без CI
- ❌ Разрешение git конфликтов (только предотвращение через scope)
- ❌ Кастомные UI для каждого агента

---

## 2. Функциональные требования

### 2.1 Task Engine

#### REQ-001: YAML Task Definition
**As a** разработчик
**I want** описать задачи и зависимости в YAML-файле
**So that** оркестратор автоматически выполнял их в правильном порядке

**Acceptance Criteria:**
```gherkin
GIVEN yaml-файл с описанием задач и depends_on
WHEN я запускаю оркестратор командой `maestro run tasks.yaml`
THEN парсер валидирует схему файла
AND строит DAG из зависимостей
AND выдаёт ошибку при циклических зависимостях
```

**Priority:** P0
**Traces to:** [TASK-001], [DESIGN-001]

---

#### REQ-002: DAG Execution
**As a** разработчик
**I want** чтобы задачи без зависимостей запускались параллельно
**So that** общее время выполнения было минимальным

**Acceptance Criteria:**
```gherkin
GIVEN DAG с 3 независимыми задачами и max_concurrent: 3
WHEN все задачи в статусе READY
THEN оркестратор запускает все 3 задачи одновременно
AND каждая задача получает отдельный процесс
```

**Priority:** P0
**Traces to:** [TASK-002], [DESIGN-002]

---

#### REQ-003: Task State Management
**As a** разработчик
**I want** видеть текущий статус каждой задачи
**So that** понимать прогресс выполнения

**Acceptance Criteria:**
```gherkin
GIVEN запущенный оркестратор с задачами
WHEN задача переходит между статусами (PENDING → READY → RUNNING → DONE)
THEN статус сохраняется в SQLite
AND доступен через CLI `maestro status`
AND доступен через REST API GET /tasks
```

**Priority:** P0
**Traces to:** [TASK-003], [DESIGN-003]

---

#### REQ-004: Scope Overlap Detection
**As a** разработчик
**I want** получать предупреждение при пересечении scope параллельных задач
**So that** избежать git-конфликтов

**Acceptance Criteria:**
```gherkin
GIVEN две параллельные задачи с scope ["src/auth/*"]
WHEN оркестратор загружает DAG
THEN выдаётся warning о пересечении
AND предлагается добавить зависимость или разделить задачу
```

**Priority:** P1
**Traces to:** [TASK-004], [DESIGN-001]

---

### 2.2 Agent Spawner

#### REQ-010: Claude Code Spawner
**As a** разработчик
**I want** автоматический запуск Claude Code в headless-режиме
**So that** задачи выполнялись без моего участия

**Acceptance Criteria:**
```gherkin
GIVEN задача с agent_type: claude_code
WHEN задача переходит в RUNNING
THEN запускается `claude --print --output-format json`
AND stdout/stderr захватывается в лог-файл
AND exit code определяет успех/неудачу
```

**Priority:** P0
**Traces to:** [TASK-010], [DESIGN-010]

---

#### REQ-011: Plugin Architecture
**As a** разработчик
**I want** добавлять новых агентов одним Python-модулем
**So that** легко расширять систему

**Acceptance Criteria:**
```gherkin
GIVEN новый spawner-класс наследующий AgentSpawner ABC
WHEN он реализует методы spawn() и is_available()
THEN оркестратор автоматически обнаруживает нового агента
AND можно использовать его в agent_type
```

**Priority:** P0
**Traces to:** [TASK-011], [DESIGN-011]

---

#### REQ-012: Git Branch Management
**As a** разработчик
**I want** автоматическое создание ветки перед запуском агента
**So that** изменения изолированы от main

**Acceptance Criteria:**
```gherkin
GIVEN задача без явного branch
WHEN оркестратор запускает агента
THEN создаётся ветка agent/<task-id>
AND агент работает в этой ветке
AND после завершения выполняется git push
```

**Priority:** P0
**Traces to:** [TASK-012], [DESIGN-012]

---

### 2.3 Coordination API

#### REQ-020: MCP Server
**As a** пользователь Claude Code
**I want** подключить MCP-сервер для координации
**So that** агент сам брал задачи и отчитывался

**Acceptance Criteria:**
```gherkin
GIVEN запущенный MCP-сервер
WHEN Claude Code подключается через `claude mcp add`
THEN доступны tools: get_available_tasks, claim_task, update_status
AND агент может атомарно claim задачу
AND конфликты при параллельном claim обрабатываются корректно
```

**Priority:** P0
**Traces to:** [TASK-020], [DESIGN-020]

---

#### REQ-021: REST API
**As a** разработчик
**I want** REST API с теми же эндпоинтами что и MCP
**So that** интегрироваться с другими инструментами

**Acceptance Criteria:**
```gherkin
GIVEN запущенный FastAPI сервер на порту 8080
WHEN я делаю GET /tasks/available
THEN получаю список READY задач в JSON
AND POST /tasks/{id}/claim выполняет атомарный claim
AND автодокументация доступна на /docs
```

**Priority:** P1
**Traces to:** [TASK-021], [DESIGN-021]

---

### 2.4 Validation & Recovery

#### REQ-030: Post-Task Validation
**As a** разработчик
**I want** автоматическую проверку результатов агента
**So that** в зависимые задачи передавался только валидный код

**Acceptance Criteria:**
```gherkin
GIVEN задача с validation_cmd: "pytest tests/"
WHEN агент завершает работу
THEN оркестратор выполняет validation_cmd
AND при провале задача переходит в FAILED
AND при успехе — в DONE
```

**Priority:** P1
**Traces to:** [TASK-030], [DESIGN-030]

---

#### REQ-031: Retry with Context
**As a** разработчик
**I want** автоматический retry при неудаче с контекстом ошибки
**So that** агент мог исправить проблему

**Acceptance Criteria:**
```gherkin
GIVEN задача с max_retries: 2 и провалившейся валидацией
WHEN retry_count < max_retries
THEN задача перезапускается
AND в промпт добавляется предыдущая ошибка
AND задержка между ретраями экспоненциальная
```

**Priority:** P1
**Traces to:** [TASK-031], [DESIGN-031]

---

#### REQ-032: State Recovery
**As a** разработчик
**I want** восстановление состояния после аварийного перезапуска
**So that** не потерять прогресс

**Acceptance Criteria:**
```gherkin
GIVEN оркестратор был убит (SIGKILL)
WHEN я запускаю `maestro run tasks.yaml --resume`
THEN состояние восстанавливается из SQLite
AND RUNNING задачи переходят в READY для перезапуска
AND DONE задачи не перезапускаются
```

**Priority:** P1
**Traces to:** [TASK-032], [DESIGN-032]

---

### 2.5 Notifications

#### REQ-040: Desktop Notifications
**As a** разработчик
**I want** получать desktop-уведомления о событиях
**So that** вмешиваться только когда необходимо

**Acceptance Criteria:**
```gherkin
GIVEN notifications.desktop: true в конфиге
WHEN задача завершается или требует внимания
THEN отправляется desktop notification
AND содержит task_id и краткий статус
```

**Priority:** P2
**Traces to:** [TASK-040], [DESIGN-040]

---

### 2.6 Dashboard

#### REQ-050: Web Dashboard
**As a** разработчик
**I want** видеть визуальное представление DAG
**So that** быстро оценивать прогресс

**Acceptance Criteria:**
```gherkin
GIVEN запущен --dashboard флаг
WHEN открываю http://localhost:8080/dashboard
THEN вижу граф задач с цветовой кодировкой статусов
AND статусы обновляются в реальном времени (SSE)
AND можно перезапустить failed-задачу из UI
```

**Priority:** P2
**Traces to:** [TASK-050], [DESIGN-050]

---

## 3. Нефункциональные требования

### NFR-000: Testing Requirements
| Аспект | Требование |
|--------|------------|
| Unit test coverage | ≥ 80% для core modules |
| Integration tests | DAG execution, spawner lifecycle, API endpoints |
| Test framework | pytest + pytest-asyncio |
| CI requirement | Все тесты проходят перед merge |

**Definition of Done для любой задачи:**
- [ ] Unit tests написаны и проходят
- [ ] Coverage не упал
- [ ] Integration test если затронуты интерфейсы
- [ ] Type hints для всех публичных API

**Traces to:** [TASK-100]

---

### NFR-001: Performance
| Метрика | Требование |
|---------|------------|
| Startup time | < 10 секунд до запуска первого агента |
| CPU overhead | < 1% в idle |
| Memory overhead | < 50MB RAM |
| Recovery time | < 5 секунд после аварии |

**Traces to:** [TASK-002]

---

### NFR-002: Reliability
| Аспект | Требование |
|--------|------------|
| Data persistence | SQLite WAL mode для ACID |
| Crash recovery | Восстановление из БД без потери данных |
| Graceful shutdown | SIGTERM ждёт текущих задач |

**Traces to:** [TASK-032]

---

### NFR-003: Extensibility
| Метрика | Требование |
|---------|------------|
| New agent integration | < 1 час (один Python-файл) |
| New notification channel | < 30 минут |
| API backwards compatibility | Версионирование через /v1/ prefix |

**Traces to:** [TASK-011]

---

## 4. Ограничения и техстек

### 4.1 Технологические ограничения

| Аспект | Решение | Обоснование |
|--------|---------|-------------|
| Язык | Python 3.12+ | Экосистема FastMCP, asyncio |
| База данных | SQLite | Zero-config, файловая БД, ACID |
| Package manager | uv | Быстрый, reproducible builds |
| Type checker | pyrefly | Статическая типизация |

### 4.2 Интеграционные ограничения

- Claude Code должен поддерживать headless-режим (`claude --print`)
- Все агенты работают с одним git-репозиторием
- Сеть не требуется для локальной координации

### 4.3 Бизнес-ограничения

- Бюджет: Open source / personal project
- Сроки: MVP за 2 недели
- Команда: Solo developer

---

## 5. Критерии приёмки

### Milestone 1: MVP
- [ ] REQ-001 — YAML parsing и DAG validation
- [ ] REQ-002 — Параллельное выполнение задач
- [ ] REQ-003 — State management в SQLite
- [ ] REQ-010 — Claude Code spawner
- [ ] REQ-012 — Git branch management
- [ ] NFR-000 — Test infrastructure

### Milestone 2: Coordination
- [ ] REQ-020 — MCP Server
- [ ] REQ-021 — REST API
- [ ] REQ-030 — Post-task validation
- [ ] REQ-031 — Retry with context

### Milestone 3: Production Ready
- [ ] REQ-004 — Scope overlap detection
- [ ] REQ-032 — State recovery
- [ ] REQ-040 — Desktop notifications
- [ ] REQ-050 — Web dashboard
