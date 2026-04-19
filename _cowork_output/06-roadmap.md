# Maestro — Приоритизированный Roadmap

**Дата:** 2026-04-05
**Источники:** `01-architecture-map.md`, `02-dag-scheduler-analysis.md`, `03-agent-integration.md`, `04-code-quality-ops.md`, `05-architecture-improvements.md`

---

## TL;DR

1. **Первым делать Quick Wins** (jitter, flock, max_concurrent cap, config validate) — 6 пунктов за 1 день, разблокируют production-readiness и устраняют thundering herd, double-start, deadlock.
2. **Критический path**: Quick Wins → BasePollLoop extraction → унификация retry/timeout → Event System. Каждый шаг строится на предыдущем.
3. **Параллельно** можно делать: документацию (COWORK_CONTEXT split), CLI команды (diagnose, logs, clean), тесты (e2e, stress), — они не зависят от core refactoring.
4. **Arbiter/ATP интеграция** — только после формализации интерфейсов (RoutingStrategy, ValidationStrategy), иначе будет tight coupling.
5. **Distributed mode** — **не нужен** при текущем масштабе (5-10 агентов). Event system + storage abstraction — достаточная подготовка.

---

## Quick Wins (до 1 дня)

### QW-1. Добавить jitter в retry backoff

- **Описание:** Добавить ±30% random jitter в формулу `base_delay × 2^n` в `RetryManager.calculate_delay()`. Три строки кода.
- **Обоснование:** Без jitter при массовом сбое все retry приходят синхронно (thundering herd). При 10 параллельных задачах + API rate limit → каскадный отказ. Описано в `02-dag-scheduler-analysis.md` §3.3 и `05-architecture-improvements.md` §1.2.
- **Impact:** 🔴 high
- **Effort:** S
- **Зависимости:** Нет
- **Файлы:** `maestro/retry.py:31-42`
- **Риски:** Минимальные. Jitter увеличивает max delay на ~30%, что приемлемо.

---

### QW-2. Добавить flock на PID file (защита от double-start)

- **Описание:** Использовать `fcntl.flock(LOCK_EX | LOCK_NB)` при записи PID file. При уже запущенном процессе — чёткая ошибка вместо конкурентных writes в SQLite.
- **Обоснование:** Сейчас два `maestro run` могут запуститься одновременно → конкурентные writes в SQLite, двойной spawn задач. `04-code-quality-ops.md` §4.1.
- **Impact:** 🔴 high
- **Effort:** S
- **Зависимости:** Нет
- **Файлы:** `maestro/cli.py:48-103`
- **Риски:** Не работает на Windows (fcntl — POSIX only). Если Windows не в скоупе — нет рисков.

---

### QW-3. Поднять max_concurrent cap с 10 до 50-100

- **Описание:** Изменить валидацию `max_concurrent: int = Field(default=3, ge=1, le=10)` → `le=100`. Убрать искусственное ограничение.
- **Обоснование:** При 50+ задачах текущий cap = 10 становится узким горлом. `05-architecture-improvements.md` §5.1.
- **Impact:** 🔴 high
- **Effort:** S
- **Зависимости:** Нет
- **Файлы:** `maestro/models.py` (поле `max_concurrent` в `ProjectConfig` и `OrchestratorConfig`)
- **Риски:** При высоком concurrency возможен fd exhaustion (ulimit), SQLite write contention. Документировать рекомендации.

---

### QW-4. Команда `maestro config validate`

- **Описание:** Новая CLI команда: загрузить YAML через `load_config()` / `load_orchestrator_config()`, показать ошибки или "Config valid". Без фактического запуска.
- **Обоснование:** Сейчас ошибки конфигурации видны только при `maestro run`. CI/CD pipeline не может проверить конфиг заранее. `05-architecture-improvements.md` §4.1.
- **Impact:** 🔴 high
- **Effort:** S
- **Зависимости:** Нет
- **Файлы:** `maestro/cli.py` (+50 строк)
- **Риски:** Нет.

---

### QW-5. Увеличить shutdown grace period с 0.5s до 5-10s

