# Maestro — Анализ интеграции с агентами

**Дата:** 2026-04-05
**Базовые документы:** `_cowork_output/01-architecture-map.md`, `_cowork_output/02-dag-scheduler-analysis.md`
**Анализируемые файлы:** `spawners/base.py` (102), `spawners/claude_code.py` (80), `spawners/codex.py` (79), `spawners/aider.py` (86), `spawners/announce.py` (75), `spawners/registry.py` (365), `workspace.py` (184), `git.py` (551), `orchestrator.py` (647), `scheduler.py` (903), `coordination/mcp_server.py` (760), `config.py` (317)

---

## TL;DR

1. Agent Interface реализован через **чистый ABC** (`AgentSpawner`) с 4 конкретными реализациями. Абстракция хорошая, но **слишком тонкая** — все spawners делают одно и то же (build prompt → Popen), отличаясь только CLI-флагами. Контекст задачи передаётся через один текстовый промпт, результат — только exit code. Нет structured output, нет streaming, нет heartbeats.
2. **Arbiter интеграция полностью отсутствует в коде** — ни одного импорта/вызова. Описание в COWORK_CONTEXT.md ("22-dim feature vector, MCP call") — aspirational design, не реализация. Maestro полностью работоспособен без Arbiter; agent_type задаётся статически в YAML.
3. Git Worktree lifecycle реализован корректно (create → use → cleanup с fallback на `shutil.rmtree`), но **нет timeout-based cleanup для orphaned worktrees**, нет лимитов на количество worktrees, нет pre-merge conflict detection.
4. **WORKFLOW.md injection не реализован** — ни `WorkflowLoader`, ни чтение `.maestro/WORKFLOW.md` в коде нет. Промпт строится только из `task.title` + `task.prompt` + `task.scope` + `retry_context`.
5. MCP server (`coordination/mcp_server.py`) — **не** для Arbiter, а для **координации агентов между собой** (claim tasks, update status, messaging). Это pull-based модель: агент сам запрашивает задачи.

---

## 1. Agent Interface

### 1.1 Архитектура абстракции

**ABC:** `spawners/base.py:15-101` — `AgentSpawner`

```python
class AgentSpawner(ABC):
    @property
    @abstractmethod
    def agent_type(self) -> str: ...       # Идентификатор типа

    @abstractmethod
    def is_available(self) -> bool: ...     # Проверка доступности CLI

    @abstractmethod
    def spawn(self, task, context, workdir, log_file, retry_context="") -> Popen: ...

    def build_prompt(self, task, context, retry_context="") -> str: ...  # Template method
```

**4 реализации:**

| Spawner | Файл | CLI команда | Ключевые отличия |
|---|---|---|---|
| `ClaudeCodeSpawner` | `spawners/claude_code.py` | `claude --print --output-format json -p "{prompt}"` | JSON output |
| `CodexSpawner` | `spawners/codex.py` | `codex --quiet --approval-mode auto-edit "{prompt}"` | Auto-approval |
| `AiderSpawner` | `spawners/aider.py` | `aider --yes-always --no-auto-commits --message "{prompt}" [files]` | Scope → positional args |
| `AnnounceSpawner` | `spawners/announce.py` | `echo "{prompt}"` | Notification-only |

**🟢 Что хорошо:**
- Чистый Strategy pattern — добавление нового агента = 1 файл ~80 строк
- `SpawnerRegistry` (`spawners/registry.py`) поддерживает auto-discovery через entry points (`maestro.spawners` group) и directory scanning — настоящая plugin architecture
- `is_available()` проверяет наличие CLI через `shutil.which()` — graceful degradation
- Fallback spawner (`registry.set_fallback()`) для unknown agent types

### 1.2 Передача контекста задачи

**Единственный канал:** текстовый промпт, формируемый в `build_prompt()` (`spawners/base.py:67-101`):

