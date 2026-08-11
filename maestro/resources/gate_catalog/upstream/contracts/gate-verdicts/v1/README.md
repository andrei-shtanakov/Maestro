# Contract `gate-verdicts/v1` — verdicts file of a gate-check run

> Канон живёт здесь (steward — producer и владелец схемы). Потребители (dispatcher
> collector; позже Maestro gates-in-DAG) **вендорят пиненую копию** каталога и держат
> две раздельные гарантии: copy-integrity (offline PR-гейт) и upstream-drift
> (scheduled-наблюдение) — не одно `in_sync`.
> Governance-бандл capability: `workstreams/WS-005-gate-verdicts/spec/` (WS-A).

## Файл

`<bundle-repo>/.steward/gate_verdicts.jsonl` — gitignored локальный артефакт
(ARCH-D1). Перезаписывается **целиком** каждым прогоном `gate-check --emit-verdicts`
(ARCH-D4); атомарная запись через temp+rename. Прогон с конфигурационной ошибкой
(exit 2) файла **не пишет**: без валидного прогона нет и вердиктов, а оставшийся
старый файл читатель сам классифицирует как stale по `source_commit`.

## Записи (JSONL, по одной на строку)

Строка 1 — обязательно `header`; дальше `artifact`- и `finding`-записи.
`kind` обязателен в каждой записи. Нормативная схема — `SCHEMA.json` (draft 2020-12,
`additionalProperties: false`: producer строг, дрейф ловится на фикстурах).

- **header** — provenance прогона: `schema_version` (const `"1"`), `source_commit`
  (40-hex HEAD), `dirty` (грязное дерево ⇒ читатель обязан классифицировать как
  stale-grade — fail-closed покрывает сам прибор), `generated_at`, `profile`, `bundle`.
- **artifact** — инвентарь бандла: `path`, `node_id|null`, `status`
  (зеркало frontmatter), `owner_roles` — **DEC-007 слаги без `@`** (список до
  миграции на singular).
- **finding** — факт нарушения: `gate_id` (`GC-*`), `verdict` (`fail`|`warn` —
  маппинг severity error→fail), `artifact`, `message`. `obligation` эмитится
  producer'ом из каталога `profiles/gate-catalog.yaml` для каждого active
  finding; словарь — `quality | approval`. `GC-APPROVAL-MISSING` (AP-5,
  2026-08-08, каталог v2) — первый и пока единственный активный
  `approval`-гейт: release-stage merge-evidence policy; остальные active-гейты
  каталога — `quality`. `tier`, `phase`, `risk_model_version`, `waiver_ref`
  остаются reserved (риск-интеграция).

**Steward пишет только факты** — findings и header. Классификацию
(pass / blocked / no-data / unreadable / stale / unresolvable) вычисляет читатель
из файла + git-фактов (ARCH-D2); файл о своей свежести не свидетельствует.

## Пространства имён `gate_id` и границы `obligation` / `enforcement`

Нормативно для любого писателя вердиктов, включая тех, кто ведёт **собственный**
формат записи со своим schema-дискриминатором (напр. `maestro.gate-verdict-record/v1`).
Решение владельца 2026-08-12, steward#62 / maestro#160; машиночитаемое зеркало —
`gate_id_namespaces` и `obligation_reserved_tokens` в `profiles/gate-catalog.yaml`.

1. **`GC-` — зарезервированное закрытое пространство.** Минтит только каталог
   steward (`^GC-[A-Z0-9]+(-[A-Z0-9]+)*$`). Неизвестный своей пиненой копии
   каталога `GC-*` писатель обязан отвергать fail-closed — не деградировать в pass
   и не досочинять запись. В самой схеме `gate-verdicts/v1` это уже жёстко:
   `gate_id` там `^GC-[A-Z0-9-]+$`, producer-специфичному id в steward-файле места
   нет и не появится.
2. **Producer-specific id разрешены** вне этого пространства в форме
   `<namespace>.<name>` (`^[a-z][a-z0-9-]*\.[a-z0-9_]+(\.[a-z0-9_]+)*$`). Строчная
   первая буква — не косметика: она делает два пространства непересекающимися по
   регистру, без сверки таблиц. Владелец такого id — **писатель записи**, steward
   не определяет ни его семантику, ни жизненный цикл. Ведущий сегмент называет
   *инструмент-источник*, а не владельца: `steward.risk_classify_*`, написанный
   Maestro, — id Maestro. Если steward когда-нибудь начнёт гейтить риск-классификацию
   сам, он выдаст этому гейту `GC-*`, поэтому задним числом пространства не
   столкнутся.
3. **Канонический маппинг существующим producer-id не выдаётся.**
   `steward.risk_classify_*`, `human.owner_approval`, `maestro.validate_strict` —
   точки энфорсмента чужого рантайма, а не гейты `gate-check`; GC-псевдонимы для них
   означали бы, что steward владеет идентичностью проверок, которых не исполняет.
   Имена сохраняются как есть, переименование не требуется.
4. **`obligation` — общая таксономия.** Producer вправе классифицировать
   `quality | approval` собственные namespace-id под свою ответственность.
   Членство в каталоге определяется **только** резолвом id по каталогу и никогда
   не выводится из наличия поля.
5. **`obligation` — ось интента, `enforcement` — ось потребителя.** Блокирует ли
   гейт переход — политика конкретного прогона, а не свойство идентичности гейта; у
   самого steward это `verdict: fail|warn` плюс стадийная политика, а не каталог.
   Потребитель держит свою ось **отдельным полем** в своей схеме
   (`enforcement: mandatory|advisory` у Maestro). Встречное обязательство steward,
   часть межрепозиторного контракта: каталог **никогда** не заведёт ключ
   `enforcement` и **никогда** не примет токены `mandatory` / `advisory` в
   `obligation_vocabulary`. Это проверка загрузчика (закрытый набор top-level
   ключей + `RESERVED_OBLIGATION_TOKENS`), а не обещание в комментарии.

## Фикстуры (`fixtures/`)

Канон и для producer-тестов, и для вендоринга потребителем:

| Фикстура | Класс | Схема-валидна? |
|---|---|---|
| `clean.jsonl` | чистый прогон: header + artifacts, findings нет | ✅ |
| `findings.jsonl` | exit 1: findings по двум артефактам (BEH-07) | ✅ |
| `malformed_line.jsonl` | битая JSON-строка (BEH-03) | ❌ строка 3 не парсится |
| `future_schema.jsonl` | header со `schema_version: "99"` (BEH-04) | ❌ header не проходит схему |
| `dangling_artifact.jsonl` | finding ссылается на артефакт вне инвентаря (BEH-06) | ✅ **намеренно**: дефект семантический, ловит читатель-резолвер, не схема |

## Версионирование

Ломающее изменение = новый каталог `v2/` рядом; `v1/` замораживается. Потребитель
с копией `v1` обязан классифицировать header `schema_version != "1"` как
unreadable/unsupported — никогда как pass.