- **Описание:** Заменить `asyncio.sleep(0.5)` между SIGTERM и SIGKILL на configurable значение (default 5s). Агенты получат время для graceful shutdown.
- **Обоснование:** 0.5s недостаточно для Claude Code (сохранение state), Aider (коммит). `03-agent-integration.md` §1.4, `04-code-quality-ops.md` §4.5.
- **Impact:** 🟡 medium
- **Effort:** S
- **Зависимости:** Нет
- **Файлы:** `maestro/scheduler.py:727,813-814`, `maestro/orchestrator.py:622-637`
- **Риски:** Увеличенный grace period = дольше ждать при shutdown. Приемлемо.

---

### QW-6. Формализовать entry points для spawners в pyproject.toml

- **Описание:** Явно зарегистрировать 4 built-in spawner'а через `[project.entry-points."maestro.spawners"]`. Делает plugin architecture explicit.
- **Обоснование:** Сейчас spawners обнаруживаются через directory scan — implicit. Entry points — стандартный Python plugin mechanism. `05-architecture-improvements.md` §1.4.
- **Impact:** 🟡 medium
- **Effort:** S
- **Зависимости:** Нет
- **Файлы:** `pyproject.toml` (+5 строк)
- **Риски:** Нет.

---

### QW-7. Добавить DEBUG logging в silent except-блоки

- **Описание:** Заменить `except OSError: pass` на `except OSError as e: logger.debug(...)` в 4 местах.
- **Обоснование:** Silent error swallowing затрудняет диагностику в production. `04-code-quality-ops.md` §1.5.
- **Impact:** 🟢 low
- **Effort:** S
- **Зависимости:** Нет
- **Файлы:** `maestro/scheduler.py:733`, `maestro/orchestrator.py:427`, `maestro/validator.py:228`, `maestro/pr_manager.py:164`
- **Риски:** Нет.

---

### QW-8. Удалить дублирующую проверку циклов из models.py

- **Описание:** Убрать `validate_no_cyclic_dependencies()` из Pydantic model_validator в `models.py`. Оставить единственную проверку в `DAG.__init__()` (алгоритм Кана).
- **Обоснование:** Два разных алгоритма проверяют одно и то же, дают разные cycle paths в ошибках → путаница. `02-dag-scheduler-analysis.md` §1.1.
- **Impact:** 🟡 medium
- **Effort:** S
- **Зависимости:** Нет
- **Файлы:** `maestro/models.py:403-441`, тесты в `tests/test_models.py`
- **Риски:** При использовании `ProjectConfig` без `DAG()` (e.g. pure config validation) цикл не обнаружится. Mitigate: `config validate` команда (QW-4) должна создавать DAG.

---

## Medium Effort (до 1 недели)

### ME-1. Извлечь `BasePollLoop` из scheduler/orchestrator

- **Описание:** Создать `maestro/loop.py` с абстрактным `BasePollLoop` (ABC): общий poll loop, signal handling, shutdown cleanup, process termination. `Scheduler` и `Orchestrator` наследуются от него.
- **Обоснование:** ~40% дублирования между scheduler.py (903 строки) и orchestrator.py (646 строк). Расхождения уже привели к багам: retry с backoff есть только в scheduler, timeout — только в scheduler. `04-code-quality-ops.md` §1.1, `05-architecture-improvements.md` §1.1.
- **Impact:** 🔴 high
- **Effort:** M
- **Зависимости:** Нет (но разблокирует ME-2, ME-3, SE-1, SE-2)
- **Файлы:** Новый `maestro/loop.py` (~200 строк), рефакторинг `maestro/scheduler.py`, `maestro/orchestrator.py`
- **Риски:** Template Method pattern может стать хрупким при расхождении режимов. Mitigate: composition over inheritance (inject strategies через конструктор).

---

### ME-2. Унификация retry logic + persistence в DB

- **Описание:** Подключить `RetryManager` к orchestrator (сейчас не используется). Добавить `next_retry_at` column в DB. При failure записывать в DB вместо in-memory dict. При `_resolve_ready()` фильтровать по `next_retry_at <= now()`.
- **Обоснование:** Orchestrator делает retry немедленно (без backoff). Retry backoff теряется при restart (in-memory). `02-dag-scheduler-analysis.md` §3.3, `04-code-quality-ops.md` §4.2, `05-architecture-improvements.md` §1.2.
- **Impact:** 🔴 high
- **Effort:** M
- **Зависимости:** ME-1 (проще делать после BasePollLoop)
- **Файлы:** `maestro/retry.py`, `maestro/database.py` (migration), `maestro/orchestrator.py`
- **Риски:** DB migration — minor, но нужно обеспечить backward compatibility.

