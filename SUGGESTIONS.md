# Предложения по доработке проекта Maestro

## 1. Текущие узкие места

### 1.1. Scope overlap не блокирует — только предупреждает (CRITICAL)

`dag.py:check_scope_overlaps()` и `decomposer.py:validate_non_overlap()` обнаруживают конфликты, но **только логируют warning**. Параллельные задачи с пересекающимися scope могут модифицировать одни и те же файлы.

- `decomposer.py:381-405` — warnings логируются, execution не блокируется
- Нет авто-добавления зависимостей для serialization конфликтных задач
- False positives: `src/api/**` vs `src/` помечаются как overlap из-за общего base dir

### 1.2. Recovery слишком наивный (HIGH)

- `recovery.py:106-167` — все задачи в `RUNNING` считаются orphaned без проверки процесса
- Может перезапустить уже выполненную работу (process finished, DB не обновлена)
- Нет проверки через `ps` / process object / PID+start_time

### 1.3. Worktree cleanup при crash отсутствует (HIGH)

- При SIGKILL orchestrator'а worktrees остаются на диске
- Нет shutdown handler / cleanup DB flag
- Нет namespace isolation — два orchestrator'а могут конфликтовать в workspace_base

### 1.4. Cost tracking с hardcoded ценами (MEDIUM)

- `cost_tracker.py:26-31` — цены зашиты в код ($3/$15 per MTok для Claude)
- Нет поддержки разных моделей (Sonnet vs Opus vs Haiku)
- Нет config override для pricing
- Token parsing хрупкий — ищет `input_tokens` / `output_tokens` в stdout

### 1.5. Polling-based мониторинг (MEDIUM)

- Фиксированный 2-секундный интервал (`scheduler.py:179`)
- Каждый цикл: полный JSON re-parse `.executor-state.json` для каждой zadacha
- Нет adaptive polling, нет delta updates
- При 100 задач: 50 file reads/sec

### 1.6. Нет backpressure (MEDIUM)

- Ready queue растёт неограниченно в DB
- Нет rejection / overflow detection
- Scope overlap check O(V² × P²) на каждом создании DAG

---

## 2. Заимствования из других проектов монорепы

### 2.1. Invariant-based scope enforcement из arbiter

- **Проблема:** scope overlaps только логируются, не блокируются
- **Взять из arbiter:**
  - Паттерн invariant rules: набор проверок, каждая возвращает pass/fail/reason
  - При fail — автоматическое действие (serialization через добавление dependency)
  - Check на каждом spawn, не только при создании DAG
- **Реализация:**
  ```python
  def enforce_scope_isolation(task: Task, running_tasks: list[RunningTask]) -> InvariantResult:
      for running in running_tasks:
          if patterns_overlap(task.scope, running.task.scope):
              return InvariantResult(passed=False, reason=f"Scope conflict with {running.task.id}")
      return InvariantResult(passed=True)
  ```
- **Объём:** ~60 строк в `scope_enforcement.py`
- **Не брать:** Decision Tree / ML inference — для Maestro достаточно rule-based

### 2.2. Process identity tracking из nullclaw

- **Проблема:** PID в DB может быть stale (process reused)
- **Взять из nullclaw:**
  - daemon_state.json паттерн: (PID, start_time, component_name) tuple
  - Health check: `os.kill(pid, 0)` + start_time verification
  - Periodic flush (каждые 5 сек)
- **Реализация:** расширить `running_tasks` в DB колонками `process_start_time`, `last_heartbeat`
- **Объём:** ~40 строк в database.py + recovery.py
- **Не брать:** daemon lifecycle management целиком — Maestro не daemon

### 2.3. Reflexion loop из hive → Retry intelligence

- **Проблема:** retry = перезапуск с тем же промптом (или предыдущей ошибкой)
- **Взять из hive:**
  - 4 вердикта после failure: RETRY (тот же промпт) / REPLAN (изменить подход) / ESCALATE (человеку) / SKIP
  - Error classification: syntax → retry, timeout → backoff, logic → replan
  - Structured context: что пробовали, какая гипотеза, что изменить
- **Реализация:** расширить `RetryManager` вердиктами и error classification
- **Объём:** ~100 строк в `retry.py`
- **Не брать:** полный reflexion с LLM-judge — Maestro полагается на validation commands

### 2.4. HITL gate из plannotator → Approval перед merge

- **Проблема:** Mode 2 (orchestrator) автоматически создаёт PR без review
- **Взять из plannotator:**
  - Опциональный approval step перед `git push` / PR creation
  - Запуск plannotator для review diff задачи / zadacha
  - Блокирующий hook с timeout
- **Реализация:** добавить `approval_required: bool` в задачу/zadacha config; при true → launch plannotator
- **Объём:** ~50 строк интеграции в orchestrator.py
- **Не брать:** полный annotation workflow — достаточно approve/deny gate

### 2.5. Structured observability из atp-platform

- **Проблема:** event log (JSONL) без rotation, без querying; logging inconsistent
- **Взять из atp-platform:**
  - structlog с contextual fields (task_id, agent_type, duration, cost)
  - Log rotation (daily или size-based)
  - Correlation IDs для связи event log ↔ SQLite ↔ agent logs
