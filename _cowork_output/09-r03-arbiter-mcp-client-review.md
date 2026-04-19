# R-03 Arbiter MCP Client — архитектурный аудит спецификации

**Объект:** `2026-04-16-r03-arbiter-mcp-client-design.md`
**Автор отчёта:** Cowork (software architect review)
**Дата:** 2026-04-16
**Режим:** read-only анализ (код не изменялся)

---

## TL;DR

1. **Спека высокого качества для этого этапа roadmap.** Видна серьёзная проработка: Strategy pattern поверх `Protocol`, три дисциплинированных исключения (Startup / Unavailable / HOLD-REJECT как значения), degraded mode с reconnect, vendoring discipline с `ARBITER_VENDOR_COMMIT`, явный mode-1 / mode-2 split. Acceptance criteria измеримые и byte-level. Можно идти в имплементацию.
2. **Самая серьёзная архитектурная дилемма — блокировка retry при недоступности arbiter в advisory-режиме** (H1). Сейчас предлагается «задача висит в FAILED, пока outcome не доставлен». В advisory это семантический конфликт: arbiter не влияет на маршрутизацию, но блокирует очередь. Нужен per-config флаг или двухрежимное поведение.
3. **Контракт `task_costs.attempt` ↔ `retry_count` нигде явно не зафиксирован** (H3). `_build_outcome` читает `SUM(tokens) WHERE attempt = retry_count + 1`, но cost-parser и terminal handler работают асинхронно — возможны заниженные или нулевые токены в outcome.
4. **Есть race между `update_task_status(FAILED)` и `update_task_status(READY)`** на окне ожидания `report_outcome` (H2). Окно растягивается с микросекунд до сотен миллисекунд; произвольные внешние транзиции (dashboard API, manual approve) могут вклиниться.
5. **Мелкие неточности в спеке:** `${VAR:-default}` — этот синтаксис отсутствует в `maestro/config.py:63` (H4); `AgentType.AUTO` смешивает sentinel и реального агента в одном enum (M6); `_reconstruct_outcome` не описывает маппинг `TaskStatus → TaskOutcomeStatus` (M7).
6. **Альтернатива к обсуждению:** outbox pattern для outcome delivery. Для R-03 избыточен, но для R-03b (mode 2 с одновременными задачами в worktree-ах) — окупается.

---

## Что сделано хорошо (фиксирую, чтобы не утерять при итерациях)

- **Protocol + две реализации вместо ABC.** `StaticRouting` как фолбэк и дефолт — OSS-путь не меняется, это подкреплено в acceptance criteria «behaves byte-identical». Золотой стандарт обратной совместимости.
- **Три дисциплинированных типа ошибок.** `ArbiterStartupError` (fail-fast при старте) / `ArbiterUnavailable` (runtime degraded) / HOLD-REJECT как значения `RouteDecision`. Чётко различает «invariant violation», «временно недоступно» и «нормальное бизнес-решение». Именно эта разбивка часто путается в интеграционных слоях.
- **Advisory-семантика инкапсулирована в `ArbiterRouting.route`, не в scheduler.** Scheduler остаётся mode-agnostic. Переключение advisory ↔ authoritative тривиально (один флаг), A/B-тестирование реального авторитетного режима становится возможным без переписывания scheduler-а.
- **`update_task_routing` перед `spawn`.** Правильный порядок записи: при крахе сразу после persist recovery сможет сопоставить outcome с `decision_id`.
- **Bounded re-attempt pass (≤5 rows/tick).** Защита от starvation планирования накопленными outcome-ами — важно для 1000+ задач за запуск.
- **Отделённый `arbiter_errors.py` от вендорнутого `arbiter_client.py`.** Тесты могут импортить `ArbiterUnavailable` без подтягивания вендорной транзитивки. Хороший layering, особенно учитывая R-14 (экстракция в PyPI).
- **Vendoring discipline.** Пин `ARBITER_VENDOR_COMMIT = "861534e"`, version-check в handshake, явный список «адаптировать / не адаптировать». Это то, чего обычно не хватает в вендорных копиях.
- **Unknown `chosen_agent` → HOLD, а не invariant violation.** Config drift (arbiter обновился, знает про новых агентов) не блокирует, а откладывает. Правильная деградация.
- **Frozen `RouteDecision`.** Целый класс багов с мутацией DTO между layer-ами отсекается бесплатно.
- **Timeout → HOLD, не FAILED и не Unavailable.** Гипотеза «arbiter up но медленный» корректно отделена от «arbiter лежит».