```
Task: {task.title}

{task.prompt}

Context from completed dependencies:
{context}                              ← result_summary от зависимостей

Scope (files you can modify):
{scope_str}                            ← список файлов/паттернов

{retry_context}                        ← ошибка предыдущей попытки (если retry)
```

**🔴 Проблема: prompt-only interface (high impact)**

Весь контекст — в одной текстовой строке. Нет:
- **Structured metadata** — агент не знает свой `task_id`, `timeout_minutes`, `max_retries`
- **System prompt injection** — нельзя задать поведенческую политику (WORKFLOW.md)
- **Environment variables** — нельзя передать `MAESTRO_TASK_ID`, `MAESTRO_CALLBACK_URL`
- **Config files** — нельзя подложить `.claude/CLAUDE.md` или `AGENTS.md` в workdir

**🔴 Проблема: prompt injection через task.prompt (high impact на security)**

`task.prompt` вставляется в промпт as-is. Если задача определяется из внешнего источника (e.g. decomposer генерирует из пользовательского описания), произвольный текст попадает в промпт агента без sanitization.

**🟡 Проблема: Aider — единственный, кто использует scope (medium)**

Только `AiderSpawner` передаёт `task.scope` как positional arguments (`aider.py:71-72`). Claude Code и Codex получают scope лишь текстово в промпте — агент может его проигнорировать. Нет enforcement.

### 1.3 Сбор результатов

| Канал | Scheduler mode | Orchestrator mode |
|---|---|---|
| **Exit code** | `process.poll()` → 0=success, else=failure (`scheduler.py:497`) | `process.returncode` (`orchestrator.py:392`) |
| **Stdout/Stderr** | Перенаправлен в log file, **не читается** до завершения | Перенаправлен в log file |
| **Log file parsing** | `cost_tracker.py` парсит JSON (только Claude Code) | Не используется |
| **State file** | ❌ Нет | `.executor-state.json` polling (`orchestrator.py:399-427`) |
| **Result summary** | Захардкожена: `"Task completed successfully"` (`scheduler.py:547`) | Нет (приходит от spec-runner) |
| **Structured output** | ❌ Нет | ❌ Нет |

**🔴 Проблема: result_summary всегда одинаков (high impact)**

В scheduler mode, при успехе, `result_summary` всегда `"Task completed successfully"` — не содержит полезной информации для downstream задач. `_build_dependency_context()` (`scheduler.py:421-451`) собирает result_summary от зависимостей, но если все они `"Task completed successfully"`, контекст бесполезен.

Claude Code в `--output-format json` пишет structured JSON в stdout, который **содержит** result summary, cost data, и изменённые файлы. Этот JSON **пишется в log file**, но scheduler **не парсит** его для result_summary. Только `cost_tracker.py` извлекает из него token usage.

**Рекомендация:** После успешного завершения Claude Code — парсить JSON log и извлекать `result` field как `result_summary`. Для Codex/Aider — хотя бы последние N строк stdout.

### 1.4 Обработка таймаутов и зависаний

**Scheduler mode** (`scheduler.py:492-509, 714-744`):

```
_monitor_running_tasks():
  for each running_task:
    if process.poll() is not None → handle completion
    elif elapsed > timeout_minutes * 60:
      1. process.terminate()      # SIGTERM
      2. await asyncio.sleep(0.5) # Grace period: 0.5s
      3. if still alive: process.kill()  # SIGKILL
      4. process.wait()           # Reap zombie
      5. _handle_task_failure()   # Retry or NEEDS_REVIEW
```

**🟡 Проблема: grace period 0.5s слишком мал (medium)**

0.5 секунды — недостаточно для graceful shutdown агента. Claude Code может сохранять state, Aider — коммитить. Стандартная практика — 5-30 секунд.

**Orchestrator mode** — таймаут **не реализован**:

```python
# orchestrator.py:381-397 — _monitor_running()
for zid, running in self._running.items():
    await self._update_progress(zid, running)
    return_code = running.process.returncode
    if return_code is not None:
        await self._handle_completion(zid, running, return_code)
```

Нет проверки elapsed time, нет timeout logic. Зависший spec-runner процесс будет работать вечно.

**🔴 Проблема: orchestrator mode не имеет timeout (high impact)**

Если spec-runner зависнет (e.g. waiting for user input, deadlock), orchestrator будет ждать бесконечно. В отличие от scheduler mode, который проверяет `elapsed > timeout_seconds`, orchestrator полагается **только** на `process.returncode`.

**Graceful shutdown** (`scheduler.py:793-819`, `orchestrator.py:604-646`):

Оба режима обрабатывают SIGTERM/SIGINT одинаково:
1. `terminate()` → sleep 0.5s → `kill()` if alive
2. Помечают задачи FAILED → READY (для возможности restart)

---

## 2. Arbiter Integration

### 2.1 Текущее состояние: полностью отсутствует

**Поиск в коде:** `grep -ri "arbiter" maestro/` — **0 результатов**. Единственные упоминания — в `COWORK_CONTEXT.md` и `SUGGESTIONS.md`.

**Что описано в COWORK_CONTEXT.md:**
- Arbiter — Rust-based MCP server
- 22-dim feature vector для роутинга задач
- Budget tracking, scope isolation
- Decision Tree inference + 10 safety invariants

**Что реализовано в коде:**
- `agent_type` — статическое поле в YAML, задаётся автором конфига
- Никакого динамического роутинга
- Никакого MCP-вызова к внешнему сервису

### 2.2 Где должна быть точка интеграции

Логическое место вызова Arbiter — **между `get_ready_tasks()` и `_spawn_task()`** в scheduler:

```python
# scheduler.py — текущий flow
ready_ids = dag.get_ready_tasks(completed)
for task_id in ready_ids[:available_slots]:
    await self._spawn_task(task_id)
    # ↑ spawner = self._spawners.get(task.agent_type.value)
    #   agent_type приходит из YAML, статически

# Гипотетический flow с Arbiter:
for task_id in ready_ids[:available_slots]:
    task = await self._db.get_task(task_id)
    recommended_agent = await arbiter.route(task)  # MCP call
    task.agent_type = recommended_agent
    await self._spawn_task(task_id)
```

### 2.3 Можно ли использовать Maestro без Arbiter?

**Да, полностью.** Maestro — standalone проект. Arbiter — planned integration, не dependency. `pyproject.toml` не содержит никаких arbiter-related зависимостей. Никакой fallback логики нет, потому что нет основной логики.

### 2.4 MCP Server — не Arbiter, а Coordination Hub

**Важное различие:** `coordination/mcp_server.py` — это **не** Arbiter. Это MCP server, через который **агенты координируют работу**:

| MCP Tool | Назначение | Кто вызывает |
|---|---|---|
| `get_available_tasks(agent_id)` | Список READY задач | Агент |
| `claim_task(agent_id, task_id)` | Атомарный захват задачи | Агент |
| `update_status(agent_id, task_id, status)` | Обновление статуса | Агент |
| `get_task_result(task_id)` | Получение результата зависимости | Агент |
| `post_message(agent_id, message)` | Сообщение другим агентам | Агент |
| `read_messages(agent_id)` | Чтение входящих сообщений | Агент |

Это **pull-based модель**: агент сам запрашивает задачи, сам обновляет статус. Дублирует `rest_api.py`, но через MCP protocol.

**🟡 Проблема: MCP и Scheduler — параллельные, не интегрированные миры (medium)**

Scheduler напрямую спаунит процессы и трекает через `Popen`. MCP server даёт агентам возможность самим claim tasks. Эти два подхода **не связаны** — если агент claim задачу через MCP, scheduler об этом не знает (он ожидает, что сам управляет lifecycle). Координация возможна только через общий SQLite + optimistic locking.

