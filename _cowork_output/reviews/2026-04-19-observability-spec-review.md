# Ревью спеки: Cross-Project Observability Design

**Дата:** 2026-04-19
**Ревьюер:** Claude (technical program manager / systems architect)
**Источник:** `2026-04-19-cross-project-observability-design.md`
**Скоуп:** Maestro, spec-runner, arbiter, atp-platform, proctor-a

---

## TL;DR

1. **Проблема и скоуп сформулированы хорошо**: аудит конкретный, non-goals явно перечислены, milestones с критериями приёмки. Это редкость — серьёзный плюс.
2. **Главное архитектурное сомнение: зачем свой протокол propagation, если есть W3C `traceparent`?** Использование `ORCHESTRA_TRACE_ID`/`ORCHESTRA_PARENT_SPAN_ID` вместо стандартного `traceparent` ломает автоматическую совместимость с любым OTel-инструментом в экосистеме (в первую очередь — с уже работающим OTel в ATP).
3. **Главный технический баг**: несколько параллельных процессов одного проекта будут конкурентно писать в один файл `<project>.jsonl`. Атомарность записи гарантирована только до PIPE_BUF (обычно 4 КБ); запись со стек-трейсом легко это пробьёт и даст перемешанные строки JSON. Спека это не адресует.
4. **Альтернатива, которую стоит серьёзно рассмотреть**: `opentelemetry-sdk` + `ConsoleLogExporter`/`OTLPFileExporter` вместо собственного `obs.py`. Это тот же «zero infra», но без 200 строк кастомного кода × 3 вендор-копии + параллельная Rust-реализация. ATP интегрируется бесплатно (адаптер не нужен).
5. **Оценка 7–10 дней до M2 оптимистична в ~2 раза**. Байт-точный golden-fixture между Rust `tracing` и Python structlog + ATP-адаптер с «301 тест зелёный» — это отдельная история.

**Вердикт**: спека крепкая по структуре и дисциплине, но фундаментальное решение «свой контракт vs OTel» стоит пересмотреть до кодирования. Если всё же идём по кастомному пути — исправить пункты P1–P3 ниже перед M1.

---

## Что сделано хорошо

| Аспект | Оценка |
|---|---|
| Проблема подкреплена аудитом (кто что использует) | ✅ |
| Non-goals явно перечислены (метрики, OTLP, Jaeger, sampling) | ✅ |
| ULID как `run_id` с байт-совместимостью с W3C `trace_id` | ✅ (с оговоркой, см. ниже) |
| 16-hex `span_id` = W3C 8 байт | ✅ |
| Вендоринг как паттерн: опирается на реальный прецедент `coordination/arbiter_client.py` в Maestro | ✅ подтверждено |
| ATP не форсируется к вендорингу, только адаптер — уважение существующей OTel-инфры | ✅ |
| Milestones (M1/M2) с конкретными критериями приёмки | ✅ |
| Тесты через shared JSON Schema + golden fixtures | ✅ правильный уровень строгости |
| Риски + митигации в отдельной таблице | ✅ |

---

## Основные замечания (ранжированные по важности)

### P1. Почему не W3C `traceparent`?

Спека определяет свой протокол propagation через `ORCHESTRA_TRACE_ID` + `ORCHESTRA_PARENT_SPAN_ID`. Но W3C Trace Context уже стандартизирован ровно для этого:

```
traceparent: 00-<trace_id(32 hex)>-<span_id(16 hex)>-<flags(2 hex)>
```

Стоимость перехода на `traceparent` — нулевая (5 строк парсера). Выгода:

- **ATP уже на OTel** — любой OTel-совместимый компонент подхватит `traceparent` автоматически, без отдельного адаптера. Секция 5.2 («ATP adapter reads `ORCHESTRA_TRACE_ID` and aliases to `_correlation_id`») превращается в ненужный слой.
- **Любая будущая OTel-instrumented библиотека** (requests, httpx, fastapi-instrumentor) поднимет trace context без дописывания.
- **Rust `tracing-opentelemetry`** — стандартный мост, принимает `traceparent` из env без кастомного Layer.
- **Совместимость с Jaeger/Tempo/etc в v2** — бесплатно, без миграции контракта.

Спека в разделе 4.2 уже аргументирует «ULID → W3C trace_id — mechanical mapping». Если mapping и так механический — сделайте его сразу. Единственная причина не делать — опасение, что `traceparent` «непривычен». Он короче, чем описываемая сейчас схема с двумя env-переменными.

**Предложение**: заменить `ORCHESTRA_TRACE_ID` + `ORCHESTRA_PARENT_SPAN_ID` на единственный `TRACEPARENT` (стандартное имя в OTel). Оставить `ORCHESTRA_LOG_DIR`/`LEVEL`/`FORMAT` — это действительно локальные настройки, не propagation.

