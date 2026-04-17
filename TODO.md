# TODO — Maestro (план от 2026-04-16)

> Стратегический контекст: `../_cowork_output/roadmap/ecosystem-roadmap.md`
> Последний недельный отчёт: `../_cowork_output/status/2026-04-10-status.md`
> Критический путь: R-01 → R-02 → R-03 (Maestro ↔ Arbiter интеграция)

## Правила ведения
- После каждой выполненной задачи проставь `[x]` и добавь хеш коммита
- Если задача стала неактуальной — зачеркни `~~...~~` с пометкой **почему**
- Не добавляй новые задачи без обновления roadmap в `_cowork_output/`

---

## День 1 — разблокировка (parallel, effort S)

- [x] **R-01: Нормализация agent IDs** — `codex` → `codex_cli` (commit `8fd0b51`)
  - `maestro/models.py:76` — `CODEX = "codex"` → `CODEX = "codex_cli"`
  - Затронутые файлы (grep уже сделан): `models.py`, `cost_tracker.py`, `spawners/codex.py`, `schemas/project_config.json`, `executor.config.yaml`, `tests/test_models.py`, `tests/test_cost_tracker.py`, `tests/test_spawners.py`, `tests/test_spawner_registry.py`
  - Мотивация: arbiter в `config/agents.toml` использует `codex_cli`, без этого R-03 вернёт reject на первом вызове
  - Verify: `uv run pytest && uv run pyrefly check`
  - Примечание при выполнении: `executor.config.yaml` и `shutil.which("codex")` / `Popen(["codex", ...])` не менялись — там фигурирует имя CLI-бинарника, а не enum‑идентификатор. `test_cost_tracker.py` менять не потребовалось: тесты используют `AgentType.CODEX` (имя константы сохранилось, изменилось только `.value`). Regen: `uv run python -m maestro.schemas.generate`. Результат: 953/953 pytest, pyrefly clean, ruff clean.

- [x] **R-09: GitHub Actions CI** — pytest + ruff + pyrefly (commits `36a1671` → `5e66357` → `05e5089`, run `24492556426` green)
  - Создать `.github/workflows/ci.yml`
  - Образец: `../spec-runner/.github/workflows/ci.yml` (заменить `mypy src` на `pyrefly check`, trigger: push на `main` + PR)
  - Matrix: Python 3.12+ (из pyproject.toml)
  - Мотивация: 29 тестов запускаются только вручную, ежедневные коммиты без safety net — блокер для open-source v0.1.0
  - Примечание: 3 job'а (lint / typecheck / test на py3.12+3.13), trigger — push на `master` + PR (фактический branch у проекта — master). Попутно применён `ruff format` к `maestro/cli.py` (pre-existing mismatch). Первый прогон вскрыл 22 pre-existing фейла, исправленных настройкой runner-а: `git config init.defaultBranch main` + `user.email`/`user.name` (тесты `test_git*` создают temp repos и делают `checkout main`/merge); `TERM=dumb` для теста (GitHub Actions форсит `FORCE_COLOR=1`, Rich игнорирует `NO_COLOR` для bold/dim, из-за чего help-строки вида `--resume` разбивались ANSI-кодами). Финальный прогон: 952 passed, 1 slow deselected, все 4 job'а green. Node.js 20 deprecation warnings (action versions) — non-blocking, можно обновить потом.

- [x] **R-08: Пометить неработающие интеграции в корневом COWORK_CONTEXT.md** (не в git)
  - Файл: `../COWORK_CONTEXT.md` (вне Maestro, но задача туда)
  - Maestro→Arbiter и Maestro→ATP помечены как существующие — это вводит в заблуждение
  - Проставить `🔴 NOT IMPLEMENTED` или `⚠️ PLANNED` рядом со стрелками
  - Сделано: `⚠️ PLANNED` заменён на `🔴 NOT IMPLEMENTED` в диаграмме интеграций для Maestro→Arbiter и Maestro→ATP. Секция «Контрактные точки → Maestro ↔ Arbiter (MCP)» получила жирный заголовок `🔴 NOT IMPLEMENTED` + disclaimer с разблокирующими R-01/R-02/R-03. Обновлён таймстемп `Последнее обновление` на 2026-04-16. Parent-директория не git-репо, коммитить некуда — изменения на диске.