---

## Проблемы и риски

### 🔴 High

#### H1. Retry-gating в advisory-режиме — семантический конфликт

**Где:** стр. 184-187, 229, 457, 516 спецификации; сравнение с `maestro/scheduler.py:748-807`.

Спека предписывает: «task stays FAILED until `report_outcome` delivered successfully, then transitions to READY». Это **корректно в authoritative-режиме** (outcome — обучающий сигнал для решения о следующем маршруте; без него arbiter деградирует).

**Но в advisory-режиме** (стр. 308: `agent_type=explicit` + `mode=advisory` → `chosen_agent` игнорируется) arbiter **не участвует в маршрутизации**. Почему тогда потеря learning-сигнала должна блокировать retry? Следствие:

- Arbiter уходит в длительный down (например, crash-loop из-за некорректного конфига).
- Все задачи после первой неудачи висят в `FAILED` с `arbiter_outcome_reported_at IS NULL`.
- `_all_tasks_complete()` (scheduler.py:870-886) считает FAILED как не-терминальное → scheduler крутится без прогресса.
- Downstream-задачи, зависящие от FAILED-задач, стоят до ручного вмешательства.

В mode `advisory` это выглядит как «arbiter решил проблемы маршрутизации никак — но парализовал очередь». Нарушается документированное обещание «opt-in, OSS path unchanged».

**Альтернативы:**
- **A (предпочтительно):** разное поведение per-mode:
  - `advisory` → outcome delivery best-effort, не блокирует `FAILED → READY`.
  - `authoritative` → строгий gating, как в текущей спеке.
- **B:** отдельный флаг `arbiter.strict_outcome_delivery: bool = False` с явной документацией. Дефолт OFF снимает риск регрессии, OPT-IN для продвинутых кейсов.
- **C (текущий подход):** оставить строгий gating в обоих режимах + добавить в спеку явный раздел «почему выбираем согласованность в ущерб ликвидности очереди» и runbook «что делать при длительном arbiter down».

Я бы выбрал **A**: это единственный вариант, где acceptance criterion «byte-identical OSS path» остаётся честным в случае **первого отказа arbiter** в advisory.

#### H2. Окно race между `FAILED` и `READY` расширяется с мкс до сотен мс

**Где:** `maestro/scheduler.py:770-783`, сравнение со стр. 184-187 спеки.

Текущий код:
```python
await self._db.update_task_status(task_id, TaskStatus.FAILED, retry_count=...)
await self._db.update_task_status(task_id, TaskStatus.READY, expected_status=TaskStatus.FAILED)
```
Окно между двумя update-ами — микросекунды. С R-03 между ними вставится `routing.report_outcome(...)` с `timeout_ms=100` + сетевой jitter: в p95 это 200-500мс.

`expected_status=FAILED` защищает от конкурентного scheduler-loop-а, но **не** от внешних путей: dashboard REST API, CLI-команды `maestro approve` / `maestro retry`, ручные манипуляции DB во время отладки. Эти пути могут переписать `FAILED → NEEDS_REVIEW` внутри окна, и последующий `update(READY, expected=FAILED)` молча не применится (или бросит на `expected_status` — зависит от реализации).

**Предложение:** единая SQL-транзакция для «mark outcome reported + clear arbiter fields + transition to READY»:
```sql
UPDATE tasks
SET status='ready',
    assigned_to=NULL,
    arbiter_decision_id=NULL,
    arbiter_route_reason=NULL,
    arbiter_outcome_reported_at=?
WHERE id=? AND status='failed' AND arbiter_decision_id=?
```
Если `rowcount=0` — транзиция не состоялась (статус изменился извне), логируем и **не** повторяем, иначе получится race с пользовательским `abandon`.