- **Объём:** ~60 строк замены logging → structlog + rotation config
- **Не брать:** Prometheus/OTel — overkill для CLI orchestrator

### 2.6. TUI Kanban из executor (предложенная идея) → Дополнение к web dashboard

- **Проблема:** web dashboard требует браузер; при SSH-сессиях неудобно
- **Взять идею:**
  - Textual TUI как альтернатива web dashboard для headless environments
  - `maestro dashboard --tui` vs `maestro dashboard --web`
  - Показывать: DAG + task status + cost + logs inline
- **Объём:** ~400 строк в `tui_dashboard.py` (Textual)
- **Не брать:** замену web dashboard — оба варианта полезны

### 2.7. Token parsing из manbot (structured agent output)

- **Проблема:** cost_tracker парсит stdout в поисках `input_tokens` — хрупко
- **Взять из manbot:**
  - Structured JSON output protocol: agent пишет `{"type":"usage","input_tokens":...}` в stderr
  - Или: парсить usage из API response headers (X-Usage-Input-Tokens)
  - Fallback: текущий regex-based parsing
- **Объём:** ~40 строк в cost_tracker.py
- **Не брать:** JSONL IPC целиком — Maestro spawns subprocesses, не long-lived processes

### 2.8. Generator-based spawner protocol из codebuff → Умные spawner-ы

- **Проблема:** Spawner-ы — тонкие обёртки над `subprocess.run()`. Exit code 0 = success, остальное = fail. Нет промежуточной проверки, нет программного контроля.
- **Взять из codebuff:**
  - `handleSteps` generator pattern: spawner yield-ит директивы (STEP, CHECK, RETRY), получает обратно состояние
  - Spawner может: запустить агента → проверить вывод → решить retry/replan/done программно
  - Гибрид prompt + code: логику ветвлений пишет человек, контент генерирует LLM
- **Реализация:**
  ```python
  class GeneratorSpawner(AgentSpawner):
      def run(self, task: Task) -> Generator[SpawnerDirective, SpawnerResult, None]:
          result = yield RunAgent(task.prompt)
          if result.exit_code != 0:
              yield RunAgent(task.prompt, context=f"Previous error: {result.stderr}")
          yield CheckTests(task.validation_cmd)
  ```
- **Объём:** ~150 строк в `spawners/generator_spawner.py`
- **Не брать:** полный SDK с custom tools — Maestro оркестрирует CLI, а не API

### 2.9. Best-of-N decomposition из codebuff → Качественные планы

- **Проблема:** decomposer генерирует один вариант разбиения на zadachi. Качество зависит от одного LLM вызова.
- **Взять из codebuff:**
  - GENERATE_N паттерн: запросить 3 варианта decomposition → выбрать лучший
  - Selector: оценить каждый план по метрикам (количество zadachi, scope overlap, estimated cost)
  - Budget-aware: decomposition делается 1 раз, 3x токены оправданы
- **Реализация:**
  ```python
  plans = [decompose(project_spec) for _ in range(3)]
  scored = [(plan, score_plan(plan)) for plan in plans]
  best = max(scored, key=lambda x: x[1])
  ```
- **Объём:** ~80 строк в `decomposer.py`
- **Не брать:** best-of-N для каждой задачи — слишком дорого по токенам

### 2.10. Propose pattern из codebuff → Preview spec перед execution

- **Проблема:** decomposer создаёт spec-файлы и сразу записывает их. Пользователь не видит план до начала работы.
- **Взять из codebuff:**
  - `propose_write_file`: показать diff/preview без фактической записи
  - Пользователь видит что будет создано → approve/deny → затем write
- **Реализация:** добавить `--dry-run` / `--propose` флаг в `maestro orchestrate`:
  - Показать список zadachi с scope, dependencies, estimated cost
  - Показать diff spec-файлов
  - Ждать confirmation перед началом
- **Объём:** ~40 строк в orchestrator.py
- **Не брать:** interactive per-file proposals — достаточно plan-level approval

### 2.11. Multi-provider fallback из codebuff → Resilient spawners

- **Проблема:** при недоступности Claude Code — задача падает. Нет fallback на другой agent.
- **Взять из codebuff:**
  - Provider routing с fallback chains: order + allow_fallbacks
  - Health check перед spawn: agent доступен? → следующий в цепочке
  - Config-driven: `fallback_chain: [claude_code, codex, aider]` в YAML
- **Реализация:**
  ```python
  class FallbackSpawner(AgentSpawner):
      def __init__(self, chain: list[AgentSpawner]):
          self.chain = chain
      async def run(self, task):
          for spawner in self.chain:
              if await spawner.health_check():
                  return await spawner.run(task)
          raise NoAvailableAgent()
  ```
- **Объём:** ~100 строк в `spawners/fallback.py` + config extension
- **Не брать:** OpenRouter-level provider routing — Maestro работает с CLI tools, не API

### 2.12. Streaming events из codebuff → Замена polling