**Рекомендация:** Определиться с моделью: push (scheduler спаунит) или pull (агенты сами берут). Сейчас поддержаны обе, но **не** как взаимодополняющие, а как **параллельные**.

---

## 3. Git Worktree Management

### 3.1 Жизненный цикл

```
Создание (orchestrator.py:284-372):
  _spawn_zadacha(zadacha_id)
    → workspace_mgr.create_workspace(zadacha_id, branch)
      → workspace_base.mkdir(parents=True, exist_ok=True)  # workspace.py:79
      → git_manager.create_worktree(path, branch)           # git.py:384-403
        → git worktree add {workspace_base}/{zadacha_id} -b {branch}
    → workspace_mgr.setup_spec_runner(workspace_path, config)
      → Пишет executor.config.yaml                         # workspace.py:110-112
      → Создаёт spec/ directory                            # workspace.py:115-116

Использование (orchestrator.py:337-352):
  → asyncio.create_subprocess_exec("spec-runner", "run", "--all", cwd=workspace)
  → Процесс работает в изолированном worktree
  → Прогресс: polling .executor-state.json

Cleanup при успехе (orchestrator.py:531):
  → workspace_mgr.cleanup_workspace(zadacha_id)
    → git_manager.remove_worktree(path, force=True)        # git.py:426-444
      → git worktree remove --force {path}
    → Fallback: shutil.rmtree(path) + git worktree prune   # workspace.py:136-138

Cleanup при shutdown (orchestrator.py:609-639):
  → Для каждого running: terminate → kill → wait
  → Задачи помечаются FAILED → READY
  → Worktrees НЕ удаляются при shutdown (!)
```

### 3.2 Обработка конфликтов при merge

**Краткий ответ: Maestro не мержит.**

В orchestrator mode merge-а обратно в main **не происходит** внутри Maestro. Flow:

1. Zadacha работает в своей branch (`feature/{zadacha_id}`)
2. При успехе: `pr_manager.push_and_create_pr()` пушит branch и создаёт PR через `gh`
3. Merge происходит в **GitHub** (через PR review/merge), не в Maestro

`git.py` имеет `merge_branch()` (`git.py:490-532`) с обработкой `MergeConflictError`:
```python
if "CONFLICT" in stderr:
    self._run_git(["merge", "--abort"])  # Откат
    raise MergeConflictError(msg)
```

Но этот метод **не вызывается** из orchestrator. Он существует для scheduler mode, где git operations идут в shared workdir.

**🟡 Проблема: нет pre-merge conflict detection (medium)**

Если две zadachi модифицируют пересекающиеся файлы, конфликт обнаружится только при попытке merge PR в GitHub. Maestro мог бы:
1. Проверять `git diff` между branches перед PR creation
2. Запускать `git merge --no-commit --no-ff` dry-run
3. Использовать scope overlap warnings из DAG (`check_scope_overlaps()`) для предотвращения параллельного запуска conflicting zadachi

### 3.3 Orphaned worktrees

**🔴 Проблема: worktrees не удаляются при crash/shutdown (high impact)**

При `_cleanup()` (`orchestrator.py:609-639`) — процессы убиваются, задачи помечаются READY, но `cleanup_workspace()` **не вызывается**. Worktree остаётся на диске.

При следующем запуске (`_spawn_zadacha`, `workspace.py:74-76`):
```python
if workspace_path.exists():
    raise WorkspaceExistsError(msg)
```
Задача **не сможет запуститься**, пока orphaned worktree не будет удалён вручную.

**Но:** в orchestrator.py:296-299 есть mitigation:
```python
if not self._workspace_mgr.workspace_exists(zadacha_id):
    workspace = self._workspace_mgr.create_workspace(zadacha_id, branch)
else:
    workspace = self._workspace_mgr.get_workspace_path(zadacha_id)
```
То есть если worktree уже существует, он **переиспользуется**. Это обходит `WorkspaceExistsError`, но:
- Branch может быть в dirty state от предыдущего незавершённого run
- `.executor-state.json` может содержать stale data
- Нет `git reset --hard` / `git clean -fd` для очистки state