---

### ME-3. Добавить timeout в orchestrator mode

- **Описание:** Добавить elapsed time check в `_monitor_running()` orchestrator'а аналогично scheduler. Зависший spec-runner процесс будет убит по timeout.
- **Обоснование:** Orchestrator не имеет timeout — зависший процесс работает вечно. `03-agent-integration.md` §1.4, `04-code-quality-ops.md` §6 (#18).
- **Impact:** 🔴 high
- **Effort:** S-M
- **Зависимости:** ME-1 (если делать в BasePollLoop — одна реализация для обоих режимов)
- **Файлы:** `maestro/orchestrator.py:381-397`
- **Риски:** Нет, если grace period достаточный.

---

### ME-4. Cascade failure propagation (BLOCKED status)

- **Описание:** Если зависимость в terminal-failure (NEEDS_REVIEW, retries исчерпаны), downstream задачи автоматически переходят в BLOCKED или ABANDONED. Добавить `skip_on_failure: bool` в TaskConfig.
- **Обоснование:** Сейчас scheduler зависает в бесконечном poll loop, если upstream в NEEDS_REVIEW. `02-dag-scheduler-analysis.md` §2.2, §2.4, `04-code-quality-ops.md` §4.3.
- **Impact:** 🔴 high
- **Effort:** M
- **Зависимости:** Нет
- **Файлы:** `maestro/models.py` (новый статус BLOCKED или расширение `TaskStatus`), `maestro/scheduler.py:657-785`, `maestro/dag.py`
- **Риски:** Новый статус BLOCKED требует изменения state machine, dashboard, CLI. Альтернатива: использовать ABANDONED.

---

### ME-5. Парсить result_summary из JSON логов Claude Code

- **Описание:** После успешного завершения Claude Code — парсить JSON log и извлекать `result` field как `result_summary`. Для Codex/Aider — последние N строк stdout.
- **Обоснование:** Сейчас result_summary всегда "Task completed successfully" — бесполезен для downstream задач. `cost_tracker.py` уже парсит JSON логи — расширение natural. `03-agent-integration.md` §1.3.
- **Impact:** 🔴 high
- **Effort:** M
- **Зависимости:** Нет
- **Файлы:** `maestro/scheduler.py:546-561`, `maestro/cost_tracker.py` (переиспользовать parser)
- **Риски:** Формат JSON лога Claude Code может измениться. Mitigate: robust parsing с fallback на текущее поведение.

---

### ME-6. Fix блокирующие async-вызовы

- **Описание:** Заменить sync `process.wait()` на `await loop.run_in_executor()`, sync `log_file.open("w")` на `os.open()` + `os.close()`.
- **Обоснование:** 3 блокирующих вызова в async-контексте могут заблокировать event loop. `04-code-quality-ops.md` §1.6.
- **Impact:** 🔴 high
- **Effort:** S
- **Зависимости:** Нет
- **Файлы:** `maestro/orchestrator.py:350`, `maestro/scheduler.py:731,817`
- **Риски:** Минимальные.

---

### ME-7. Orphaned worktree cleanup при startup + recovery

- **Описание:** При startup orchestrator: `git worktree prune` + reset dirty worktrees. Добавить `workspace_mgr.cleanup_stale()`. При переиспользовании worktree: `git checkout -- .` + `git clean -fd`.
- **Обоснование:** Worktrees не удаляются при crash/shutdown. При restart — dirty state или WorkspaceExistsError. `03-agent-integration.md` §3.3, `04-code-quality-ops.md` §4.4.
- **Impact:** 🔴 high
- **Effort:** M
- **Зависимости:** Нет
- **Файлы:** `maestro/workspace.py`, `maestro/orchestrator.py:284-372`, `maestro/recovery.py`
- **Риски:** `git clean -fd` удалит untracked файлы в worktree. Нужен explicit `--clean-workspaces` flag или confirmation.

---

### ME-8. Разделить COWORK_CONTEXT.md на ARCHITECTURE.md + ROADMAP.md

- **Описание:** Текущий COWORK_CONTEXT.md описывает как реализованное, так и нереализованное (Arbiter, WORKFLOW.md, ATP, agents.toml) без явного разделения. Разделить на два документа.
- **Обоснование:** Новый разработчик будет искать код, которого нет. Серьёзная проблема onboarding. `05-architecture-improvements.md` §4.2.
- **Impact:** 🔴 high
- **Effort:** S-M
- **Зависимости:** Нет
- **Файлы:** `COWORK_CONTEXT.md` → `ARCHITECTURE.md` + `ROADMAP.md`
- **Риски:** Нет.

---

### ME-9. Команда `maestro diagnose`

- **Описание:** CLI команда для диагностики: stuck tasks (RUNNING дольше timeout), orphaned worktrees, DB integrity, stale PID file, disk space.
- **Обоснование:** Сейчас диагностика только вручную: DB query + tail log + git worktree list. `05-architecture-improvements.md` §4.1, `04-code-quality-ops.md` §3.4.
- **Impact:** 🔴 high
- **Effort:** M
- **Зависимости:** Нет
- **Файлы:** `maestro/cli.py` (+100 строк)
- **Риски:** Нет.

---

### ME-10. E2e тесты с AnnounceSpawner

- **Описание:** Добавить 2-3 end-to-end теста: YAML → spawn (AnnounceSpawner) → validate → DONE. Тестировать полный lifecycle без внешних зависимостей.
- **Обоснование:** Нет e2e, stress, race condition тестов. AnnounceSpawner — идеален: instant completion, no external deps. `04-code-quality-ops.md` §2.5.
- **Impact:** 🔴 high
- **Effort:** M
- **Зависимости:** Нет
- **Файлы:** Новый `tests/test_e2e.py`
- **Риски:** Нет.

---

### ME-11. Передача structured metadata агентам через env vars

- **Описание:** При spawn агента передавать env vars: `MAESTRO_TASK_ID`, `MAESTRO_CALLBACK_URL`, `MAESTRO_TIMEOUT_MINUTES`, `MAESTRO_RETRY_COUNT`. Позволит агентам интегрироваться с координацией.
- **Обоснование:** Сейчас агент не знает свой task_id, timeout, callback URL. Вся информация — текстовый prompt. `03-agent-integration.md` §1.2.
- **Impact:** 🔴 high
- **Effort:** S-M
- **Зависимости:** Нет
- **Файлы:** `maestro/spawners/base.py`, все spawners (`claude_code.py`, `codex.py`, `aider.py`, `announce.py`)
- **Риски:** Минимальные. Env vars не влияют на поведение агентов, если они их не используют.

---

### ME-12. Унифицировать logging стиль между модулями

- **Описание:** Везде использовать `self._logger` вместо модульного `logger`. Добавить `task_id` в log records (через ContextVar или prefix). Синхронизировать event_log и standard logging.
- **Обоснование:** Разный стиль logging между модулями. event_log и standard logging не связаны. `04-code-quality-ops.md` §3.1.
- **Impact:** 🟡 medium
- **Effort:** M
- **Зависимости:** Нет
- **Файлы:** Все модули `maestro/`
- **Риски:** Механическая работа, но большой объём файлов.

---

### ME-13. JSON Schema для YAML конфигов (auto-gen из Pydantic)

- **Описание:** Сгенерировать JSON Schema из `ProjectConfig.model_json_schema()` и `OrchestratorConfig.model_json_schema()`. Сохранить в `maestro/schemas/`. Добавить ссылку в `$schema` поле YAML.
- **Обоснование:** IDE autocomplete для YAML, внешняя валидация, документация API. `05-architecture-improvements.md` §4.2.
- **Impact:** 🟡 medium
- **Effort:** S
- **Зависимости:** Нет
- **Файлы:** Новый `maestro/schemas/`, обновить `examples/`
- **Риски:** Нет.

---

### ME-14. Helper метод `_spawn_with_log()` в AgentSpawner base

- **Описание:** Извлечь дублированный fd management (os.open → Popen → os.close) из 4 spawners в базовый метод.
- **Обоснование:** Идентичный паттерн в 4 файлах. `04-code-quality-ops.md` §1.1.
- **Impact:** 🟡 medium
- **Effort:** S
- **Зависимости:** Нет
- **Файлы:** `maestro/spawners/base.py`, `claude_code.py`, `codex.py`, `aider.py`, `announce.py`
- **Риски:** Нет.

---

### ME-15. Поддержка `${VAR:-default}` в env var substitution

- **Описание:** Расширить regex в `resolve_env_vars()` для поддержки значений по умолчанию: `${VAR:-fallback}` и mandatory check `${VAR:?error}`.
- **Обоснование:** Стандартная shell-совместимая фича. `04-code-quality-ops.md` §5.2.
- **Impact:** 🟡 medium
- **Effort:** S
- **Зависимости:** Нет
- **Файлы:** `maestro/config.py:100-130`
- **Риски:** Нет.

---

## Strategic (1+ неделя)

### SE-1. Event System (`asyncio.Queue` EventBus)

- **Описание:** Гибридная модель: `asyncio.Queue` для internal events (process_completed, task_claimed, shutdown) + polling как fallback. Заменить `process.poll()` на `await process.wait()` в asyncio tasks. Интегрировать с event_log.py.
- **Обоснование:** Polling O(n) при каждой итерации. При 50+ задачах — 50+ DB queries/sec. Event-driven = O(1) реакция. Фундамент для heartbeats, MCP callbacks, webhook triggers. `02-dag-scheduler-analysis.md` §5.2, `05-architecture-improvements.md` §1.2.
- **Impact:** 🟡 medium
- **Effort:** L
- **Зависимости:** ME-1 (BasePollLoop) — обязательно
- **Файлы:** Новый `maestro/events.py` (~200 строк), рефакторинг `maestro/loop.py`
- **Риски:** Async event flow сложнее для debugging, чем линейный poll. Нужен fallback reconciliation для missed events.

---

### SE-2. RoutingStrategy + Arbiter adapter

- **Описание:** Создать `maestro/routing.py` с `RoutingStrategy` ABC, `StaticRouting` (текущее поведение), `ArbiterRouting` (MCP call к Arbiter + graceful fallback на static). Конфиг: `routing: {type: static|arbiter, url: "..."}`.
- **Обоснование:** Arbiter integration полностью отсутствует в коде (0 импортов). COWORK_CONTEXT.md описывает 22-dim feature vector, MCP, budget tracking — ничего не реализовано. RoutingStrategy — фундамент для любого динамического роутинга. `01-architecture-map.md` §7, `03-agent-integration.md` §2, `05-architecture-improvements.md` §2.
- **Impact:** 🔴 high
- **Effort:** M-L
- **Зависимости:** ME-1 (лучше после BasePollLoop). QW-6 (entry points формализуют available agents).
- **Файлы:** Новый `maestro/routing.py` (~150 строк), `maestro/scheduler.py`, `maestro/config.py`
- **Риски:** Зависит от Arbiter API spec. Без spec можно реализовать только StaticRouting + stub ArbiterRouting.

---

### SE-3. ValidationStrategy + ATP integration

- **Описание:** Рефакторить `Validator` → `CommandValidator(ValidationStrategy)`. Добавить `CompositeValidator` для chaining. Создать `ATPValidator` для валидации через ATP Platform API.
- **Обоснование:** Текущий validator — только subprocess execution. ATP Platform обещает глубокую верификацию (security, style, coverage). ValidationStrategy pattern расширяем без изменения core. `05-architecture-improvements.md` §3.
- **Impact:** 🟡 medium
- **Effort:** M-L
- **Зависимости:** ATP API spec
- **Файлы:** `maestro/validator.py` (рефакторинг), `maestro/scheduler.py`
- **Риски:** ATP в hot path — если медленный, замедляет pipeline. Нужен timeout + circuit breaker.

---

### SE-4. Agent-specific concurrency pools

- **Описание:** Вместо одного `max_concurrent` — лимиты per agent type: `concurrency: {global: 20, per_agent: {claude_code: 5, codex: 3, aider: 10}}`.
- **Обоснование:** Разные агенты имеют разные rate limits и стоимость. Нет способа сказать "max 2 Claude + 5 Aider". `02-dag-scheduler-analysis.md` §2.3, `05-architecture-improvements.md` §5.1.
- **Impact:** 🟡 medium
- **Effort:** M
- **Зависимости:** Нет
- **Файлы:** `maestro/models.py`, `maestro/scheduler.py`, `maestro/orchestrator.py`
- **Риски:** Усложнение scheduling logic. Простая реализация: per-agent counter check в `_spawn_ready()`.

---

### SE-5. WORKFLOW.md injection в промпт агента

- **Описание:** Реализовать `WorkflowLoader`: читать `.maestro/WORKFLOW.md` из workdir, инжектировать в промпт. Для Claude Code — альтернатива: записать как `.claude/CLAUDE.md` (нативная поддержка). Добавить `max_workflow_size` в конфиг.
- **Обоснование:** Описан в COWORK_CONTEXT.md, не реализован. Без WORKFLOW.md нет repository-specific agent policies. `01-architecture-map.md` §7, `03-agent-integration.md` §4.
- **Impact:** 🔴 high
- **Effort:** M
- **Зависимости:** Нет
- **Файлы:** `maestro/spawners/base.py:67-101`, новый `maestro/workflow.py` (~100 строк)
- **Риски:** Prompt size limits. WORKFLOW.md > 50KB может не вместиться. Mitigate: truncation + warning.

---

### SE-6. Prometheus метрики + /metrics endpoint

- **Описание:** Добавить `prometheus_client` с базовыми метриками: tasks_completed_total{status, agent_type}, running_tasks_count, queue_depth, task_duration_seconds (histogram). Endpoint `/metrics` в REST API.
- **Обоснование:** Нет операционных метрик. Dashboard + event_log недостаточны для production monitoring. `04-code-quality-ops.md` §3.2.
- **Impact:** 🟡 medium
- **Effort:** M
- **Зависимости:** Нет
- **Файлы:** `maestro/coordination/rest_api.py`, новый `maestro/metrics.py`, `pyproject.toml`
- **Риски:** Дополнительная зависимость (prometheus_client). Можно сделать optional.

---

### SE-7. Pre-merge conflict detection

- **Описание:** Перед PR creation: `git merge --no-commit --no-ff` dry-run между branch и main. Если конфликт — warning + не создавать PR автоматически. Использовать scope overlap warnings для превентивной проверки.
- **Обоснование:** Конфликты обнаруживаются только при merge PR в GitHub, не в Maestro. `03-agent-integration.md` §3.2.
- **Impact:** 🟡 medium
- **Effort:** M
- **Зависимости:** Нет
- **Файлы:** `maestro/pr_manager.py`, `maestro/git.py`
- **Риски:** Dry-run merge может быть медленным для больших репо. Делать optional.

---

### SE-8. Heartbeat mechanism от агентов

- **Описание:** Agent периодически сигнализирует "я жив" через MCP/REST callback. Если heartbeat пропущен > threshold — считать зависшим, kill + retry.
- **Обоснование:** Timeout-based detection не различает "агент думает" и "агент завис". Heartbeat даёт точную информацию. `02-dag-scheduler-analysis.md` §4.3.
- **Impact:** 🔴 high
- **Effort:** L
- **Зависимости:** ME-11 (env vars для callback URL), SE-1 (event system для обработки heartbeats)
- **Файлы:** `maestro/coordination/rest_api.py`, `maestro/scheduler.py`, spawners
- **Риски:** Требует изменения протокола взаимодействия с агентами. Не все агенты поддержат heartbeat.

---

### SE-9. Conditional tasks (when/skip_if)

- **Описание:** Добавить `when` / `skip_if` поле в TaskConfig. Поддержка условий: `exit_code`, `scope_changed`, `always`. Позволит пропускать задачи по условию.
- **Обоснование:** Нет skip logic. Если агент обнаружил "changes не нужны", downstream задачи всё равно запустятся. `02-dag-scheduler-analysis.md` §1.3.
- **Impact:** 🟡 medium
- **Effort:** L
- **Зависимости:** ME-4 (cascade failure — связанная фича)
- **Файлы:** `maestro/models.py`, `maestro/dag.py`, `maestro/scheduler.py`
- **Риски:** Усложнение state machine. Нужно чёткое определение: when evaluated, что является "scope changed".

---

### SE-10. Push vs Pull: определить модель координации

- **Описание:** MCP server и Scheduler — параллельные, не интегрированные миры. Определить приоритетную модель: push (scheduler спаунит) или pull (агенты claim через MCP). При push — MCP server становится read-only API. При pull — scheduler становится task queue.
- **Обоснование:** Если агент claim задачу через MCP, scheduler об этом не знает. Координация только через SQLite + optimistic locking. `03-agent-integration.md` §2.4.
- **Impact:** 🟡 medium
- **Effort:** L
- **Зависимости:** ME-1 (BasePollLoop)
- **Файлы:** `maestro/coordination/mcp_server.py`, `maestro/scheduler.py`
- **Риски:** Breaking change для MCP consumers (если они есть). Нужен анализ: кто реально использует MCP server.

---

### SE-11. DB per project (multi-project support)

- **Описание:** Вместо единого `maestro.db` — отдельная DB per project: `{project_dir}/.maestro/maestro.db`. Namespace isolation.
- **Обоснование:** При 10+ репозиториях все задачи в одной DB. Нет namespace/isolation. `05-architecture-improvements.md` §5.2.
- **Impact:** 🟡 medium
- **Effort:** M-L
- **Зависимости:** Нет
- **Файлы:** `maestro/database.py`, `maestro/cli.py`, `maestro/config.py`
- **Риски:** Migration path для existing single-DB setups. Backward compatibility.

---

## Рекомендуемый порядок (Critical Path)

### Этап 0: Quick Wins (день 1)

Все QW-* пункты можно делать **параллельно** — они независимы друг от друга.

```
QW-1 (jitter)       ─┐
QW-2 (flock)         ─┤
QW-3 (max_concurrent)─┤── Все параллельно, каждый = 1-2 часа
QW-4 (config validate)┤
QW-5 (grace period)  ─┤
QW-6 (entry points)  ─┤
QW-7 (debug logging) ─┤
QW-8 (dedup cycles)  ─┘
```

### Этап 1: Core Refactoring (дни 2-5)

**Критический path:**
```
ME-1 (BasePollLoop) ──→ ME-2 (retry unification) ──→ ME-3 (orchestrator timeout)
```

**Параллельно (не зависят от ME-1):**
```
ME-4 (cascade failure)     }
ME-5 (result_summary)      }
ME-6 (async fixes)         }── Все параллельно между собой
ME-7 (worktree cleanup)    }
ME-8 (docs split)          }
ME-9 (diagnose)            }
ME-10 (e2e тесты)          }
ME-11 (env vars)           }
```

### Этап 2: Integration (дни 6-10)

**Критический path:**
```
ME-1 → SE-1 (Event System) → SE-8 (Heartbeats)
```

**Параллельно:**
```
SE-2 (Arbiter routing)     }
SE-3 (ATP validation)      }── Параллельно, не зависят друг от друга
SE-4 (Agent pools)         }
SE-5 (WORKFLOW.md)         }
SE-6 (Prometheus)          }
```

### Этап 3: Scale & Advanced (по необходимости)

```
SE-7 (pre-merge detection) }
SE-9 (conditional tasks)   }── По мере появления потребности
SE-10 (push vs pull)       }
SE-11 (DB per project)     }
```

### Что делать ПЕРВЫМ

**ME-1 (BasePollLoop)** — это **главный разблокирующий фактор**. Из него растут:
- Унификация retry (ME-2)
- Timeout для orchestrator (ME-3)
- Event system (SE-1)
- Heartbeats (SE-8)

Без ME-1 каждое из этих улучшений нужно реализовывать дважды (scheduler + orchestrator), что удваивает усилия и создаёт новые расхождения.

**Рекомендация:** Quick Wins (день 1) → ME-1 BasePollLoop (дни 2-3) → ME-6 async fixes (день 3) → ME-2 retry (день 4) → ME-4 cascade failure (день 5).

### Что можно делать ПАРАЛЛЕЛЬНО

Следующие пункты **не имеют зависимостей** между собой и могут выполняться одновременно:

**Параллельный поток 1 (Core):** ME-1 → ME-2 → ME-3 → SE-1
**Параллельный поток 2 (DX):** ME-8 → ME-9 → SE-5 → SE-6
**Параллельный поток 3 (Reliability):** ME-4 → ME-7 → ME-10
**Параллельный поток 4 (Agent Interface):** ME-5 → ME-11 → SE-2

---

## Открытые вопросы к автору

1. **Arbiter** — актуален ли он? Если да — есть ли API spec? Это определяет приоритет SE-2.
2. **ATP** — есть ли API spec? Без него SE-3 можно начать, но не завершить.
3. **Windows support** — нужен ли? QW-2 (flock) — POSIX only.
4. **max_concurrent = 10** — осознанное ограничение (API rate limits) или произвольный лимит?
5. **Push vs Pull** — какая модель координации приоритетна? Это определяет SE-10.
6. **Multi-repo** — реальный use case? Определяет приоритет SE-11.
7. **COWORK_CONTEXT.md** — кто поддерживает? Как обновлять при изменениях кода?
