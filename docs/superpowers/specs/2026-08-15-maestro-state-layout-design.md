# Per-project, per-run state databases — design

**Status:** revision 3 (2026-08-15).

Revision 2 closed the run/invocation confusion but left four contradictions, all found in
review and all verified against the code before this rewrite. The stage lock is keyed
`(repo, stage)` and so could not attribute liveness to a *run* — an interrupted run was read
as running whenever any other run of the same repository held the lock (§B.3). Putting
`needs_human` in the terminal enum contradicted §A.1, since the database outlives a human
pause (§B.1.1, confirmed by `maestro/service/decide.py:30`). The selection table quietly turned plain
`orchestrate` — which clears state by design, `maestro/cli.py:1437` — into an implicit resume
(§C.2). And `_local/<name>` collided for two remoteless checkouts sharing a basename
(§3.3).

Revision 1 had bound the database to a `pipeline_id` minted per CLI invocation, which
would have broken `--resume`.

Owner decisions carried through: the legacy database is **not** migrated; `dispatcher#147`
is filed first but does **not** block this change; retention gets a trigger rather than a
policy, and cross-run cost aggregation is out of scope at two rows in `task_costs`.

## 1. What the layout is today, and the failure it produced

`maestro/cli.py:115-116` fixes one database for everything:

```python
DEFAULT_DB_DIR = Path.home() / ".maestro"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "maestro.db"
```

There is no project key anywhere in that path. `--db` exists (six call sites in
`cli.py`) and is the only way to separate two projects — which makes separation an
operator act, repeated by hand, remembered or not.

**It was not remembered, and the cost was measured.** On 2026-08-15 the single file
held three unrelated things at once:

| Rows | Origin |
|---|---|
| 3 `gate_approvals`, `ex_post`, actor `human` | **kapelle** S2 pilot, 2026-08-05 |
| 7 `workstreams` (`w-contracts` … `w-runtime`) | **disputatio** wave 1, 2026-08-08 |
| 4 `tasks` (`greet`, `compute`, `summarize`, `corr-demo-note`) | July phase-0.5 demos |

And it was a week stale. It reported disputatio wave 1 as `w-contracts` done, three
workstreams `ready`, three `pending` — while the wave was in fact closed: all six
workstreams integrated at `e218bd4`, 785 passed (`5ffb797` in the umbrella repo).

The reason is the escape hatch working exactly as designed. Runs 2 and 3 were started
with `--db` into the workspace directory:

| Database | Written | Wave state it records |
|---|---|---|
| `~/.maestro/maestro.db` | 08-08 18:50 | 1 done (PR #4), 3 ready, 3 pending |
| `~/labs/disputatio-ws/maestro-run2.db` | 08-08 20:19 | 1 `needs_review`, 6 pending — a restart from scratch |
| `~/labs/disputatio-ws/maestro-run3.db` | 08-09 10:36 | 2 done (PR #5, #6), 1 `needs_review`, 3 ready, 1 pending |

Three runs, none of which reached the end; the wave was finished by hand under
`manual bypass containment (Maestro orchestration SUSPENDED)`. Two of the restarts have
a recorded cause — `incident-2026-08-08-false-done` and
`incident-2026-08-09-w-adapters-false-done`, each with a database snapshot, refs and
workstream states.

**So the requirement this design serves is not tidiness.** Starting a run from a clean
slate *while the previous run's state survives as evidence* is a demonstrated,
twice-exercised need. The current layout meets it only by the operator inventing a path,
and the invented paths are invisible to every consumer.

The consumer that matters is `dispatcher/core/discovery.py:13`, which pins
`_DEFAULT_MAESTRO_DB = ~/.maestro/maestro.db`. For a week the read plane showed a closed
wave as unfinished and could not see runs 2 and 3 at all — not because it was broken, but
because it was reading the file it was told to read.

## 2. What moves, and what deliberately does not

**Maestro's orchestration state moves.** It is runtime state of the orchestrator, not
content of the product being built. In product-delivery mode the target repository
belongs to someone else, and writing orchestrator bookkeeping into it is a `write_scope`
violation in spirit (ADR-ECO-007 D2). The codebase already takes this position:
`maestro/execution/models.py:22` excludes `.maestro/**` from synchronisation.

**spec-runner's execution state does not move, and must not.**
`spec-runner/docs/state-schema.md` declares `spec/.executor-state.db` a **stable contract
surface** whose consumer is "Maestro (read-only)". It sits beside the spec it executes,
describes that repository's tasks, and is covered by that repository's ignore rules.
Relocating it would break a declared-stable contract and separate the state from the
thing it describes, for no gain.

The boundary is ownership, not location: **task execution belongs to spec-runner inside
the project; orchestration belongs to Maestro in `~/.maestro/`.**

## 3. Layout and project identity

```
~/.maestro/
  projects/<host>/<owner>/<repo>/
    runs/<run-id>/
      state.db
      logs/
    locks/
  maestro.db          # legacy, frozen, never written again
  service.env
```

A run is a **directory**, not a bare file. Logs move under it so that state and telemetry
of one run are co-located and removable as a unit; §D depends on that.

### 3.1 Host is part of the identity

`<owner>/<repo>` alone collides: `github.com/acme/app`, `gitlab.com/acme/app` and an
enterprise `git.company/acme/app` are three different repositories with one key. The host
segment is therefore mandatory.

Case is normalised **per host, not globally**. GitHub treats owner and repository names
case-insensitively; an arbitrary Git remote does not have to. Normalising everything to
lowercase would silently merge two distinct repositories on a case-sensitive host.

### 3.2 Identity comes from the remote, never from `project:`

`project:` in `project.yaml` is **not** admissible as the key. It is operator-chosen and
already inconsistent across two live configs — `project: disputatio` against
`project: kapelle-s2`, where the second names a pilot rather than a repository.

The key is parsed from `repo_url`, which `OrchestratorConfig` already requires
(`maestro/models.py:1765`, `Field(..., min_length=1)`).

### 3.3 Mode 1 is in scope, and needs a derivation of its own

Mode 1 (`maestro run <tasks.yaml>`) loads a different model: `load_config` →
`ProjectConfig` (`maestro/models.py:863`), which carries `repo:` — a **local path** — and no
`repo_url`. A path cannot be the identity (ADR-ECO-007 D2), so Mode 1 would otherwise
have to keep writing the legacy database, which contradicts "legacy, never written
again".

Mode 1 therefore derives the same identity **at runtime from the checkout**:
`git -C <repo> remote get-url origin`, parsed by the same rule as §3.2.

A checkout with **no** `origin` resolves into an explicitly local namespace:

```
projects/_local/<sanitised-name>-<hash of the canonical git common dir>/
```

The bare name alone would collide — `/tmp/a/project` and `/work/b/project` are two
independent repositories with one basename, which is the mixing this whole design
removes. The path therefore appears as a **local fingerprint**, never as identity: a
local-only repository genuinely has no portable identity, and the honest encoding says
"this locator, on this machine" rather than inventing a portable-looking name. The hash
is taken over the canonical git common dir so that worktrees of one repository resolve
together.

### 3.4 What "refuses to start" covers, and what it does not

An absent remote in Mode 1 is **resolvable** — it lands in `_local/` (§3.3) and is not a
refusal.

The refusal is for the cases where identity cannot be established at all: a `repo_url`
that does not parse, a `repo:` path that is not a git checkout, a remote the parser
cannot map to host/owner/repo. In those cases the run stops with the reason stated.

What is never done is falling back to `project:`. That would be a silent downgrade to the
ambiguous identifier, which is the defect, not the mitigation.

## A. Run, invocation and service-tick identity

Three levels exist today and revision 1 conflated them:

```
service tick  →  CLI invocation (orchestrate / orchestrate --resume)  →  logical run
```

### A.1 The logical run is the unit that owns a database

- **fresh** orchestration mints a new `run_id` and creates `runs/<run-id>/`;
- **resume** reuses the existing `run_id` and reopens the same `state.db`;
- a **service tick** has its own `tick_id` and never creates a run directory — it decides
  `fresh | resume | noop_complete | noop_blocked` (`service/decide.py`) and acts on the
  selected run;
- a **review tick** reads the state of a selected run and creates nothing.

This is not a new distinction; it is the one `decide_orchestrate` already documents:
*"anything still advanceable → `resume` (never a fresh run over a half-finished DAG)"*.
Binding a database to each CLI invocation would have broken exactly this.

### A.2 `run_id` is the telemetry `pipeline_id`, pinned rather than minted

`ORCHESTRA_PIPELINE_ID` is already the contract: `maestro/_vendor/obs.py:144,164` reads it and
falls back to `ulid.new()`, and `maestro/gates.py:174` reads it too. So this design does not
introduce a correlation id — it stops the fallback from firing per invocation.

For a fresh run the id is minted once and exported. **For a resume it is read out of the
selected run's `run` row and exported**, so every invocation of one logical run shares a
`pipeline_id` and its logs land under that run's directory.

### A.3 Startup order

The database path can no longer be resolved before the configuration is read, which
reverses today's order (`_service_run` at `maestro/cli.py:2437` opens `Database(db_path)` first):

1. load config;
2. resolve repository identity (§3);
3. decide `fresh | resume` (existing `decide_orchestrate` over the selected run);
4. fresh → mint `run_id`; resume → read it from the selected run;
5. resolve the run directory and `state.db`;
6. export `ORCHESTRA_PIPELINE_ID=<run_id>`;
7. initialise logging, then open the database.

Step 6 must precede step 7: logging reads the variable at setup, and a late export leaves
the first records under a different id.

### A.4 Resolving the circular identity in `maestro/service/locks.py`

`project_key(project, db_path)` (`maestro/service/locks.py:55`) hashes the database path into the lock
identity, deliberately: *"the same project name against two databases is two independent
instances."* Once the database path is derived from the project identity, that is
circular — the key would depend on the path that depends on the key.

**The lock identity becomes `(repo-identity, stage)`**, dropping both `project:` and the
database path.

This is a behaviour change and is called out rather than buried: today two runs of one
project against two `--db` files are independent lock instances and may run in parallel.
Afterwards they serialise per stage. That is the correct behaviour — both would drive the
same repository and the same worktrees, and the parallelism they had was an artefact of
identity being keyed on a file path — but any workflow that relied on it will now block,
visibly, on the stage lock.

## B. Run lifecycle and liveness

### B.1 The schema has no run-level row today

The current schema is entirely per-entity — `workstreams`, `tasks`, `gate_approvals`,
`workstream_reworks` and so on. Each run database therefore gains one row:

```sql
CREATE TABLE run (
    run_id       TEXT PRIMARY KEY,  -- the ULID; equals the directory name
    repo_key     TEXT NOT NULL,     -- <host>/<owner>/<repo>, as resolved in §3
    started_at   TEXT NOT NULL,
    outcome      TEXT CHECK (outcome IN
                   ('completed','cancelled','superseded','failed')),
    ended_at     TEXT,              -- set with outcome, never alone
    reason       TEXT,              -- free-form detail, never load-bearing
    suspended_at TEXT,              -- a human pause; NOT an ending
    suspend_reason TEXT
);
```

`outcome` is typed and separate from `reason` because a consumer that has to infer
"failed" from prose will infer it differently each time.

### B.1.1 Three levels of outcome, and why `needs_human` is not one of them here

Revision 2 put `needs_human` in the terminal enum, which contradicted §A.1: the database
belongs to the logical run and survives resume, but a terminal row ends it. After a human
approval the code would then have to either rewrite a finished row or mint a new run and
lose the resume identity. Both are wrong.

`maestro/service/decide.py:30` already states the correct semantics: *"DONE/ABANDONED are terminal;
NEEDS_REVIEW is terminal **for the loop** … but is reported separately."* A human pause
ends a tick, not a run.

| Level | Values | Where it lives |
|---|---|---|
| logical run outcome | `completed`, `cancelled`, `superseded`, `failed` | `run.outcome` + `ended_at` |
| current suspension | waiting on a human | `run.suspended_at` + `suspend_reason`, **no** `ended_at` |
| invocation / tick outcome | `ok`, `needs_human`, `failed`, `infra_error` | already exists in the tick result |

`failed` is admitted as a run outcome only when the run cannot be advanced further. A
failure that workstream rework can still address leaves the run non-terminal — otherwise
rework would face the same rewrite-a-finished-row problem.

`outcome IS NULL` means **no terminal record was written**, which §B.3 shows is not the
same as *interrupted*, and is also not the same as *suspended*.

**Deriving the terminal state from workstream statuses instead was considered and
rejected**, on evidence from the runs this design exists because of. `maestro-run3.db`
ends with two workstreams `done`, one `needs_review`, three `ready` and one `pending`.
That distribution is indistinguishable between "the run stopped early" and "the run is
waiting on a human". A run either recorded its own ending or it did not.

### B.2 There is no `current` pointer file

A pointer is state that can outlive what it points at, and this repository has just paid
for exactly that failure: the single `maestro.db` was, in effect, a pointer that kept
claiming authority for a week after the work moved elsewhere.

### B.3 Liveness is observed, not inferred from a NULL

Revision 1 said a run without a terminal record is *interrupted*. That erases the case
that matters most: a **running** run also has `ended_at IS NULL`. Fail-closed must not
mean discarding a knowable fact.

Liveness is read from the lock, reusing the model `maestro/service/locks.py` already establishes —
*flock is the authority; the pid file is diagnostics only*.

**But the lock alone attributes liveness to the wrong run.** After §A.4 the lock is keyed
`(repo-identity, stage)`, not by run. So: run A is interrupted with `outcome` NULL; run B
of the same repository starts and holds the orchestration lock; a collector classifying A
sees the lock held and reports A as running. The lock proves *"an orchestration stage is
live in this repository"* — never *"this run is live"*.

Attribution therefore needs the holder's identity, and the mechanism already exists:
`maestro/service/locks.py:157` writes a `<stage>.pid` sidecar under the held lock. That sidecar gains the
holder's `run_id`, and a run is **running** only when both hold:

| `outcome` | stage lock | holder `run_id` | Reported as |
|---|---|---|---|
| not NULL | — | — | terminal, with the typed outcome |
| NULL | held | equals this run | **running** |
| NULL | held | a different run | **interrupted / unknown** |
| NULL | free | — (ignored) | **interrupted / unknown** |

The conjunction is what makes this safe. A sidecar file outlives the process that wrote
it, so it can never grant liveness on its own; it only *attributes* liveness that the
lock has already proven. When the lock is free the sidecar is not read at all.

This does change one line of the `maestro/service/locks.py` contract — "nothing branches on it" — for the
`run_id` field specifically. The pid, host and boot identity stay diagnostics: they
explain *which process*, never *whether one is alive*.

## C. Run selection and command resolution

### C.1 Newest ULID is not a selection policy

Within a single millisecond ULIDs are **not** monotonic. Verified against the pinned
`ulid 1.1.0`: six ids minted back to back in one millisecond do not sort into mint order.
So lexicographic maximum answers "latest id", not "latest started", and never "the run to
act on".

The failure it produces is concrete: a run stops at `needs_human`; a second run is
started by mistake and dies in preflight before materialising anything; its id is now the
maximum, and the run that actually needs a human disappears from the default view.

### C.2 The resolver returns a classified list

`resolve_runs(repo_key)` returns every run with its `outcome`, liveness (§B.3) and
`started_at`. Commands choose by their own question, and the defaults differ because the
questions differ:

| Caller | Selection |
|---|---|
| `orchestrate` (no flag) | **starts a fresh run** — unchanged semantics; refuses if a run is live, and lists any non-terminal runs so the operator chooses explicitly |
| `orchestrate --resume` | the single resumable run; refuses if there are several |
| `orchestrate --run <id> --resume` | that run, explicitly |
| service tick | its own policy — `decide_orchestrate` over the selected run |
| dashboard / collector | all runs, newest first, each with its state |
| any command | `--run <run-id>` overrides |

**Plain `orchestrate` must not become an implicit resume.** Today it deliberately does the
opposite: without `--resume` it *clears* existing workstreams, and says so —
`"Clearing N existing workstreams state (use --resume to continue where you left off)"`
(`maestro/cli.py:1437`). An earlier draft of this section had plain `orchestrate` pick up the
newest non-terminal run, which would have changed a destructive-by-design command into an
auto-resuming one as a side effect of a storage change. Storage layout does not get to
redefine CLI semantics.

What does change is that clearing is no longer *in place*: a fresh run gets its own
directory, so the previous run survives as evidence instead of being erased. That is the
whole point of §1, and it is achieved without touching what the command means.

Automatic resume-or-fresh selection stays where it already lives — the service tick, which
owns that decision today via `decide_orchestrate`.

"Refuses if several are resumable" is deliberate: two advanceable runs on one repository
is an operator-visible anomaly, and picking one silently is how the current layout lost a
week.

### C.3 Workstream commands need a project and a run

Most workstream commands take only a workstream id. Once state is per-run, the same
workstream id exists in many databases, so these commands resolve `(repo-identity, run)`
by §C.2 first and take `--run` to disambiguate. Without this they would pick a database
by accident, which is the defect in a new place.

## D. Durability, permissions and growth

**A run directory becomes discoverable only when it is complete**, and the ordering is
load-bearing:

1. create the directory under a temporary name;
2. create `state.db` and write the `run` row;
3. **close the database** — WAL and shm included;
4. rename the directory into `runs/`;
5. reopen `state.db` at its final path for the run itself.

Step 3 is not caution for its own sake: renaming a directory out from under an open
SQLite connection leaves the WAL and shm files associated with a path that no longer
exists, and the failure mode is a torn database rather than an error. A collector
enumerating `runs/` must never observe a database without its `run` row — that state is
indistinguishable from a corrupted run.

**Permissions.** `~/.maestro/` and everything under it is created `0700` / `0600`. The
state carries prompts, absolute paths, costs and operator decisions; the current
directory is world-readable by default and should not be.

**Growth has a trigger, not a policy.** Retention is deliberately *not* designed here:
this is a repository where run evidence twice supported an incident investigation, and a
deletion policy for evidence is its own decision with its own owner. What this design
commits to is the trigger — the layout makes a run removable as one directory, and
`~/.maestro` gains a size report so growth becomes visible before it becomes a problem.
The existing 13 130 log directories (91 % of them empty) are the evidence that unbounded
growth is real rather than theoretical.

**Cost reporting is a recorded consequence, not a deliverable.** `task_costs` is
per-database, so after the split a cost query answers "this run" rather than "everything".
Cross-run aggregation is **not** designed here: `task_costs` holds two rows in total, so
there is no demand to design against yet.

## E. Legacy

`~/.maestro/maestro.db` is **not migrated**. Migration would require assigning every row
to a project, and for the July demo tasks (`greet`, `compute`, `summarize`) the honest
answer is "none". The file stays where it is, readable, and Maestro stops writing to it.

`--db` survives unchanged as an escape hatch. It is not the defect; the absent default key
was. When `--db` is given it selects that database directly, and the resolver of §3 is not
consulted at all.

**A database reached through `--db` may have no `run` row** — every database written
before this change is in that state, including the three that motivated it. Such a
database is read as a single anonymous run: `run_id` unknown, `outcome` unknown, liveness
unknown, and it is labelled *legacy* rather than silently rendered as interrupted. It is
never written a `run` row retroactively, because inventing a `started_at` and a `repo_key`
for rows whose origin is exactly what is in question would manufacture the evidence this
design exists to preserve.

## F. Sequencing, and the window this opens

`dispatcher#147` is filed **before** this change and does not block it (owner decision,
2026-08-15).

The window must be stated plainly rather than discovered: once Maestro stops writing the
old path, the dashboard stops moving. That is a lie of a different kind — frozen rather
than stale — and it is the smaller one, because a value that never changes is legible as
broken, whereas the current file changed just often enough to look alive. Keeping the
legacy file in place (§E) is what keeps the dashboard rendering something rather than
erroring.

Done, on the dispatcher side, when the collector enumerates
`~/.maestro/projects/*/*/*/runs/*/state.db`, reports runs newest-first per repository, and
distinguishes running from interrupted by the lock (§B.3) rather than by `ended_at`.

## G. Testing

Identity:

- parsing `repo_url` across forms: `https://`, `git@`, trailing `.git`, and a non-GitHub host;
- two hosts with identical `owner/repo` resolve to different directories;
- case folding applies on a case-insensitive host and does **not** merge two repositories on a case-sensitive one;
- Mode 1 derives the same key from `git remote get-url origin`, and a checkout with no remote lands in `_local/` rather than failing;
- **two remoteless checkouts sharing a basename (`/tmp/a/project`, `/work/b/project`) resolve to different `_local/` directories**, and two worktrees of one repository resolve to the same one;
- an unresolvable identity — unparseable remote, non-git `repo:` — refuses to start, with a stated reason and no fallback to `project:`.

Run identity and lifecycle:

- fresh mints a `run_id`, resume reuses it, and both export the same `ORCHESTRA_PIPELINE_ID`;
- a resumed run writes its logs under the original run directory, not a new one;
- a service tick creates no run directory;
- the `run` row is written at start with `outcome` NULL and completed exactly once;
- each terminal `outcome` value round-trips;
- **a needs-human pause sets `suspended_at` and leaves `ended_at` NULL; after the human acts, the same `run_id` resumes in the same database** — no new run, no rewritten terminal row;
- a failure that rework can still address leaves the run non-terminal.

Liveness and selection (§B.3, §C):

- `outcome` NULL with the stage lock held **by this run** reports running; with the lock free, interrupted;
- **run A interrupted while run B of the same repository holds the lock: A reports interrupted, not running** — the case the lock alone gets wrong;
- **plain `orchestrate` performs no implicit resume**: it starts a fresh run, and refuses while a run is live;
- selection does **not** assume ULID ordering within a millisecond — the test asserts the documented policy (live, else newest non-terminal), never that the lexicographic maximum is the newest started;
- two non-terminal runs make `--resume` refuse rather than choose;
- `--run` overrides every default.

Durability:

- a run directory is never visible in `runs/` without its `run` row (interrupt between create and rename);
- the database is closed before the directory is renamed, and reopened after — asserted on the ordering, not only on the end state;
- created files and directories are `0600` / `0700`;
- `--db` still overrides the resolver, and a database with no `run` row is reported as *legacy* rather than as interrupted, and is not written a row retroactively;
- `~/.maestro/maestro.db` is not opened for writing by any code path after the change.

## H. Non-goals

- Migrating or reconciling the legacy database (§E).
- Changing spec-runner's state location or its schema (§2).
- Editing dispatcher — a neighbouring repository; the request is `dispatcher#147`.
- A retention or garbage-collection policy (§D) — trigger only.
- Cross-run cost aggregation (§D).
- A cross-project index. Enumeration by directory is sufficient for the consumer that
  exists, and an index is exactly the kind of durable pointer §B.2 rejects.