### P2. ULID как trace_id — не совсем «no information loss»

Раздел 4.2 утверждает «Conversion to OTel is `bytes.hex()`, no information loss». Формально верно, но:

- W3C требует, чтобы `trace_id` был 16 случайных байт. ULID — это **48-бит timestamp + 80-бит randomness**. В строгой OTel-реализации валидный trace_id не должен содержать монотонный префикс.
- Практически почти все backend'ы (Tempo, Jaeger, Honeycomb) примут такой trace_id. Но если соберётесь экспортировать в системы с энтропийной валидацией (редко, но бывает — некоторые managed APM) — упрётесь.
- Компромисс ULID (человеко-сортируемость) vs random 16-hex (чистая OTel-совместимость) сделан в пользу первого. Это ок, но скажите явно: «мы отступаем от OTel-рекомендации random-trace_id ради `jq`-дружелюбия».

**Предложение**: либо честно зафиксировать trade-off в тексте, либо сгенерировать trace_id как случайные 16 байт, а ULID использовать отдельным полем `pipeline_id` для UX.

### P3. Конкурентная запись в общий файл = перемешанные JSON-строки

Раздел 4.4: «Each process writes to `$ORCHESTRA_LOG_DIR/<project>.jsonl` (append mode)».

Под Maestro запускается N параллельных spec-runner'ов в разных worktree'ах (подтверждено — DAG-оркестратор с параллелизмом). Все они пишут append в один `spec-runner.jsonl`.

POSIX гарантирует атомарность write() только до **PIPE_BUF** — 4096 байт на Linux. JSON-строка с `error.stack` (полный traceback Python или `Error: ...` в Rust) легко пробивает 4 КБ. Результат — перемешанные строки, невалидный JSONL, jq падает.

**Три варианта решения**:

| Вариант | Плюсы | Минусы |
|---|---|---|
| **A.** Файл на процесс: `<project>-<pid>-<span_id>.jsonl` | Без блокировок, без кастомной логики | Больше файлов; `merge-logs` должен их все подхватить (glob) |
| **B.** `flock()` на каждом write | Один файл на проект | Контеншн при высокой нагрузке; спец-код для обеих рантаймов |
| **C.** Писать в stdout, parent ловит и пишет в файл | Естественно для subprocess-модели; централизованная запись | Parent становится bottleneck; lost logs при крэше parent'а |

**Предложение**: вариант A. Самый простой, мерж всё равно батчевый в конце.

### P4. Альтернатива на уровне подхода — OTel-SDK + file exporter

Спека обосновывает свой контракт так: «Rust нужен свой код в любом случае → унификация должна быть на уровне формата, а не кода». Это логично **при данной постановке**. Но постановка стоит оспорить.

Что даёт `opentelemetry-sdk` с `ConsoleLogExporter` или file exporter:

- JSON-формат логов/спанов — стандартизирован OTel Logs Data Model. Не надо писать схему самим.
- `structlog` → `OTLPLogExporter(file://)` — 1 процессор в цепочке.
- Rust: `tracing-subscriber` + `tracing-opentelemetry` + `opentelemetry-stdout` — общий путь, живая экосистема.
- Subprocess propagation — через `TRACEPARENT` из коробки.
- ATP не меняется вообще. Его `TracerProvider` уже экспортирует OTel — просто добавить file exporter параллельно OTLP.
- Upgrade до v2 — **поменять exporter**, не код. `OTLPFileExporter → OTLPGrpcExporter`. 5 строк.

**Оценка стоимости**:

| Параметр | Кастомный `obs.py` | OTel-SDK + file exporter |
|---|---|---|
| Новый код | ~200 LOC Python + ~150 LOC Rust + schema + golden fixtures | ~30 LOC Python wrapper + ~30 LOC Rust setup |
| Внешние зависимости | `structlog` (уже есть) | `opentelemetry-sdk`, `opentelemetry-exporter-otlp` (~10 МБ в venv) |
| ATP интеграция | Адаптер 1 день | Не нужна — ATP уже пишет OTel |
| Rust интеграция | Параллельная реализация + byte-exact golden test | `tracing-opentelemetry` bridge — 5 строк |
| Freeze contract v1 | Да, с маркерами в каждой вендор-копии | Не нужен — контракт это OTel |
| Upgrade до v2 | Переписать экспорт во всех 4+1 местах | Сменить exporter URL |
| Риск дрейфа Rust vs Python | Реальный (формат времени, ID) | Нулевой — оба на OTel |
| Human-friendly в `jq` | Да (свой формат) | Да (OTel Logs Data Model — тоже JSON на строке) |

