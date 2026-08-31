# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## `../_cowork_output/` — dev-only

Координационный dev-scratch воркспейса; у пользователей и клонов проекта его НЕТ.
Shipped/runtime-код никогда не читает и не резолвит пути под ним; кросс-репные
контракты вендорятся пиненой копией внутрь, не ссылкой наружу. Ссылаться на него
могут только dev-тулинг самого воркспейса и документация. Канонические факты живут
в репо-владельце (пример: SSOT agents-catalog — `atp-platform/method/agents-catalog.toml`,
ADR-ECO-003). Полное правило (SSOT): `../prograph-vault/authored/rules/cowork-output.md`.

## Project Overview

Maestro is an AI Agent Orchestrator with two operation modes:

1. **Task Scheduler** (`maestro run`) — coordinates multiple AI coding agents (Claude Code, Codex, Aider) on tasks defined in a single YAML config. All tasks share one directory.

2. **Multi-Process Orchestrator** (`maestro orchestrate`) — decomposes a project into independent work units ("workstreams"), runs each in an isolated git worktree via spec-runner, and creates PRs on completion.

## Development Commands

```bash
# DB path: every command below defaults to ~/.maestro/maestro.db (writers AND
# readers). Pass --db <path> on both sides only for a per-project/isolated DB —
# `--db maestro.db` (repo-local) works only if the run was started the same way.

# === Task Scheduler (original mode) ===
uv run maestro run <config.yaml>
uv run maestro run config.yaml --resume  # Resume after crash
uv run maestro status
uv run maestro retry <task-id>
uv run maestro stop                          # Stop the running scheduler
uv run maestro approve <task-id>             # Approve an AWAITING_APPROVAL task

# === Multi-Process Orchestrator (new mode) ===
uv run maestro orchestrate <project.yaml>   # Run orchestrator
uv run maestro workstreams                   # Show workstreams status
uv run maestro workstream-approve <workstream-id>  # Approve a NEEDS_REVIEW workstream — records the durable gate approval (phase+sha) and re-queues
uv run maestro workstream-rework <workstream-id> --reason "<why>" [--instructions "<next attempt>"] [--refresh-from project.yaml]  # Rework (NOT approve): NEEDS_REVIEW/FAILED -> READY into re-decomposition; fail-closed liveness proof; audited
uv run maestro workstream-resolve-ambiguity <workstream-id> --statement "<how verified>"  # Resolve a recovery-ambiguity marker after manual cleanup (unblocks rework)
uv run maestro workstream-quarantine <workstream-id> --reason "<why>"  # Forbid this workstream's result from progressing (#166): no new dispatch, delivery withheld (finished -> NEEDS_REVIEW). Does NOT kill a running execution; NOT a rework, NOT an approval. Refuses once delivery started (MERGING/PR_CREATED/DONE) — there the remedy is a revert
uv run maestro workstream-unquarantine <workstream-id> --reason "<why>"  # Lift the durable freeze ONLY: no status change, no approval, no resume, nothing started
uv run maestro workstream-continue <workstream-id>  # Queue a continuation over the EXISTING tasks.md (#166 B): runs the MISSING tasks, regenerates nothing, respawns no author. Only queues — the orchestrator re-checks the preconditions right before spawning, and that late check is the guarantee
uv run maestro workstream-recapture <workstream-id>  # Retry ONLY post-mortem evidence capture for the same execution after a `post-mortem capture failed` block (no executor, no decomposition); NOT an approval
uv run maestro postmortem <project.yaml> --gc         # Apply the post-mortem retention policy (same one the orchestrator applies after each capture)
uv run maestro check-scope <workstream-id> --base <base-branch>  # deterministic scope containment (exit 1 on escape)
uv run maestro workspaces <project.yaml>     # List active worktrees
uv run maestro review-pr <project.yaml> <workstream-id>  # Drive spec-runner's review-bot loop over that workstream's PR (needs spec-runner >= 2.21.0; exits 0 complete / 1 infra / 2 needs-human / 3 already-running)
uv run maestro review-pr <project.yaml> --all            # Same, sequentially over every workstream PR

# === Scheduled autonomous runs (service wrapper) ===
uv run maestro service run <project.yaml> [--stage orchestrate|review]   # One tick: decides resume/fresh/no-op from DB state (this is what launchd/systemd call)
uv run maestro service install <project.yaml> --schedule "03:00"         # Generate + load the launchd/systemd user unit (refuses if harness binaries/credentials don't resolve)
uv run maestro service status <project.yaml>                             # Recent ticks (stage, decision, outcome, exit)
uv run maestro service uninstall <project.yaml>                          # Unload + remove the unit

# === Mode-2 config authoring ===
uv run maestro init                          # Scaffold project.yaml from cwd
uv run maestro validate project.yaml         # Preflight: cycles, scope overlap, repo sanity
uv run maestro validate project.yaml --strict --no-fs  # CI mode, no filesystem access

# === Model catalog management (ADR-ECO-003b D3) ===
uv run maestro models init --path ~/.config/atp/agents-catalog.toml   # Scaffold user catalog
uv run maestro models list                                            # Show resolved catalog
uv run maestro models discover --observed observed.json               # Propose additions (exit 2 = new found)
uv run maestro models update --observed observed.json --dry-run       # Apply proposals (Plane 1 only)

# === Agent benchmarking (R-06b M5) ===
uv run maestro benchmark swe-mini --agent claude_code            # Run one ATP benchmark
uv run maestro benchmark swe-mini --agent opencode --json        # Machine output (stdout = JSON)
# MAESTRO_ARBITER_BIN set -> result reported to arbiter (fire-and-forget)

# === Log utilities ===
uv run maestro merge-logs <pipeline-dir>     # Time-sort per-pid JSONL into merged.jsonl
uv run maestro costs                   # database-wide cost summary (read-only; TOTAL / by-harness / by-task; unpriced = UNKNOWN, not $0)

# === Tests ===
uv run pytest
uv run pytest tests/test_models.py -v       # Single file
uv run pytest -k "test_dag" -v              # By pattern

# === Type checking ===
uv run pyrefly check

# === Linting and formatting ===
uv run ruff format .
uv run ruff check .
uv run ruff check . --fix

# === Dependencies (NEVER use pip) ===
uv add <package>
uv add --dev <package>
```

