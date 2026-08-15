# Per-project, per-run state databases — design

**Status:** revision 2 (2026-08-15). Revision 1 was reviewed and found not
implementation-ready: it bound the database to a telemetry `pipeline_id` that is minted
per CLI invocation, which would have broken `--resume`; it treated `ended_at IS NULL` as
meaning *interrupted*, which erases a live run; and its identity rule was unavailable in
Mode 1. This revision adds §A–§C (run identity, liveness, selection), resolves a circular
identity found during verification (§A.4), and narrows the layout to Mode 2 with an
explicit rule for Mode 1 (§3.3). Owner decisions carried forward from revision 1: the
legacy database is **not** migrated, and `dispatcher#147` is filed first but does **not**
block this change.

## 1. What the layout is today, and the failure it produced

`cli.py:115-116` fixes one database for everything:

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
`execution/models.py:22` excludes `.maestro/**` from synchronisation.

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
(`models.py:1765`, `Field(..., min_length=1)`).

### 3.3 Mode 1 is in scope, and needs a derivation of its own

Mode 1 (`maestro run <tasks.yaml>`) loads a different model: `load_config` →
`ProjectConfig` (`models.py:863`), which carries `repo:` — a **local path** — and no
`repo_url`. A path cannot be the identity (ADR-ECO-007 D2), so Mode 1 would otherwise
have to keep writing the legacy database, which contradicts "legacy, never written
again".

Mode 1 therefore derives the same identity **at runtime from the checkout**:
`git -C <repo> remote get-url origin`, parsed by the same rule as §3.2. A checkout with
no `origin` resolves into an explicitly local namespace,
`projects/_local/<sanitised-repo-name>/`, which is a *namespace*, not a path-as-identity:
it says "this repository has no remote identity" rather than pretending a path is one.

### 3.4 A run with no resolvable identity refuses to start

Falling back to `project:` would be a silent downgrade to the ambiguous identifier, which
is the defect, not the mitigation.

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

`ORCHESTRA_PIPELINE_ID` is already the contract: `_vendor/obs.py:144,164` reads it and
falls back to `ulid.new()`, and `gates.py:174` reads it too. So this design does not
introduce a correlation id — it stops the fallback from firing per invocation.

For a fresh run the id is minted once and exported. **For a resume it is read out of the
selected run's `run` row and exported**, so every invocation of one logical run shares a
`pipeline_id` and its logs land under that run's directory.

### A.3 Startup order

The database path can no longer be resolved before the configuration is read, which
reverses today's order (`_service_run` at `cli.py:2434` opens `Database(db_path)` first):

1. load config;
2. resolve repository identity (§3);
3. decide `fresh | resume` (existing `decide_orchestrate` over the selected run);
4. fresh → mint `run_id`; resume → read it from the selected run;
5. resolve the run directory and `state.db`;
6. export `ORCHESTRA_PIPELINE_ID=<run_id>`;
7. initialise logging, then open the database.

Step 6 must precede step 7: logging reads the variable at setup, and a late export leaves
the first records under a different id.

### A.4 Resolving the circular identity in `locks.py`

`project_key(project, db_path)` (`locks.py:55`) hashes the database path into the lock
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
    run_id      TEXT PRIMARY KEY,   -- the ULID; equals the directory name
    repo_key    TEXT NOT NULL,      -- <host>/<owner>/<repo>, as resolved in §3
    started_at  TEXT NOT NULL,
    outcome     TEXT CHECK (outcome IN
                  ('completed','needs_human','failed','cancelled','superseded')),
    ended_at    TEXT,
    reason      TEXT                -- free-form detail, never load-bearing
);
```

`outcome` is typed and separate from `reason` because a consumer that has to infer
"failed" from prose will infer it differently each time. `outcome IS NULL` means **no
terminal record was written** — which §B.3 shows is not the same as *interrupted*.

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

Liveness is read from the lock, reusing the model `locks.py` already establishes —
*flock is the authority; the pid file is diagnostics only*:

| `outcome` | stage lock held | Reported as |
|---|---|---|
| not NULL | — | terminal, with the typed outcome |
| NULL | held | **running** |
| NULL | free | **interrupted / unknown** |

The pid, host and boot identity stay diagnostics: they explain *which* process, never
*whether* one is alive.

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

| Caller | Default selection |
|---|---|
| `orchestrate` (no flag) | the live run if one exists, else the newest non-terminal run |
| `orchestrate --resume` | the newest non-terminal run; refuses if several are non-terminal |
| service tick | same as `--resume`, then `decide_orchestrate` over it |
| dashboard / collector | all runs, newest first, each with its state |
| any command | `--run <run-id>` overrides |

"Refuses if several are non-terminal" is deliberate: two advanceable runs on one
repository is an operator-visible anomaly, and picking one silently is how the current
layout lost a week.

### C.3 Workstream commands need a project and a run

Most workstream commands take only a workstream id. Once state is per-run, the same
workstream id exists in many databases, so these commands resolve `(repo-identity, run)`
by §C.2 first and take `--run` to disambiguate. Without this they would pick a database
by accident, which is the defect in a new place.

## D. Durability, permissions and growth

**A run directory becomes discoverable only when it is complete.** The directory is
created under a temporary name, the `run` row is written, and only then is it renamed
into `runs/`. A collector enumerating the directory must never observe a database without
its `run` row — that state is indistinguishable from a corrupted run.

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
was. When `--db` is given, it selects the database directly and the run directory is its
parent — the escape hatch stays an escape hatch and does not acquire a second meaning.

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
- Mode 1 derives the same key from `git remote get-url origin`, and a checkout with no remote lands in `_local/` rather than failing or using a path;
- an unresolvable identity refuses to start, with a stated reason and no fallback to `project:`.

Run identity and lifecycle:

- fresh mints a `run_id`, resume reuses it, and both export the same `ORCHESTRA_PIPELINE_ID`;
- a resumed run writes its logs under the original run directory, not a new one;
- a service tick creates no run directory;
- the `run` row is written at start with `outcome` NULL and completed exactly once;
- each terminal `outcome` value round-trips.

Liveness and selection (§B.3, §C):

- `outcome` NULL with the stage lock held reports **running**; with the lock free, **interrupted**;
- selection does **not** assume ULID ordering within a millisecond — the test asserts the documented policy (live, else newest non-terminal), never that the lexicographic maximum is the newest started;
- two non-terminal runs make `--resume` refuse rather than choose;
- `--run` overrides every default.

Durability:

- a run directory is never visible in `runs/` without its `run` row (interrupt between create and rename);
- created files and directories are `0600` / `0700`;
- `--db` still overrides the resolver;
- `~/.maestro/maestro.db` is not opened for writing by any code path after the change.

## H. Non-goals

- Migrating or reconciling the legacy database (§E).
- Changing spec-runner's state location or its schema (§2).
- Editing dispatcher — a neighbouring repository; the request is `dispatcher#147`.
- A retention or garbage-collection policy (§D) — trigger only.
- Cross-run cost aggregation (§D).
- A cross-project index. Enumeration by directory is sufficient for the consumer that
  exists, and an index is exactly the kind of durable pointer §B.2 rejects.