- [x] **R-06a: Пример `validation_cmd: "atp test ..."`** (quick win, 0 строк кода) (commit `5c4c25f`)
  - Файл: `examples/tasks.yaml` или новый `examples/with-atp-validation.yaml`
  - Показать, как `validator.py` запускает ATP CLI после задачи
  - Мотивация: открывает доступ к ATP-оценке без ожидания R-03
  - Сделано: `examples/with-atp-validation.yaml` (88 строк). 3 паттерна: (1) pytest + ATP через `&&`; (2) ATP-only для задач без unit-тестов + JSON artifact для retry; (3) `--tags=smoke` для быстрых повторов. Маппинг exit-кодов ATP (0/1/2) на Maestro state machine задокументирован в заголовке. Валидация: `maestro.config.load_config` парсит все 3 `validation_cmd` корректно. Примечание: команда ATP CLI — `atp test`, не `atp run` (как было в TODO).

---

## Неделя 2 — формализация (effort M)

- [x] **R-04: ExecutorState Pydantic-модель** (commits `0498c82` + `cc9ee02`, CI run `24494341902` green)
  - Сейчас `.executor-state.json` парсится как dict в `maestro/orchestrator.py` и `maestro/workspace.py`
  - Создать `ExecutorState` в `maestro/models.py` (рядом с `Task`, `Zadacha`)
  - Зафиксировать версию `spec-runner` в `pyproject.toml`
  - Добавить contract test: Maestro генерирует конфиг → spec-runner его парсит
  - Мотивация: единственная работающая интеграция держится на неформальном контракте, ломается при любом обновлении spec-runner
  - Сделано: 4 типизированные модели (`ExecutorState`/`ExecutorTaskEntry`/`ExecutorTaskAttempt`/`ExecutorTaskStatus`) с `extra="ignore"` для форвард-совместимости. Новый модуль `maestro/spec_runner.py` — integration boundary: константа `SPEC_RUNNER_REQUIRED_VERSION="2.0.0"`, helper `read_executor_state(spec_dir)` с приоритетом SQLite (read-only `file:?mode=ro` URI — не блокирует writer'а) + fallback JSON, детектом опциональных колонок через `PRAGMA table_info`. **Побочный баг-фикс:** `orchestrator._update_progress` читал stale `.executor-state.json`, которого нет в spec-runner 2.0 — progress в дашборде и БД молча стоял. Теперь через `read_executor_state` работает и с SQLite. +11 contract-тестов (1010 всего): version pin, JSON parsing + unknown fields + malformed, SQLite real schema, SQLite-beats-JSON, corrupt-SQLite fallback, `to_executor_config()` shape, round-trip + invalid status rejection.

---

## Недели 3+ — критическая цепочка интеграции (effort M → L)

- [x] **R-02: Расширение TaskConfig полями Arbiter** (commit `8a3cba8`, CI run `24493970314` green)
  - `maestro/models.py:81-154` (`Task`/`TaskConfig`)
  - Добавить required поля: `task_type` (7 enum), `language` (6 enum), `complexity` (5 enum)
  - Маппинг `priority`: int(-100..100) → enum(low/normal/high/urgent)
    - `-100..-26` → `low`, `-25..25` → `normal`, `26..75` → `high`, `76..100` → `urgent`
  - Опциональная автоинференция: `language` из scope (`*.py`→python, `*.rs`→rust), `task_type` из prompt (ключевые слова: "fix"→bugfix, "test"→test)
  - Reference: `arbiter-core/src/types.rs`
  - Сделано: 4 StrEnum (`TaskType`/`Language`/`Complexity`/`Priority`) в snake_case под arbiter. Поля в `TaskConfig` — optional (auto-inference через `infer_task_type`/`infer_language`/`infer_complexity` в `Task.from_config`), в `Task` — required с дефолтами (feature/other/moderate) для обратной совместимости с прямым конструированием в тестах/scheduler. Приоритет остался `int` + helper `priority_int_to_enum(int)`. DB миграция: ALTER TABLE для pre-R-02 схемы через `_migrate_tasks_arbiter_columns` (использует `PRAGMA table_info` для идемпотентности). +46 тестов (999 всего). Регенерирована `project_config.json`. Дальше — R-03 (MCP-клиент), используем `priority_int_to_enum` и enum-поля напрямую на payload.

- [x] **R-03: MCP-клиент Arbiter в Maestro** (ветка `feat/r-03-arbiter-client`, 16 коммитов `ba8b950..80b7a2f`)
  - Новые модули: `maestro/coordination/arbiter_client.py` (vendored от arbiter@`861534e`), `maestro/coordination/routing.py` (`StaticRouting` + `ArbiterRouting` + `task_status_to_outcome_status` + `make_routing_strategy` фабрика), `maestro/coordination/arbiter_errors.py`
  - Модели: `AgentType.AUTO`, `ArbiterConfig`, `ArbiterMode`, `RouteAction`, `RouteDecision`, `TaskOutcome`, `TaskOutcomeStatus`; Task получил `routed_agent_type`/`arbiter_decision_id`/`arbiter_route_reason`/`arbiter_outcome_reported_at`
  - Scheduler: `_spawn_task` советуется с routing → ASSIGN/HOLD/REJECT; `_handle_task_completion`/`_handle_task_failure` доставляют outcome; mode-aware retry gating через `reset_for_retry_atomic` с decision_id guard; `_outcome_reattempt_pass` в main loop (bounded 5/tick) с authoritative abandon timer
  - Recovery: `recover_arbiter_outcomes()` закрывает висящие решения после краша, интегрировано в `StateRecovery.recover(routing=…)`
  - CLI: `maestro run` читает `ProjectConfig.arbiter`, строит routing через `make_routing_strategy`, плюмит `arbiter_enabled`, закрывает subprocess в `finally`
  - Event log: 10 новых `EventType` (ARBITER_ROUTE_DECIDED/HOLD/REJECTED/HOLD_SUMMARY/OUTCOME_REPORTED/OUTCOME_ABANDONED/UNAVAILABLE/RECONNECTED/RETRY_RESET_SKIPPED + RECOVERY_ARBITER_DECISIONS_CLOSED), `HoldThrottle` helper
  - DB: 4 новых колонки на `tasks` + миграция + `update_task_routing` / `mark_outcome_reported` / `reset_for_retry_atomic` / `get_tasks_with_pending_outcome` / `abandon_pending_outcome_and_release`
  - Тесты: +113 новых (1112/1112), pyrefly clean, `ruff check maestro/` clean
  - Пример: `examples/with-arbiter.yaml`

### Follow-ups разблокированные R-03
- [ ] **R-03b**: Mode 2 (`maestro orchestrate`) zadacha-level routing. Gate: ≥1 неделя стабильного Mode-1 dogfood
- [ ] **R-05**: Maestro↔Arbiter интеграционные тесты с реальным subprocess (зависит от R-10)
- [ ] **R-10**: Arbiter CI, собирающий `arbiter-mcp` binary как CI artifact
- [ ] **R-NN**: Wiring `cost_tracker` в scheduler outcomes, чтобы `TaskOutcome.tokens_used/cost_usd` несли реальные значения (сейчас None)
- [ ] **Mini-R**: `schema_migrations` journal table + линейный migration list (до того как миграций станет > 5)
- [ ] **R-14**: Вынести vendored `arbiter_client.py` в отдельный PyPI-пакет `arbiter-py`

---

## Чего НЕ делать до стабилизации

- ❌ Shared type library (R-14, XL) — преждевременно, сначала зафиксировать схемы
- ❌ `agent-infra.yaml` декларативная конфигурация (R-15, XL)
- ❌ Monorepo vs multi-repo решение (R-16, XL)

---

## Как проверить факт выполнения

Все задачи кросс-проектные — их «готовность» проверяется конкретными grep/ls (образец в `~/.claude/projects/.../memory/roadmap-status-2026-04-16.md`). После R-01/R-02/R-03 прогнать:

```bash
# R-01
grep -rn "codex_cli\|\"codex\"" maestro/ tests/
# R-02
grep -n "task_type\|complexity\|language" maestro/models.py
# R-03
grep -rn "arbiter\|route_task\|ArbiterClient" maestro/
# R-09
ls .github/workflows/
```