## Gotchas

Each of these has cost a full session at least once — they are not hypothetical.

- **Run pytest in the FOREGROUND.** A workspace watchdog kills long-running *background*
  `pytest` processes under contention: the run reports "killed" with empty output, and the
  orphaned processes keep holding SQLite locks (once for 15h). Verify with targeted
  foreground runs (specific files, `-k` halves) plus `pyrefly check` and `ruff`, and let PR
  CI carry the full suite. Never offload the suite to a background wait.
- **Any test that builds a `Database` must close it via a fixture** (`yield d; await d.close()`).
  An unclosed aiosqlite connection is a ResourceWarning-as-error plus a lingering thread —
  it surfaces as a ~120s hang, not as a failure.
- **pyrefly silently checks ZERO files under an excluded path.** It honors every
  gitignore/exclude file *up the tree* and excludes dot-directories (`**/.[!/.]*/**/*`), so
  running it from inside `.worktrees/<name>/` reports a clean `0 errors` having examined
  nothing. Put linked worktrees at a **sibling, non-dotted, non-ignored** path
  (`../maestro-<slug>-wt`), and treat the `INFO N errors` completion line as the evidence.
- **Other sessions share this workspace.** A parallel actor may hold `master` in another
  worktree (`git worktree list`) and may be mid-migration in the same files — check before
  claiming a migration number, and expect tmp-path/plugin-ordering flakiness under broad
  `-k` selection that disappears when the file is run standalone.

## Architecture

### Core modules in `maestro/`

