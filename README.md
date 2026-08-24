# Maestro

> AI Agent Orchestrator — coordinate multiple coding agents with DAG-based scheduling and git worktree isolation

## Quick Start

```bash
uv add maestro
uv run maestro run examples/hello.yaml
```

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), git. Mode 2 also needs [gh CLI](https://cli.github.com/) and [spec-runner](https://github.com/andrei-shtanakov/spec-runner) >= 2.16.0.

**A model source is required.** Maestro no longer bakes in a default model, so
one of the following must be true or the run fails loud: set `$ATP_CATALOG` to
a model catalog (or run `atp models init`), set `MAESTRO_CLAUDE_MODEL` /
`MAESTRO_CODEX_MODEL`, or let arbiter routing supply a model. With none of
these, `maestro run` halts with a clear `CatalogNotConfigured` error before
spawning any agent.

## What It Does

Maestro coordinates AI coding agents (Claude Code, Codex, Aider) on complex multi-part tasks. It resolves dependencies between tasks as a DAG, runs independent tasks in parallel, and handles retries, validation, and crash recovery. Two modes cover different workflows:

- **Task Scheduler** — run tasks from a YAML config in a shared directory
- **Multi-Process Orchestrator** — decompose a project into independent units, run each in an isolated git worktree, and auto-create PRs

## Mode 1: Task Scheduler

Run multiple AI agent tasks with dependency ordering in a single repository.

- Define tasks, dependencies, and file scopes in a YAML config
- DAG-based scheduling runs independent tasks in parallel
- Post-task validation commands catch broken builds early
- Crash recovery with `--resume` picks up where you left off

```yaml
# examples/hello.yaml
project: hello-maestro
repo: ~/projects/hello

defaults:
  agent_type: announce       # No real agent — just logs the task

tasks:
  - id: greet
    title: "Say hello"
    prompt: "Hello from Maestro!"

  - id: compute
    title: "Crunch numbers"
    prompt: "Computing 2 + 2 = 4"

  - id: summarize
    title: "Summarize results"
    prompt: "All tasks completed successfully."
    depends_on: [greet, compute]
```

```bash
uv run maestro run config.yaml           # Run tasks (mints a run)
uv run maestro run config.yaml --resume  # Resume after crash
uv run maestro status                    # Check progress
uv run maestro retry <task-id>           # Retry a failed task
```

Run these from the repository `repo:` points at, or add `--config config.yaml`
— state is keyed by that checkout's git remote. See
[Where state lives](#where-state-lives).

## Mode 2: Multi-Process Orchestrator

Decompose a project into isolated work units ("workstreams"), each running in its own git worktree.

- Auto-decompose a project description into workstreams, or define them manually
- Each workstream gets an isolated git worktree and feature branch
- Task specs are generated per workstream and executed via spec-runner
- Completed workstreams are pushed and PRs are auto-created via `gh`

See [`examples/project.yaml`](examples/project.yaml) for a fully annotated config.

```bash
uv run maestro orchestrate project.yaml           # Run orchestrator
cd <repo> && uv run maestro workstreams           # Check workstreams status
uv run maestro workstreams --config project.yaml  # ...or name the repository
uv run maestro workstreams --run 01J8Z...         # ...or disambiguate two runs
uv run maestro workspaces                         # List active worktrees
```

`workstreams` and its siblings resolve `(repository, run)` before they open
anything, so they need the repository's checkout, a `--config`, or a `--db`.
See [Where state lives](#where-state-lives).

### Config authoring: `init` and `validate`

- `uv run maestro init` — scaffold a commented `project.yaml` from the current
  directory (git-derived autofill for `project`/`repo`).
- `uv run maestro validate project.yaml` — preflight checks before you run:
  dependency cycles, scope overlap between workstreams, and repo sanity.
- `uv run maestro validate project.yaml --strict --no-fs` — CI mode: `--strict`
  treats warnings as errors (exit 1), `--no-fs` skips filesystem access for a
  deterministic check with no real repo required.

Findings come in three severities. `error` always blocks. `warning` blocks only
under `--strict` — every warning class, including `scope-no-match`. `info` never
blocks, even under `--strict`.

`--no-fs` is not a milder `--strict`: it removes the filesystem tier entirely,
so the warnings that tier produces (`scope-no-match`, repo sanity) are never
raised at all. A config that fails `--strict` can therefore pass
`--strict --no-fs` — the two check different amounts, and the combined CI form
above is the weaker of the two by design.

`maestro orchestrate` also runs this preflight automatically as a fail-fast
gate before spawning any workstream.

### Dual-mode repos: direct spec-runner runs + Maestro

Maestro owns `spec-runner.config.yaml` inside each worktree: it regenerates
the file from `project.yaml`'s `spec_runner` block on every workstream
launch. That makes `project.yaml` the single source of truth for worktree
configs — and it means the target repo must **not** track its own
`spec-runner.config.yaml`. A tracked copy gets overwritten inside the
worktree, and the overwrite then surfaces ex-post as a scope violation
(preflight warns about this: `spec-runner-config-tracked`).

For a repo that is *also* driven by spec-runner directly (outside Maestro),
the canonical setup is:

1. Untrack the file — `git rm --cached spec-runner.config.yaml` — and add it
   to `.gitignore`.
2. Keep a local, untracked copy of `spec-runner.config.yaml` for direct
   `spec-runner` runs.
3. Treat `project.yaml` as the SSOT for everything Maestro-driven.

This is the current interoperability contract between the two tools, not a
final design — a shared convention may replace it later.

**Spec-runner version:** Mode 2 requires spec-runner **>= 2.16.0** and
preflight enforces this fail-closed before any worktree is created. Older
versions (2.15.x) commit the harness-owned `spec/.gitignore` into task
commits, which the ex-post scope gate flags as a scope escape — green
workstreams end up in NEEDS_REVIEW through no agent's choice. The supported
path is upgrading; declaring `spec/.gitignore` in every workstream's scope
or `maestro workstream-approve` after the block is an emergency path for
legacy 2.15.x only. For local development against an *unpublished*
spec-runner build, set `MAESTRO_SPEC_RUNNER_ALLOW_UNVERIFIED=1` to downgrade
the version check to a warning (the `--spec-prefix` capability probe stays
mandatory). A `spec/.gitignore` deliberately tracked by the user remains a
user file and is checked by the normal scope rules.

### Where state lives

State is per project and per run:

```
~/.maestro/
  projects/<host>/<owner>/<repo>/
    runs/<run-id>/
      state.db
      logs/
    locks/
  maestro.db          # legacy, frozen — see below
  service.env
```

**Identity comes from the git remote, never from `project:` and never from a
filesystem path.** `project:` is operator-chosen and has named a pilot rather
than a repository in live configs; a path is not portable. The key is the
`origin` remote — host included, because `github.com/acme/app` and
`gitlab.com/acme/app` are two repositories with one `owner/repo`. A checkout
with no `origin` lands in `projects/_local/<name>-<hash>/`, fingerprinted by its
git common dir so two unrelated checkouts sharing a basename stay apart and two
worktrees of one repository come together.

**One rule for naming the repository, in every command.** The invocation names
it either with a config or with the current directory:

1. `--db <path>` names a database directly and skips resolution entirely;
2. otherwise the config — `maestro run tasks.yaml`, `maestro orchestrate
   project.yaml`, or `--config project.yaml` on the commands that carry none —
   supplies identity (`repo_url` when the config declares it, else the `origin`
   of the checkout `repo:` points at);
3. otherwise the checkout in the current directory.

So the commands that used to work from anywhere must now be run **from the
repository's checkout**, or be told which repository they mean:

```bash
cd ~/labs/app && maestro workstreams            # resolves ~/labs/app's run
maestro workstreams --config ~/labs/app/project.yaml
maestro workstreams --run 01J8Z...              # disambiguate two runs
maestro workstreams --db ~/labs/app-run3.db     # name the database outright
```

Every one of them says what it resolved before it acts, and refuses — with the
key and where the key came from — rather than guessing. `--db` cannot be
combined with `--run` or `--config`: it skips the resolver those steer, and
acting while silently dropping them is exactly the wrong-database success this
layout removes.

**`~/.maestro/maestro.db` is a frozen legacy file.** It is not migrated (its
rows predate any project key, and for the July demo tasks the honest owner is
"none") and no default path writes to it any more. It is still readable, and
`maestro state-usage` names it. Two caveats worth knowing:

- *Frozen means "never the default", not "immutable" — for the commands that
  change things.* An explicit `--db ~/.maestro/maestro.db` at a **mutating**
  command (`retry`, `approve`, `workstream-*`, `orchestrate`, `run`) opens it
  like any other database, which runs schema initialisation and therefore
  writes. If you want it untouched under one of those, copy it first. The
  **view-only** commands — `status`, `workstreams`, `check-scope`,
  `service status`, `costs` — open `--db` read-only and never initialise a
  schema, so listing this file no longer rewrites it. The cost of that
  honesty is that a pre-split file has no `workstreams` table to list, and
  those commands say so instead of creating one.
- *If you installed a service unit before this change, reinstall it.* The old
  `maestro service install` baked `--db ~/.maestro/maestro.db` into the
  generated unit. `service run` is a mutating command, so an existing unit
  keeps opening — and rewriting — the legacy file on **every tick**, and
  nothing on the machine will tell you. Run `maestro service uninstall
  project.yaml` and `maestro service install project.yaml …` again so the
  tick resolves the current run instead.
- *Reading a WAL-mode database read-only legitimately creates `-wal` and `-shm`
  beside it.* The real `~/.maestro/maestro.db` is WAL-mode, so merely
  describing it drops two sidecars next to it. The database itself is
  untouched — the connection is `SQLITE_OPEN_READONLY`, no frame is appended
  and no checkpoint runs — but anyone hashing the *directory listing* rather
  than the file will see a change.

**Growth is visible before it is a problem:**

```bash
maestro state-usage    # runs and bytes per repository, plus the legacy file
```

There is deliberately no retention policy yet.

A run directory holds `state.db` and `logs/`. Mode 1 (`maestro run`) defaults
its structured event log and per-task logs there — beside the state database,
never inside the target repo's working tree (#217); an explicit `--log-dir`
overrides. **Not every log has moved:** the obs OTel JSONL stream is still
written under `logs/<run-id>/` relative to the current directory (or
`$ORCHESTRA_LOG_DIR`), Mode 2 still defaults to `<repo_path>/logs`, and the
service writes under `~/.maestro/service-logs/`. So a run directory is still
not removable as a unit, and `maestro state-usage` does not see the logs
outside it.

**Ending a run.** A run records its own ending: `completed` when everything is
terminal, `failed` only when it cannot advance, `cancelled` on Ctrl-C. A
needs-human pause sets `suspended_at` and leaves the run resumable under the
same id. What Maestro will *not* do is decide on your behalf that an
interrupted run is obsolete — orchestrating again leaves the older run marked
`interrupted`, because that is the one fact saying it died mid-flight, and
overwriting it would replace evidence about that run with a claim about a
different one. When you have decided, say so:

```bash
maestro run-end <run-id> --outcome superseded   # or: cancelled
```

`completed` and `failed` are refused there deliberately: those are observations
a finishing run makes about itself from its own workstream table, not
assertions an operator can make by hand. Until an older run is ended, two open
runs make every run-resolving command ask for `--run`.

One more trap, since it reads like a contradiction when you hit it: `chmod
0500` on the directory holding a database — the natural way to protect
evidence — makes *reading* it fail with `attempt to write a readonly database`.
SQLite needs to create its journal beside the file. Protect the file, not the
directory.

## Examples

| File | Description |
|------|-------------|
| [`hello.yaml`](examples/hello.yaml) | Minimal quick-start with the `announce` agent (no AI needed) |
| [`tasks.yaml`](examples/tasks.yaml) | Full task scheduler config with dependencies, validation, and git settings |
| [`parallel-refactor.yaml`](examples/parallel-refactor.yaml) | DAG-based parallel refactoring across multiple modules |
| [`project.yaml`](examples/project.yaml) | Multi-process orchestrator with manual workstreams definitions |
| [`maestro-builds-maestro.yaml`](examples/maestro-builds-maestro.yaml) | Meta-dogfooding — Maestro implements its own backlog |
| [`dogfood-maestro.yaml`](examples/dogfood-maestro.yaml) | Dogfooding config — Maestro runs quick wins from its own backlog in parallel |
| [`with-arbiter.yaml`](examples/with-arbiter.yaml) | Optional Arbiter-driven routing (advisory mode) — `agent_type: auto` lets the policy engine pick the best agent |
| [`with-atp-validation.yaml`](examples/with-atp-validation.yaml) | Post-task validation via the ATP Platform CLI through `validation_cmd` |

## Optional: Arbiter routing

Add an `arbiter:` section to your project YAML to delegate per-task agent selection to the [Arbiter](https://github.com/andrei-shtanakov/arbiter) policy engine. Advisory mode honors your explicit `agent_type` and feeds the learning loop; authoritative mode lets the arbiter override your choice and gates retries on outcome delivery. When the section is absent, Maestro runs the zero-config static-routing path — no subprocess, no routing overhead. See [`examples/with-arbiter.yaml`](examples/with-arbiter.yaml).

## Scheduled autonomous runs (optional)

Once a project runs unattended well, schedule it:

```bash
maestro service install project.yaml --schedule "03:00"                  # nightly product tick
maestro service install project.yaml --schedule "05:00" --stage review   # PR review, later and separate
maestro service status project.yaml                                      # what the machine did overnight
```

The scheduler starts `maestro service run`, not `orchestrate` — the
wrapper decides per tick whether to resume an unfinished DAG, start a
fresh one, or do nothing, because only Maestro's state knows which is
right. What that buys you:

- **No stacked runs.** A tick that finds the previous one still going
  records `skipped` and exits 0; the two stages of one project (and
  different projects) still run concurrently, each under its own lock.
- **Honest unit colours.** Red means broken. A workstream parked for a
  human, or a PR whose review needs you, exits 0 and notifies instead —
  otherwise you learn to ignore a permanently red unit.
- **Install refuses rather than fails at 03:00.** Scheduled runs get no
  shell profile, so `service install` resolves every harness binary up
  front and writes no unit if one is missing. Credentials are satisfied
  either by an exported key or by the harness CLI's own login store
  (Maestro spawns those CLIs and never calls a model API itself), so a
  logged-in `claude` is a valid setup; only the absence of both is
  refused. Keys you *do* export go in `~/.maestro/service.env`
  (mode 0600, yours to fill); add more with `--require-env NAME`, or
  skip the check with `--skip-credential-check`. `--dry-run` always
  renders the unit, reporting problems instead of blocking the preview.
- **Conservative cleanup.** Each tick prunes worktrees only when the
  workstream is finished *and* its branch is merged; anything unmerged,
  dirty, or awaiting review is kept and reported.

Every tick is recorded (`maestro service status`) with its decision,
outcome and exit code.

## Post-PR review (optional)

Maestro creates the PR; the review-bot loop belongs to
[spec-runner](https://github.com/andrei-shtanakov/spec-runner). Once a
workstream is DONE and its PR is open, drive that loop over it:

```bash
maestro review-pr project.yaml ws-006   # one PR
maestro review-pr project.yaml --all    # every workstream PR, sequentially
```

Each run verifies every review-bot comment against the code, fixes the
valid ones with tests, replies in the threads, and reports back. Exit
codes: `0` complete, `1` infrastructure failure, `2` needs a human,
`3` already running elsewhere. Requires **spec-runner >= 2.21.0**.

Notes:

- **Advisory, post-delivery.** The workstream is already DONE and its
  feature commit already merged into the base branch, so review fixes
  move the PR head only — this is cleanup on the PR, not a correctness
  gate for dependent workstreams.
- **Resumable and safe to re-run.** State lives outside the review
  checkout (`~/.maestro/review-state/<repo>/<pr>/`), so re-invocation
  never re-processes a comment or replies twice; a per-PR lock keeps a
  cron run and an operator run from colliding.
- **Nothing is discarded silently.** A workspace with unpushed fix
  commits is published on the next run; a dirty or force-pushed one is
  refused until you commit or pass `--discard-local`.

## Notifications

Desktop notifications (macOS/Linux) are on by default. Adding a `webhook_url`
under `notifications:` additionally POSTs a versioned JSON envelope on
lifecycle events (started / completed / needs-review / PR created):

```yaml
notifications:
  desktop: true
  webhook_url: "https://your-receiver.example/maestro"  # may embed a token
```

```json
{
  "schema": "maestro.notification/v1",
  "event_id": "01K1X...",
  "event": "workstream_pr_created",
  "occurred_at": "2026-08-06T12:34:56Z",
  "subject_id": "ws-001",
  "subject_title": "Auth refactor",
  "entity_kind": "workstream",
  "status": "pr_created",
  "message": null,
  "url": "https://github.com/o/r/pull/1"
}
```

Contract notes:

- **Delivery semantics:** at-least-once within a live process and graceful
  shutdown; best-effort across a hard crash (no durable outbox). Retries
  are bounded (3 attempts, wall-clock budget, `Retry-After` honored within
  it); `event_id` is stable across retries and sent as the
  `Idempotency-Key` header, so receivers can deduplicate.
- **Allowlisted payload:** `message` is never forwarded (it may carry
  stderr or verifier reasons); `url` is set only for events whose link is
  the payload (PR created). Redirects are not followed; the webhook URL
  never appears in Maestro's logs.
- **Generic receiver, not a Slack/ntfy payload:** Slack Incoming Webhooks
  and ntfy expect their own formats — point `webhook_url` at your own
  receiver or a small relay that reformats the envelope for them.

The `telegram_token` / `telegram_chat_id` config fields are deprecated and
non-functional; they will be removed in a future config-schema window.

## Supported Agents

| Agent | Key | Notes |
|-------|-----|-------|
| Claude Code | `claude_code` | Default. Requires `claude` CLI |
| Codex | `codex_cli` | Requires `codex` CLI |
| OpenCode | `opencode` | Requires `opencode` CLI |
| Aider | `aider` | Requires `aider` CLI |
| Announce | `announce` | Dry-run mode — logs tasks without running an agent |

## Development

```bash
git clone https://github.com/andrei-shtanakov/maestro.git
cd maestro
uv sync
uv run pytest
uv run ruff check .
uv run pyrefly check
```

## Codex review kit (vendored)

`scripts/review/` + `.github/codex/review-schema.json` — вендор-копия
codex-review-кита из steward (независимое ревью дифа другой моделью), пин —
`scripts/review/PIN`. Copy-integrity проверяет джоба `review-kit-integrity`
в CI (чекер исполняется извлечённым из base), дрейф от продюсера ловит
вахта `review-kit-drift.yml`. `review-prompt.md` — данные этого репо (вне
integrity), generated-файлы объявляются в `.gitattributes`
(`linguist-generated`). Локальный прогон: `sh scripts/review/local.sh`.
Ре-вендор — рецепт в комментарии PIN; смена состава кита — двухшаговая
дисциплина из шапки `checksum.sh`.

## License

MIT — see [LICENSE](LICENSE).