#### H3. `_build_outcome` не синхронизирован с cost-parser-ом

**Где:** стр. 190-192 спеки, `maestro/cost_tracker.py`.

Спека: «`tokens_used = SUM(input_tokens + output_tokens) FROM task_costs WHERE task_id=? AND attempt=retry_count+1`».

Две проблемы:

1. **`retry_count + 1` — правильный ли `attempt`?** На момент terminal handler-а: `task.retry_count = X`. `_handle_task_failure` вскоре **инкрементирует** до `X+1` и пишет в БД. Какой `attempt` попадёт в новую запись `task_costs`? Это нужно проверить по `cost_tracker.py`; если cost_tracker использует `task.retry_count` на момент парсинга (который может быть после инкремента, если log-tail-er async) — получим `attempt=X+1` записи для текущего прогона, **не совпадающие** с тем, что сейчас агрегирует `_build_outcome`.

2. **Race с log-parser-ом.** В Maestro cost записывается через парсинг выхлопа агента — это происходит либо синхронно при `poll()` завершения процесса, либо лениво отдельным collector-ом. Если `_build_outcome` вызвается до того, как последние строки лога спарсены, `SUM(...)` вернёт частичные или нулевые токены. Arbiter получит обучающий сигнал «задача не стоила ничего» — это корраптирует его decision tree.

**Предложение:**
- Явный контракт в спеке: «Все `task_costs` для attempt N должны быть записаны **до** вызова `report_outcome` для этого attempt».
- Либо сделать `tokens_used` / `cost_usd` в `TaskOutcome` опциональными (`None` допустим) и передавать `None`, если cost-parse не завершился — пусть arbiter сам разбирается.
- Принудительный `flush` cost-parser-а как шаг в terminal handler перед `_build_outcome`.

#### H4. В спеке заявлен `${VAR:-default}`, но парсер его не поддерживает

**Где:** стр. 296 спеки, `maestro/config.py:63`.

Спека: «Env-var substitution uses the existing `${VAR:-default}` mechanism in `config.py`».

Реальный regex: `\$\{([A-Za-z_][A-Za-z0-9_]*)\}` — **никакого `:-default` нет**. Примеры `${ARBITER_BIN}` в YAML работают, но `${ARBITER_BIN:-/usr/local/bin/arbiter-mcp}` развернётся некорректно.

Это не просто текстовая ошибка: пользователи, ориентирующиеся на пример YAML, попробуют default-syntax, получат либо tail-match-fail парсера, либо (если regex не сматчится) — буквальную строку `${ARBITER_BIN:-...}` в `binary_path`. Дальше `validate_required_when_enabled` (стр. 258-266) пропустит это, и упадёт только `ArbiterStartupError` по несуществующему файлу — диагностировать будет сложно.

**Предложение (любое из):**
- Убрать из спеки упоминание default-syntax, пока он не добавлен.
- Добавить поддержку `${VAR:-default}` в `resolve_env_vars` отдельным мини-R (10 строк + тест).
- Если поддержки не будет — проверить в `ArbiterConfig.validate_required_when_enabled`, что `binary_path` не содержит `${`, и выбросить понятную ошибку.

---

### 🟡 Medium

#### M1. Миграции без `schema_version` — надвигается техдолг

**Где:** `maestro/database.py:341-368`, спека стр. 216-222.

`_migrate_tasks_arbiter_columns` — отдельный метод с жёстко прошитым списком. Спека предлагает ещё один — `_migrate_tasks_arbiter_routing`. Через N релизов их будет 5-7, каждый идемпотентный по `PRAGMA table_info`, но:
- Порядок применения нигде не зафиксирован (хотя сейчас он коммутативен — не факт, что останется).
- Нет journal-а «что применено, когда» — диагностика миграционных багов без него мучительна.
- Ручные правки через `sqlite3` CLI не учитываются.

**Предложение:** ввести сейчас (пока миграций 2) минимальную таблицу:
```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL
);
```
И линейный список migrations-функций. Это не R-03 scope (S-effort), но откладывать до 5 миграций — больнее.