**Shared infrastructure:**
- **models.py**: Pydantic models (Task, TaskStatus, Workstream, WorkstreamStatus, OrchestratorConfig)
- **config.py**: YAML parsing with defaults merging, env var substitution, `load_orchestrator_config()`
- **catalog.py**: Model catalog loader (ADR-ECO-003b). `resolve_model()` applies the precedence `routed > MAESTRO_<H>_MODEL > catalog-default > fail-loud`; the catalog (loaded from `$ATP_CATALOG`, no baked default) supplies only the last-resort *default* layer, used when neither a routed model nor the env var provides one. Also emits a status-graded coherence warning. Fault taxonomy by blast radius: `CatalogError` (global — halts the run) vs `HarnessModelUnresolved` (per-task — sends that task to `NEEDS_REVIEW`). `check_catalog_references()` validates Plane 3 against Plane 1 on every load (#188): V1–V5 are `CatalogMalformed` — the catalog is rejected whole, because partial acceptance would route work over a silently pruned agent set — while V6 (deprecated reference) and V7 (a `harnesses.*.kind` outside the vendored `harness_kind` set) warn. **V1/V5 arm on Plane 2 being DECLARED, not on it having entries** (`_reference_checks_armed` reads `model_fields_set`): a bare `[harnesses]` header declares zero harnesses, so every enrollment is a V1 violation — canon from devtools#47, which overruled Maestro's earlier scaffolding reading. An **absent** plane leaves them unevaluated, and that is still a hole rather than a decision: `maestro models init` emits no Plane 2, so on catalogs it scaffolds the harness reference is unverified — the loader announces that with `catalog.reference_checks_not_armed` rather than reading silence as health (`@id:models-init-harnesses-plane`). The ADR-ECO-003 enum vocabularies are **vendored, not declared**: `model_statuses()`/`harness_kinds()` read `vocabulary.toml` from `maestro/resources/catalog_conformance/` via `importlib.resources` (shipped in the package because the conformance set lives under `tests/`, which is not in the wheel; a test asserts the two copies are byte-identical so one pin covers both). No Python names any value, so an additive upstream bump is adopted by re-vendoring alone; `CatalogModel.status` is therefore a `str` with a validator rather than a `Literal`, and an unreadable vocabulary is fail-loud (`CatalogVocabularyUnavailable`), never an empty set. Conformance against the SSOT fixture set is `tests/test_catalog_conformance.py` over a pinned vendored copy (`tests/fixtures/catalog-conformance/v1/`, devtools `070acdc`), integrity-checked before parametrization so a truncated copy cannot shrink into a smaller green suite; the one unmet expectation (missing `$ATP_CATALOG` file must error) is held by a strict `xfail`, not by a softened test
- **catalog_discovery.py**: Pure diff logic for `maestro models` — observed-manifest contract (missing vendor key = not observed; empty list = observed-and-empty), alias-aware new-model detection, vendor-conflict reporting, TOML-escaped Plane-1 rendering
- **catalog_cli.py**: `maestro models init|list|discover|update` Typer sub-app (ADR-ECO-003b D3) — init from the shipped inert template (`maestro/resources/`), read-only discover (public exit contract 0/2/1), update via validate-then-atomic-replace
- **database.py**: SQLite layer with async CRUD, WAL mode (tasks + workstreams tables)
- **dag.py**: DAG building, cycle detection, topological sort, scope overlap warnings
- **git.py**: Git operations (branch, rebase, push, worktree, merge)
- **cli.py**: Typer CLI (run, status, retry, stop, approve, orchestrate, workstreams, workstream-approve, check-scope, workspaces, merge-logs, costs, models)
- **scheduler.py**: Main scheduler loop — polls DAG, spawns agents, monitors completion
- **validator.py**: Post-task validation (run validation_cmd)
- **retry.py**: Exponential backoff retry logic with jitter
- **recovery.py**: State recovery after crash
- **cost_tracker.py**: Token usage parsing and cost calculation
- **event_log.py**: Structured event logging for task lifecycle
- **merge_logs.py**: Standalone merge-logs CLI — time-sorts per-pid JSONL into merged.jsonl
- **preflight.py**: Mode-2 config validation — ValidationReport (errors/warnings), dangling-dep + cycle detection (shared dag.find_cycle), two-tier scope-overlap (static heuristic + exact file-set intersection), repo/glob filesystem checks; runs standalone (`maestro validate`) and as a fail-fast gate inside `maestro orchestrate`
- **scaffold.py**: `maestro init` generator — commented project.yaml template with git-derived autofill, self-checked against OrchestratorConfig before writing
- **spec_runner.py**: Integration boundary between Maestro and the external spec-runner
- **transitions.py**: The single declarative status→side-effect table (`TASK_EFFECTS` / `WORKSTREAM_EFFECTS`) plus its dispatcher — events and notifications are *derived* from a status transition, never hand-synced at each call site (idea #10). A totality test asserts every status has an entry, so **adding a status forces a table entry** — that is why a new phase like `VERIFYING` necessarily touches this file.
- **logging_bridge.py**: `ObsBridgeHandler` + `setup_logging` — routes every stdlib `logging` call into the obs OTel JSONL pipeline; WARNING+ is additionally mirrored to stderr (replacing `lastResort`). The vendored `_vendor/obs.py` is untouched by this.
- **changed_paths.py**: Git-diff → repo-relative POSIX path list, the input to both the scope gate and the verifier diff
- **correlation.py**: WorkCorrelation v1 reference implementation (`contracts/work-correlation/`) — Maestro mints `work_item_id` (= task/workstream id), surjective status projection onto the common enum with `source_status` kept verbatim, spec↔DAG bridge (`<parent>/<TASK-nnn>` + `source_locator`)

**Multi-process orchestration (new):**
- **orchestrator.py**: Main async loop — decompose, spawn, monitor, PR creation. On resume it first reconciles workstreams stranded by a prior hard crash (DECOMPOSING/RUNNING/MERGING/PR_CREATED → READY; a live-orphan RUNNING (process_pid) or DECOMPOSING (generation_pid) → NEEDS_REVIEW; FAILED by the retry rule) so the main loop can advance them. The spawn→persist window (crash between spawning a subprocess and persisting its pid) is closed by a spawning sentinel written before the spawn: recovery reads it as a possible live orphan → NEEDS_REVIEW. A recovery NEEDS_REVIEW thus means either a confirmed live orphan or a spawn-in-progress (state uncertain).
- **workspace.py**: Git worktree lifecycle (create, setup, cleanup)
- **decomposer.py**: Project decomposition via Claude CLI into workstreams (`decompose`) + async spec generation delegated to `spec-runner plan --full` (`generate_spec` — spec-runner owns the tasks.md format; runs as a background task in the orchestrator, budget-capped via `SpecRunnerConfig.spec_gen_budget_usd`)
- **pr_manager.py**: GitHub PR creation via `gh` CLI

**Governance gates (Mode 2, opt-in — absent `gates:` config is a byte-identical no-op):**
- **gates.py**: Guard hooks on exactly two workstream edges — **ex-ante** before `READY -> RUNNING` (classifies the *declared* `scope`) and **ex-post** before `RUNNING -> MERGING` (classifies the *actual* diff, so it catches scope violations the author actually committed). Risk tiers come from `steward risk-classify` and **only** from there — Maestro never computes risk itself (DESIGN-610/612). Fail-closed at every tier: a mandatory gate whose verdict is missing or errored *blocks*. A blocked workstream goes to `NEEDS_REVIEW` carrying an approval marker; a new commit changes the SHA and invalidates the approval. Gates whose enforcement point lies outside these two edges (branch protection, PR review) are recorded as advisory annotations, not blocks — that transition belongs to the git host, not to Maestro's table.
- **gate_catalog.py**: The `gate_id` namespace rule (#160) over a **vendored** copy of steward's gate catalog (`maestro/resources/gate_catalog/upstream/`, pinned at steward `afd192f`; shipped in the package because the sibling checkout does not exist for installed users). The two namespaces are asymmetric on purpose: a canonical `GC-*` id **must resolve** in the vendored catalog, and one that does not is fail-closed — never a pass, never an invented record; a producer id (`<namespace>.<name>`) is validated by **shape only and never resolved**, because outside `GC-*` steward defines nothing and catalog membership is decided by resolving the id, never inferred from a field's presence. The leading segment names the originating *tool*, not the owner: `steward.risk_classify_*` written here is a Maestro-owned id. Patterns and reserved tokens are read from the vendored file rather than re-declared in Python — that mirror exists because consumers vendor the file and not the loader, and it is not a knob.
- **gate_approvals.py**: The `gate_approvals` table is the *single authority* on "was this (workstream, phase, sha) approved", written in one transaction by `maestro workstream-approve`. The marker in `error_message` is operator UX plus the H-6 resume-position signal; the verdict store is pure evidence. **Neither of those grants approval** — only the table does.
- **approver.py** (+ orchestrator wiring): opt-in `gates.approver` hook (#137) — an *automated operator* for ex-post blocks: an external critic command gets a run-keyed request envelope (built ONLY from the immutable `gate_block_contexts` snapshot persisted at block time) and returns a verdict under a strict echo handshake; PASS goes through the same approval API with `actor='agent'` (post-verdict cost check + stale-SHA recheck + CAS), everything else stays NEEDS_REVIEW for the human. Guard skips are `not_run` observations that never consume the one-paid-evaluation-per-SHA slot (`gate_approver_runs`, migration 20); kill-switches (`enabled: false`, `MAESTRO_APPROVER_DISABLED=1`) are reversible. Design spec: `docs/superpowers/specs/2026-08-06-expost-approver-cmd-design.md`.
- **tasks_spec.py**: Vendored `tasks.md` format contract (spec-runner 2.24.0, `VENDORED_FROM_SPEC_RUNNER`) + `find_dangling_dependencies` (#165) — every dependency must resolve inside the CURRENT revision of the generated file. Runs after `plan --full` and before any spawner, so a rework's cross-revision reference costs a cheap block instead of a paid generation plus a spawned process. Refs are filtered by the prefixes the task headers actually use, which is what keeps `**Traces to:** [REQ-001]` and `**Blocks:** [TASK-999]` from reading as dependencies. Also owns `SELF_CONTAINED_DEPENDENCIES_INSTRUCTION` — prevention appended to every rework regeneration, never the guarantee
- **retry_policy.py**: Retry-fitness classification (#165) — three `stop_reason` values (`validation_failed`, `state_spec_mismatch`, `dependency_blocked_after_skip`) route straight to NEEDS_REVIEW because a retry pays a full re-decomposition it cannot benefit from; everything else, **including unknown, dynamic `error_*`, empty and absent**, keeps the existing policy. Keys off the typed reason only — never `stop_detail` prose, never run duration
- **completeness.py**: The DONE completeness decision (#164) — pure counters-in/verdict-out, four blocking reasons (`incomplete` / `unknown_total` / `inconsistent` / `unreadable`) because each asks the operator for something different, plus the `completeness` approval phase and the evidence-freshness rule that stops one approval from accepting the next partial result
- **postmortem.py**: Evidence capture before any destruction (#164) — `backup()`-consistent state snapshot + harness logs + self-describing manifest, committed by a single directory rename out of `.partial/`, bounded by a byte cap, with retention. Capture hangs off finalization's `on_collected` (the one moment every transport agrees on: collect applied, nothing destroyed yet) and a failure there raises `EvidenceCaptureFailed`, which skips cleanup and preserves the workspace. A missing state db is NOT a capture failure — capture runs for failed runs too, and raising would cost them their retry
- **config_drift.py**: Resume-time comparison of `project.yaml` against the workstream configuration the run persisted at creation (#198) — pure, no I/O. Persisting the config is correct (a run must not change its own rules mid-flight); the defect was continuing **silently**, which reads as "my edit applied and did not help". Compares every field `Workstream.from_config` sets, plus the derived `branch` and the id set; `scope`/`depends_on` are order-insensitive. Detection lives in `_ensure_workstreams`, but the halt (`ConfigDriftDetected`) is raised in `run` **after** recovery: drift forbids new dispatch, decomposition and delivery, never the liveness/reconciliation pass over existing handles. Fail-closed with no override flag, and the run records no outcome so it stays resumable. An empty `workstreams:` is the one shape persisted rows cannot disambiguate (auto-decomposed vs section deleted), so migration 28 records `run.workstreams_declared`; NULL (pre-migration runs) **fails open** deliberately — halting every legacy auto-decomposed run would be worse than the hole
- **scope_gate.py**: Pure containment matcher (no git, no FS, no DB) — `find_escapes` answers only "which changed paths are matched by none of the declared scope globs". Backs `maestro check-scope` (exit 1 on escape) and the ex-post gate.

**Subpackages:**
- **spawners/**: AgentSpawner ABC + implementations (claude_code, codex_cli, aider, announce, opencode) + registry. opencode (`opencode run --format json -m opencode/<model>`, ADR-ECO-003c) is the first open-model agentic harness: open models (glm-5.1, qwen3.6, …) reach routing as `opencode@<model>`. Its cost comes from opencode's own per-step `part.cost` (persisted as `TaskCost.reported_cost_usd`); when the log reports none, the arbiter sees unknown (`cost_usd=None`), never 0.0
- **coordination/**: MCP server (FastMCP) + REST API (FastAPI) with /workstreams endpoints; Arbiter routing (`routing.py` strategies, vendored `arbiter_client.py` MCP client, `arbiter_errors.py`)
- **benchmark/**: R-06b/R-07 benchmark-aware routing — async runner, ATP client, spawner→responder adapter, and Arbiter feedback wiring (`arbiter_report.py`)
- **notifications/**: Desktop notifications (macOS/Linux)
- **dashboard/**: Web UI with DAG visualization (Mermaid.js) + SSE updates
- **execution/**: The transport/isolation layer every long-running subprocess goes through — `ExecutionRequest` → `Isolator` (`BareIsolator` identity / `DockerIsolator` / `VerifierDockerIsolator`) → backend (`LocalBackend`, `SshBackend`) → durable `TaskHandle`, plus `finalize.py` (single-owner finalization; a cleanup fault never rewrites the exit code), `resolver.py` (per-dispatch backend choice), `reservations.py` (`(workdir, scope)` locks with conservative path-anchor overlap), and `probe()` — the one isolation-aware, fail-closed recovery boundary shared by Mode-1 `StateRecovery` and the Mode-2 orchestrator. Task, validation and verification executions are all the same machinery, distinguished only by `execution_phase`.
- **domain/**: Mode-2 domain verification (Stage B) — `VerificationProvider` Protocol, verdict contract v2 (`schemas/verdict_v2.json`) with its run-keyed handshake, `CommandVerifier`, and the evidence ledger. Activation is **by presence**: no `domain.verification.verifier` in `project.yaml` means the pre-Stage-B path runs byte-identically.
- **verifier/**: Mode-1 task-level verifier gate (config / diff / envelope / prompt / judge). Reuses the `domain/` verdict primitives additively rather than shipping a parallel stack.
- **resources/**: Shipped inert templates (e.g. the catalog scaffold `maestro models init` copies)
- **schemas/**: JSON-schema generation for config/contract artifacts
- **_vendor/**: Vendored observability lib (`obs.py`) — structlog-based spans, trace propagation, and `child_env()` for cross-process trace continuity

### Task State Machine (scheduler mode)

```
PENDING -> READY -> RUNNING -> VALIDATING -> DONE
             |        |  |         |
             |        |  |         └-> FAILED -> READY (retry)
             |        |  |              |
             |        |  └──FAILED──────┴-> NEEDS_REVIEW -> READY
             |        |                      |
             |        └──NEEDS_REVIEW────────┘   (catalog default unresolved for harness)
             |                            |
             |                            └-> ABANDONED
             |
             └-> AWAITING_APPROVAL -> READY   (requires_approval; `maestro approve
                       |             <task-id>` sets READY, then scheduler runs it)
                       └-> ABANDONED
```

A verifier-enabled task (project-level `verifier:` block + the task has both
`validation_cmd` and a non-empty `scope`) inserts a third phase between
`VALIDATING` and `DONE`: `VALIDATING -> VERIFYING -> {DONE (PASS), FAILED/READY
(FAIL, retried), NEEDS_REVIEW (ERROR, fail-closed)}`. See the verifier gate
bullet under Key Design Decisions below. No `verifier:` block keeps
`VALIDATING -> DONE` byte-identical to today.

### Workstream State Machine (orchestrator mode)

```
PENDING -> DECOMPOSING -> READY -> RUNNING -> MERGING -> PR_CREATED -> DONE
                            |        |  └-> FAILED -> READY (retry)
                            |        |              └-> NEEDS_REVIEW
                            |        └-> VERIFYING   (domain verification)
                            |               |-> PASS + evidence commit  -> MERGING
                            |               |-> FAIL, rework left       -> READY
                            |               |-> FAIL, budget exhausted  -> NEEDS_REVIEW
                            |               |-> ERROR retries exhausted -> FAILED
                            |               └-> live orphan/ambiguity   -> NEEDS_REVIEW
                            |        (READY + reverify marker -> VERIFYING;
                            |         finalization happens INSIDE VERIFYING)
                            └-> ABANDONED
```

The **completeness gate** (#164, always-on) sits at the head of the success
continuation, before both diff gates: `done` (from the post-mortem archive)
must equal `workstreams.subtask_total`, or the workstream goes to
NEEDS_REVIEW instead of MERGING. Fail-closed on an uncaptured denominator, a
missing archive or an unreadable manifest; no config key or env var disables
it. An all-no-op run passes and is reported as a structured event —
completeness is not productivity.

**Quarantine (#166, always-on when set).** `quarantined_at` on the workstream
row — deliberately not a status, because a quarantined workstream's process
keeps running and the row must stay RUNNING for the existing
`expected_status=RUNNING` CAS. While set: the workstream never becomes ready
(no dispatch) and delivery is withheld — a finished quarantined workstream
**parks in NEEDS_REVIEW for an operator decision** instead of merging. The
guarantee is the CAS on `RUNNING -> MERGING` carrying
`require_not_quarantined`; the check at the head of `_handle_success` is only
an optimisation that avoids paying for a risk classification on a diff nobody
will deliver. A row already in MERGING/PR_CREATED/DONE cannot be quarantined —
the remedy after delivery is a revert.

**Continuation (#166 B).** `resume_reason = continue_tasks` re-dispatches
spec-runner over the existing plan. Four preconditions are **re-checked
immediately before the spawn**, and that late check — not the one
`workstream-continue` ran — is the guarantee: a live process or handle (never a
second spec-runner over one worktree), a present worktree, a `tasks.md` that
passes #165's validator, and a readable executor state DB. Any of them
unproven → NEEDS_REVIEW with a distinct reason, resume marker cleared so the
next loop does not retry itself, nothing spawned, nothing generated, counter
untouched. `continuation_count` records **accepted dispatch attempts** (it
moves inside the `READY -> RUNNING` CAS); past a threshold the operator is
warned, never blocked.

**Recovery capture is phased (#166 B).** `probe/classify` (unchanged) → decide
whether capture is needed → one shared checkpoint → the branch's original
action. Only **provably-dead / stranded** executions are archived: a live orphan
returns to monitoring un-archived (a process still writing yields a torn
snapshot), and a stranded DECOMPOSING has no executor state to keep. Capture
runs **before** cleanup, requeue or the `FAILED -> READY` reset — each of which
overwrites what is being captured — records `captured_by: recovery`, and is
idempotent per execution. A capture failure preserves the worktree and routes
to `workstream-recapture`.

**Shutdown drains (#166).** The first SIGTERM/SIGINT forbids new dispatch and
keeps the loop monitoring live executions until each finalizes
(`_should_keep_looping`); it terminates nothing. A **second** signal forces
termination and may leave work for recovery. Before #166 a routine
`maestro stop` terminated everything and reset each workstream to plain READY,
which means "Always regenerate" — the same destruction as an external SIGKILL.

Two more always-on guards sit earlier in the lifecycle (#165): the generated
`tasks.md` is validated for dangling dependencies **after spec-gen and before
any spawner** (a violation blocks with no retry consumed; an unreadable file
logs `tasks_validation.skipped` and proceeds, since spec-runner remains the
final validator), and a failure whose typed `stop_reason` is one of
`validation_failed` / `state_spec_mismatch` / `dependency_blocked_after_skip`
goes straight to NEEDS_REVIEW instead of spending another re-decomposition.
Unknown, dynamic `error_*`, empty and absent reasons keep the existing retry
policy — unclassified is not unfit.

A **second retry-fitness axis** (#209) reads the archived per-attempt
`error_code` for spec-runner's `TASK_BLOCKED` — an agent's deliberate refusal,
which spec-runner itself treats as fatal. A retry cannot lift a refusal, and
worse, it regenerates the spec and destroys the executor state the operator's
remedy (`spec-runner tdd repair`) works against; NEEDS_REVIEW keeps the
worktree and that state alive. Keyed off the **attempt**, never the task
status: `TASK_BLOCKED` is fatal, so the task never reaches `failed`. The
verdict is three-valued — `blocked` / `not_blocked` / `unreadable` — and
`unreadable` fails closed, because on this path `_handle_completion` is
unreachable without a committed archive, so an unreadable one contradicts a
guarantee. The exception that only looks like silence: the archive's own
`state_missing: true` keeps its retry (no database means no attempt was ever
recorded, so no refusal existed), which is the retry #164 deliberately
preserved.

**Three recovery paths out of NEEDS_REVIEW, and they must not be confused:**

| Verb | `resume_reason` | What runs |
|---|---|---|
| `workstream-approve` on a completeness block | `completeness_accept_partial` | Nothing. Accepts the incomplete result and continues the existing delivery tail over the untouched worktree — no author respawn, no spec-gen, no new sha. Catching up the missing tasks is #166's concern and has no mechanism here. |
| `workstream-recapture` after a capture failure | `postmortem_recapture` | Only the archive step, for the same execution, then the same delivery tail. Not an approval: nothing about the result is accepted. |
| `workstream-rework` | `operator_rework` | An ordinary re-decomposition through DECOMPOSING — the author is respawned and the spec regenerated. |
| `workstream-unquarantine` | *(none)* | Lifts the durable quarantine freeze and nothing else — no status change, no approval, no dispatch. Listed here because it is easy to mistake for a resume; it is not one. |
| `workstream-continue` | `continue_tasks` | Runs the tasks that are **missing**, over the existing `tasks.md` — no regeneration, no author respawn. The only member of this family that executes work. |

The first two refuse rather than falling back to a respawn: a respawn would
regenerate the spec and mint a new sha, voiding the very approval that got
there. A completeness approval is bound to BOTH the worktree sha and the
evidence snapshot (`evidence=<execution_id>` in the marker), so it goes stale
after a rework or a re-collect and cannot silently accept a different partial
result.

With a `gates:` block configured, two guard edges are inserted (see `gates.py` above):
`READY -> RUNNING` is preceded by the **ex-ante** gate and `RUNNING -> MERGING` by the
**ex-post** gate; either one blocking routes the workstream to `NEEDS_REVIEW` instead of
advancing. Both are fail-closed — a missing or errored verdict blocks at every tier.

An ex-post gate block that the operator approved resumes at the ex-post edge (H-6): READY + approval marker + unchanged worktree HEAD -> straight to MERGING tail, no regen/respawn; the marker in error_message clears only at DONE.

### Key Design Decisions

- **Two modes**: Scheduler for single-process tasks, Orchestrator for multi-process isolation
- **Workspace isolation**: git worktree per workstream (lightweight, shares .git)
- **Two-level hierarchy**: Orchestrator manages workstreams, spec-runner manages subtasks within each
- **Git strategy**: `feature/<workstream-id>` branch per workstream, subtask branches merge into it, then PR to main
- **Communication**: REST API callbacks from spec-runner (state file polling deprecated) for local/Docker backends. Phase 2a SSH backend (remote/NAT'd executors, unreachable for inbound callbacks) deliberately reintroduces polling in a different shape: a WAL-safe `sqlite3.backup()` snapshot of the remote spec-runner DB mirrored back over SSH each tick (`maestro/execution/ssh_mirror.py`), not the old raw state-file poll. Docker Isolation Phase 1 adds a container-backed execution path (`backend: docker`, local Docker isolation); recovery for docker-backed executions keys off the execution-handle label, not a pid. Phase 2b enables **Mode-1 remote** (`maestro run` on an ssh backend): a static per-workdir arming gate + a `(workdir, scope)` reservation lock (conservative path-anchor overlap) + scope-bounded collect. Validation still runs locally after collect (`validation_backend` deferred); collect is `scope_paths` only (patch-collect deferred); local Docker Mode-1 is unchanged. Phase 2c enables **SSH + Docker isolation** (`transport: ssh` + `isolation: {type: docker}`): the center rewrites the launch argv into a remote `docker run` (supervisor unchanged), drives container lifecycle/recovery via `DockerCli` run over SSH, defaults the container user to the remote uid:gid (root only via explicit `user: "0:0"`), and recovery probes BOTH the remote process-group and the container (fail-closed → NEEDS_REVIEW).
- **Validation backend (PR1 + PR2 + PR3)**: post-task validation runs through the execution layer as a second `ExecutionRequest` (`validation_backend: local | same | <name>`, default **`same`** since PR3). Non-local (docker/named-local/SSH) validation is durable — its own `execution_id` + `execution_phase='validation'` handle. PR2 adds SSH validation: a fresh remote layout, a real `CollectPolicy(none)` no-op in `SshTaskHandle` (no collect for validation), and dual-probe recovery routed through `ExecutionBackend.probe()` as the single isolation-aware boundary (bare + docker, fail-closed → NEEDS_REVIEW) shared by Mode-1 `StateRecovery` and the Mode-2 orchestrator; GC is transport-aware (never docker-GCs an SSH terminal handle). `check_validation_backends` no longer fails loud on an SSH target; `maestro validate` remains Mode-2 only. **PR3 flipped the default `validation_backend` `local -> same`** (release-noted in CHANGELOG): validation runs in the task's own backend by default — a no-op when the task runs on bare `local` (`same` -> `default_backend` -> `local`), and a durable in-backend validation when the task runs on docker/SSH. Existing persisted tasks keep their recorded value (no data-migration; migration 12 rebuilds `tasks` only to change the column default, under `foreign_keys=OFF` so CASCADE children survive).
- **Verifier gate (Mode-1, opt-in)**: distinct from "Domain verification (Stage B)" below (that one is Mode-2/workstream-level; this is Mode-1/task-level). A third task phase, `VERIFYING`, gated by an adversarial LLM judge over the task's scope-bounded diff — inserted only when `Scheduler._verifier_enabled` holds: a project-level `verifier:` block is present AND the task has both a `validation_cmd` and a non-empty `scope` (nothing to gate, or no bounded diff to judge, otherwise). Config (`maestro/models.py::VerifierConfig`): `runner: claude` is the only supported value this slice; `model` is required, via `verifier.model` or `$MAESTRO_VERIFIER_MODEL` (`maestro/verifier/config.py::resolve_verifier_model` — a precedence isolated from the main harness's `resolve_model`: never reads `MAESTRO_<HARNESS>_MODEL`, never falls back to a catalog default, so the judge can't silently end up on an expensive main model); `timeout_seconds`, `max_diff_bytes`; `backend: local` (default) or `backend: docker` — "policy isolation" (scratch cwd, no collect, envelope on stdin, `inherit_env=False`) for the local backend, or strict Docker filesystem/process isolation (read-only root, cap-drop=ALL, no-new-privileges, non-root uid:gid, tmpfs `/scratch`, digest-pinned image, one `ANTHROPIC_API_KEY` env-file) for the docker backend. Note: Docker backend provides **filesystem/process isolation, not network isolation** — the container retains unrestricted bridge networking so the judge can reach the API. Eager fail-loud Docker preflight validation runs globally; `backend: local` remains byte-identical. Reuses the shared execution layer with `execution_phase="verification"` (same recovery/probe machinery as task/validation executions) — migrations 16/17 add `tasks.verifier_baseline_sha` (nullable, the gate's diff baseline) and `task_costs.execution_phase`/`task_costs.model`. Fail-closed routing: PASS -> `DONE` (+ auto-commit); FAIL -> the existing retry path (judge findings folded into the retry context, same NEEDS_REVIEW-on-exhaustion as any validation failure); ERROR (bad envelope, judge-process crash, unresolvable model, oversize/binary diff) -> `NEEDS_REVIEW` straight from VALIDATING/VERIFYING, never softened to FAIL. Once verifier-enabled, a task's `(workdir, scope)` reservation is held for its WHOLE lifecycle — first dispatch through every retry and NEEDS_REVIEW, releasing only at DONE/ABANDONED (unlike the post-collect release for non-verifier tasks) — for unambiguous diff attribution; a restart reconstructs this from any non-terminal task with a recorded baseline, not just an open execution handle. Durable + recoverable: a crash mid-`VERIFYING` is recovered fail-closed straight to `NEEDS_REVIEW` (never auto-re-run), reconciling the `execution_phase='verification'` handle once the judge process is provably dead; `maestro retry`'s `NEEDS_REVIEW -> READY` requeue fences on that handle still being open, succeeding once reconciled. `maestro costs` gains `by_phase`/`by_model` breakdowns so verifier judge spend is visible separately from task/validation cost (an envelope with no usage leaves the row UNKNOWN, never $0). See `examples/with-verifier.yaml` and `examples/with-verifier-docker.yaml` for examples.
- **`git.run_branch` (Mode-1, opt-in)**: run-level branch isolation — start/continuation gates (phase A) plus per-seam live tripwires that suspend-with-drain on foreign branch movement (phase B, spec §7).
- **Conflict prevention**: Workstreams define `scope` (file/dir globs), decomposer validates non-overlap
- **Storage**: SQLite (single file, no external services)
- **Spec-runner**: External package (PyPI) handles subtask execution within a worktree
- **Domain verification (Stage B)**: activation is by-presence — `domain.verification.verifier` in a workstream's `project.yaml` turns the VERIFYING phase on; an absent `domain:` (or no `verifier`) takes the pre-Stage-B path byte-identically (zero-change guarantee, proven by the unchanged full test suite). Verdict contract v2 is run-keyed with a strict handshake (echoed run_id/attempt/sha); malformed or mismatched echoes are protocol ERROR, never softened to FAIL. The verifier subprocess never receives `workstream_id`/`rework_attempt` via argv (the argv placeholder set is a small template shared across an entire profile) — all five echo-checked fields (`profile_sha256`, `verified_source_commit`, `verified_source_tree`, `workstream_id`, `rework_attempt`) are conveyed via `MAESTRO_*` env vars instead. The verifier subprocess env is not inherited wholesale (`inherit_env=False`); besides the five `MAESTRO_*` vars it gets an explicit `PATH`/`HOME`/`USER` passthrough from the parent env (when present) so CLI toolchains invoked as the verifier command (e.g. `claude`) can resolve non-absolute argv[0] and locate user config/keychain identity — this env never reaches the author. The evidence ledger lives at `<db_dir>/evidence/`, outside the worktree, durably recording each attempt and unreachable by the author until delivery. Exactly one evidence commit lands on the branch at delivery, tagged `Maestro-Verification-Run: <run_id>`; finalization checks that trailer first, so re-running it is idempotent across retries/crashes. Rework (author respawn) and reverify (operator-approved resume) are distinct `resume_reason` paths — author respawn fires ONLY on a genuine FAIL, never on ERROR or an ambiguous recovery outcome. `criteria_visibility: verifier_only` is capability-gated in preflight and refused unless the author backend is docker-isolated. `CommandVerifier` runs through the shared execution layer (`execution_phase="verification"`), inheriting the same recovery/probe machinery — including live-orphan re-poll — as task and validation executions.

### Orchestrator Flow

```
1. Load project.yaml
2. Decompose project into workstreams (Claude CLI or manual config)
3. For each ready workstream:
   a. Create git worktree + branch (+ ensure repo-local harness excludes)
   b. Write spec-runner.config.yaml (spec_prefix: maestro-)
   c. Generate spec/maestro-tasks.md via `spec-runner plan --full --spec-prefix maestro-`
   d. Spawn `spec-runner run --all --spec-prefix maestro-` subprocess
      (harness artifacts stay untracked — never committed, never in the PR)
4. Monitor processes (poll returncode + callbacks)
5. On success: create PR (if auto_pr), then merge feature branch into base BEFORE marking DONE (DONE is gated on the merge — a conflict routes the workstream to NEEDS_REVIEW with the worktree left intact; a crash mid-merge is recoverable via startup recovery), then cleanup worktree
6. On failure: retry or mark NEEDS_REVIEW
```

## Tech Stack

- Python 3.12+, uv for package management
- FastAPI + uvicorn for REST API and dashboard
- FastMCP for MCP server
- SQLite (aiosqlite) for state persistence
- PyYAML for configuration
- Pydantic for data models
- Typer + Rich for CLI
- git worktree for workspace isolation
- gh CLI for PR creation
- spec-runner (external) for subtask execution

## Ideas and Docs

- Направление/идеи (авторинг workstream'ов, scaffold/SDK): см. `docs/idea-workstream-framework.md`
- **`TODO.md` — командный уровень.** Микрошаги реализации живут в
  `docs/superpowers/specs/` + `docs/superpowers/plans/`, поштучные решения — в SDD-леджере
  `.superpowers/sdd/progress.md`. Пункты `TODO.md` несут опциональные инлайн-теги
  `@owner:` / `@blocked_by:` / `@trigger:` (контракт — «Правила ведения» в самом файле).
  Их читает `robin-runtime`, опознавая пункт по нормализованному тексту **первой строки**:
  дописать тег безопасно, **переформулировать существующий открытый пункт — нет**
  (даёт фантомную пару «закрыт/открыт» в дайджесте).

## Repo scope & boundaries

- **Этот репо:** `maestro` — git-корень `all_ai_orchestrators/maestro/`, remote `git@github.com:andrei-shtanakov/maestro.git`.
- **Соседи (READ-ONLY reference):** все остальные подпроекты воркспейса — их код не
  редактировать. Состав флота — `ai-orchestrators-workspace/workspace-manifest.toml`
  (SSOT); рукописные списки соседей в CLAUDE.md не ведём — они дрейфуют.
- **Канон имени репо = имя каталога после обычного `git clone`** (`maestro`, `libretto`).
- Нужна правка у соседа → **стоп**: запиши handoff в `../prograph-vault/authored/notes/`
  (кросс-проектное) или `../_cowork_output/` (черновик), не трогай его файлы.
- Кросс-репные контракты — **вендорить пиненой копией внутрь**, не ссылаться наружу.
- Полное правило (SSOT): `../prograph-vault/authored/rules/repo-boundaries.md`.

## Git workflow (у репо есть remote)

- Ветка `<type>/<slug>` → push → `gh pr create`. **Прямые коммиты в `master`
  запрещены**, как и локальный мерж ветки в `master` в обход PR.
- Ревью PR: **Copilot по умолчанию НЕ запрашивается** (решение владельца 2026-08-25,
  metered-бюджет; включение — строка `Copilot-ревью: запрашивать` в этой секции или
  явная просьба владельца; на «Copilot encountered an error» НЕ перезапрашивать —
  троттлинг у кромки бюджета, перезапрос платный). Умолчание ревью с гейтом
  codex-review — терминальный цикл (решение владельца 2026-08-28): итерировать
  локально `sh scripts/review/local.sh` до чистого вердикта (подписочный codex,
  $0 API) → пушить **драфтом** (CI отвечает deferred) → приёмочное ревью
  `sh ../devtools/review-pr.sh <repo> <pr> --dry-run`, затем без `--dry-run` —
  вердикт публикуется PR-ревью от **ai-prosto**; CI-прогон после снятия драфта —
  advisory-фолбэк, его красноту/зависание не перегонять (SSOT:
  `../prograph-vault/authored/rules/git-workflow.md`).
- **Мерж — агент по умолчанию** (ADR-ECO-011 «DarkFactory», ратифицирован 2026-08-30):
  при approve ревью-контура и зелёных обязательных проверках PR мержит агент —
  `gh pr merge` **от учётки ai-prosto** (`merged_by` — наблюдаемый различитель
  agent/human, аудит `gh pr list --json mergedBy`) — и выполняет хвост чистки ниже.
  Request-changes или неприбывшее включённое ревью = `unknown` ⇒ мерж не выполняется,
  PR остаётся человеку. Человеческий мерж — opt-in: строка `Мерж: человек` в этой
  секции (здесь НЕ объявлена) либо `merge_policy` экосистемного конфига. Объявление
  прогона (`merge_authority: human`, ADR-ECO-008 D5) — третий, самый узкий уровень:
  прогон может ужесточить политику до человеческого мержа, ослабить репо-оверрайд —
  нет. **Всегда человеку, без переопределения:** PR по authority-root путям
  (ADR-ECO-004 I2) и PR без предъявленного evidence базового слоя.
- После мержа (кем бы то ни было): `git switch master && git pull --ff-only`, затем удалить
  влитую ветку в **обеих половинах**: локально `git branch -d <ветка>` (после squash-мержа
  `-d` откажется — сверить, что `git diff master <ветка>` пуст, и удалить
  `git branch -D <ветка>`) и на origin
  `git push origin --delete <ветка>`, если GitHub не удалил сам; затем `git fetch --prune`.
- Никогда не делать force-push в общие ветки; не трогать другие репо (см. scope выше).
- Полное правило (SSOT): `../prograph-vault/authored/rules/git-workflow.md`.

## Входящие запросы (inbox)

В начале работы проверь входящие: `gh issue list --label inbox --state open`.
Issue с лейблом `inbox` — запрос от соседнего репо, ещё **не** пункт плана.
Принять = завести пункт в `TODO.md` с указанным `slug:`; принял под другим
именем — поправь `slug:` в теле issue.
Отказать = `gh issue close --reason "not planned"`.
Нужна работа в соседнем репо — не редактируй его: заведи там issue
(`slug:` + `from:` + проза). Правило: ADR-ECO-006 — канон в `ecosystem-kb`
(каталог `prograph-vault/` в корне воркспейса),
`authored/decisions/2026-07-28-adr-eco-006-cross-repo-issue-inbox.md`.

Исходящее ожидание — вторая половина того же ритуала: «ждём соседа» существует
**только** как чекбокс `TODO.md` с `@blocked_by:todo://<repo>/<id>` (переходно —
`<repo>#<номер>`); память сессий, заметки и handoff-доки — лишь зеркало. Находка
PF-BLOCKER-STALE по этому репо = «ожидание доставлено — действуй или переставь тег».
Правило (SSOT): `../prograph-vault/authored/rules/cross-repo-waits.md`.
