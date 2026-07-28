# TODO — Maestro (план от 2026-04-16, снапшот 2026-07-26)

> Стратегический контекст: `../prograph-vault/authored/notes/ecosystem-roadmap.md`
> Последний экосистемный статус: `../_cowork_output/status/2026-07-24-status.md`
> Бэклог идей (research digest): `../prograph-vault/authored/notes/2026-07-22-ideas-from-ai-repos-research.md`
> Критический путь: ✅ закрыт (R-01..R-04 shipped в v0.2.0, observability M1+M2 закрыты, arbiter#9 фикс 2026-04-25)
> Июльский трек изоляции и верификации (#90…#110) закрыт — см. раздел «Июль 2026» ниже.

## Правила ведения
- После каждой выполненной задачи проставь `[x]` и добавь хеш коммита
- Если задача стала неактуальной — зачеркни `~~...~~` с пометкой **почему**
- Не добавляй новые задачи без обновления roadmap в `_cowork_output/`
- **Уровень пунктов — командный.** Микрошаги реализации живут в
  `docs/superpowers/specs/` + `docs/superpowers/plans/` и в SDD-леджере
  `.superpowers/sdd/progress.md`; этот файл их намеренно не дублирует.
- **Инлайн-теги** (все опциональные; контракт —
  `../_cowork_output/2026-07-26-plan-fields-and-todo-coverage-handoff.md` §3):
  `@owner:<handle>` · `@blocked_by:<repo>#<slug>` · `@trigger:"<проверяемое условие>"` ·
  `@id:<node-id>`, в хвост первой строки пункта. Отсутствие тега значит «неизвестно» и это
  осмысленный ответ — выдумывать триггер там, где его нет, хуже, чем оставить пусто.
  Грепается кросс-репно: `grep -rn "@blocked_by:" */TODO.md`.
  - `@id` — канонический идентификатор пункта (ADR-ECO-005 PF-2B): строчная грамматика
    `[a-z0-9][a-z0-9._-]{0,63}` (напр. `r-03b`, не `R-03b`), из него строится URI
    `todo://maestro/<id>`. Переходно `@blocked_by` принимает и legacy `<repo>#<slug>`,
    и канонический `todo://<repo>/<id>`.
- **Не переформулируй текст существующего открытого пункта.** Robin (`robin-runtime`)
  опознаёт пункт по нормализованному тексту первой строки; с robin-runtime#27 теги
  исключены из ключа, поэтому *дописать* тег безопасно, а *переписать* формулировку —
  значит отчитаться в дайджесте о фантомной паре «закрыт/открыт».

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
  - Создать `ExecutorState` в `maestro/models.py` (рядом с `Task`, `Workstream`)
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
  - Тесты: +113 новых (1112/1112), pyrefly clean, `ruff check .` clean, `ruff format --check .` clean
  - Пример: `examples/with-arbiter.yaml` (смоук-проверен через `maestro.config.load_config`); `examples/tasks.yaml` — arbiter=None, zero-config путь не задет
  - Pending manual acceptance (требует локальной сборки arbiter-mcp): (a) advisory + kill arbiter → retry всё равно идёт; (b) authoritative + kill, < abandon_outcome_after_s → FAILED держится; (c) authoritative + kill, > abandon_outcome_after_s → `arbiter.outcome.abandoned` событие + unblock

### Follow-ups разблокированные R-03

Дальнейший трек ведётся в Linear (Maestro / Arbiter проекты, team Labs). Ниже — snapshot на 2026-04-17.

- [ ] **R-03b** (LABS-TBD): Mode 2 (`maestro orchestrate`) workstream-level routing. Gate: ≥1 неделя стабильного Mode-1 dogfood после v0.2.0 @owner:andrei @trigger:"≥1 неделя стабильного Mode-1 dogfood после v0.2.0" @id:r-03b
- [x] **R-05 contract-level** (commit `f1f7d26`, 2026-04-25): 4 e2e теста против реального `arbiter-mcp` бинарника в `tests/test_arbiter_real_subprocess.py`. Auto-skip без бинарника; `MAESTRO_ARBITER_BIN` override. Покрывает: decision_id i64, int→str coercion, route→report_outcome round-trip, distinct rowids.
- [x] **R-05 CI job** (2026-05-07): новый `arbiter-e2e` job в `.github/workflows/ci.yml` — sibling-checkout Maestro + arbiter (`andrei-shtanakov/arbiter`), `cargo build --release --bin arbiter-mcp` под Swatinem cache, прогон `tests/test_arbiter_real_subprocess.py` с `MAESTRO_ARBITER_BIN`. Ref-strategy: PR/push на pinned `ARBITER_PINNED_SHA=d1a8ecd` (arbiter#9 fix), weekly schedule (Mon 06:00 UTC) на `master` для drift-check. Локальный smoke: 4/4 теста зелёные.
- [x] **R-05 scheduler-driven e2e** (2026-05-07): `tests/test_scheduler_arbiter_real_subprocess.py` — 2 теста скрещивают real arbiter-mcp + Scheduler full cycle + MagicMock spawner. (1) ASSIGN happy-path: real arbiter routes → mock exit 0 → outcome reported back to real arbiter → DONE; проверяет int→str round-trip decision_id через TEXT-колонку. (2) Retry-gating с real rowids: exit 1 → ADVISORY reset → второй route real arbiter mint'ит fresh i64 ≠ первого. HOLD/REJECT покрыты в `test_scheduler_arbiter_integration.py` через FakeArbiter — дублирование через real subprocess не оправдано (требует seed'инга cost/failure history)
- [x] **arbiter#9 client-side fix** (commit `e5915f2`, 2026-04-25): `_extract_decision_id` коэрсит `int → str` для `arbiter_decision_id TEXT` колонки и stale-guard. Парная с arbiter `d1a8ecd`. 8 unit-тестов в `TestExtractDecisionId`
- [x] **R-10** (LABS-91 / arbiter#8, `7e6de56`): Arbiter CI release-binary. Готово: linux-x64 + macos-arm64 30-day artifacts. Открыто: tag-triggered GitHub Release upload, `pyrefly check` в Python job
- [x] **R-NN** (LABS-84, commit `ab279f2`): wire `cost_tracker` в `Scheduler._record_cost`. `TaskOutcome.tokens_used` / `cost_usd` теперь несут реальные значения. Model variants / structured usage — отдельно под LABS-49
- [x] **Mini-R** (LABS-85, commit `627c12d`): `schema_migrations` journal + линейный migration runner. Добавление миграции #3+ = одна строка в `ordered` + метод
- **R-14** (vendored `arbiter_client.py` → PyPI `arbiter-py`) — дубликат, канонический
  пункт живёт ниже, в «Follow-ups from R-06b M4». Дедуплицировано 2026-07-26, чтобы
  не считать одну работу дважды.

### R-06b — Agent benchmarking via ATP

> Дизайн: `../prograph-vault/authored/decisions/2026-04-25-r06b-design.md`
> M0 (design) approved by virtue of M1 landing.

- [x] **R-06b M1 thin slice** (2026-05-07): новый `maestro/benchmark/` модуль — `BenchmarkRunner` + Protocols (`ATPClientLike`, `BenchmarkRun`, `AgentResponder`), Pydantic-модели (`BenchmarkResult`, `BenchmarkTaskResult`, `AgentResponse`). Async API (Maestro async-first; M2 spawner и M3 ATP HTTP-клиент будут async). Mock-only тесты в `tests/test_benchmark_runner.py` — 2 кейса: happy path с агрегацией tokens/cost и agent-error path (None ≠ 0 для отсутствия измерений). Цель M1 достигнута: API shape залочен, M2..M5 могут идти параллельно
- [x] **R-06b M2 spawner integration** (2026-05-08): `maestro/benchmark/spawner_responder.py` — `SpawnerResponder` обёртывает любой `AgentSpawner` (claude_code/codex_cli/aider) и реализует `AgentResponder`. Синтез минимального `Task` под benchmark prompt, `asyncio.to_thread(process.wait)` под `asyncio.wait_for(timeout)`, парсинг tokens/cost через существующий `cost_tracker.{parse_log,calculate_cost}` (без db side-effects). `response.text` = full log content (M2 punt; M3 уточнит per-benchmark extraction). +4 теста в `tests/test_spawner_responder.py`: happy path, timeout (kill + unblock), non-zero exit, unknown agent_type short-circuit
- [x] **R-06b M3 auth + live ATP** (2026-05-08): новый `maestro/benchmark/atp_client.py` — `MaestroATPAdapter` оборачивает `atp_sdk.AsyncATPClient` (PyPI `atp-platform-sdk>=2.0.0`) под M1 Protocols. Auth UX делегирован SDK: token resolution `explicit → ATP_TOKEN env → ~/.atp/config.json` (Device Flow encapsulated в SDK, Maestro его не дублирует). Конструкторы `from_env`/`from_token`. Bridge-перевод: `run_id: int → str`, raw ATPRequest dict → typed `_Task` (вытащены `metadata.task_index` + `task.description` + `task_id`), submit оборачивает `response: str` в ATPResponse (`status="completed"|"failed"` по непустоте, текст в `ArtifactStructured`), `finalize()` делает GET `/runs/{id}/status` и читает `total_score`. `score_components={}` пока ATP не экспортирует breakdown. +6 тестов через monkeypatch `AsyncATPClient._request` (`FakeRequestQueue`): auth headers, env fallback, run_id-cast, end-to-end iteration с проверкой ATPResponse shape + task_id reuse, failed-status path, finalize при отсутствии total_score. 1156/1156 pytest, pyrefly clean, ruff clean
- [x] **R-06b M4** (2026-05-23, merged via PRs #19/#20/#21, last merge SHA `5edb359`; main M4 merge `3066ded`): new MCP tool `report_benchmark` in arbiter-mcp + `maestro/benchmark/arbiter_report.py` helper. Persist-only into new `benchmark_runs` table (single row + per_task jsonb); `INSERT...ON CONFLICT(run_id) DO NOTHING` idempotency; fire-and-forget emit with `BenchmarkResult.report_status`/`report_error` (immutable `model_copy`). Schema-first contract in `_cowork_output/benchmark-contract/report_benchmark-v1.schema.json`. Vendored client `MIN_ARBITER_PROTOCOL=(1,1)` + `ARBITER_VENDORED_FROM_SHA` pin + CI drift check. New typed `ArbiterContractError` differentiates JSON-RPC contract breaks (-32600/-32602/-32603) from transient `ArbiterUnavailable`. 5 distinct obs events (`benchmark.report.{skipped,succeeded,duplicate,failed,contract_break}`); contract_break gets ERROR severity. Smoke script `scripts/smoke_benchmark_report.py` + 3-case e2e in `arbiter-e2e` CI job (created/duplicate/contract_break). Arbiter Phase 1: merged via PR #11 at SHA `7aeb6b1`; subsequent hardening via PRs #13/#14/#15 (latest arbiter master `81fe183`). Recommended minimum SHA for full feature: `151004b` (PR #13). Full design: `docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md` + plan `docs/superpowers/plans/2026-05-23-r06b-m4-arbiter-wiring.md`.
- [x] **R-06b M5 CLI**: `maestro benchmark <benchmark-id> --agent claude_code` (closed by feat/benchmark-cli)

### Follow-ups from R-06b M4

- [x] **M3-obs / arbiter trace** (2026-07-19): W3C `traceparent` инжектится в `params._meta` каждого `tools/call` (`arbiter_client._call_tool_once`); пропуск при нулевом trace-id; e2e-тест подтверждает, что пинованный arbiter игнорирует `_meta`. Arbiter-side чтение `_meta.traceparent` — handoff в `prograph-vault/authored/notes/2026-07-19-arbiter-meta-traceparent-handoff.md`.
- [ ] **R-06b M4b**: revisit `max_per_task=200` sampling for swe-bench-full (>1000 tasks). Trigger: first PROD swe-bench-full run. @owner:andrei @trigger:"первый PROD-прогон swe-bench-full"
- [ ] **R-07 prereq (GIN index)**: GIN index on `benchmark_runs.per_task` jsonb. Trigger: when R-07 starts writing SQL filters on per_task. @owner:arbiter @blocked_by:arbiter#R-07 @trigger:"R-07 начинает писать SQL-фильтры по per_task"
- [ ] **R-07 prereq (normalize)**: normalize `benchmark_task_results` table (migration from jsonb blob). Trigger: same as GIN — formal query demand. @owner:arbiter @blocked_by:arbiter#R-07 @trigger:"тот же формальный запрос, что у GIN"
- [ ] **R-07 prereq (retention)**: TTL / archive policy for `benchmark_runs`. Trigger: table > 10k rows OR > 1 GB total JSON blobs. @owner:arbiter @trigger:"benchmark_runs > 10k строк ИЛИ > 1 GB JSON"
- [ ] **R-14**: vendored `arbiter_client.py` → standalone PyPI `arbiter-py` package. M4 enlarged vendor surface. @owner:arbiter @blocked_by:arbiter#arbiter-py
- [ ] **Unscheduled — outbox**: persistent outbox + background retry for benchmark report. Trigger: if fire-and-forget shows real CI churn. @owner:andrei @trigger:"fire-and-forget даёт реальный CI-churn"
- [ ] **Unscheduled — arbiter-initiated benchmark**: outgoing benchmark trigger from arbiter ("router uncertain → run benchmark"). From design open question #2. @owner:arbiter
- [ ] **M5 / multi-tenant auth**: service-account ATP token for CI; multi-tenant arbiter auth as separate ticket if arbiter ever leaves subprocess trust model. @owner:atp @trigger:"arbiter выходит за subprocess-trust-модель"

### Новое из v0.2.0 dogfood (LABS-87..90)

- [x] **LABS-87** (2026-05-07): validation-failure path теперь репортит outcome в arbiter с retry-gating. `_handle_validation_failure` отзеркалил `_handle_task_failure`: build outcome (status FAILURE) → `_try_report_outcome` → ADVISORY/AUTHORITATIVE-aware reset. Both paths (retry-available + exhausted-NEEDS_REVIEW) шлют outcome. +4 теста в `test_scheduler_arbiter_integration.py` (advisory+retry, exhausted, advisory+arbiter-down, authoritative+arbiter-down). Routing API не расширен — `validation_passed` остаётся out-of-scope
- [x] **LABS-88** (Low): CI guard для unreferenced public modules (commit `c002f46`) — `tests/test_no_unreferenced_modules.py`, grimp import-graph, allowlist `maestro.schemas.generate` (python -m)
- [ ] **LABS-89** (Medium): release automation (version-vs-tag guard + release-drafter) @owner:andrei
- [x] **LABS-90** (Medium): per-example YAML smoke test в CI (commit `e9cbb1c`) — `tests/test_examples_smoke.py`, parametrized `examples/*.yaml` (Mode-1 `load_config`; Mode-2 `load_orchestrator_config`+`validate_project(check_fs=False)`) + `observed-models.json`; dummy `${VAR}` env; caught+fixed drifted `maestro-builds-maestro.yaml` (`repo: .`)

### Observability (cross-project) — M1 closed, M2 closed 2026-04-25

- [x] **M1** (commits `e3feefd`, `4688633`, `279193e`): cross-process trace continuity. Vendored `obs.py` от spec-runner@`fa6b106`, contract в `_cowork_output/observability-contract/` (log-schema, propagation, 4 fixtures), CLI `init_logging("maestro")`, child_env() пропагация в orchestrator
- [x] **M2** (commit `d474120`, 2026-04-25): scheduler instrumentation. `obs.span("scheduler.session")` + `obs.span("task.spawn")` (subprocess inheritance через TRACEPARENT), 4 структурированных emit'а (`task.completed`/`task.validation_failed`/`task.failed`/`task.timeout`), `spawn_env()` helper в `spawners/base.py` пропагирует трасу в claude_code/codex/aider/validator subprocesses. 3 теста в `test_scheduler_observability.py`
- [x] **M3 (runtime-decision instrumentation)** (closed by feat/observability-m3): `scheduler.tick` emit-on-change per poll cycle + `task.route` span around the routing decision (covers static + arbiter, records latency/decision_id; failure → `task.route.failed`).
- [x] **M-obs stdlib bridge** (2026-07-19): все stdlib `logging` вызовы (~93 call-sites в ~16 модулях) маршрутизируются в obs OTel JSONL через `maestro/logging_bridge.py` (`ObsBridgeHandler` + `setup_logging` в cli.py); WARNING+ дублируются в stderr (замена lastResort). Vendored `_vendor/obs.py` не тронут.
- [ ] **M3 — observability dashboards** (pending): separate project (backend/viz over the OTel JSONL or the existing `maestro/dashboard/` UI). @owner:andrei
- [x] **M3 — W3C traceparent into the MCP JSON-RPC envelope** (2026-07-19, Maestro-side done): injection in `params._meta` on every `tools/call`; arbiter-side reading is the remaining half (handoff note in prograph-vault).
- [ ] **Single async pytest plugin** (follow-up к фиксу R-05 2026-07-19): в тестах конкурируют pytest-asyncio (`asyncio_mode=auto`) и anyio-плагин — владелец `@pytest.mark.anyio`-теста зависит от порядка регистрации плагинов (uv 0.11.29 флипнул порядок в CI → cross-loop падения real-subprocess тестов). Точечный фикс: маркеры сняты в 3 real-subprocess файлах. Системно: стандартизироваться на одном плагине (anyio, по конвенции) и убрать pytest-asyncio. Trigger: следующий флип порядка или новые async-фикстуры с loop-bound состоянием. @owner:andrei @trigger:"следующий флип порядка плагинов или новые async-фикстуры с loop-bound состоянием"

---

## C4 — Decomposer delegation

- [x] **Delegate spec generation to spec-runner plan --full** (closed by feat/c4-decomposer-delegation): spec-runner owns the tasks.md format; removed SPEC_GENERATION_PROMPT and _write_spec_files.

---

## Июль 2026 — governance, изоляция и верификация (закрыто)

Треки, которых этот файл раньше не покрывал вовсе. Детали — в
`docs/superpowers/specs/` + `docs/superpowers/plans/`, поштучные решения — в
`.superpowers/sdd/progress.md`.

- [x] **Gates-in-DAG v1.0→v1.3** (#72, #73, #75, #77, #78): опциональные ex-ante
      (READY→RUNNING) и ex-post (RUNNING→MERGING) guard-хуки; тиры считает
      `steward risk-classify`, Maestro риск сам не вычисляет; fail-closed;
      таблица `gate_approvals` — единственный авторитет «одобрено ли
      (workstream, phase, sha)»; verdict-записи в `logs/<ULID>/gate_verdicts.jsonl`
      → `EvidenceRef kind=gate-verdict`.
- [x] **Idea #7a — исполняемый scope-gate** (#92, #93): `maestro check-scope`,
      детерминированная проверка containment, fail-closed на git-ошибке.
- [x] **Idea #10 — transition hooks** (#94, #96): одна декларативная таблица
      `TASK_EFFECTS`/`WORKSTREAM_EFFECTS` (`maestro/transitions.py`) вместо ручной
      синхронизации событий и нотификаций; тест на тотальность по всем статусам.
- [x] **Idea #25 — `maestro costs`** (#97): read-only сводка; неоценённое = UNKNOWN, не $0.
- [x] **Distributed Execution Phase 0→2c** (#90, #98, #99, #100, #101):
      transport-agnostic `LocalBackend` → local Docker isolation → SSH-бэкенд
      (Mode 2) → Mode-1 remote (reservations + scope-bounded collect) → SSH+Docker.
      MVP закрыт: local+bare / local+docker / ssh+bare / ssh Mode-1 / ssh+docker.
- [x] **`validation_backend` PR1→PR3** (#102, #103, #104): пост-таск валидация
      идёт через execution-слой вторым `ExecutionRequest`; дефолт флипнут
      `local → same` (release-noted).
- [x] **Stage B — domain verification FSM** (#105, #106, #109): фаза `VERIFYING`
      для Mode-2, verdict-контракт v2 с run-keyed handshake, evidence-ledger вне
      worktree, ровно один evidence-коммит на ветке.
- [x] **Idea #6 — Mode-1 adversarial verifier gate** (#107, #108): третья durable
      фаза `VERIFYING` для задач, LLM-судья по scope-bounded дифу, fail-closed
      (ERROR → NEEDS_REVIEW, никогда не смягчается до FAIL).
- [x] **Strict Docker verifier sandbox** (#110): `verifier.backend: docker` —
      read-only rootfs, cap-drop=ALL, no-new-privileges, non-root, tmpfs `/scratch`,
      digest-пиненый образ. Это FS/process-изоляция, **не** сетевая.

### Открытые follow-ups июльского трека

- [ ] **Verifier: CHECK-констрейнт на `task_costs.execution_phase`** @owner:andrei
      Схемное ужесточение, требует rebuild таблицы. Отдельным маленьким PR — решение
      2026-07-26: три verifier-follow-up'а не бандлить в один.
- [ ] **Verifier: envelope без usage не должен схлопываться в $0** @owner:andrei
      В `maestro costs` такая строка обязана оставаться UNKNOWN. Корректность.
- [ ] **Verifier: кэш `load_catalog`** @owner:andrei @trigger:"замер показал реальную стоимость повторных load_catalog"
      Перф; браться только после замера, не раньше.
- [ ] **Verifier-docker: интеграционные и smoke-тесты не проверены против живого демона** @owner:andrei @trigger:"первый прогон с доступным docker-демоном (CI или локально)"
      `tests/integration/test_verifier_docker_*.py` сейчас чисто скипаются без docker,
      то есть контейнерные ассерты не подтверждены ни разу.
- [ ] **Verifier-docker: мелочи из леджера #110** @owner:andrei
      Коллизия имён `get_open_verification_handle` (ед.ч.) / `...handles` (мн.ч.) —
      сегодня предикаты состояний эквивалентны, новое состояние разойдётся молча;
      collection-time `docker info` probe на 10s в каждом прогоне сьюты; `docker pull`
      без таймаута.
- [ ] **Distributed Execution Phase 3 — routing/registry/queues** @owner:andrei
      Сознательно отложено через все фазы 0…2c.
- [ ] **Mode-1 remote: patch-collect** @owner:andrei
      Сегодня collect умеет только `scope_paths`.
- [ ] **Полный именованный реестр `backends: {}`** + публикация образа `maestro-runner` @owner:andrei
- [ ] **Хвост Phase 2b/2c** (детали — в `.superpowers/sdd/progress.md`) @owner:andrei
      reap/recovery re-hold reconciliation; local not-started held-not-released;
      `SshBackend` scope ключуется по `include`, а не по `mode`; arbiter-outcome на
      collect-conflict; `mktemp -d` без таймаута в `can_run`; дедуп ветки
      `decode_transport_ref`+isolation между probe и GC.
- [ ] **Stage B: ssh+docker dual-probe зеркало в `orchestrator.py`** @owner:andrei @trigger:"первый нелокальный бэкенд верификатора в Mode 2"
      TODO стоит в коде; сегодня верификатор Mode-2 пинён на локальный бэкенд.

---

## Бэклог идей из research-дайджеста (2026-07-22)

> Источник: `../prograph-vault/authored/notes/2026-07-22-ideas-from-ai-repos-research.md`
> Закрыто оттуда: #6 (#107), #7a (#92), #10 (#94), #25 (#97).

- [ ] **Idea #1 — сериализуемый RunState** со schema-version, interruptions, approvals @owner:andrei
      Сначала отдельный discovery-проход: состояние Maestro уже живёт в SQLite, надо
      понять, что именно добавляет версионированный снапшот сверху.
- [ ] **Idea #3 — семафорный dispatch и лимиты конкурентности** подзадач @owner:andrei
      Изолированный контекст на файл-бандл (default 8, BatchStrategy по языку/директории).
- [ ] **Idea #8 — guardrails с tripwire** на input/output/tool-вызовы @owner:andrei
      Сначала fit-спайк: какие границы Maestro реально наблюдает — иначе это, как и
      отклонённый #17, окажется заботой харнесса, а не оркестратора.
- [ ] **Idea #21 — handover-блоки с обязательной секцией Test Result** @owner:andrei
      Оркестратор валидирует структуру и требует переделать. Лёгкая структурная
      верификация свободного текста без JSON-схем.
- ~~**Idea #17 — architect/editor split**~~ — **отклонено 2026-07-23**: aider уже
  делает сплит внутри себя и агрегирует стоимость; пара моделей непредставима в
  контракте `<harness>@<model>` (а это контракт arbiter, не наш); спроса нет.
  Обоснование: `../prograph-vault/authored/notes/2026-07-23-idea17-architect-editor-maestro-fit.md`.

---

## Кросс-репные watch-items

- [ ] **`executor-config v0-provisional` висит без потребителя** @owner:andrei @blocked_by:dispatcher#DESIGN-301
      dispatcher запинил `contracts/executor-config/v0-provisional/schema.json`
      (DESIGN-301), и единственная ссылка на него во всей экосистеме — наш план-док
      `docs/superpowers/plans/2026-07-17-specrunnerconfig-passthrough.md`. Либо довести
      passthrough до реального потребителя, либо явно пометить контракт отложенным,
      чтобы пин не висел зомби (рекомендация статуса 2026-07-24).

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

---

## Catalog distribution follow-ups (ADR-ECO-003b)

- [ ] XDG default catalog path ($XDG_CONFIG_HOME/<eco>/agents-catalog.toml) once the @owner:andrei @blocked_by:atp-platform#eco-namespace @trigger:"<eco> namespace ратифицирован"
      <eco> namespace is ratified; extend `resolve_catalog_path`.
- [x] `maestro models init | list | discover | update` CLI (ADR-003b D3) (closed by feat/models-cli).
- [ ] Shared `CLAUDE_MODEL` / `CODEX_MODEL` cross-tool override layer. @owner:andrei
- [ ] `default = true` field in the catalog `[[agents]]` schema to disambiguate the @owner:atp @blocked_by:atp-platform#agents-catalog-default-flag
      A/B window (cross-repo, PM-owned) — removes the `HarnessModelUnresolved`
      ambiguity raise.
- [ ] Extract the loader to a shared PyPI lib with a cross-reader behavioral @owner:andrei
      conformance test (precedence + alias resolution across Maestro / ATP / arbiter).
- [ ] `maestro models`: detect the same observed model id under TWO vendors in @owner:andrei
      one manifest — today it renders an unparseable Plane-1 block (two
      `[models."id"]` tables); update refuses safely via the validation gate
      (cryptic tomllib message), discover --out writes the broken block while
      exiting 2. Should become its own report category or fold into
      vendor_conflicts.

## opencode follow-ups (ADR-ECO-003c)

- [x] Cost-from-log: surface `part.cost` (and optionally cache_read/cache_write)
      from opencode JSONL into TaskCost/TaskOutcome instead of PRICING-based 0.
      Constraint (recorded in parse_opencode_log docstring): cache_read must
      NOT be billed at full input price — in real runs cache_read ~= input.
      Until then opencode reports cost_usd=None (unknown) to the arbiter.
      (closed by feat/cost-from-log)
- [x] opencode entry in the ecosystem SSOT catalog (atp-platform/method/
      agents-catalog.toml) — cross-repo; the test fixture already carries
      harness=opencode / glm-5.1.
      Verified 2026-07-05: atp-platform/method/agents-catalog.toml has
      [harnesses.opencode] + one routable [[agents]] opencode/glm-5.1
      (promoted 2026-07-03, gate 003a D4) + two Path B non-routable entries;
      Maestro's loader resolves default_model_for_harness('opencode') ==
      'glm-5.1' against it. Done upstream by the atp-platform actor.
- [x] Routed-path token telemetry: `parse_and_create_cost` keys the parser off
      the DECLARED `task.agent_type` (scheduler.py), so a task routed to
      opencode (`agent_type: auto`, or an authoritative arbiter override)
      never reaches `parse_opencode_log` — token usage is silently zero and
      the drift canary is bypassed. `cost_usd` stays None on that path, so
      router honesty holds; only the token signal is lost. Pre-existing
      structural gap (a claude→codex override mis-parses the same way).
      Fix alongside cost-from-log: dispatch the parser by EFFECTIVE harness
      (`harness_of_agent_id(task.routed_agent_type)` fallback) at the same
      call site.
      (closed by feat/cost-from-log)
- [ ] Recovery-path reported cost: `_reconstruct_outcome` (recovery.py) always @owner:andrei
      reports cost_usd=None even when a persisted TaskCost row with
      reported_cost_usd exists for the crashed attempt — honest-unknown, but
      real dollars the DB already holds are lost on crash-recovery reports.
- [ ] Responder `cost or None` (spawner_responder.py) collapses a genuine @owner:andrei @trigger:"free/local open-модели реально бегут под opencode"
      reported $0.00 into None ("confirmed free" reads as "unknown") — becomes
      real when free/local open models run under opencode.
- [ ] Codex cost-from-log (research): `codex exec` writes plain text (no @owner:andrei
      `--output-format json`); `parse_log` routes CODEX through the Claude JSON
      parser, which extracts nothing. Investigate whether codex can emit
      structured usage/cost (tokens + cost) and, if so, add a dedicated codex
      parser + `parse_log` route. (Deferred from the claude cost-from-log spec.)

- [x] opencode parser: guard `part.cost >= 0.0` (parity with the claude cost
      guard) (commit `a7b361f`). `parse_opencode_log` accepts a negative `part.cost`; a negative
      sum then fails `TaskCost.reported_cost_usd`'s `ge=0.0` validator and
      silently drops the whole row (tokens included) — the same silent-drop
      failure mode the NaN guard already prevents. The claude guard added
      `cost >= 0.0`; opencode's did not (so "guards mirror opencode exactly" is
      not literally true for the negative case). Low-probability (opencode is
      unlikely to emit a negative cost) but a real latent drop. (From the claude
      cost-from-log final review.)

- [x] scaffolder emits portable repo_path (commit `2e20051`): `maestro init` / `scaffold.py` sets
      `repo_path=str(cwd.resolve())` — an absolute path baking in the username,
      so every generated config is born non-portable (see PR #53, which fixed
      the proctor configs by hand). The loader already `expanduser()`s, so the
      scaffolder should emit a home-relative `~/...` path when cwd is under
      `$HOME` (else keep absolute). Small; needs a design call on the exact
      rule + a scaffold test. (From PR #52/#53 Copilot review.)

- [x] Orchestrator startup recovery: workstreams stranded in DECOMPOSING or
      RUNNING after a hard crash are not re-resolved on `--resume`
      (`_resolve_ready` only picks PENDING/READY). Pre-existing; surfaced during
      C4 final review (Minor #4). Add crash-recovery re-resolution. (closed by feat/orchestrator-startup-recovery)
- [x] Orchestrator recovery follow-ups (from startup-recovery final review):
      (a) DECOMPOSING orphan liveness — record the `plan --full` generation pid
      so a stranded DECOMPOSING can be liveness-checked like RUNNING (today it
      re-decomposes blindly, could race an orphaned generation writing spec/).
      (closed by feat/decomposing-generation-pid-liveness)
      (b) Move `_merge_into_base` BEFORE the DONE transition (or add a
      merged-into-base check) so a crash during the base merge doesn't leave a
      workstream showing DONE with an unmerged feature branch.
      (closed by feat/base-merge-before-done)

- [x] Uniform spawn→persist window closure (RUNNING + DECOMPOSING) (closed by feat/spawn-persist-window-closure): a hard crash
      between spawning the subprocess and persisting its pid leaves status set
      with pid=NULL and a live orphan → recovery reads None → READY → re-run
      races the orphan. Close both windows symmetrically (e.g. a "spawning"
      sentinel pid recovery treats as "assume live → NEEDS_REVIEW"), including
      the already-merged RUNNING path. (From the gen-pid liveness spec's
      residual-risk section.) Fold in the parked-row cleanup: the recovery
      live-orphan branch leaves the stale pid (process_pid / generation_pid) on
      the NEEDS_REVIEW row — clear it for BOTH states together (harmless to
      recovery, but cleaner for REST/dashboard).