**Моё мнение**: OTel-путь короче и меньше обязательств. Единственный реальный аргумент за кастомный путь — если `structlog` + `obs.py` уже частично есть и хочется минимизировать shift. Но 200 строк Python + 150 строк Rust + golden fixtures + CI-чек дрейфа вендор-копий — это не минимум.

Кастомный `obs.py` оправдан, если: (а) не хочется тащить OTel в Rust, (б) нужна жёсткая гарантия zero-dep кроме structlog, (в) OTel Logs Data Model не подходит по каким-то полям. Пункты (а) и (б) — стилистические; (в) требует обоснования в спеке, которого нет.

### P5. Рассогласование Python ↔ Rust форматов — прогнозируемый источник боли

Если всё же оставляем кастомный контракт:

- **Формат времени**: Python `datetime.isoformat()` даёт `2026-04-19T14:23:11.482913+00:00` (либо `Z` через `.strftime`), Rust `chrono::Utc::now().to_rfc3339()` — `2026-04-19T14:23:11.482913912Z`. Наносекундная точность vs микро. Золотой fixture byte-exact **не пройдёт** без явной нормализации в обеих рантаймах.
- **Span ID в Rust**: `tracing` генерирует свой u64 span ID; если переопределять на 8 случайных байт — надо писать custom Layer, отключающий встроенный. В спеке сказано «custom Layer» — OK, но явно зафиксируйте «tracing native span ids disabled».
- **Order полей в JSON**: `serde_json` и `structlog.JSONRenderer` сериализуют словари в разном порядке. Golden fixture должен сравнивать **как parsed JSON**, не байтами. Спека в тесте делает `jsonschema.validate` на parsed — хорошо, но слова «match JSON Schema byte-for-byte» (строка 227) стоит вычистить, иначе это станет ложной целью.

### P6. Отсутствует redaction / PII policy

