# `maestro service` — scheduled autonomous runs — design

**Status:** proposed
**Date:** 2026-08-06
**Track:** notify/post-PR step 6 (owner order 2026-08-06); TODO
`@id:service-install-design`
**Owner constraints (verbatim, 2026-08-06):** a separate operational
track, NOT tied to #137. A launchd/systemd generator by itself solves
none of: single-instance locking, resume after crash, stale worktrees,
SQLite ownership, credentials, log rotation, recurring schedule vs
continuing an existing run. "Сначала нужны durable команды и
идемпотентный resume; затем внешний service wrapper. Иначе
автоматизируем запуск процесса, но не жизненный цикл."

## 1. Why now, and what changed

The two things that used to make an unattended run pointless are gone:
`approver_cmd` (#137) unblocks gate-blocked workstreams without a live
operator, and `maestro review-pr` (#149) handles the PR review round.
A nightly run can now actually reach DONE and get its PR reviewed.

What is still missing is the *lifecycle*, and this spec is deliberately
about that — not about writing a plist. The central decision:

> **The scheduler starts a Maestro-owned wrapper, never `orchestrate`
> directly.** `maestro service run <project.yaml>` decides — using
> Maestro's own state — whether this tick should resume, start fresh, or
> do nothing at all. launchd/systemd cannot make that decision, and a
> generator that hands them `maestro orchestrate` bakes in the wrong one.

## 2. Command surface

```
maestro service run <project.yaml>        # one tick (what the scheduler calls)
maestro service install <project.yaml>    # write + load the platform unit
maestro service uninstall <project.yaml>  # unload + remove it
maestro service status <project.yaml>     # unit state + last tick outcome
```

`install` supports `--schedule "HH:MM"` (daily, the default shape) or
`--every <minutes>`, `--dry-run` (print the unit, write nothing), and
`--force` (overwrite an existing unit). Generated units are named
`com.maestro.<project-slug>` (launchd) / `maestro-<project-slug>`
(systemd), so several projects coexist.

**Platforms:** macOS launchd (`~/Library/LaunchAgents/…plist`,
`RunAtLoad=false`) and Linux systemd **user** units (`~/.config/systemd/
user/…{service,timer}`, `Persistent=true`). System-wide units, Windows,
and cron are out of scope in v1 (cron gives no supervision and no
`KeepAlive` semantics; a user can still call `service run` from cron
themselves — that is exactly why the wrapper owns the decisions).

## 3. The seven lifecycle problems

### 3.1 Single-instance locking — today's lock is *global*, and that is a bug for services

`_acquire_pid_lock` flocks `~/.maestro/maestro.pid` — one Maestro
process **per machine**, regardless of project. Two scheduled projects
whose windows overlap would have the second one exit with "Maestro is
already running", indistinguishable from a real conflict.

v1 makes the lock **per (db_path, project)**:
`~/.maestro/locks/<project-slug>-<db-hash>.pid`, same flock mechanics
(OS-released on death, no stale-lock protocol). The legacy global path
stays honored for `maestro run`/`orchestrate` invoked the old way, so
nothing changes for existing users; the service path always uses the
scoped lock. A tick that cannot take the lock is **not an error**: it
logs `skipped: already running`, records the tick, and exits **0** —
otherwise every long run would paint the unit red on the next tick.

### 3.2 Recurring schedule vs continuing an existing run

The wrapper's decision table, evaluated per tick against the DB:

| DB state | Decision |
|---|---|
| lock held by a live process | `skipped_running`, exit 0 |
| no workstreams yet | `fresh` — `maestro orchestrate <project.yaml> --db <db>` |
| non-terminal workstreams exist (READY/RUNNING/…) | `resume` — the same invocation plus `--resume` |
| all terminal, none NEEDS_REVIEW | `noop_complete` — nothing to do, exit 0 |
| all terminal, some NEEDS_REVIEW | `noop_blocked` — exit 0, notify once per (workstream, sha) |

`noop_blocked` is deliberately not an error: a workstream parked for a
human is a normal end state, and a nightly unit must not flap red
forever because of it. Notification (not exit code) is how the human
finds out — deduplicated so a week of nightly ticks does not produce
seven identical alerts.

The wrapper **never** decides "start a second project run in parallel";
concurrency inside a run is `max_concurrent`'s job.

### 3.3 Resume after crash

Nothing new is invented: `orchestrate --resume` already reconciles
workstreams stranded by a hard crash (DECOMPOSING/RUNNING/MERGING/
PR_CREATED → READY; live orphans → NEEDS_REVIEW), migration-18 rework
markers and migration-20 approver sentinels finalize fail-closed, and
`maestro review-pr` resumes from spec-runner's own durable state. The
wrapper's only job is to **choose** whether to pass `--resume` to
`maestro orchestrate <project.yaml>` (table above) instead of silently
starting a fresh run over a half-finished DAG.

### 3.4 Stale worktrees

A crashed tick can leave worktrees behind. The wrapper runs a bounded
sweep **before** deciding (never during a live run — the lock is held):

- `git worktree prune` in the project repo (removes only administrative
  records for directories that no longer exist — safe by construction);
- worktrees whose workstream is DONE/ABANDONED **and** whose branch is
  merged into the base branch are removed;
- everything else — a worktree for a NEEDS_REVIEW workstream, an unmerged
  branch, a dirty tree — is **kept and reported**, never removed. Review
  workspaces (`~/.maestro/review-workspaces/`) are not touched here at
  all: they have their own retention policy and their own `--gc`.

### 3.5 SQLite ownership

The lock key is **(db_path, project)** (§3.1) — that, not the file
layout, is what makes "one writer" true; a shared DB holding several
projects is therefore safe, and a separate DB per project is a
recommendation, not an invariant.

The default deserves care. `service install` **resolves and records the
DB path in the unit** (defaulting to the same `~/.maestro/maestro.db`
that manual `maestro orchestrate` uses) rather than leaving the unit to
re-resolve a default later. Two reasons: a unit pointing at a *different*
DB than the operator's manual runs would see an empty database and
decide `fresh` over an in-progress DAG — the worst possible outcome of
this whole design; and a future change of the default must never
silently move a running service to another DB. `install` warns when the
resolved DB is already used by another installed unit (supported, but
worth knowing), and `--db` overrides for a per-project file.

Two additional rules:

- the unit runs as the **installing user** (no root, no `sudo`) — a DB
  written once by root is then unusable by the user, a classic and
  unpleasant failure;
- WAL files live next to the DB, so the DB path must be on a local
  filesystem; the installer refuses a path under a network mount it can
  detect and warns otherwise.

### 3.6 Credentials — the biggest practical trap

launchd/systemd start with a **minimal environment**: no shell profile,
no `~/.zshrc`, often no `PATH` beyond `/usr/bin:/bin`. Maestro's
spawners use `inherit_env=True`, so the agent CLI sees exactly what the
service manager gave the parent — which is why "works in my terminal,
silently fails at 03:00" is the default outcome.

v1 makes this explicit rather than magical:

- `service install` **preflights** the environment it is about to write:
  it resolves the harness binaries (`claude`, `codex`, `spec-runner`, …)
  to absolute paths and checks that the credentials each configured
  harness needs are available non-interactively; missing pieces are a
  **refusal with instructions**, not a warning (a unit that cannot
  authenticate is worse than no unit).
- The generated unit carries an explicit `PATH` (the resolved absolute
  binary directories) and an `EnvironmentFile` /
  `EnvironmentVariables` block sourced from a **user-owned env file**
  (`~/.maestro/service.env`, mode `0600`, created empty on first
  install). Secrets live there, never inside the unit file, never in
  the repo, never in the DB.
- macOS keychain caveat, stated honestly: a background agent may not
  reach a login-keychain item that requires an unlocked session. Where a
  harness depends on that, the env-file path (API key) is the supported
  configuration and the installer says so.

### 3.7 Log rotation

Ticks write to `~/.maestro/service-logs/<project-slug>/` — the unit's
stdout/stderr, one file per tick (`tick-<ULID>.log`). The tick metadata itself lives in the
`service_ticks` DB table (§4), not in a sidecar file — the log files are
raw output only. Rotation is Maestro's, not logrotate's:
after each tick, files older than `--keep-days` (default 14) **and**
beyond `--keep-ticks` (default 100) are deleted, most recent kept. The
per-run obs logs under the project's `logs/<ULID>/` are untouched — they
belong to the run, not the service.

## 4. Tick ledger (migration 22)

Append-only, one row per tick, so `service status` and a human can see
what the machine did overnight:

```sql
CREATE TABLE service_ticks (
    tick_id       TEXT PRIMARY KEY,       -- ULID
    project       TEXT NOT NULL,
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    decision      TEXT NOT NULL CHECK (decision IN
                    ('fresh','resume','skipped_running',
                     'noop_complete','noop_blocked')),
    exit_code     INTEGER,
    reason        TEXT,
    log_path      TEXT,
    swept_worktrees INTEGER NOT NULL DEFAULT 0
);
```

Same discipline as `post_pr_review_runs`: the row is written before the
decision is acted on, finalized once by a CAS on `finished_at IS NULL`,
and immutable afterwards. `maestro service status` prints the unit state
plus the last N ticks.

## 5. Exit-code contract (what the service manager sees)

| Exit | Meaning |
|---|---|
| 0 | tick handled — including `skipped_running`, `noop_complete`, `noop_blocked` |
| 1 | infrastructure failure (config unreadable, DB unusable, preflight regression) |
| 2 | the orchestrate run itself ended with failures needing a human |

launchd/systemd retry policy is deliberately **off** (`KeepAlive=false`,
no `Restart=`): the next scheduled tick is the retry, and it will make a
fresh decision from the DB. Automatic restarts would stack runs.

## 6. Non-goals

- No new daemon, no long-lived supervisor process: a tick is a normal
  `orchestrate` run that exits.
- No system-wide (root) units, no Windows, no cron generation in v1.
- No change to `orchestrate`'s own semantics — the wrapper only chooses
  its arguments.
- No secret storage in Maestro: the env file is the user's, mode-checked
  but never written to with secrets by us.
- No remote/multi-host scheduling (the scoped lock is same-host, like
  every other lock in Maestro today).

## 7. Testing plan (implementation PR)

- Decision table: each row of §3.2 against a seeded DB, including
  `skipped_running` with a really-held lock and exit 0.
- Scoped lock: two projects run concurrently (previously impossible);
  same project twice → second skipped; lock released on process death.
- Legacy path: `maestro run`/`orchestrate` without the service still use
  the global pid file — byte-identical behavior.
- Stale sweep: DONE+merged worktree removed; NEEDS_REVIEW, unmerged, and
  dirty worktrees kept and reported; review workspaces never touched;
  `git worktree prune` invoked in the project repo only.
- Credentials preflight: missing binary / missing API key → install
  refuses with instructions and writes no unit; success path writes an
  absolute `PATH` and references the env file; env file is created 0600
  and never contains values we wrote.
- Unit generation: launchd plist and systemd service+timer golden files
  (schedule and interval variants), `--dry-run` writes nothing,
  `--force` overwrite, uninstall unloads and removes, per-project names
  do not collide.
- Rotation: `--keep-days`/`--keep-ticks` boundaries; the newest tick log
  is never deleted; project `logs/<ULID>/` untouched.
- Ledger: sentinel before action, CAS finalize once, `service status`
  output; migration 22 fresh+upgrade and journal tripwires.
- Zero-change guarantee: no `service` invocation → nothing about
  existing commands changes.

## 8. Open questions for review

1. **Should `service run` also drive `maestro review-pr`** for PRs the
   tick created (a second stage inside the tick), or stay strictly
   "orchestrate only" with review scheduled as its own unit? A separate
   unit is simpler and keeps failure domains apart; one tick doing both
   is fewer moving parts for the user. v1 recommendation: **separate
   unit** (`service install <config> --stage review`), same wrapper,
   same ledger, different decision table.
2. **Scoped lock migration.** Keeping the legacy global lock for
   non-service invocations means two lock regimes coexist. The
   alternative — moving everything to the scoped lock now — is cleaner
   but changes behavior for existing users who rely on "one Maestro at a
   time" as a safety net. v1 recommendation: coexist, with the scoped
   lock also refusing when the legacy global lock is held (safe
   superset), and a follow-up to retire the global one.