**Рекомендация:**
1. При startup: `workspace_mgr.cleanup_all()` или `git worktree prune`
2. При переиспользовании: сбрасывать worktree к HEAD branch (`git checkout -- .` + `git clean -fd`)
3. Добавить `recovery.py`-подобный механизм для orphaned worktrees

### 3.4 Лимиты git worktrees

**В коде лимитов нет.** `max_concurrent` (1-10 из `OrchestratorConfig`) ограничивает число **одновременных** процессов, но не worktrees. Worktrees от завершённых задач удаляются (`_handle_success` → `cleanup_workspace`), но при failures worktrees **сохраняются** (для debug).

**Git ограничения:**
- Теоретический лимит: нет hard limit в git
- Практический: каждый worktree = полная копия working tree (без .git). Для большого репозитория (10GB) × 10 worktrees = 100GB disk
- Performance: `git status`, `git gc` замедляются с ростом worktrees (shared .git/objects)
- `.git/worktrees/` directory accumulates metadata

**🟡 Рекомендация (medium):** Добавить `max_worktrees` в конфиг и проверять `len(workspace_mgr.list_workspaces())` перед созданием нового.

---

## 4. WORKFLOW.md Injection

### 4.1 Текущее состояние: не реализовано

**Заявлено в COWORK_CONTEXT.md:**
```
.maestro/
├── workflow.yml    # Runtime конфиг
└── WORKFLOW.md     # Агентная политика (промпт, инжектируется в system prompt)
```

**В коде:**
- Нет класса `WorkflowLoader`
- Нет чтения `.maestro/WORKFLOW.md`
- `config.py` обрабатывает только `tasks.yaml` / `project.yaml` — не `workflow.yml`
- `build_prompt()` (`spawners/base.py:67-101`) не имеет параметра для policy/workflow

### 4.2 Как промпт строится сейчас

```python
# spawners/base.py:86-101
def build_prompt(self, task, context, retry_context=""):
    scope_str = ", ".join(task.scope) if task.scope else "any"
    prompt = f"""Task: {task.title}

{task.prompt}

Context from completed dependencies:
{context if context else "No prior context available."}

Scope (files you can modify):
{scope_str}
"""
    if retry_context:
        prompt += f"\n{retry_context}"
    return prompt
```

**Компоненты промпта:**
1. `task.title` — из YAML
2. `task.prompt` — из YAML
3. `context` — `result_summary` от зависимостей
4. `scope` — glob patterns из YAML
5. `retry_context` — ошибка + validation output от предыдущей попытки

**Отсутствует:**
- System prompt / behavioral policy
- Repository-specific instructions (WORKFLOW.md)
- Global agent constraints
- Output format requirements

### 4.3 Как это должно работать (design intent из COWORK_CONTEXT.md)

```
1. WorkflowLoader находит .maestro/WORKFLOW.md в целевом репо
2. Содержимое файла инжектируется как system prompt или prepend к user prompt
3. workflow.yml задаёт runtime параметры (max_parallel, timeout, retry, states)
4. Политика применяется ко ВСЕМ задачам в этом репо
5. Task-level overrides возможны через tasks.yaml
```

### 4.4 Рекомендуемый дизайн реализации

```python
# Минимальная реализация в build_prompt():
def build_prompt(self, task, context, retry_context="", workflow_policy=""):
    parts = []

    if workflow_policy:
        parts.append(f"## Repository Policy\n{workflow_policy}")

    parts.append(f"Task: {task.title}")
    parts.append(task.prompt)
    # ... rest of current prompt

    return "\n\n".join(parts)
```

**🔴 Проблема: нет контроля размера (high impact если реализовать)**

