# validation_backend PR3 — default flip `local → same` (Implementation Plan)

**Goal:** Flip the default `validation_backend` from `"local"` to `"same"` so post-task
validation runs by default in the same backend the task ran in (env-parity), with a
release note. No-op for users without docker/ssh backends; behavior change only where a
task actually runs on docker/SSH.

**Process:** controller-driven inline + final opus whole-branch review focused on resume
and fresh/upgraded schema parity. FOREGROUND-only pytest; DB tests close via `finally`.

## Global Constraints
- Default flip must be a strict no-op when `default_backend == "local"` and task
  `backend in {None, "local"}` (`same` → `resolve(None)` → `"local"` → non-durable
  `_run_validation`). Verified: `Scheduler.__init__` does `self._execution = execution or
  ExecutionConfig()`, `ExecutionConfig.default_backend = "local"`, `resolve(None) →
  default_backend`.
- Decision B: NO data-migration of existing rows — persisted tasks keep their `'local'`;
  only new tasks/configs get `'same'`. Safe resume (never change behavior mid-run).
- Decision A (schema parity, variant 1): change the tasks column DEFAULT to `'same'` for
  BOTH fresh (SCHEMA_SQL) and upgraded (new migration 12 rebuild) DBs. Migration 11 stays
  untouched (immutable history).
- Decision C: release note lives in `CHANGELOG.md` `## Unreleased` → `### Changed`.

## Task 1 — migration-first test (TDD), then migration 12
**Files:** Test `tests/test_db_migration_tasks_validation_default.py` (new);
`maestro/database.py` (SCHEMA_SQL:75 default; ordered list; new
`_migrate_tasks_validation_backend_default_same`).

Migration 12 rebuilds `tasks` to change ONLY the column default `'local' → 'same'`.
`tasks` has children with `ON DELETE CASCADE` (task_dependencies/agent_logs/task_costs)
and `foreign_keys=ON`, so `DROP TABLE tasks` under FK=ON would cascade-delete children.
Migrations run inside an implicit transaction where `PRAGMA foreign_keys` is a no-op.
Therefore migration 12 MUST: `commit()` (→ autocommit) → `PRAGMA foreign_keys=OFF` →
create `tasks_new` (canonical full schema incl `backend`, `validation_backend ... DEFAULT
'same'`) → `INSERT INTO tasks_new (<cols-from-old-PRAGMA>) SELECT <same> FROM tasks` →
`DROP TABLE tasks` → `ALTER TABLE tasks_new RENAME TO tasks` → recreate
`idx_tasks_status` → `commit()` → `PRAGMA foreign_keys=ON`. Idempotency guard: read
`PRAGMA table_info(tasks)`; if `validation_backend` `dflt_value` already normalizes to
`same`, return (fresh DBs get `'same'` from SCHEMA_SQL and skip the rebuild).

**Tests (mandatory):**
1. Existing row `'local'` stays `'local'` after migration 12 (upgraded path).
2. A `task_costs` (CASCADE child) row survives the rebuild (proves FK-OFF; no cascade).
3. Fresh and upgraded DBs show identical per-column schema (name→type/notnull/default),
   and `validation_backend` default is `'same'` in BOTH.
4. `idx_tasks_status` exists post-rebuild; version 12 journaled + idempotent re-connect.

## Task 2 — flip model defaults + fix stale descriptions
**Files:** `maestro/models.py` (TaskConfig ~476, Task ~563).
Both `validation_backend` Field `default="local" → "same"`. Update descriptions: drop the
stale "SSH targets fail preflight" clause (PR2 enabled SSH validation); state the new
default and that non-local targets run durably in that backend.

## Task 3 — behavioral invariants + existing test
**Files:** `tests/test_validation_backend_persistence.py`,
`tests/test_validation_backend_default_flip.py` (new).
- Update `test_validation_backend_defaults_local` → asserts default `"same"`.
- Invariant (no-op): a Task with default `same`, no `execution` config, `backend=None`
  resolves validation to the bare `local` backend (id `"local"`) → non-durable path.
- Invariant (behavior change): with `execution.default_backend = "docker"`, default `same`
  resolves validation to the docker backend (id != `"local"`) → durable path.
  (Assert via `Scheduler._resolve_validation_backend(task).id`, not a live run.)

## Task 4 — release note + docs
**Files:** `CHANGELOG.md` (`## Unreleased` → `### Changed`), `maestro/CLAUDE.md`
(update the "default still local (flip=PR3)" line to "default `same` since PR3").

## Verify
Targeted: the new migration test + `test_validation_backend_persistence.py` +
`test_validation_backend_default_flip.py` + `test_database.py` + `test_scheduler.py`.
Then FULL suite (touches a shared migration path), `uv run pyrefly check`, `uv run ruff
format . && uv run ruff check .`. Final opus whole-branch review focused on resume +
schema parity → address → push → PR.