#### M2. Arbiter должен различать `chosen_agent` vs `agent_used` — не зафиксировано в контракте

**Где:** стр. 194 спеки.

В advisory-режиме `agent_used ≠ chosen_agent` — это **основной learning-сигнал**: «модель думала codex_cli, мы сделали claude_code, вот результат». Arbiter обязан:
- Принимать такой outcome без ошибки.
- Использовать `agent_used` как actual label, не `chosen_agent`.

В спеке R-03 это не зафиксировано как требование к arbiter-стороне. Если arbiter наивно берёт `chosen_agent` из своего же decision (не передаём — декодирует из `decision_id`) и использует его как label — advisory-режим не даёт правильного обучения.

**Предложение:** добавить в спеку отдельный раздел «Contract to Arbiter (cross-repo requirement)» с пунктами:
- `report_outcome` MUST accept `agent_used` и использовать его как actual label.
- `report_outcome` SHOULD be idempotent by `decision_id` (см. M4).
- `timeout_ms` MUST быть honored server-side (server-side cancellation at ≤timeout).

Это позволит arbiter-команде работать параллельно с точной знаей ожиданий.

#### M3. `timeout_ms: 100` — оптимистичный дефолт

**Где:** стр. 254 спеки.

MCP поверх stdio + JSON-RPC serialization + feature extraction + decision-tree inference в Rust: 100мс реалистичен для hot-path, но для cold-path (первый вызов после idle / после подъёма subprocess / GC в Rust allocator) легко 300-500мс. Под нагрузкой 30+ задач tail latency ещё выше.

Мапка: timeout → HOLD → next tick retry. При `poll_interval=1.0` каждый tail-timeout добавляет +1с к latency задачи. На 30+ задач это заметный caskade.

**Предложение:**
- Дефолт **500мс**, документация «100мс только на идеальном steady-state, для стабильной работы 500мс».
- Задача для R-05: измерить p50/p95 `route_task` на реальном arbiter, зафиксировать SLO.

#### M4. Идемпотентность `report_outcome` — молчаливое требование

**Где:** стр. 354-378 спеки.

Recovery-flow: Maestro упал **во время** recovery между `report_outcome(...)` и `mark_outcome_reported(...)`. Следующий запуск увидит ту же строку с `arbiter_outcome_reported_at IS NULL` и отправит outcome повторно. Если arbiter наивно инкрементирует counter-ы (success++/failure++) — double counting исказит его метрики и обучение.

Это не R-03 багa, но **требование к arbiter**, которое нужно явно зафиксировать.

**Предложение:** в спеку — «Arbiter MUST be idempotent on `report_outcome` by `decision_id`. R-03 assumes this; if violated, R-03 recovery semantics break». Если arbiter-сторона этого не делает — это блокер для R-05/продакшена.

#### M5. `AgentType.AUTO` смешивает sentinel и реального агента

**Где:** стр. 298 спеки, `maestro/models.py:72-78`.

Текущий `AgentType`: `CLAUDE_CODE`, `CODEX`, `AIDER`, `ANNOUNCE`. Используется как ключ в `scheduler._spawners: dict[str, SpawnerProtocol]` (scheduler.py:437).

Добавление `AUTO = "auto"`:
- Тест `spawners.get(AgentType.AUTO.value)` → None → `SchedulerError`, если arbiter не поспел переписать `assigned_to`. Invariant «AUTO всегда переписан routing-ом до lookup» держится только пока код делает проход через routing; любой путь в обход (recovery, manual retry) рушит инвариант.
- `task_costs.agent_type = "auto"` — формально валидно, семантически мусор.
- Grep по всему коду `== AgentType.CLAUDE_CODE` / `== AgentType.CODEX` может пропустить `AUTO` и работать молча некорректно.

**Альтернативы:**
- **A:** Отдельное поле `TaskConfig.agent_preference: AgentType | None = None` (None = auto). `Task.from_config` ставит `task.agent_type` в `None` или использует default. Более типо-строго.
- **B:** Оставить `AgentType` как есть, завести отдельный `Literal["auto"] | AgentType` для `TaskConfig.agent_type`. Runtime-тип `Task.agent_type: AgentType` (после routing).
- **C (текущее)** — приемлемо, если в коде явно добавить invariant-check: «agent_type=AUTO никогда не доходит до spawner-lookup».

