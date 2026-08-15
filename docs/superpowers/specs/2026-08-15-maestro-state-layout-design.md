# Per-project, per-run state databases — design

**Status:** approved (2026-08-15). Scope confirmed with the owner on two points
that were open when drafting: the existing `~/.maestro/maestro.db` is **not**
migrated, and the dispatcher issue is filed first but does **not** block this
change. Companion request: `dispatcher#147` (inbox).

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
  projects/<owner>/<repo>/runs/<run-id>.db
  maestro.db          # legacy, frozen, never written again
  service.env
```

The key is **canonical repository identity derived from `repo_url`** — the rule
ADR-ECO-007 D2 adopted for `write_scope`: a repository is named by its manifest key or
`owner/name`, never by a filesystem path.

`project:` in `project.yaml` is **not** admissible as the key. It is operator-chosen and
already inconsistent across two live configs — `project: disputatio` against
`project: kapelle-s2`, where the second names a pilot rather than a repository. A key
that drifts reproduces the mixing this design removes.

**A run with no resolvable `repo_url` refuses to start.** Falling back to `project:` would
be a silent downgrade to the ambiguous identifier, which is the defect, not the mitigation.

## 4. The run id is the ULID Maestro already mints

Maestro generates a ULID per run: it is `pipeline_id` in the telemetry and the name of the
log directory, `logs/<ULID>/`. The run database takes **the same** id, so
`runs/<ULID>.db` and `logs/<ULID>/` join without a single new identifier.

This is the cheapest part of the design and the most valuable. Today `pipeline_id` appears
in 12 844 log attributes and nothing else consumes it; the state and the telemetry of one
run cannot be joined at all. Reusing it closes that locally — a partial, concrete instance
of the correlation identity left open as OQ in ADR-ECO-007.

ULIDs also sort lexicographically by time, which §5 relies on.

## 5. Determining the current run — and why there is no pointer file

The newest run is the greatest ULID under the project's `runs/`.

**This requires one new table, and the schema does not have it today.** The current schema
is entirely per-entity — `workstreams`, `tasks`, `gate_approvals`, `workstream_reworks` and
so on — with no row describing the run itself. So each run database gains a single-row
table:

```sql
CREATE TABLE run (
    run_id     TEXT PRIMARY KEY,   -- the ULID; equals the file name and the log dir
    repo_key   TEXT NOT NULL,      -- <owner>/<repo>, as resolved in §3
    started_at TEXT NOT NULL,
    ended_at   TEXT,               -- NULL until a clean finish
    stop_reason TEXT               -- NULL until a clean finish
);
```

**Deriving the terminal state from workstream statuses instead was considered and
rejected**, on evidence from the runs this design exists because of. `maestro-run3.db`
ends with two workstreams `done`, one `needs_review`, three `ready` and one `pending`.
That distribution is indistinguishable between "the run stopped early" and "the run is
waiting on a human" — which is precisely the ambiguity §5 is written to remove. A run
either recorded its own ending or it did not; nothing else answers the question.

A `current` pointer file is **rejected**. A pointer is state that can outlive what it
points at, and this repository has just paid for exactly that failure: the single
`maestro.db` was, in effect, a pointer that kept claiming authority for a week after the
work moved elsewhere.

**A run with no terminal record reads as `interrupted / unknown`, never as `in progress`.**
This is the load-bearing rule of the section. Absence of an ending is absence of
information, and rendering it as "running" is how a dead run keeps looking alive — the
precise defect being removed. Fail closed.

## 6. Legacy

`~/.maestro/maestro.db` is **not migrated**. Migration would require assigning every row to
a project, and for the July demo tasks (`greet`, `compute`, `summarize`) the honest answer
is "none". The file stays where it is, readable, and Maestro stops writing to it.

`--db` survives unchanged as an escape hatch. It is not the defect; the absent default key
was.

## 7. Sequencing, and the window this opens

`dispatcher#147` is filed **before** this change and does not block it (owner decision,
2026-08-15).

The window must be stated plainly rather than discovered: once Maestro stops writing the
old path, the dashboard stops moving. That is a lie of a different kind — frozen rather
than stale — and it is the smaller one, because a value that never changes is legible as
broken, whereas the current file changed just often enough to look alive. Keeping the
legacy file in place (§6) is what keeps the dashboard rendering something rather than
erroring.

Done, on the dispatcher side, when the collector enumerates
`~/.maestro/projects/*/*/runs/*.db`, reports the newest run per project by ULID order, and
renders a run without a terminal record as interrupted.

## 8. Testing

- key derivation from `repo_url` across forms: `https://`, `git@`, trailing `.git`, mixed case;
- refusal, with a stated reason, when `repo_url` is absent or unparseable — no fallback to `project:`;
- the run database is created under the derived key, and two projects run concurrently without touching each other's files;
- newest-run selection across several ULIDs, including ids minted in the same millisecond;
- the `run` row is written at start with `ended_at` NULL, and completed exactly once on a clean finish;
- a run killed before completion leaves `ended_at` NULL and is reported `interrupted`, not `in progress`;
- `--db` still overrides the resolver;
- `~/.maestro/maestro.db` is not opened for writing by any code path after the change.

## 9. Non-goals

- Migrating or reconciling the legacy database (§6).
- Changing spec-runner's state location or its schema (§2).
- Editing dispatcher — a neighbouring repository; the request is `dispatcher#147`.
- A cross-project index or query surface. Enumeration by directory is sufficient for the
  consumer that exists; an index is the kind of durable state that goes stale, and §5
  explains what this design thinks of those.