WORKFLOW.md может быть произвольного размера. Если он 50KB, а prompt limit агента — 100K tokens, может не хватить места для самой задачи. Нужен:
- `max_workflow_size` в конфиге (default: 4000 chars)
- Truncation с warning
- Или: передача через file (`.claude/CLAUDE.md`), а не inline в prompt

**Альтернативный подход (для Claude Code):**

Вместо инжекции в prompt, **записать** WORKFLOW.md как `.claude/CLAUDE.md` в workdir перед spawn:
```python
# В scheduler перед spawn:
workflow_path = workdir / ".maestro" / "WORKFLOW.md"
if workflow_path.exists():
    claude_md = workdir / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(exist_ok=True)
    shutil.copy(workflow_path, claude_md)
```
Claude Code автоматически подхватит этот файл как system instructions. Это **нативнее** и не тратит prompt tokens.

---

## 5. Сводная таблица проблем

| # | Проблема | Impact | Файл:строки | Рекомендация |
|---|---|---|---|---|
| 1 | result_summary всегда "Task completed successfully" | 🔴 High | `scheduler.py:546,561` | Парсить JSON log Claude Code для реального summary |
| 2 | Orchestrator не имеет timeout для процессов | 🔴 High | `orchestrator.py:381-397` | Добавить elapsed check аналогично scheduler |
| 3 | WORKFLOW.md injection не реализован | 🔴 High | `spawners/base.py:67-101` | Добавить workflow_policy параметр или file injection |
| 4 | Orphaned worktrees при crash не очищаются | 🔴 High | `orchestrator.py:609-639` | Добавить cleanup в startup + recovery |
| 5 | Нет structured metadata для агента | 🔴 High | `spawners/base.py:67-101` | Передавать env vars: MAESTRO_TASK_ID, etc. |
| 6 | MCP и Scheduler — параллельные модели | 🟡 Medium | `mcp_server.py`, `scheduler.py` | Определиться: push vs pull |
| 7 | Grace period 0.5s при kill | 🟡 Medium | `scheduler.py:727` | Увеличить до 5-10s |
| 8 | Нет pre-merge conflict detection | 🟡 Medium | `orchestrator.py:472-504` | Dry-run merge перед PR |
| 9 | Scope enforcement только текстовый | 🟡 Medium | `spawners/aider.py:71-72` | Для Claude Code: использовать `--allowedTools` |
| 10 | Нет лимита на количество worktrees | 🟢 Low | `workspace.py` | Добавить max_worktrees config |

---

## Открытые вопросы к автору

1. **Push vs Pull** — MCP server позволяет агентам самим claim tasks, но scheduler спаунит процессы напрямую. Какая модель приоритетна? Планируется ли "autonomous agent" mode, где агент сам запрашивает задачи через MCP?

2. **WORKFLOW.md injection** — какой подход предпочтительнее: inline в prompt, файл `.claude/CLAUDE.md` в workdir, или env variable `CLAUDE_SYSTEM_PROMPT`? Для Codex/Aider — только inline вариант?

3. **Arbiter timeline** — это planned feature или deprecated idea? Если planned — будет ли это отдельный MCP server (как описано в COWORK_CONTEXT.md) или встроенная логика в scheduler?

4. **result_summary** — есть ли plan парсить JSON output от Claude Code? `cost_tracker.py` уже парсит эти логи для token usage — расширение на result_summary было бы natural extension.

5. **Orchestrator timeout** — это осознанный пропуск (spec-runner сам управляет timeouts?) или баг? Если spec-runner может зависнуть — orchestrator должен иметь watchdog.

6. **Worktree recovery** — при restart orchestrator видит existing worktree и переиспользует, но не сбрасывает state. Это by design (resume)? Или нужен explicit `--clean-workspaces` flag?

7. **Scope enforcement** — планируется ли pre-spawn validation (reject if scope files don't exist) или post-completion audit (check that agent only modified files within scope)?