Я бы выбрал **A**: самое явное разделение слоёв, минимум неявных инвариантов.

#### M6. `_reconstruct_outcome(task)` не описывает маппинг статусов

**Где:** стр. 371 спеки.

На terminal задаче без `reported_at` вызывается `_reconstruct_outcome`. Но какой `TaskOutcomeStatus` ставить для каждого `TaskStatus`? Спека перечисляет только `INTERRUPTED` для RUNNING.

Необходимо явно:
```python
DONE         → SUCCESS
ABANDONED    → CANCELLED
FAILED       → FAILURE
NEEDS_REVIEW → FAILURE  # max retries exhausted
VALIDATING   → INTERRUPTED  # crashed mid-validation
RUNNING      → INTERRUPTED  # crashed mid-run (как в спеке)
READY/PENDING/AWAITING_APPROVAL → ???  # these shouldn't have decision_id
```

**Предложение:** вынести в спеку явную таблицу маппинга. Последние три — invariant violation (`decision_id IS NOT NULL` только для RUNNING/VALIDATING/terminal), логировать и пропускать.

#### M7. HOLD events — нет throttle, log может пухнуть

**Где:** стр. 388-394 спеки, `arbiter.route.hold`.

Спека правильно требует throttle для `arbiter.unavailable` (стр. 393, «first time only, not per-call»). Но `arbiter.route.hold` пишется **каждый tick**, пока задача держится в HOLD. При degraded arbiter с default-HOLD — `poll_interval=1s` × N задач × часы ожидания = мегабайты event-log-а.

**Предложение:** такой же throttle: первый HOLD для задачи логируется, последующие с тем же `reason` — suppressed до смены reason или выхода из HOLD. В памяти: `dict[task_id, (reason, count, first_seen)]`.

---

### 🟢 Low

#### L1. `error_code = первая строка error_message`

**Где:** стр. 193 спеки.

Для bugfix-классификации arbiter-а полезна либо последняя строка stacktrace (обычно `ValueError: foo`), либо вся первая строка `error_message`. Но при `error_message = "Task timed out after 30 minutes\nLast stdout: ..."` первая строка обрежет важное. Это не баг, но стоит зафиксировать: брать ли «first line» или «first substring matching pattern».

#### L2. `reconnect_interval_s: 60` — без jitter

Один Maestro — не проблема. При multi-Maestro (dogfood-пайплайны, CI) синхронизированный reconnect-шторм. Стандарт: ±20% jitter. Учитывая, что R-03 ориентирован на single-instance — решение не критичное.

#### L3. `ARBITER_MCP_REQUIRED_VERSION` — strict equals

**Где:** стр. 416 спеки.

Любой патч arbiter (0.1.1) сломает Maestro. В pre-1.0 это защита от drift, но стоит задокументировать upgrade policy: «bump requires re-vendor» → явный шаг в release-чеклисте arbiter-а.

#### L4. `_degraded: bool` теряет `since`

**Где:** стр. 93 спеки.

Событие `arbiter.reconnected(downtime_s=X)` — откуда `X`? Спека упоминает `_last_reconnect_attempt`, но это момент **попытки** reconnect, не момент падения. Нужен `_degraded_since: datetime | None`, выставляемый при первом Unavailable.

#### L5. `aclose()` — drain inflight calls?

**Где:** стр. 99 спеки.

«Close subprocess cleanly (drain stdin, SIGTERM, wait)». Если в shutdown есть inflight `route_task` — они получат broken pipe и корректно упадут в `ArbiterUnavailable` → static fallback. Вопрос: ждёт ли `aclose` их завершения, или рвёт? Для shutdown на SIGINT это ОК (быстрее = лучше), но документировать нужно.

---

## Архитектурная альтернатива — Outbox pattern для outcome delivery

Текущий подход (inline + re-attempt pass) работает, но сплетает две ответственности:
- Обновление state задачи.
- Доставка learning-сигнала arbiter-у.