- **Проблема:** мониторинг через polling 2 сек, full JSON re-parse на каждом цикле
- **Взять из codebuff:**
  - Streaming events через SSE: агент → orchestrator → dashboard в реальном времени
  - Event types: task_started, step_completed, tool_called, task_done, error
  - Nested events: orchestrator → zadacha → task (с parent IDs)
- **Реализация:** заменить polling loop в `scheduler.py` на event-driven:
  - Subprocess stdout → event parser → SSE broadcast
  - Dashboard подписывается на SSE stream вместо polling
- **Объём:** ~150 строк (event protocol + SSE sender + dashboard subscriber)
- **Не брать:** async generator streaming целиком — SSE проще и достаточен

### 2.13. SDK pattern из codebuff → Programmatic API

- **Проблема:** Maestro только CLI. Нельзя встроить в CI/CD pipeline программно.
- **Взять из codebuff:**
  - `CodebuffClient` паттерн: Python SDK с `maestro.orchestrate(config)` API
  - Event callbacks для мониторинга: `on_task_started`, `on_task_completed`
  - Resumable runs через `previousRun` state
- **Реализация:** вынести core logic из CLI в library:
  ```python
  from maestro import Orchestrator

  orch = Orchestrator(config_path="project.yaml")
  result = await orch.run(on_event=lambda e: print(e))
  ```
- **Объём:** ~200 строк API surface (thin wrapper вокруг существующего кода)
- **Не брать:** npm SDK — Maestro это Python

---

## 3. Quick wins (высокий импакт, низкие усилия)

| # | Что сделать | Усилия | Импакт |
|---|------------|--------|--------|
| 1 | Scope overlap → auto-add dependency (serialization) вместо warning | 3ч | Предотвращение file conflicts |
| 2 | Recovery: проверять `os.kill(pid, 0)` перед пометкой orphaned | 1ч | Не перезапускать выполненную работу |
| 3 | Cleanup handler (atexit + signal) для worktrees | 2ч | Нет orphan worktrees после crash |
| 4 | Config-based pricing вместо hardcoded dict | 1ч | Точный cost tracking |
| 5 | Event log rotation (daily, max 10 files) | 1ч | Нет unbounded file growth |
| 6 | Adaptive polling: 0.5s при running tasks, 5s при idle | 1ч | Меньше I/O, быстрее реакция |

---

## 4. Что НЕ брать

| Паттерн | Источник | Причина отказа |
|---------|----------|---------------|
| Decision Tree ML routing | arbiter | Maestro использует config-based agent assignment, не policy engine |
| Goal-driven graph generation | hive | Maestro работает с declarative YAML specs, не natural language goals |
| Container isolation | nanoclaw | git worktrees уже дают filesystem isolation |
| 100+ MCP tools | hive | Maestro оркестрирует CLI agents, не управляет tools напрямую |
| MITM proxy observability | cc-wiretap, pylon | Maestro контролирует spawning, proxy не нужен |
| Multi-channel gateway | openclaw | Maestro — CLI orchestrator, не messaging platform |
| vtable extensibility | nullclaw | Python SpawnerRegistry + entry points достаточен |

---

## 5. Архитектурная идея: Mode 3 — Hybrid

Сейчас Mode 1 (scheduler, shared dir) и Mode 2 (orchestrator, worktrees) полностью независимы.

**Идея Mode 3:** scheduler использует worktrees для задач с конфликтующим scope.

```
Анализ DAG:
  ├─ Задачи без scope overlap → запуск в shared dir (быстро, как Mode 1)
  └─ Задачи с scope overlap → создать worktree (изоляция, как Mode 2)
```

**Плюсы:**
- Не нужно выбирать mode заранее
- Минимум worktrees (только при реальных конфликтах)
- Автоматическая оптимизация на основе DAG analysis

**Сложность:** средняя (~200-300 строк). Требует unified task model поверх существующих Mode 1/2.

---

## 6. Приоритетный roadmap

### Phase 1: Safety (2-3 дня)
- Scope overlap enforcement (auto-dependency из arbiter)
- Process identity tracking (PID + start_time из nullclaw)
- Worktree cleanup handler (atexit + signals)
- Recovery improvement (проверка живости процесса)

### Phase 2: Intelligence (2-3 дня)
- Retry вердикты (RETRY/REPLAN/ESCALATE из hive)
- Config-based pricing + model variants
- HITL approval gate (из plannotator)

### Phase 3: Observability (2-3 дня)
- Structured logging (из atp-platform)
- Event log rotation
- Adaptive polling → SSE streaming events (из codebuff)
- TUI dashboard option (из executor идеи)

### Phase 4: Intelligence (2-3 дня)
- Generator-based spawner protocol (из codebuff)
- Best-of-N decomposition (из codebuff)
- Propose/preview перед execution (из codebuff)
- Multi-provider fallback chains (из codebuff)

### Phase 5: Architecture (3-5 дней)
- Mode 3 hybrid (auto worktree при scope conflict)
- Python SDK для programmatic API (из codebuff)
- Integration tests для full orchestration flow
- Crash recovery e2e tests