В `attrs` попадут аргументы задач (prompt'ы, код, иногда ключи). Raw-логи в `logs/<run_id>/*.jsonl` — это потенциальная помойка. Стоит:

- Добавить processor в structlog chain: `_drop_keys={"api_key","token","password",...}` (recursive).
- Или явно указать в спеке: «v1 предполагает dev-only, логи не публикуются, redaction — в v2».

Сейчас ни того ни другого нет. Это дыра с точки зрения безопасности, даже на дев-стенде.

### P7. Мерж только в конце + отсутствие стратегии при краше

Раздел 4.4: «Maestro at pipeline end runs `maestro merge-logs`». Если pipeline падает с SIGKILL / OOM — мержа нет, отлаживать нечего. Добавьте:

- `maestro merge-logs <run_id>` как **standalone CLI**, запускаемый вручную для незавершённых ранов.
- Или tail-and-merge демон (сложнее, не для v1).

Это одно предложение в документе, но сейчас его нет.

### P8. Ошибки и цепочки

Поле `error: {type, message, stack?}` — плоское. В Python 3.11+ есть `ExceptionGroup` и `__cause__`/`__context__`; в Rust — `source()` chain. Спека теряет всю цепочку. Для многопроцессной отладки причин сбоев это важно. Предложение: `error: {type, message, stack?, caused_by?: <error>}` (рекурсивно).

### P9. Event taxonomy — фритекст

`event: "task.started"` не валидируется enum'ом. `tasks.started`, `task.start`, `task_started` пролетят в прод. Как минимум — regex в схеме (`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`). Лучше — реестр допустимых event-ов per-project или общий префикс-словарь (`pipeline.*`, `task.*`, `spec.*`, `agent.*`, `arbiter.*`).

### P10. Оценка времени оптимистична

| Шаг | Заявлено | Реалистично |
|---|---|---|
| 1. Reference `obs.py` + тесты | 1–2 дня | 2–3 дня |
| 2. Vendor в Maestro + `child_env()` | 1–2 дня | 2 дня |
| 3a. Rust `obs.rs` с byte-exact golden | 2–3 дня | 4–5 дней (формат времени, span_id override, правка `tracing-subscriber` JSON layer) |
| 3b. Vendor в arbiter Python client | 1 день | 1 день |
| 4. Freeze + CI-check дрейфа вендор-копий во всех проектах | 0.5 дня | 1–2 дня (CI-инфра в 4 репо) |
| 5. ATP adapter, 301 тест зелёный | 1 день | 2–3 дня (изучить текущую цепочку processors, провести в processing chain, не сломать existing OTel) |
| 6. Merge + cookbook | 0.5 дня | 1 день |
| **Итого до M2** | **7–10 дней** | **13–17 дней** |

Не катастрофа, но стоит закладывать честно.

---

## Мелочи, которые легко поправить

- Раздел 4.3, таблица: `ORCHESTRA_LOG_FORMAT` default «json if `ORCHESTRA_TRACE_ID` is set, else console» — это скрытая магия, легко сюрпризит в тестах. Лучше всегда дефолтить в `json`; для локального запуска человек явно ставит `console`.
- Раздел 5.1: «structlog processor chain … TimeStamper(iso=True, utc=True)» — microsecond precision не дефолт, укажите явно: `TimeStamper(fmt="iso", utc=True, key="ts")` + кастомный processor для микросекунд.
- Раздел 4.4: упомянуть, что пустой `ORCHESTRA_LOG_DIR` в CI может привести к записи в `./logs/<run_id>/` относительно cwd subprocess'а, который у Maestro бывает worktree'ом. Фикс: Maestro всегда явно сетает абсолютный путь.
- Раздел 7, шаг 7 (proctor-a): если deferred — уберите из v1 плана, вынесите в отдельный backlog. Сейчас это создаёт впечатление, что оно «часть v1».
- Раздел 8, риски: добавить «Конкурентная запись в общий JSONL» (см. P3) и «Дрейф форматов времени Python/Rust» (см. P5).
- Раздел 9, deferred: упомянуть log rotation / retention — `logs/<run_id>/` накопится.

---

## Рекомендуемые действия

### Если решение по P1/P4 — остаёмся на кастомном контракте

1. **`_cowork_output/observability-contract/`**: зафиксировать trade-off «ULID vs W3C random» в `rationale.md` (P2); добавить regex для `event` (P9); расширить `error` каскадом (P8).
2. **spec-runner**: в `obs.py` — writer с файлом на процесс `<project>-<pid>.jsonl` (P3), redaction-processor (P6), явная микросекундная точность времени (P5).
3. **arbiter Rust**: явный override span_id в кастомном Layer, нормализация timestamp в naive UTC микросекунды (P5).
4. **Maestro**: `merge-logs` как standalone CLI, работает с незавершённым ран-каталогом (P7); абсолютный `ORCHESTRA_LOG_DIR` в env для детей.
5. **Оценка**: заложить 13–17 дней до M2 (P10).

### Если решение — переключиться на OTel-SDK (рекомендую рассмотреть)

1. Переписать раздел 3 «Approach»: контракт = OTel Logs Data Model + W3C trace context, экспорт в v1 — file exporter, в v2 — OTLP Collector.
2. Python: `opentelemetry-sdk` + `opentelemetry-exporter-otlp` + `LoggingInstrumentor` в каждом проекте. `structlog` остаётся, его processor пушит в OTel logger.
3. Rust: `tracing-opentelemetry` bridge в arbiter-core.
4. ATP: ничего не меняется, просто добавляется file exporter параллельно существующему OTLP (если он включён) или вместо.
5. Subprocess propagation — `TRACEPARENT` (стандарт).
6. Golden fixtures — против OTel Logs Data Model schema (есть публичная).

Оценка по этому пути: **4–7 дней до M2**, потому что Rust/Python дрейф ≈0, адаптер ATP не нужен, freeze-logic не нужна.

---

## Сводка решения

| Пункт | Текущее решение | Моя рекомендация |
|---|---|---|
| Propagation | `ORCHESTRA_TRACE_ID` + `ORCHESTRA_PARENT_SPAN_ID` | W3C `TRACEPARENT` |
| `run_id` формат | ULID | Либо ULID + trade-off зафиксирован, либо 16-hex random + отдельный human `pipeline_id` |
| Унификация кода | Вендоринг `obs.py` + параллельный Rust | OTel-SDK с file exporter (минус 350 LOC и golden-fixture дрейф) |
| Sink | Один файл на проект | Файл на процесс `<project>-<pid>.jsonl` |
| Merge | В конце pipeline | + standalone CLI для незавершённых ранов |
| Ошибки | Плоский объект | Каскад с `caused_by` |
| Event names | Фритекст | Regex + словарь префиксов |
| PII / secrets | Не адресовано | Redaction processor обязателен |
| Оценка | 7–10 дней | 13–17 (кастом) или 4–7 (OTel) |

---

## Итог

Спека дисциплинированная и реализуемая. Но две ключевые развилки — **W3C `traceparent` vs свой env-протокол** (P1) и **OTel-SDK vs кастомный `obs.py`** (P4) — стоит закрыть до того, как начнёт писаться код, иначе v1→v2 миграция будет не «сменить exporter», а «переписать 4 проекта».

Если сохранять текущий курс, то перед M1 критично закрыть P3 (конкурентная запись) и P5 (Rust/Python дрейф): без этого golden-fixture тесты превратятся во фликающие и команда начнёт их скипать.
