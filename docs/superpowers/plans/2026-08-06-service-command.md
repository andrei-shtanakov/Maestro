# `maestro service` Implementation Plan

> REQUIRED SUB-SKILL: superpowers:executing-plans.

**Goal:** implement `docs/superpowers/specs/2026-08-06-service-install-design.md`
(revision 2, approved 2026-08-06).

**Architecture:** new `maestro/service/` package — `locks.py` (two-level
flock hierarchy), `decide.py` (pure decision tables), `sweep.py` (stale
worktrees), `units.py` (launchd/systemd generation + credentials
preflight), `tick.py` (the wrapper) — plus migration 22 and the
`maestro service` Typer sub-app. Orchestrate/review stages are
independent jobs.

## Global constraints (from the spec)

- Lock identity **(project-key, stage)**: legacy takes
  `~/.maestro/locks/global.lock` EXCLUSIVE; scoped takes it SHARED plus
  `~/.maestro/instances/<project-key>/<stage>.lock` EXCLUSIVE. flock is
  authority, `<stage>.pid` is diagnostics only.
- `<project-key>` = sanitized slug + short hash of (db_path, project).
- Decisions: orchestrate — fresh / resume / skipped_running /
  noop_complete / noop_blocked; review — review / skipped_running /
  noop_complete.
- Exits: 0 handled (incl. skipped/noop and review needs-human), 1 infra,
  2 orchestrate run with failures.
- Ledger `service_ticks` (migration 22): stage + decision + outcome,
  sentinel then CAS finalize on `finished_at IS NULL`.
- Notifications: review outcomes belong to `maestro review-pr` (which
  gains dedup on (repo, pr_number, head_sha, outcome)); the service
  notifies only `noop_blocked` and tick infra failures.
- Sweep never removes NEEDS_REVIEW / unmerged / dirty worktrees, never
  touches review workspaces.
- Install refuses on missing binaries/credentials; unit carries absolute
  PATH + 0600 env file; runs as installing user.

## Tasks

- [ ] 1. `service/locks.py` + tests: hierarchy in both directions.
- [ ] 2. Migration 22 + DB APIs + journal tripwires.
- [ ] 3. `service/decide.py` + tests: both decision tables.
- [ ] 4. `service/sweep.py` + tests: retention rules.
- [ ] 5. `service/tick.py` + tests: sentinel→act→CAS, exit mapping,
      notification ownership.
- [ ] 6. `service/units.py` + tests: launchd/systemd goldens, preflight.
- [ ] 7. CLI sub-app + tests.
- [ ] 8. review-pr notification dedup (prerequisite) + tests.
- [ ] 9. Docs (CHANGELOG, README, CLAUDE.md); format/lint/pyrefly/pytest.