**Альтернатива:** отдельная таблица `arbiter_outcomes_outbox`:
```sql
CREATE TABLE arbiter_outcomes_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    delivered_at TIMESTAMP,
    attempts INTEGER DEFAULT 0,
    last_error TEXT
);
```

Terminal handler делает единую транзакцию: обновляет `tasks` + вставляет row в outbox (**at-least-once гарантировано**). Отдельный worker (может быть тот же main_loop, отдельная функция) забирает `delivered_at IS NULL` и доставляет.

**Плюсы:**
- Retry-gating становится опцией per-config: «ждать `delivered_at` перед FAILED → READY» — отдельное решение, не вшито в terminal handler.
- Идемпотентность через PK outbox-а + `decision_id` — проще для дебага.
- Recovery тривиален: просто «выбрать `delivered_at IS NULL`», без разборок по `TaskStatus`.
- Для mode-2 (R-03b) масштабируется без изменений.

**Минусы:**
- +1 таблица, +1 worker-loop.
- Для R-03 (один процесс, одна БД, одна очередь) overkill.

**Вердикт:** для R-03 — оставить inline. Упомянуть в спеке как **планируемый рефакторинг к R-03b**, чтобы retry-gating не замуровывать в scheduler hot-path.

---

## Открытые вопросы к автору

1. **H1 — retry-gating в advisory:** блокировать `FAILED → READY` при arbiter down в advisory-режиме — это осознанное решение или упущение? Если осознанное — где runbook «что делать при длительном arbiter-down»?
2. **H3 — cost-tracker contract:** когда гарантированно записан последний `task_costs` row для attempt-а? До `poll() != None` в `_monitor_running_tasks` или после? Это определяет, нужен ли в terminal handler flush.
3. **H2 — транзакционность:** приемлемо ли требовать одну SQL-транзакцию для «mark reported + clear fields + transition READY»? Или проще смириться с race-окном и документировать?
4. **M2 — cross-repo contract:** какой документ будет фиксировать требования к arbiter-стороне (idempotency `report_outcome`, honoring timeout, `agent_used` как actual label)? Спека R-03 или отдельный ADR?
5. **M5 — `AgentType.AUTO`:** не хочется ли развести `TaskConfig.agent_preference` (`None = auto`) и runtime `Task.agent_type` (всегда конкретный)? Текущий подход работает, но обрастает invariant-check-ами.
6. **M7 — HOLD throttle:** запланировано или нужно поднимать отдельно? Без этого event log при длительном degraded будет мусорным.
7. **Outbox pattern:** стоит ли в R-03 заложить будущий outbox (хотя бы через интерфейс routing-стратегии), чтобы R-03b не пришлось переделывать terminal handler-ы?
8. **Schema migrations journal (M1):** пускать ли вместе с R-03, или отдельным мини-R до того, как миграций станет 5+?
9. **`${VAR:-default}` (H4):** пример в спеке менять или парсер расширять? От этого зависит UX первой интеграции.

---

## Summary по приоритетам

| Impact | Кол-во | Пункты |
| ------ | ------ | ------ |
| 🔴 High | 4 | H1 (retry-gating в advisory), H2 (race с внешними транзициями), H3 (cost-tracker contract), H4 (`${VAR:-default}` в спеке) |
| 🟡 Medium | 7 | M1 (schema migrations), M2 (cross-repo contract), M3 (timeout=100мс), M4 (идемпотентность), M5 (AgentType.AUTO), M6 (status mapping), M7 (HOLD throttle) |
| 🟢 Low | 5 | L1 (error_code parsing), L2 (jitter), L3 (version pin policy), L4 (`_degraded_since`), L5 (`aclose` drain) |

**Рекомендация:** H1, H3, H4 разобрать **до старта имплементации** — это решения формата «одна строка в YAML → поведение всей системы». H2 можно зафиксировать в комментарии к коду и адресовать первым багом. M-пункты — в rолонг имплементации. L-пункты — чистый follow-up.

Спека готова к переходу в имплементацию после итерации по H-пунктам и принятия решения по cross-repo contract-у к arbiter-стороне (M2).
