# Changelog

## Unreleased

### Changed
- **BREAKING (wire): `report_benchmark` now sends `score` as a FRACTION in
  [0,1], not a percent.** ATP reports the benchmark-plane score as a percent
  (`unit: percent`, `range {0,100}`); arbiter's canonical wire unit is a
  fraction, and its consumer feeds the number straight into a tiebreaker whose
  arithmetic assumes [0,1]. Every run above 1% used to arrive clamped to a
  perfect, indistinguishable `1.0` (arbiter#81). The conversion lives only at
  the wire boundary (`_score_as_fraction`): `BenchmarkResult.score` stays the
  percent ATP reported, which is what `maestro benchmark` shows a human and what
  `--json` emits. The shared schema now declares `minimum: 0` / `maximum: 1`, so
  an out-of-range score is rejected on ingest with `-32602` — a contract break
  the producer fixes, already classified here as such rather than as a transient.
- **The arbiter publication gate softened — but not everywhere.** arbiter now
  accepts `score_semantics` as an optional payload field, stores it verbatim and
  branches on `quality_signal` itself (arbiter#82). So a non-quality run is no
  longer silenced: it is sent with its block, stored, stays inspectable, and
  their reader keeps it out of the routing tiebreaker (`stored_not_routed`).
  Two cases stay withheld, and the reason is the shape of *their* rule: their
  `semantics_permit_routing` returns **true** for an absent block — a deliberate
  deviation so their existing legacy rows keep feeding R-07 — so a run whose
  semantics we could not read must never be sent, because on that wire an absent
  block does not mean "unknown", it means "eligible". An unfinalized run stays
  withheld too: its `0.0` is a placeholder, not a measurement.
  `_build_wire_payload` now refuses outright to build a block-less payload.
- **`payload_version` deliberately stays `1.0.0`.** arbiter rejects an unknown
  version, so bumping it for the additive `score_semantics` field would have
  been breaking rather than cosmetic — both sides would have had to move in
  lockstep. The schema already tolerated the extra key
  (`Request.additionalProperties: true`).

- **BREAKING (arbiter reporting): a benchmark result is no longer reported to
  arbiter unless it is an evaluated, finalized, interpretable quality score.**
  ATP's benchmark plane scores a task 100 when the agent returned a *completed*
  response, whatever it contained, and says so on the wire via `score_semantics`
  (contract v1). arbiter's routing tiebreaker reads
  `score_components.rank_score` and **falls back to the scalar `score`** when it
  is absent (`arbiter-mcp/src/db.rs::get_benchmark_score`), so a completion rate
  reported as a bare number silently becomes a routing input. Reports are now
  withheld fail-closed with a named reason — `quality_signal_false`,
  `semantics_unknown`, `score_not_finalized`, `unsupported_schema_version` —
  carried as the new `report_status: "withheld"` and a
  `benchmark.report.withheld` obs event. Since ATP wires no evaluators into this
  plane today, in practice **every current run is withheld**; that is the point,
  not a regression, and the numbers it stops sending were already being clamped
  to `1.0` on arrival (see the unit mismatch filed as arbiter#…). Nothing about
  local results changes: `maestro benchmark` still runs, prints and returns them.
- **(CLI) `maestro benchmark --json` gained `semantics` and `score_finalized`.**
  Additive, but the shape of a documented output changed: `semantics` carries
  `kind`, `quality_signal` and `caveats`. The verbatim upstream block is
  deliberately **not** serialized — it is an unbounded blob whose shape upstream
  controls — and stays in-process as `ScoreSemantics.raw`. The human summary now
  marks a non-quality score rather than printing the bare number.
- **(Contract) score components are no longer typed `dict[str, float]`.**
  ATP's forward-compat fixture proves a component value may be an object, and
  the previous type would have raised on the very payload the contract exists to
  tolerate. `BenchmarkResult.score_components` is now `dict[str, Any]`; the
  narrowing to numbers happens only at the arbiter wire boundary, where our own
  `report_benchmark-v1` schema promises `additionalProperties: {"type":
  "number"}`. Dropped component names are logged **by name**
  (`benchmark.report.components_dropped`), because one of them — `rank_score` —
  is the only component arbiter's tiebreaker reads.

- **BREAKING (state layout): orchestration state moved to
  `~/.maestro/projects/<host>/<owner>/<repo>/runs/<run-id>/`.** One database
  held everything before, with no project key anywhere in the path, and on
  2026-08-15 the single file held three unrelated projects at once and was a
  week stale. State is now per repository and per run: `state.db` and that
  run's `logs/` live together under the run directory, and `locks/` beside
  `runs/`. Identity is the repository's `origin` remote — host, owner, name —
  never `project:` and never a filesystem path; a checkout with no remote lands
  in `projects/_local/<name>-<hash>/`, fingerprinted by its git common dir. Run
  `maestro state-usage` to see what a machine holds. Consumers that pinned
  `~/.maestro/maestro.db` must enumerate
  `~/.maestro/projects/*/*/*/runs/*/state.db` instead (`dispatcher#147`); until
  they do, a dashboard reading the old path **freezes** rather than lying, which
  is the smaller of the two failures.
- **BREAKING (CLI): commands that fell back to `~/.maestro/maestro.db` now
  resolve a run, and refuse when they cannot.** `run`, `status`, `retry`,
  `approve`, `postmortem`, `check-scope`, `costs` and the eight `workstream*`
  commands take identity from the config the invocation names — `maestro run
  tasks.yaml`, `maestro orchestrate project.yaml`, or the new optional
  `--config` — and otherwise from the checkout in the current directory. So a
  bare `maestro workstreams` run from an unrelated directory, which used to
  succeed against the shared database, now refuses and names the key it derived
  and where it derived it from. `--run <run-id>` disambiguates two runs;
  `--db <path>` still names a database directly and now refuses to be combined
  with `--run` or `--config`, which it would otherwise silently ignore.
- **A run now records its own ending, and `maestro run-end` is new.** The
  `run` row gained `outcome`/`ended_at` with the layout, but nothing wrote
  them: every finished run kept `outcome` NULL, was classified `interrupted`,
  and a second `maestro orchestrate` of the same repository left two runs that
  looked open — after which every resolving command, and **every
  `maestro service run` tick**, exited 1 asking for `--run`. Both `run` and
  `orchestrate` now write `completed`, `failed` (only when nothing can advance
  — an `abandoned` work item), or `cancelled` (Ctrl-C) before they exit, and a
  needs-human pause writes `suspended_at` **without** `ended_at`, so the same
  run id stays resumable in the same database. A failure that rework can still
  address deliberately leaves the run non-terminal. Starting a fresh run does
  **not** mark the previous one `superseded`: that would replace
  `interrupted` — the one fact that says a run died mid-flight — with a fact
  about a different run, so the operator decides instead, with
  `maestro run-end <run-id> --outcome superseded|cancelled`. Two consequences
  worth knowing: a scheduled tick over a repository whose every run has ended
  is now a green no-op rather than exit 1, and a resolving command with no
  `--run` falls back to the newest run when every run is terminal (selecting
  is not resuming) while still refusing when several runs are open.
- **`~/.maestro` and everything under it is 0700/0600 — including what the
  locks create.** `create_run` honoured this; `maestro/service/locks.py` used a
  bare `mkdir` and `open("w")` and so left `locks/` and
  `projects/<host>/<owner>/<repo>/` world-readable at 0755 and every lock file
  at 0644. Because a mode was only ever set at creation, whichever path got
  there first decided it permanently — and on a fresh machine
  `maestro service run <config> --db <path>` takes the lock without any
  `create_run` running, which is how `~/.maestro` itself ended up 0755. An
  existing directory under the maestro home is now repaired to 0700 as well;
  nothing above the home is ever touched.
- **BREAKING (locks): lock identity no longer includes the database path.**
  Stage locks are keyed `(repository, stage)`, so two runs of one project
  against two different `--db` files now serialise per stage instead of
  proceeding in parallel.
- `~/.maestro/maestro.db` is **frozen, not migrated**: it stays readable and is
  named by `maestro state-usage`, and no default path writes to it. "Frozen"
  means "never the default", not "immutable" — an explicit
  `--db ~/.maestro/maestro.db` at a **mutating** command (`retry`, `approve`,
  the `workstream*` family, `orchestrate`, `run`, `service run`) still opens it
  for writing and initialises the schema, so copy it first if you want it
  untouched under one of those.
- **The view-only commands no longer rewrite what they list.** `status`,
  `workstreams`, `check-scope`, `service status` and `costs` open `--db`
  read-only (`mode=ro`) and never initialise a schema. Previously
  `maestro workstreams --db ~/.maestro/maestro.db` — the natural way to look at
  the pre-split evidence — ran `initialize_schema()` and turned a 1-table,
  12 288-byte file into 21 tables and 200 704 bytes. `--db` now also reports
  what the named file *is*: a database with no `run` row is labelled *legacy*
  and never backfilled (spec §E). The cost of the honesty: a pre-split file has
  no `workstreams`/`service_ticks` table, so those views now refuse with "no
  such table" instead of silently creating one. `check-scope` reports an
  unreadable database as exit 2 (invalid input), never 1 (its "escapes found").
- **BREAKING (service): reinstall any service unit installed before this
  release.** The pre-change `maestro service install` baked
  `--db ~/.maestro/maestro.db` into the generated unit. `service run` is a
  mutating command, so a machine with an existing unit keeps opening — and
  rewriting the schema of — the legacy file on **every tick**, and nothing
  announces it. `maestro service uninstall <config>` then `maestro service
  install <config> …` replaces the pinned path with per-tick resolution.
- `maestro service install` no longer bakes a database path into the generated
  unit. A unit outlives every run it starts, so a pinned path would make a
  03:00 tick act on a run that has since ended; each tick now resolves the
  current run for itself. An explicit `--db` is still honoured and still pinned.
- `maestro run --clean` now only means anything together with `--db`. A
  resolved fresh run gets its own empty database by construction, and clearing
  a resumed one would delete the `run` row that is its identity.
- **BREAKING (verdict log): `maestro.gate-verdict-record/v1` -> `/v2` — the
  `obligation` field is now `enforcement` (#160).** The two names denote
  different axes and v1 used steward's name for ours: `obligation`
  (`quality | approval`) is the **intent** of a gate and belongs to steward's
  catalog, resolved from `gate_id`; `enforcement` (`mandatory | advisory`) is
  **this consumer's** answer to "does it block the transition", which steward
  neither defines nor validates. Renaming a required field is incompatible, so
  the `schema` discriminator on every line of `logs/<ULID>/gate_verdicts.jsonl`
  moves to `/v2` rather than gaining an alias — anything that parses these
  lines must branch on it. `obligation` is **absent**, not null: classifying
  our own producer ids with steward's taxonomy is permitted but has no
  consumer yet, and an always-null field would read as "unclassified" rather
  than "not claimed". Catalog owner's ruling on maestro#160 / steward#63
  (2026-08-12); steward's loader now permanently bars `mandatory`/`advisory`
  from `obligation_vocabulary`, so the two axes cannot collide by name again.
- **`gate_id` namespaces are now enforced (#160).** `GC-` is steward's closed
  namespace: a `GC-*` id **unknown to the vendored catalog blocks the
  transition** fail-closed instead of being silently dropped, and the refusal
  is recorded under Maestro's own `maestro.gate_id_namespace` — minting a
  record under the id we are refusing would be the "invent the entry" half of
  what the ruling forbids. A `GC-*` the catalog **does** know is annotated
  advisory (`verdict: missing`): it is a real gate, enforced by gate-check in
  the target repo's CI rather than at Maestro's two edges — the existing M-2
  case. Producer ids (`<namespace>.<name>`) are validated by **shape only and
  never resolved** against the catalog, so `steward.risk_classify_*`,
  `human.owner_approval` and `maestro.validate_strict` keep their names: they
  are enforcement points of this runtime, not gate-check gates, and no
  canonical aliases were issued for them. An id in neither namespace is
  rejected. In practice nothing changes for current runs — steward's
  `tier_gates` carries only producer ids today — but that profile is
  operator-editable, which is why the guard exists.
- steward's gate catalog is **vendored, not referenced**:
  `maestro/resources/gate_catalog/upstream/` carries `profiles/gate-catalog.yaml`
  (catalog v2) and the normative `contracts/gate-verdicts/v1/README.md`, pinned
  at steward `afd192f`. Shipped inside the package, because the sibling checkout
  they came from does not exist for anyone who installed Maestro. The id
  patterns and the reserved-token list are read **from the vendored file** —
  steward publishes that mirror precisely because consumers vendor the file and
  not the loader, and it is not a knob: the `GC-` pattern cannot be widened
  locally. Copy-integrity (digest) and upstream provenance/drift (against the
  sibling, skipped where absent) are separate tests, because a local edit, a
  fabricated pin and a stale pin are different defects.

### Changed
- **The ADR-ECO-003 enum vocabularies are now vendored, not declared
  (devtools#51).** `models.*.status` and `harnesses.*.kind` belong to
  ADR-ECO-003, which published them only as prose — an inline comment in an
  example TOML — so all three catalog loaders hand-copied them and could
  diverge on the vocabulary itself, one storey above the drift the conformance
  set exists to catch. No fixture would have seen it: `v7-unknown-kind` checks
  that an unknown kind is flagged, not that three loaders call the same set
  known. Maestro carried two such copies, and asymmetrically — the status copy
  hard-rejected an unknown value while the missing kind copy stayed silent.
  Upstream now ships `vocabulary.toml` inside the pinned conformance set, so
  both copies are **deleted**: `MODEL_STATUSES`, `HARNESS_KINDS` and the
  `ModelStatus` Literal are gone, and `model_statuses()`/`harness_kinds()` read
  the vendored file. An additive upstream bump is adopted by re-vendoring
  alone — no Python edit names any value any more, which is what makes this a
  vendored contract rather than a maintained constant.
  `CatalogModel.status` is consequently a plain `str` with a field validator
  instead of a `Literal`. Rejection of an unknown status stays where it was, at
  validation time, and stays an error; what is lost is the static type and the
  enum in the generated JSON Schema, which is the price of not re-declaring
  someone else's contract. An unreadable vocabulary is **fail-loud**
  (`CatalogVocabularyUnavailable`), never a silent empty set — empty would
  reject every catalog on `status` and, worse, stop flagging every `kind`.
  The file is shipped **inside the package**
  (`maestro/resources/catalog_conformance/`) and read through
  `importlib.resources`: the conformance set lives under `tests/`, which is not
  in the wheel, so a checkout-relative path would have worked for developers
  only. A test asserts the shipped copy and the set's copy are byte-identical,
  so one pin still covers both, and the conformance pin moves to
  `devtools@070acdc` with the new `vocabulary-roundtrip.toml` case — a valid
  fixture using every vocabulary value, which goes red for any loader whose
  known set quietly lost one.

### Fixed
- **`--resume` no longer continues silently against an edited config (#198).**
  A run persists its workstream configuration when it is created and every
  later tick works from that copy, which is correct — a run must not change
  the rules under itself mid-flight. What was wrong was the silence: editing
  `workstreams[].scope` and resuming was indistinguishable from "the edit
  applied and did not help", and a green `maestro validate` on the edited file
  actively encouraged that reading. The reported case spent a full
  fix → resume → identical-refusal cycle before anyone opened `state.db` and
  saw seven scope entries where the file had eleven.
  On resume, the persisted workstreams are now compared against the config:
  every field `Workstream.from_config` sets (`title`, `description`, `scope`,
  `depends_on`, `priority`, `backend`), the derived `branch` — because editing
  `branch_prefix` was silently ignored the same way — and the set of ids, since
  a workstream added to the YAML was also being dropped without a word.
  Reordering `scope` or `depends_on` is not drift: order means nothing to a
  glob match or a dependency set, and flagging it would train operators to
  ignore the check. A run created by auto-decomposition has nothing to compare
  and is unaffected.
  Drift is **fail-closed with no override flag**: nothing is dispatched,
  decomposed or delivered, and the message names the diverged fields, states
  that the persisted version stays in force, and splits the remedy —
  `description`/`scope` can be adopted with `maestro workstream-rework
  --refresh-from` (a rework, so it re-decomposes and respawns the author; it is
  not a free config update), while everything else means reverting the edit or
  starting a new run. A bypass flag was considered and rejected: it would turn
  a fail-closed signal into a permanent detour, which is how the silence would
  come back.
  An empty `workstreams:` section is the one shape the persisted rows cannot
  disambiguate on their own — an auto-decomposed run and a run whose section
  the operator deleted look identical — so migration 28 records how a run's
  workstreams were created (`run.workstreams_declared`, nullable). Declared and
  now absent reports every workstream as removed; auto-decomposed stays silent.
  Runs created before the migration answer NULL and **fail open**: halting
  every legacy auto-decomposed run on resume would be a worse defect than the
  hole, and per-run state directories are short-lived enough that the unknown
  window closes on its own.
  The halt runs **after** crash recovery, not before. Drift forbids new
  dispatch, decomposition and delivery — never the liveness and reconciliation
  pass over handles that already exist. Raising earlier would have traded one
  silent failure for another: a typo in `title` would leave a crash-stranded
  execution unobserved. The run records **no** outcome and stays open, so
  fixing the config and resuming continues the same run.

### Added
- **Conformance pin bumped to `devtools@2533ff7`; the two forks Maestro flagged
  came back decided, and one went against us (#192).** Wiring the shared set in
  #188 surfaced two places where it declined to rule, so each consumer was
  deciding privately — the drift the set exists to prevent, one storey up. Both
  are now canon, with fixtures.
  **An empty `[harnesses]` plane declares zero harnesses.** A bare header with
  `[[agents]]` rows present is V1 for every row, fail-closed. Maestro read it as
  schema scaffolding, arguing that the shipped template teaches an empty
  `[models]` header; the ruling points out that `[models]` is *required* and
  `[harnesses]` is not, and nobody writes a scaffolding header for an optional
  table — which is the better argument. Arming moved from "the mapping is
  non-empty" to "the key was declared" (`model_fields_set`). An empty plane with
  no agents stays valid: only "nothing to resolve with, and something to
  resolve" is rejected. Catalogs from `maestro models init` are untouched, since
  that template emits no Plane 2 at all — and *that* absence remains an unarmed
  hole, announced rather than assumed healthy.
  **An unknown `harnesses.*.kind` now warns (V7).** Never rejects: Maestro does
  not launch from Plane 2, so an unfamiliar kind is information, not an
  obstruction. The warning names the vocabulary's owner and the repair, because
  a false positive is possible by construction — `HARNESS_KINDS` is a hand-made
  copy of a vocabulary ADR-ECO-003 publishes only as prose. It sits beside the
  model-status Literal so the pair cannot drift apart, is marked INTERIM, and a
  test pins both against the vendored set's valid fixtures. The structural fix —
  a machine-readable vocabulary inside the pinned surface — is requested as
  devtools#51; these constants are meant to be deleted, not maintained.
  The regression test that pinned Maestro's losing reading is what forced this
  change: it failed on the pin bump instead of letting the divergence sit, which
  is the entire reason positions get pinned to tests rather than to prose.

- **The catalog loader now checks the catalog against itself, and the shared
  conformance set is wired into the suite (#188).** Until now `load_catalog()`
  parsed Plane 1 and Plane 3 and validated neither against the other: an
  `[[agents]]` row could name a model no `[models.*]` table declares, name it
  twice, or name one marked `retired`, and the catalog loaded clean. The five
  referential rules of the shared vocabulary (V1 unknown harness, V2 unknown
  model, V3 retired reference, V4 duplicate enrollment, V5 routable enrollment
  on a non-routable harness) now raise `CatalogMalformed`, which halts the run.
  Rejecting the whole catalog is deliberate: accepting it partially would route
  work over a silently pruned agent set, which is the failure the checks exist
  to prevent. V6 (a reference to a `deprecated` model) warns at load, in
  addition to the existing spawn-time warning. Aliases resolve before V2 fires,
  so an `[[agents]]` row naming an alias is not a dangling reference.
  **V1 and V5 are armed only when the catalog carries a `[harnesses.*]`
  plane** — and that is a hole, not a decision. Catalogs scaffolded by
  `maestro models init` carry no such plane at all, so on most real catalogs
  "the harness reference is checked" is simply untrue. An absent plane means
  *unverifiable*, never *valid*, and the loader now says so with a
  `catalog.reference_checks_not_armed` event; teaching the scaffold template to
  emit the plane is the actual fix and is tracked separately
  (`@id:models-init-harnesses-plane`).
  The fixtures behind all of this are a **pinned vendored copy** of a contract
  owned by devtools (`devtools@2a5c154
  contracts/catalog-conformance-fixtures/v1`), verified per-file and by
  `tree_sha256` against its own manifest **before** the parametrized cases run,
  so a truncated copy fails loudly instead of quietly shrinking into a smaller
  green suite.
  One expectation is knowingly **not** met: `$ATP_CATALOG` pointing at a
  missing file still returns "no catalog" plus an info log rather than an
  error. The contract (ADR-ECO-003b D2, already implemented by arbiter) is
  accepted as correct — Maestro is the diverging consumer, not the dissenting
  one — but reversing it is a breaking change against a recorded 2026-07-02
  decision and gets its own PR rather than riding along inside a test-wiring
  change. A strict `xfail` holds the position, so fixing it silently is
  impossible: the day the loader complies, that test fails as XPASS.

- **Per-workstream quarantine (#166 half A).**
  `maestro workstream-quarantine <id> --reason "<why>"` forbids one
  workstream's result from progressing, and **does not kill anything**: a
  running execution finishes normally, because terminating work in order to
  isolate it is precisely the loss this issue was filed about. What stops is
  dispatch (a quarantined workstream never becomes ready) and delivery (a
  finished quarantined workstream **parks in NEEDS_REVIEW** awaiting an
  operator decision instead of merging). Nothing is reverted — the branch,
  worktree and commits stay exactly as the author left them.
  - It is **not a rework and not an approval.** Rework re-decomposes and
    respawns the author; approve grants a gate. Quarantine grants nothing and
    starts nothing.
  - `maestro workstream-unquarantine <id> --reason "<why>"` **only lifts the
    durable freeze.** It changes no status, records no approval, sets no resume
    reason and starts nothing; whatever gate or review the workstream owed, it
    still owes. The orchestrator picks it up on a later loop if its status makes
    it eligible.
  - **A row that has already entered delivery (MERGING/PR_CREATED/DONE) cannot
    be quarantined** — the command refuses with a plain sentence, because after
    delivery the remedy is a revert and accepting the quarantine would claim to
    have prevented something that already happened.
  - Both verbs are idempotent, and a repeat says which case it hit rather than
    reporting the same success twice. `maestro workstreams` grows a
    `Quarantined` column showing the flag **and the age** — a quarantine raised
    a minute ago reads differently from one standing for two days.
  - Durable via migration 25 (`workstreams.quarantined_at` +
    `quarantine_reason`, plus a `workstream_quarantines` audit table).
    Deliberately not a status: the process keeps running, so the row stays
    RUNNING and every existing `expected_status=RUNNING` CAS keeps working.
- **`maestro workstream-continue <id>` — finish an interrupted workstream
  without starting over (#166 half B).** It re-dispatches spec-runner against
  the **existing** `spec/maestro-tasks.md`: no regeneration, no author respawn,
  no new SHA, and the partial work is kept. Before this, whatever ended a run —
  a stop, a kill, a crash — left the workstream in READY, and READY means
  "Always regenerate": a fresh spec, a fresh LLM lottery, fresh money.
  - **It only queues the request.** The orchestrator dispatches it on its next
    loop. The command checks the preconditions so a refusal is fast and
    readable, but that answer is not the authority: between it and the spawn a
    live process can appear, the worktree can vanish or tasks.md can change, so
    the preconditions are re-checked immediately before spawning and **that
    late check is the guarantee.**
  - **Fail-closed on four preconditions**, each with its own reason because each
    needs a different action: a live process or execution handle (never run a
    second spec-runner over one worktree), a missing worktree, a tasks.md that
    fails #165's dangling-dependency validation, and an unreadable executor
    state database (without it "continue" is a fresh start wearing the wrong
    name). A refusal parks the workstream in NEEDS_REVIEW, clears the resume
    marker so the next loop does **not** retry by itself, spawns nothing,
    generates nothing and leaves the counter untouched. A new explicit
    `workstream-continue` is accepted once the cause is gone.
  - **How it differs from its neighbours**, which is the distinction most worth
    keeping straight:

    | Verb | What it does |
    |---|---|
    | `workstream-continue` | Runs the tasks that are **missing**, over the existing plan |
    | `workstream-approve` | **Accepts** the current incomplete result and delivers it; executes nothing |
    | `workstream-rework` | **Re-decomposes** from scratch and respawns the author |
    | `workstream-recapture` | Retries **only** the evidence archiving, for the same execution |

  - **The counter records accepted dispatch attempts**, not requests: it moves
    inside the `READY -> RUNNING` CAS, so a queued continuation that is later
    refused, cancelled or lost to a race counts nothing. Past a threshold the
    operator is **warned, never blocked** — an explicit audited action is not an
    automatic loop, and forbidding the N+1th without new knowledge would only
    invite a workaround. Migration 26 adds the column.
- **Recovery now archives the evidence of interrupted runs (#166 half B).** A
  hard kill skips finalization, so #164's post-mortem archive was never written
  for exactly the runs an operator most needs to inspect. Recovery now captures
  it, and **only for provably-dead / stranded executions**:
  - a **live orphan** (or a possibly-live handle) is returned to
    review/monitoring and is **not** archived — a process still writing its
    state and logs would yield a torn snapshot that looks like evidence; its own
    finalization captures a consistent archive later;
  - a stranded **DECOMPOSING** is not archived either, having produced no
    executor state to preserve;
  - the decision comes from recovery's existing classification, not from a
    second probe that could disagree with it.
  - **Capture completes before anything that could overwrite the evidence** —
    cleanup, requeue or the `FAILED -> READY` reset that leads to the next
    dispatch. It records `captured_by: recovery` so interrupted evidence is
    distinguishable from evidence taken at an orderly finalization, and it is
    idempotent for the same execution, so a restart loop reconciles instead of
    multiplying archives. An expected capture failure preserves the worktree and
    hands the operator the `workstream-recapture` path instead of proceeding.


- **Dangling dependencies are caught before the executor starts (#165).** A
  rework regenerates `spec/maestro-tasks.md` wholesale, and the decomposer —
  told it is continuing after TASK-021 — emits `**Depends on:** [TASK-021]`
  for a task that exists only in the revision it replaced. spec-runner
  rejects that correctly, but at run time, after Maestro has paid for
  generation and spawned a process. Maestro now validates the generated file
  immediately after `plan --full` and **before any spawner**: every dependency
  must resolve inside the current revision, and a violation blocks the
  workstream into NEEDS_REVIEW naming each referencing task and missing id
  (`TASK-022 -> TASK-021`). The block consumes **no retry** — a retry would
  re-decompose, which is exactly the waste being fixed. An unreadable
  tasks.md is a *skip*, not a pass: it logs
  `workstream.tasks_validation.skipped` and proceeds, because spec-runner
  remains the final validator and blocking on Maestro's own path assumption
  would turn early diagnosis into an outage. The three outcomes
  (`skipped` / `passed` / `blocked`) are distinct log events so a skip can
  never be misread as a clean bill of health.
- **Rework always carries the self-contained-dependency rule.** Every rework
  regeneration gets the constraint appended to its decomposition input,
  independently of `--instructions`: the risk comes from replacing the plan
  revision the model remembers, not from whether an operator typed
  instructions. The rule is **prevention only** — correctness comes from the
  validator above, which runs whether or not the model honoured it.

- **`maestro workstream-approve <id>` (gates v1.1, H-5):** the sanctioned
  operator re-queue for gate-blocked workstreams — NEEDS_REVIEW → READY with
  `error_message` preserved (used to require a raw sqlite UPDATE).

### Changed
- **`maestro stop` now drains instead of destroying work (#166 half A).** The
  first signal (SIGTERM/SIGINT) **forbids new dispatch and waits for live
  executions to finish**, monitoring each to its own finalization; it
  terminates nothing. Previously it terminated every running handle and reset
  each workstream to READY, which meant "Always regenerate" — so a routine
  stop discarded partial work exactly as an external SIGKILL did.
  - **A second signal forces termination** and may leave work needing recovery
    on the next start. That escalation is deliberate: without it the only
    escape from a long drain would be SIGKILL, the hammer this change removes.
    A forced shutdown is recorded distinctly (the structured
    `orchestrator.shutdown.forced` event, and a human-readable cause on the
    affected rows) so a deliberate stop is legible afterwards rather than
    looking like a crash.
  - Recovery's automatic behaviour is unchanged: it still classifies by
    process/handle liveness. The cause is diagnostics for a person, not a
    branch condition.


- **`spec-runner >= 2.24.0` is now required (#169b).** The floor moves from
  2.16.0 to the release that closed the false-green exit class: `run --all`
  no longer exits 0 with work undone, and the run records an honest
  `last_run_stop_reason`. Two mechanisms shipped in this same cycle depend on
  that being *true* rather than merely available — the completeness gate
  (#164) treats a zero exit as a claim it verifies against the counters, and
  the retry classifier (#165) routes three typed stop reasons away from a
  retry that cannot help. Both degrade safely on an older spec-runner
  (fail-closed and retry-as-before), so the pin is what turns "best effort"
  into a guarantee. Surfaces re-verified at the bump: `plan --full`,
  `run --all`, `--spec-prefix`, `status --json` (`total_tasks`), `review-pr`,
  and the two vendored contracts, which already recorded 2.24.0 in
  `VENDORED_FROM_SPEC_RUNNER`.

- **Retries no longer pay for a failure they cannot change (#165).** Every
  Mode-2 retry costs a full re-decomposition (fresh spec-generation spend),
  and the pilot burned three of them on one validation error, hitting the
  identical failure each time. Maestro now classifies the failure from
  spec-runner's typed `stop_reason` (delivered by #169a — before it, those
  string values were dropped by an integer cast and never reached Maestro at
  all) and routes three known reasons straight to NEEDS_REVIEW instead of
  retrying:

  | `stop_reason` | Outcome | Why |
  |---|---|---|
  | `validation_failed` | NEEDS_REVIEW, no retry | Re-generating the spec does not remove the established cause |
  | `state_spec_mismatch` | NEEDS_REVIEW, no retry | A configuration fact, not a transient one |
  | `dependency_blocked_after_skip` | NEEDS_REVIEW, no retry | Re-running does not unblock; something must change first |
  | `task_failed_stop` | **retry unchanged** | May be a rate limit or a flaky test |
  | `max_consecutive_failures` | **retry unchanged** | Same, in bulk |
  | `budget_exceeded` | **retry unchanged** | Whether a retry gets a fresh budget is spec-runner's business |
  | unknown / dynamic `error_*` / empty / **absent** | **retry unchanged** | Unclassified is not unfit |

  "Unfit for automatic retry" is a claim about policy, not mathematics: a
  fresh LLM decomposition could in principle differ. What is certain is that
  re-generating cannot remove the cause and spends budget reaching the same
  place, so a human — who can change the input — is the right next step. The
  classification never keys off `stop_detail` prose or how fast the run
  failed; a fast failure is at least as likely to be an infrastructure hiccup,
  and treating it as terminal would trade one bad behaviour for another. The
  NEEDS_REVIEW message carries the exact `stop_reason` and the policy
  rationale.

- **DONE now means the work finished, not just the process (#164).** A new
  always-on **completeness gate** compares the executor's completed subtask
  count against the planned total before a Mode-2 workstream is delivered.
  Previously `DONE = spec-runner exited 0 + merge ok`, which merged a branch
  containing 1 of 9 tasks into the base while the display honestly read
  "1/9 done". Four distinct blocking verdicts, each asking the operator for
  something different: `incomplete`, `unknown_total`, `inconsistent`
  (`done > planned`, i.e. the counters describe different revisions of the
  plan), and `unreadable`. An all-no-op run **passes** and is reported as a
  structured event — completeness is not productivity; judging usefulness
  belongs to verification. There is deliberately no kill-switch: the audited
  per-SHA approve is the only way past it.
- **Post-mortem archives (#164).** Every execution's evidence — a
  `backup()`-consistent snapshot of the executor state database, the harness
  logs, and a self-describing `manifest.json`
  (`maestro.postmortem-manifest/v1`) — is copied to
  `<db_dir>/postmortem/<workstream>/<ts>-<execution_id>/` **before anything is
  destroyed**, and the gate reads that archive rather than a live worktree.
  This is what makes local and SSH runs one code path: on SSH the executor
  logs are never collected back (`*.log` is excluded from the collect rsync)
  and the remote is `rm -rf`'d during finalization, so a DONE-time hook would
  have been log-empty for every remote run while looking like it worked.
  Capture is committed by a single directory rename, so a crash leaves either
  ignorable `.partial/` garbage or a complete archive. **If capture fails,
  nothing is destroyed**: cleanup is skipped and the workspace preserved.
  Retention is bounded (`postmortem.keep_per_workstream`,
  `postmortem.max_archive_bytes`) with `maestro postmortem <config> --gc` for
  operator-driven pruning. The `postmortem:` config block carries retention
  only — there is no `enabled: false`, and an absent block means defaults.
- **`maestro workstream-recapture <id>`** — retry *only* evidence capture for
  the same execution after a `post-mortem capture failed` block: no executor,
  no decomposition, no new SHA. Explicitly **not** an approval; it refuses a
  workstream that carries no recapture token rather than becoming a generic
  requeue.

- **`maestro service install` no longer demands an API key the normal
  path never reads.** Maestro spawns harness CLIs and never calls a
  model API itself, so a credential is satisfied by *either* an exported
  key *or* the CLI's own login store (`~/.claude.json`); only the
  absence of both is refused. Previously a blanket `ANTHROPIC_API_KEY`
  requirement rejected working `claude`-login setups. `--dry-run` now
  renders the unit and reports problems instead of blocking the preview.
- **`maestro review-pr` deduplicates its notifications** by
  `(repo, pr_number, head_sha, outcome)`. Repeated scheduled runs over
  an unchanged PR are now silent; a new review-bot round moves the head
  SHA and alerts again. Prerequisite for the review service stage, which
  deliberately sends no notification of its own.
- **`maestro review-pr` — post-PR review wrapper.** Drives
  `spec-runner review-pr` (the review-bot loop: verify each comment
  against the code, TDD-fix the valid ones, reply in threads) over
  Maestro-created PRs, without owning any of it. A separate
  operator/cron-invocable command — the orchestrator is untouched and
  no `WorkstreamStatus` changes (the workstream is already DONE; this
  is advisory post-delivery cleanup, not a DAG-correctness guarantee).
  Review workspaces are keyed by (repo, PR) with the spec-runner
  `state_file` held **outside** the checkout, so removing a finished
  worktree never destroys the never-reply-twice guarantee or unpushed
  fix commits; a saved continuation is published by Maestro itself
  (`--force-with-lease` against the verified expected SHA, never a
  plain force) so spec-runner's strict head check can pass. Per-PR
  `flock` (exit 3 = already running, no run row), retention by exit
  code, `post_pr_review_runs` audit (migration 21, immutable after a
  CAS finalization), three new notification events, and a preflight
  gate requiring **spec-runner >= 2.21.0** (the release whose
  `review-pr --json` emits exactly one JSON document per exit path —
  spec-runner#116, filed from here). Design:
  `docs/superpowers/specs/2026-08-06-post-pr-review-command-design.md`.
- **approver_cmd hook (#137).** Opt-in `gates.approver` block: an ex-post
  gate block parked in NEEDS_REVIEW may now be auto-approved by an
  external critic command — an *automated operator* over the existing
  single-authority approval API, recorded with `actor='agent'` and the
  full verdict document (every critic's vote) in the new append-only
  `gate_approver_runs` table (migration 20, which also adds
  `gate_approvals.actor`/`approval_run_id` and the immutable
  persist-at-block `gate_block_contexts` snapshot the request envelope
  is built from). Strict run-keyed contract
  (`maestro.approval-request/v1` on stdin,
  `maestro.approval-verdict/v1` on stdout, 4-field echo handshake,
  declared critic-independence check, bounded stdout/stderr and field
  limits). Fail-closed everywhere: timeouts, protocol violations,
  unknown/exceeded cost budgets, stale SHAs and interrupted runs all
  leave the workstream waiting for the human; a PASS re-queues through
  the H-6 resume path after a post-verdict recheck + CAS transaction.
  Guard skips are `not_run` observations (never burning the
  one-evaluation-per-SHA slot — the kill-switches, `enabled: false` and
  `MAESTRO_APPROVER_DISABLED=1`, are reversible). Evidence lines in
  `gate_verdicts.jsonl` now carry an explicit
  `schema: maestro.gate-verdict-record/v1` discriminator. Design:
  `docs/superpowers/specs/2026-08-06-expost-approver-cmd-design.md`.
- **Webhook notification channel.** A configured `notifications.webhook_url`
  now POSTs a versioned JSON envelope (`maestro.notification/v1`) on
  lifecycle notifications, delivered by a managed bounded queue with a
  background worker: bounded retries (3 attempts inside a wall-clock
  budget; 408/429/5xx/transport errors retry, `Retry-After` honored but
  capped; other 4xx permanent; redirects disabled), graceful-shutdown
  drain with a deadline, and visible overflow/undelivered accounting.
  Semantics: at-least-once within a live process and graceful shutdown;
  best-effort across a hard crash (a durable outbox is a possible
  follow-up behind the same queue seam). Security: per-event field
  allowlist (`message` is never forwarded; `url` only for PR-created),
  `event_id` stable across retries and sent as `Idempotency-Key`, and the
  webhook URL never reaches logs — including httpx's own request lines,
  which are filtered. `httpx` becomes a direct dependency.

- **`validation_backend` default flipped `local` → `same` (validation_backend
  PR3).** Post-task validation now runs by default in the *same* backend the
  task ran in (environment parity), instead of always on the local host.
  **No-op for the common case:** a task with no `execution` config (or
  `default_backend: local`) and no per-task `backend` still validates on the
  bare local host — `same` resolves to the default `local` backend.
  **Behavior change only where a task actually runs on a Docker or SSH
  backend:** its validation now executes durably inside that backend (its own
  execution handle), so the validation command sees the task's real
  toolchain/filesystem rather than the host's. Set `validation_backend: local`
  explicitly to keep host-local validation. Existing persisted tasks keep their
  recorded value (no data-migration; migration 12 only changes the column
  default for new rows); only newly-created tasks pick up the new default.

- **Routing (D2):** the arbiter can now route to any harness that has a
  registered spawner; the closed `AgentType` enum no longer gates spawns
  (`scheduler.py`). Under **arbiter routing**, an unknown harness → retryable
  HOLD (`unknown_agent`); `auto` → refuse (`auto_not_resolved`) — semantics
  unchanged. Under **static/scheduler routing** (no arbiter to re-route), an
  unregistered harness fails **terminally** (`SchedulerError`, task FAILED) —
  a HOLD there would leave the task READY forever and hang the run.
- **Model execution (D1):** the arbiter-routed model (`<harness>@<model>`) is now
  passed into `spawn()` and executed. Each spawn emits an `agent.model_resolved
  {harness, model, source}` log for observability.
- **Catalog-driven model defaults (AI#4, ADR-ECO-003b):** the baked
  `DEFAULT_<H>_MODEL` constants have been removed. The model is now resolved
  from a user-config catalog loaded from `$ATP_CATALOG` (see `maestro/catalog.py`).
  New precedence is **`routed` (arbiter) > `MAESTRO_<H>_MODEL` env > catalog
  default > fail-loud** — env is now a fallback used only when routing
  supplies no model, and the catalog default is used only when neither routing
  nor env supply one. A status-graded coherence warning (`retired` /
  `deprecated` / `unknown`) is logged when the routed or env-supplied model
  doesn't cleanly match an `active` catalog entry.
  Fault taxonomy is split by blast radius: a malformed or unconfigured
  catalog raises a global `CatalogError` (`CatalogNotConfigured` /
  `CatalogMalformed`) that halts the whole run, while a harness with no (or
  an ambiguous) routable default raises a per-task `HarnessModelUnresolved`
  that sends only that task to `NEEDS_REVIEW`.
  **Breaking change:** a run with no routed model, no `MAESTRO_<H>_MODEL`,
  and no `$ATP_CATALOG` now fails loud (`CatalogNotConfigured`: "model
  catalog not configured: set $ATP_CATALOG (or run 'atp models init')")
  instead of silently falling back to a built-in default model.

---

### Fixed
- **Approvals could be silently discarded (found while implementing #164).**
  `gate_approvals.phase` carried `CHECK (phase IN ('ex_ante','ex_post'))`
  while the approval was inserted with `INSERT OR IGNORE` — which suppresses
  CHECK violations exactly as readily as duplicate keys. Any approval for a
  phase outside those two recorded **nothing and reported success**, so the
  operator saw "approved" and the gate blocked again on the same SHA.
  Migration 24 rebuilds the table with the widened CHECK, and the insert now
  uses `ON CONFLICT(workstream_id, phase, sha) DO NOTHING` so only the
  intended UNIQUE collision (idempotent re-approval) is suppressed and a
  constraint violation raises. Pre-existing `ex_ante`/`ex_post` approvals were
  never affected; the defect could only bite a new phase.

- **Preflight scope-overlap no longer flags DAG-ordered workstreams as a
  merge-conflict risk (#121).** When a `depends_on` path (direct or
  transitive) already orders two workstreams, they never run concurrently,
  so their scope overlap is reported at a new `info` severity — without the
  misleading "add a depends_on edge" advice — instead of `warning`. Info
  findings never fail `maestro validate --strict`. Genuinely parallel
  overlaps keep the warning unchanged.

- **Gates approval memory survives phase overwrites (H-3):** the single-slot
  `error_message` marker gets overwritten when a later phase blocks; the
  verdict store (`gate_verdicts.jsonl`) is now the durable approval memory —
  a recorded owner-approval block for the exact (workstream, phase, sha)
  counts as operator approval on re-evaluation (NEEDS_REVIEW → READY is a
  human-only transition). New commits still invalidate approvals (SHA-bound).
- **Orchestrator spec commits no longer trip the scope gate (H-4):**
  `spec/**` and `spec-runner.config.yaml` are Maestro's own infra commits and
  are excluded from ex-post classification and the declared-scope check.
- **`constraints.authority_context` on route_task calls (RD-006 M4):** the
  scheduler now sends the authority execution context `{role, phase}` to
  arbiter — `role` from the task's function (`review` tasks act as reviewers,
  everything else the scheduler executes is `implement`), `phase: execution`.
  Rides in constraints only, never in the task payload (arbiter structurally
  keeps it out of the 22-dim feature vector; Maestro keeps it out of
  capability features). Enables arbiter's role/phase-scoped allowlist
  enforcement (arbiter #50/#51) once `config/authority.toml` is vendored.
- **Gates-in-DAG runtime (WS-006 handoff M-1..M-3):** opt-in `gates:` section in
  `project.yaml` — the orchestrator evaluates risk gates at two transition
  edges by shelling out to `steward risk-classify` (single source of truth for
  tiers): **ex-ante** before READY→RUNNING over the declared workstream scope,
  **ex-post** before RUNNING→MERGING over the actual diff (scope violations
  escalate). Fail-closed: a missing/errored verdict on a mandatory gate blocks
  the transition; blocked workstreams route to NEEDS_REVIEW and an operator
  re-queue approves the gate for that exact SHA (a new commit invalidates the
  approval). Every evaluation appends verdict-records to
  `logs/<ULID>/gate_verdicts.jsonl` (addressable via EvidenceRef
  `kind=gate-verdict`); gates enforced beyond these edges (branch protection,
  PR reviews) are recorded as advisory annotations. New: `maestro/gates.py`,
  `GatesConfig`, preflight checks `gates-steward-missing` /
  `gates-risk-model-missing`, and a legal READY→NEEDS_REVIEW workstream
  transition. No behavior change when `gates:` is absent.
- **EvidenceRef `kind: gate-verdict` (WS-006 handoff M-4):** typed pointer to
  one gate verdict-record in `logs/<ULID>/gate_verdicts.jsonl`, addressed by
  `pipeline_id` + `gate_id` + full 40-hex `sha` (verdicts are SHA-bound).
  Pre-adoption additive change to `contracts/observability/evidence-ref.schema.json`
  with the WorkCorrelation inline copy kept in sync; new builder
  `gate_verdict_evidence()` in `maestro/correlation.py`.
- **`maestro validate <project.yaml>` (preflight, Mode 2):** static and
  filesystem checks over an orchestrator config before a run — dependency
  cycles (`dag-cycle`, error) via the shared `dag.find_cycle`, scope overlap
  between workstreams (`scope-overlap`, warning; two-tier: a static heuristic
  plus an exact file-set intersection when `--no-fs` is not set), empty scope
  (`scope-empty`, warning), missing/non-git repo (`repo-missing` /
  `repo-not-git`, errors), scope globs matching nothing on disk
  (`scope-no-match`, warning), and scope globs that are unsafe to expand —
  absolute, empty, containing a `..` segment, or otherwise rejected by the
  glob engine (`scope-invalid-pattern`, warning; contributes no files and
  never raises). `--strict` treats warnings as errors (exit 1);
  `--no-fs` skips filesystem checks for deterministic, repo-less runs. The
  same checks now run as a fail-fast gate inside `maestro orchestrate`
  (`maestro/preflight.py`).
- **`maestro init [PATH] [--force] [--project NAME]`:** scaffolds a commented
  `project.yaml` template with git-derived autofill (project name, repo path)
  and self-checks the generated config against `OrchestratorConfig` before
  writing (`maestro/scaffold.py`).

### Deprecated
- **`notifications.telegram_token` / `telegram_chat_id`.** Never wired to a
  runtime channel; setting them now logs a deprecation warning. Use
  `webhook_url` (e.g. via a small relay). The fields will be removed in a
  future config-schema window.
- **PR-created notification.** Entering `PR_CREATED` with an actual PR now
  fires a desktop notification carrying the PR URL. The URL travels as a
  structured transition payload (`Notification.url`) — never re-read from
  the mutable DB after the fact — and the effect stays declarative: a new
  `notification_requires_url` gate on the transition table means the two
  PR-less paths into `PR_CREATED` (PR-creation error, `auto_pr: false`
  convergence) keep firing the event log entry but stay silent.
- **Honest workstream progress (#123).** The progress denominator no longer
  grows lazily during a run: the planned subtask total is captured once
  from `spec-runner status --json` (spec-runner's own tasks.md parser)
  right after spec generation and persisted (`workstreams.subtask_total`,
  migration 19). A final progress refresh runs before any terminal
  transition, and no-op completions (spec-runner >= 2.16, #97) are counted
  and rendered — a run whose last task was a legitimate no-op now finishes
  as `5/5 done (1 no-op)` instead of the archaeology-inducing `DONE 4/5`.
  All of it is display-only and fail-open: any failure to obtain the total
  or read the final state keeps the previous label and never blocks
  completion.
- **`maestro workstream-rework <id>` (#124).** Sanctioned operator rework
  for a gate-blocked/failed workstream: `NEEDS_REVIEW/FAILED -> READY`
  with `resume_reason='operator_rework'` into the existing
  re-decomposition path (same worktree, same lineage, idempotent harness
  state cleanup). Mandatory `--reason` (audit-only, never enters the
  prompt), optional `--instructions` (next-attempt addendum, keyed by an
  explicit audit seq) and `--refresh-from <project.yaml>`
  (description/scope only, re-validated before anything is written;
  topology fields refused). Fail-closed liveness proof: pid-NULL alone is
  insufficient — open execution handles and the new durable
  recovery-ambiguity marker (written by startup recovery when parking
  possibly-live workstreams) block the command until proven terminal or
  explicitly resolved via the new `maestro workstream-resolve-ambiguity`.
  One CAS UPDATE + append-only audit row per rework (migration 18);
  nothing is ever written to `gate_approvals`; the Stage B rework budget
  is untouched. Unknown `resume_reason` values now fail closed to
  NEEDS_REVIEW instead of silently plain-resuming. `maestro workstreams`
  shows an operator-rework counter (warning-styled from 3).
- **Preflight spec-runner version gate (#122).** Mode 2 now requires
  spec-runner >= 2.16.0, enforced fail-closed by `maestro validate` and the
  `maestro orchestrate` preflight before any worktree is created: older
  versions (2.15.x) commit the harness-owned `spec/.gitignore` into task
  commits, which the ex-post scope gate flags as a scope escape. A version
  below the minimum, unparseable `spec-runner --version` output, or a
  missing binary all block with `spec-runner-version-unsupported`;
  `MAESTRO_SPEC_RUNNER_ALLOW_UNVERIFIED=1` (for unpublished local builds)
  downgrades the block to a warning. The scope gate itself is unchanged —
  `spec/.gitignore` deliberately stays visible to changed-paths.

### Upgrade impact
- **Upgrade spec-runner before the next `maestro orchestrate`.** The preflight
  version gate is fail-closed: an installed spec-runner below 2.24.0 now
  blocks before any worktree is created, naming the found and required
  versions. This includes 2.16–2.23, which were acceptable until now — a user
  who upgraded once for #122 and stopped is exactly whom the gate must stop,
  since those versions still exit 0 with work undone.
  `MAESTRO_SPEC_RUNNER_ALLOW_UNVERIFIED=1` remains the documented escape for
  unpublished local builds and downgrades the block to a warning, never to
  silence.

- **A workstream that is mid-run when you upgrade may stop for an operator
  decision.** The completeness gate is fail-closed on an uncaptured
  denominator, and `workstreams.subtask_total` is nullable — it has only been
  recorded since migration 19. A workstream that started before that (or
  whose spec-generation never captured a total) reaches the gate with
  `subtask_total IS NULL`, blocks as `unknown_total`, and waits in
  NEEDS_REVIEW until an operator runs `maestro workstream-approve <id>`
  (accept the result as-is and continue delivery) or
  `maestro workstream-rework <id>` (redo the work). This is the intended
  change — it is precisely the case the incident was — but it means an
  unattended upgrade of a running wave can pause at delivery instead of
  merging. Nothing is lost: the worktree and the branch are left intact.
- **Delivery now requires a post-mortem archive.** Any code path that reaches
  the delivery tail without one blocks fail-closed. In normal operation the
  archive is written during finalization, so this only surfaces for runs whose
  finalization predates the upgrade.
- Two new migrations run automatically on first connect: **23**
  (`postmortem_archives`, additive) and **24** (`gate_approvals` rebuilt with
  the widened phase CHECK, data preserved).

- **`maestro service` — scheduled autonomous runs.** Generates and loads
  a launchd/systemd **user** unit that starts a Maestro-owned wrapper
  (`maestro service run`), never `orchestrate` directly: only Maestro's
  own state can decide resume vs fresh vs no-op, and that decision table
  lives in the wrapper. Two independent stages (`--stage orchestrate |
  review`) with their own schedule, lock, ledger rows and logs.
  Locking is a two-level flock hierarchy so legacy and scoped runs
  exclude each other in *both* directions (legacy takes `global.lock`
  exclusive; a scoped stage takes it shared plus an exclusive
  per-(project, stage) lock) — different projects, and the two stages of
  one project, run concurrently. A tick that cannot take its lock is
  recorded and exits 0 rather than painting the unit red. Adds the
  `service_ticks` ledger (migration 22, sentinel + CAS finalize, with
  `decision` and `outcome` as distinct axes), a conservative
  stale-worktree sweep that never removes unmerged/dirty/NEEDS_REVIEW
  trees, Maestro-owned log rotation, and an install-time preflight that
  **refuses** to write a unit whose harnesses or credentials cannot be
  resolved non-interactively (a scheduled run gets no shell profile —
  the classic silent 03:00 failure). Design:
  `docs/superpowers/specs/2026-08-06-service-install-design.md`.

## v0.4.0 — Rename Zadacha → Workstream (2026-05-23)

**Breaking changes** (no backward compatibility):
- CLI: `maestro zadachi` → `maestro workstreams`
- REST API: `/zadachi` endpoints → `/workstreams`; `/zadachi/{zadacha_id}` → `/workstreams/{workstream_id}`
- `project.yaml`: top-level key `zadachi:` → `workstreams:`
- DB schema: `zadachi` table → `workstreams`; `zadacha_dependencies` → `workstream_dependencies`; `zadacha_id` columns → `workstream_id`. Migration auto-applied on first run.
- Python API: `Zadacha`, `ZadachaStatus`, `ZadachaConfig`, `ZadachaNotFoundError`, `ZadachaAlreadyExistsError` → `Workstream`, `WorkstreamStatus`, `WorkstreamConfig`, `WorkstreamNotFoundError`, `WorkstreamAlreadyExistsError`. Code that imports these symbols must update.

**Motivation:** transliterated Russian word ("zadacha" / "zadachi") in identifiers was confusing for English-speaking users and code review. `Workstream` is the natural English term for the concept — a parallel independent track of work that owns its own git worktree, spec-runner subprocess, and final PR.

**Scheduler-mode `Task` concept is UNAFFECTED** — only the orchestrator-mode concept was renamed.

---

## v0.3.0 (2026-05-23)

### Added
- `maestro/benchmark/arbiter_report.py` — `report_benchmark_to_arbiter(result, client)` helper; never raises (except `CancelledError`); returns a copy with `report_status` / `report_error` set.
- `BenchmarkResult.report_status` (`Literal["ok","failed","skipped"]`) + `.report_error` (`str | None`) on the M1 model.
- `BenchmarkTaskResult.task_type` and `.score` (additive; populated from ATP `metadata.task_type` when present).
- `BenchmarkRunner.run(..., run_id: str | None = None)` — caller-provided `run_id` overrides ATP's for CI-retry idempotency.
- `ArbiterClient.report_benchmark_raw(payload)` — low-level MCP method.
- `ArbiterContractError(code, message, data)` — distinguishes JSON-RPC contract breaks (`-32600`/`-32602`/`-32603`) from transient `ArbiterUnavailable`.
- Vendored client: `ARBITER_PROTOCOL_VERSION = "1.1.0"`, `MIN_ARBITER_PROTOCOL = (1, 1)`, `ARBITER_VENDORED_FROM_SHA = "7aeb6b1..."`; `start()` validates server-advertised `protocolVersion` (major-mismatch → `ArbiterContractError`, minor-low → WARNING).
- `_cowork_output/benchmark-contract/report_benchmark-v1.schema.json` — single source of truth (schema-first).
- `scripts/smoke_benchmark_report.py` — CI smoke against a real arbiter subprocess.
- 5 distinct observability events: `benchmark.report.{skipped,succeeded,duplicate,failed,contract_break}` (contract_break gets ERROR severity).

### Configuration
- `MAESTRO_BENCHMARK_REPORT_MAX_PER_TASK` env override (default 200) for per_task truncation.

### Changed
- `ARBITER_MCP_REQUIRED_VERSION` bumped `"0.1.0" → "0.2.0"` to match arbiter Phase 1 binary.
- `_send_and_receive` now raises `ArbiterContractError` (not `ArbiterUnavailable`) on JSON-RPC error codes -32600/-32602/-32603.

### Tests
- New: `tests/test_benchmark_arbiter_report.py` (~33 tests — projection, classification, helper paths, obs emit), `tests/test_benchmark_contract.py` (~9 tests — JSONSchema validation + forward-compat), `tests/test_arbiter_real_subprocess_benchmark.py` (3 e2e cases: created + duplicate + contract_break), `tests/test_arbiter_client_version.py` (5 version-sync tests), `tests/test_arbiter_errors.py` (4 contract-error tests), `tests/test_benchmark_models.py` (4 additive-field tests).
- Extended: `tests/test_arbiter_client.py` (+5 method/error-classification tests), `tests/test_benchmark_runner.py` (+4 run_id/task_type tests), `tests/test_benchmark_atp_client.py` (+3 task_type extraction tests).

### Cross-repo
- Requires `arbiter-mcp` at SHA `151004be4f0cf7ed20d3e734de8aaecf6b67c0ed` (PR #13 merge — latest behavioural change for `report_benchmark`: validation/runtime error classification, non-empty IDs, RFC3339 `ts`) or later. Earliest compatible SHA is `7aeb6b1a987a2610c9f2cddb38d90f42d849da42` (initial M4 baseline) — `151004be` is the recommended pin because it includes the input-validation hardening from PR #13. Advertises `protocolVersion="1.1.0"`, new `report_benchmark` MCP tool, `benchmark_runs` table migration.

Design: `docs/superpowers/specs/2026-05-23-r06b-m4-arbiter-wiring-design.md`.
Plan: `docs/superpowers/plans/2026-05-23-r06b-m4-arbiter-wiring.md`.

## v0.2.0 (2026-04-17)

### Added
- **Arbiter MCP client integration (R-03)** — optional policy-engine routing.
  Declare an `arbiter:` section in the project YAML to spawn an arbiter
  subprocess, ask it to route every ready task (`advisory` or `authoritative`
  mode), and report back outcomes for the learning loop. See
  [`examples/with-arbiter.yaml`](examples/with-arbiter.yaml) for a full
  configuration reference. When the section is absent or `enabled: false`,
  Maestro stays on the zero-config `StaticRouting` path — **byte-identical
  to v0.1.0**; no subprocess, no routing overhead.
- `AgentType.AUTO` routing sentinel — let the arbiter pick the agent per task.
- New `maestro/coordination/` subpackage: `routing.py` (`StaticRouting`,
  `ArbiterRouting`, `make_routing_strategy` factory), `arbiter_client.py`
  (vendored MCP client), `arbiter_errors.py`.
- `Task` gains persisted arbiter routing fields (`routed_agent_type`,
  `arbiter_decision_id`, `arbiter_route_reason`, `arbiter_outcome_reported_at`)
  with automatic SQLite migration for pre-R-03 databases.
- Scheduler delivers outcomes on completion/failure, gates retries on
  arbiter mode (advisory retries regardless of delivery failure;
  authoritative waits for successful `report_outcome`), and runs a
  bounded re-attempt pass (5/tick) each loop iteration with an
  authoritative abandon timer (`abandon_outcome_after_s`, default 300s)
  as the escape hatch when the arbiter stays unreachable.
- Crash recovery closes dangling arbiter decisions on startup via
  `recover_arbiter_outcomes` (available standalone or through
  `StateRecovery.recover(routing=...)`).
- 10 new structured `EventType` members cover the route/outcome/recovery
  lifecycle; `HoldThrottle` helper collapses repeat HOLD events.
- Dependency bump: `authlib` 1.6.9 → 1.6.11 (transitive via `fastmcp`).

### Compatibility
- Zero-config projects (no `arbiter:` section) behave exactly as in v0.1.0.
  No subprocess is spawned, no routing overhead, and the scheduler's
  route-then-spawn path short-circuits through `StaticRouting`.
- SQLite migration is idempotent; upgrading an existing v0.1.0 database
  adds four nullable columns with no data changes.

### Docs
- [`docs/superpowers/specs/2026-04-16-r03-arbiter-mcp-client-design.md`](docs/superpowers/specs/2026-04-16-r03-arbiter-mcp-client-design.md) —
  architecture spec.
- [`docs/superpowers/plans/2026-04-16-r03-arbiter-mcp-client.md`](docs/superpowers/plans/2026-04-16-r03-arbiter-mcp-client.md) —
  32-step implementation plan (all complete).

### Tests
- +113 tests (1112 total), `pyrefly check` 0 errors, `ruff check .` clean,
  `ruff format --check .` clean.

## v0.1.0 (2026-04-06)

First public release.

### Features
- **Mode 1 (Task Scheduler):** DAG-based scheduling of AI coding agents
  (Claude Code, Codex, Aider) in a shared directory
- **Mode 2 (Multi-Process Orchestrator):** Decompose projects into independent
  workstreams, run each in isolated git worktrees via spec-runner, auto-create PRs
- Spawner registry with 4 built-in spawners (claude_code, codex, aider, announce)
- SQLite state persistence with crash recovery
- CLI: run, status, retry, stop, orchestrate, workstreams, workspaces
- Web dashboard with DAG visualization and SSE updates
- Desktop notifications (macOS/Linux)
- Auto-commit per task with git diff summary
- Dogfood-tested: Maestro builds itself (3 weeks of real usage)
