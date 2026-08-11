# DONE completeness gate + post-mortem archive (#164) — design

**Status:** approved (revision 2, 2026-08-10 — every §10 question answered by
the owner and folded in; Copilot review on the revision-1 PR #172 raised no
contract comments, which was the owner's stated condition for leaving
`proposed`). Four acceptance decisions are recorded verbatim in §2 and are not
re-litigated. Depends on the `executor_meta` string fields merged as #169a
(PR #171, `16150e5`). Explicitly does **not** include #165 (rework dangling
deps / retry classification) — see §11.

## 1. What the pilot saw, and why the exit code was not enough

Workstream `w-contracts` re-ran after a rework. spec-runner executed
`TASK-001` of 9 generated tasks and exited 0 after ~9 minutes. Maestro walked
`RUNNING -> MERGING -> PR_CREATED -> DONE`, merged a branch containing one
task into the base, and deleted the worktree. `maestro workstreams` displayed
`1/9 done` throughout — honestly. Dependent workstreams then started
decomposing against a base that carried 1/9 of the contract layer, and the
wave was killed by hand.

Two separate failures, and they must not be conflated:

- **The decision ignored data Maestro already had.** The whole completion
  criterion is one line — `orchestrator.py:2864`:
  `if return_code == 0: await self._handle_success(...)`. DONE means
  "spec-runner exited 0 and the merge applied". That is a true statement about
  the *process*, not about the *work*. #123 made the display honest; the
  decision never consulted it.
- **Cleanup destroyed the evidence.** `spec/.executor-maestro-logs/` and the
  executor state lived only inside the worktree, which
  `_merge_and_pr` removes at `orchestrator.py:3052`. Post-factum, the cause of
  the premature exit 0 was unknowable — spec-runner's #169 explanation
  (a whole class of false-green exits) arrived as an upstream hypothesis that
  our own artifacts could no longer confirm or refute.

## 2. Fixed decisions (owner, at acceptance 2026-08-10)

1. **Unknown `subtask_total` is fail-closed.** There must be an explicit
   manual approve/recovery path; a silent transition to DONE must not exist.
2. **The gate's source of truth is the counters:** `done == subtask_total`.
   `stop_reason` / `stop_detail` are diagnostic context and an input to retry
   classification (#165) — never a second independent DONE criterion.
3. **Post-mortem is captured before *any* destruction of the worktree**, not
   only on anomaly. Archiving is bounded by a retention/GC policy. **If the
   artifacts cannot be saved, the worktree is not destroyed.**
4. **The SSH path must be verified factually**, and the spec must require a
   single source of the final executor state for local and SSH alike. The gate
   must not be made load-bearing on a presumed `workspace_path/spec`.

## 3. Decision 4 first: what is actually true about SSH

Decision 4 turned out to be the load-bearing one — it moves the capture point
for every transport. Verified against the code, not assumed:

| Fact | Evidence |
|---|---|
| Mode-2 SSH collects the **whole worktree** back | `CollectPolicy(mode="whole_worktree", conflict_policy="fail", on_failure="collect")` — `orchestrator.py:263` |
| The executor **state DB is** collected back (not excluded, not forbidden) | `RSYNC_EXCLUDES_COLLECT` — `execution/ssh_launch.py:10`; `forbidden=[".git", ".maestro"]` — `execution/ssh_handle.py:299` |
| The executor **logs are NOT** collected back | spec-runner writes `spec/.executor-<prefix>logs/<task_id>-*.log` (`spec-runner/src/spec_runner/config.py:343`, `cli_info.py:363`); `RSYNC_EXCLUDES_COLLECT` contains `*.log` |
| The live mirror carries **only** the state DB | `ProgressMirrorPolicy(remote_globs=[".executor-maestro-state.db"])` — `orchestrator.py:266` |
| The remote workspace is destroyed **before** the gate would run | `finalize_handle` = reap → persist → collect → persist → **cleanup** (`execution/finalize.py:39`); `SshTaskHandle.cleanup()` does an ownership-checked remote `rm -rf` (`ssh_handle.py:312`). `_handle_completion` is called only *after* finalization returns (`orchestrator.py:2004`) |
| A failed collect never reaches the gate | collect raises → finalize returns without cleanup → `NEEDS_REVIEW`, "remote workspace preserved" (`orchestrator.py:1999`) |

Consequences:

- My earlier suspicion that `_final_progress_refresh` reads a presumed path on
  SSH was **wrong as stated**: after a successful collect the state DB *is*
  present at `workspace_path/spec`. The counters are reachable there.
- The real defect is the other half: **on SSH the executor logs never arrive
  locally at all, and the remote is `rm -rf`'d during finalization** — before
  any DONE-time hook could run. A post-mortem placed at the DONE gate would be
  empty of logs for every remote run, by construction. That is worse than the
  incident being described, because it would look like it worked.

**Therefore the single source and the single capture point are the same
moment: finalization's `on_collected` callback** — after collect has applied,
before anything is cleaned up (`finalize.py:53`). At that instant, for every
transport, the worktree exists locally, the state DB is current, and the
remote is still alive. Both halves of #164 hang off that one hook:

```
reap → persist terminal → collect → [ capture post-mortem ] → cleanup(remote)
                                            │
                                            └─→ archive dir (state snapshot + logs + manifest)
                                                     │
_handle_completion → _handle_success → [ completeness gate reads the ARCHIVE ] → …
```

The gate reads the archived snapshot, not the live worktree. That is what
makes local and SSH one path instead of two, and it is why capture cannot be
deferred to the gate.

## 4. The completeness gate

### 4.1 Placement

`_handle_success` (`orchestrator.py:2882`) already begins with
`_final_progress_refresh` (`:2895`), then runs `_gate_scope` (`:2899`) and
`_gate_ex_post` (`:2904`). The completeness gate becomes the **first** guard
in that row, before the scope gate:

```
_handle_success
  ├─ _final_progress_refresh          (existing, display)
  ├─ _gate_completeness   ← NEW       (always-on, fail-closed)
  ├─ _gate_scope                      (existing, always-on)
  ├─ _gate_ex_post                    (existing, opt-in)
  └─ VERIFYING | _merge_and_pr
```

Ordering rationale: an incomplete run's diff is not worth classifying. Both
existing gates answer "is this diff acceptable"; there is no point paying a
`steward risk-classify` call, or blaming a scope escape, on work that is not
finished. Completeness is a precondition of the diff being meaningful.

It is **always-on**, like `_gate_scope` and unlike the opt-in `gates:` block.
No config key and no env var turn it off; the audited manual approve is the
only way past it (§10.3).

### 4.2 Semantics

Inputs, all from the archived snapshot of §3:

- `done` — `ExecutorState.done`: tasks in `SUCCESS` (`models.py:1574`).
- `planned` — `workstreams.subtask_total` (#123, migration 19), the one-shot
  capture of spec-runner's `status --json` `total_tasks` taken after spec-gen.
- `state.total` — `len(state.tasks)`, which **under-counts mid-run**:
  spec-runner registers tasks lazily (documented at `models.py:1595`). It is
  not a denominator; it is used only as the lower bound already applied by
  `progress_label` (`max(self.total, total)`).

Verdict:

| Condition | Verdict |
|---|---|
| `planned is not None and done == planned` | **pass** → continue to `_gate_scope` |
| `planned is not None and done < planned` | **block** — `incomplete` |
| `planned is not None and done > planned` | **block** — `inconsistent` (see below) |
| `planned is None` | **block** — `unknown_total` (decision 1) |
| archive/state unreadable | **block** — `unreadable` (fail-closed, never softened) |

`done > planned` is not merely defensive: `subtask_total` is captured once
after spec-gen, and a rework rewrites `spec/maestro-tasks.md` (that rewrite is
the subject of #165). A stale denominator smaller than the work actually done
means the two numbers describe different revisions, so the gate must not
declare completeness from them. It blocks with a distinct reason rather than
passing on `>=`.

`stop_reason` / `stop_detail` (#169a) are read and recorded in the block
message and the archive manifest, and never consulted for the verdict
(decision 2). A conflicting pair — `done == planned` with
`stop_reason == "task_failed_stop"` — **passes the gate** and is logged at
WARNING with both facts. Per decision 2 the counters decide; after the pin
bump (#169b) such a run would additionally have arrived with a non-zero exit
code and never reached the gate at all.

### 4.3 No-op semantics (exact)

spec-runner ≥ 2.16 marks an attempt that succeeded with nothing to commit
(`attempts.no_op`, #97). Maestro surfaces it as
`ExecutorTaskEntry.attempts[-1].no_op` and
`ExecutorState.noop_done` (`models.py:1581`) — SUCCESS tasks whose **last**
attempt is an explicit no-op.

For this gate:

- **A no-op task counts as done.** `state.done` counts `SUCCESS` regardless of
  `no_op`, and that is correct: spec-runner deemed the task complete with
  nothing to write. The gate measures completeness, not productivity.
- `no_op is None` (older state files, pre-2.16) is **not** treated as a no-op;
  it is unknown provenance and irrelevant to the count. The version gate
  already pins ≥ 2.16.0 (`spec_runner.py:42`), so `None` here means a legacy
  on-disk file, not a legacy spec-runner.
- **All-no-op passes, and emits a structured diagnostic** (owner decision,
  §10.2). A 9/9 run where `noop_done == 9` produces an empty diff; the gate
  passes it and `_gate_scope` sees no changed paths. Judging whether the work
  was *substantively useful* belongs to verification — Stage B's domain
  verifier in Mode 2, the verifier gate in Mode 1 — not to a completeness
  check. What this gate owes the operator is visibility, not a verdict:
  - a structured event through the obs pipeline,
    `workstream.completeness.all_no_op`, with `workstream_id`, `execution_id`,
    `done`, `planned` and `head_sha` as fields (not interpolated prose, so it
    is queryable in the JSONL);
  - `all_no_op: true` in the archive manifest (§6.1), so the fact survives in
    the evidence and not only in a log stream;
  - **not** a notification-channel event — that track has a single-owner
    discipline per event and an advisory diagnostic does not earn one (§9).
- The block message carries the no-op count so an operator reading
  `completed 8 of 9 (3 no-op)` is not misled into thinking three tasks were
  skipped by the gate.

## 5. Where a blocked workstream lands, and how it gets out

### 5.1 State

Block → **`NEEDS_REVIEW`**, transitioned from `RUNNING` with
`expected_status=RUNNING` (the same CAS discipline every other guard uses),
carrying a durable marker in `error_message` and a row in the evidence side
table (§6.4). Message shape, stable enough to grep and to show in
`maestro workstreams`:

```
completeness: completed 1 of 9 (0 no-op) at <sha> — stop_reason=task_failed_stop
```

`NEEDS_REVIEW` is chosen over `FAILED` because nothing failed: the run
succeeded at what it did. It is the same terminal-for-now state the scope and
ex-post gates use, so operator tooling, notifications and recovery need no new
vocabulary.

### 5.2 Approve — deliver the partial work as-is

Reuses the existing single approval authority rather than inventing a second
one: `gate_approvals` is already "the single authority on *was this
(workstream, phase, sha) approved*" written in one transaction by
`maestro workstream-approve` (`gate_approvals.py`). This gate registers a
third `phase` value alongside the ex-ante/ex-post phases.

Resume is the **H-6 mechanism that already exists**: `READY` + approval marker
+ unchanged worktree HEAD resumes straight into the delivery tail with no
regeneration and no respawn. Concretely, approval sets
`resume_reason = RESUME_DELIVER` (new constant next to `RESUME_REVERIFY` /
`RESUME_REWORK` in `maestro/domain/resume.py:8`), and the READY dispatch —
which is already exhaustive and routes an unknown `resume_reason` to
`NEEDS_REVIEW` (`orchestrator.py:1533`) — gains one branch that jumps to
`_merge_and_pr`, exactly as the `RESUME_REVERIFY` branch (`:1550`) jumps to
`_run_verification`. **No spec-gen, no author respawn, no new money.**

A new commit changes the SHA and invalidates the approval, as with every other
gate approval.

### 5.3 Rework — throw the partial work away and redo it

`maestro workstream-rework <id> --reason ...` already exists (#124) and
already routes `NEEDS_REVIEW -> READY` into re-decomposition with a
fail-closed liveness proof and an audit row. Nothing new is needed. This is
the correct verb when the partial result is not worth delivering.

### 5.4 Continue the remaining tasks without re-decomposing — deliberately deferred

The verb an operator will actually want for a 1/9 block is "run the other
eight tasks". spec-runner is resumable by construction (its state DB plus
`spec/maestro-tasks.md`); the obstacle is entirely on our side — the plain
READY path is documented "Always regenerate" (`orchestrator.py:1606`).

Adding it here means introducing a second meaning of READY ("re-dispatch
against the existing tasks.md"), which is **precisely the resume-without-regen
acceptance criterion of #166**. Specifying it in this PR would smuggle #166's
architectural stage into #164. This spec therefore:

- ships **approve** (§5.2) and **rework** (§5.3) as #164's exit paths;
- names `RESUME_DELIVER` as the first member of a family that #166 extends
  with a continuation counterpart, so #166 does not have to re-litigate the
  dispatch shape;
- records the operator cost honestly: until #166, finishing a partially
  completed workstream means paying one re-decomposition (`rework`) or
  delivering 1/9 (`approve`).

**Boundary confirmed by the owner (2026-08-10):** #164 gets approve + rework
only; "catch up the remaining tasks" is #166's responsibility, and the two
meanings of READY are not introduced inside #164.

**One naming correction to that confirmation.** The owner's wording named
`RESUME_DELIVER` as the constant for "catch up the remaining tasks". In this
spec `RESUME_DELIVER` is #164's *approve* path — resume into the delivery tail
and ship the partial work as-is — which is what actually ships here. Letting
one constant mean both "deliver as-is" and "run the missing tasks" would make
the exhaustive READY dispatch ambiguous at the exact point it is designed to be
total. So:

| Constant | Meaning | Owner |
|---|---|---|
| `RESUME_DELIVER` | approved → resume into `_merge_and_pr`, deliver as-is, no regen | **#164 (this spec)** |
| `RESUME_CONTINUE` | re-dispatch spec-runner against the existing `tasks.md` | **#166** (name reserved, not defined here) |

The intent behind the confirmation is unaffected — continuation stays outside
#164 either way. Only the label moves, and #166 may rename its own member
freely.

## 6. Post-mortem archive

### 6.1 Location and layout

Outside the worktree, next to the existing evidence ledger (`<db_dir>/evidence/`),
so the same "author cannot reach it" property holds:

```
<db_dir>/postmortem/<workstream_id>/<utc-iso-compact>-<execution_id>/
    manifest.json
    executor-state.db          # consistent snapshot, not a file copy
    logs/<task_id>-*.log       # the .executor-<prefix>logs tree, verbatim
```

**The archive root is anchored to `db_dir`, never to the process cwd** (owner
decision, §10.4). `<db_dir>` is the directory of the active Maestro DB
(default `~/.maestro/`), so a `--db` run keeps its own archives beside its own
state. Two properties follow, and both are the reason for the rule: the
archives travel with the database they describe (copy the DB directory and the
evidence comes along), and recovery does not depend on where the operator
happened to stand when the incident run was launched — a cwd-relative root
would make the same DB resolve different archive sets from different
directories, which is exactly the "undiagnosable after the fact" failure #164
is about. Nothing in the archive path is derived from the project config's
location or from `repo_path`.

`manifest.json` (`maestro.postmortem-manifest/v1`) carries the run identity
and the numbers the gate used, so the archive is self-describing without the
DB: `workstream_id`, `execution_id`, `attempt`, `backend_id`, `transport`,
`exit_code`, `done`, `noop_done`, `planned` (`subtask_total`), `state_total`,
`all_no_op` (§4.3), `last_run_stop_reason`, `last_run_stop_detail`, `branch`,
`head_sha`, `captured_at`, `bytes_written`, and `truncated` (§6.3).

`execution_id` in the directory name keys the archive to one execution
attempt, which is what makes repeated attempts distinguishable and the gate's
read unambiguous.

### 6.2 Consistency of the state snapshot

The executor state DB is live SQLite in WAL mode; a plain `copy` of `.db`
without `-wal` is not a snapshot. Reuse the machinery that already solved
this: `snapshot_locally()` in `execution/ssh_mirror.py:32` opens the source
read-only and uses `sqlite3.Connection.backup()`. The remote case is already
solved too — `SNAPSHOT_SCRIPT` + `mirror_once` produce a consistent snapshot
over SSH.

### 6.3 Bounds and retention

Config lives in a **top-level `postmortem:` block in the project config**
(owner decision, §10.4). It holds retention/GC settings **only** — there is
deliberately **no `enabled: false` key**: capture-before-destruction is the
invariant this whole spec exists to establish, and a config switch that turns
it off recreates the incident with one line of YAML. An absent `postmortem:`
block therefore means *defaults*, not *off* — the same reading `gates:` does
**not** have, and the difference is intentional: `gates:` is an optional
policy, this is an invariant.

Unbounded log copying would grow `<db_dir>/postmortem/` without limit, so:

- **Per-archive byte cap** (`postmortem.max_archive_bytes`, default 64 MiB):
  logs are copied newest-first until the cap; the manifest records
  `truncated: true` with the count of files omitted. A truncated archive is
  still a *complete archive* for §6.5 purposes — truncation is a recorded
  policy outcome, not a failure.
- **Retention** (`postmortem.keep_per_workstream`, default 5): after a
  successful archive, older archives for that workstream beyond N are pruned,
  oldest first. Pruning happens after the new archive is committed, never
  before — a failed prune must never cost the fresh evidence.
- **Explicit GC surface**: `maestro postmortem --gc` for operator-driven
  cleanup, following the precedent of `maestro review-pr --gc` (#149), which
  only collects after the owning entity is confirmed finished.

### 6.4 Persistence

A new table (`postmortem_archives`), migration **23** — next free number at
the time of writing; re-check before implementing, since a parallel session
may claim it:

```
postmortem_archives(
  workstream_id TEXT NOT NULL,
  execution_id  TEXT NOT NULL,
  path          TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  bytes_written INTEGER NOT NULL,
  truncated     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (workstream_id, execution_id)
)
```

The row is written **after** the archive directory is committed (§6.5), so a
row's existence implies a complete archive on disk. `maestro workstreams`
gains no new column; the path surfaces in the block message and in
`maestro workstream-*` output.

### 6.5 Atomicity, and what happens when archiving fails

**Atomicity.** Everything is written into a sibling `…​.partial/` directory,
then committed with a single `os.replace()` of the directory onto its final
name — atomic within a filesystem, and `<db_dir>` is one filesystem by
construction. A crash therefore leaves either a `.partial/` (ignorable
garbage, swept by `--gc`) or a complete archive. There is no state in which a
half-written archive is indistinguishable from a finished one.

**Failure (decision 3): nothing is destroyed.** Two destruction points exist
and both must honor it:

- **Remote (`rm -rf`) inside `handle.cleanup()`.** Capture runs in
  `on_collected`, which today is awaited un-guarded by `finalize_handle`
  (`finalize.py:53`), so a raising callback propagates out of finalization.
  This spec makes that contract explicit: **an `on_collected` failure is
  treated exactly like a collect failure — `cleanup()` is skipped and the
  remote workspace is preserved**, and `FinalizationResult` grows an
  `archive_error` field so the caller can distinguish it from
  `collect_error`. The workstream routes to `NEEDS_REVIEW`
  ("post-mortem capture failed; workspace preserved"). It does **not** proceed
  to the gate, because the gate's own input is the archive.
- **Local worktree removal at `_merge_and_pr:3052`.** Guarded by the
  `postmortem_archives` row: the worktree is removed only if a committed
  archive exists for this execution. Otherwise cleanup is skipped, a WARNING
  is logged, and the path is left for the operator. The workstream stays
  DONE — the merge did apply, and rewriting a correct terminal state because a
  diagnostic copy failed would be a worse lie than the leftover directory.

This deliberately makes a disk-full or permission fault *stop delivery* on the
remote path. That is the same trade the project already takes with every
fail-closed gate, and it is what decision 3 asks for in the literal case where
the evidence cannot be preserved.

## 7. Migration behaviour: old databases and legacy runs

- **No data migration.** `subtask_total` is already nullable (migration 19).
  Migration 23 is additive (one new table); pre-23 databases get it via
  `CREATE TABLE IF NOT EXISTS`, following `_migrate_gate_approvals`
  (migration 6) and `_migrate_verification_attempts_table` (14).
- **Rows created before migration 19 have `subtask_total IS NULL`** and, by
  decision 1, block as `unknown_total` on completion. This is a real
  behaviour change on upgrade: a workstream that would silently have gone DONE
  now waits for an operator. It is the intended change, and it is the exact
  case the incident was.
- **In-flight workstreams at upgrade time** are the sharp edge: a workstream
  that was already `RUNNING` when Maestro was upgraded reaches the new gate
  with whatever `subtask_total` it captured — `NULL` if it started before 19.
  Such a run blocks and needs one `workstream-approve`. This must be
  release-noted in `CHANGELOG.md`, with the same prominence as the
  `validation_backend` default flip (`local -> same`) got.
- **Legacy JSON executor state** (pre-2.0 on-disk format) is read by the same
  `read_executor_state`, so the gate works there unchanged; `no_op` is `None`
  throughout, which §4.3 already defines as "not a no-op".
- **`maestro validate` is unaffected** — it is a config preflight and has no
  view of run completeness.

## 8. Test matrix

The owner's requirement is a regression test reproducing the pilot's scenario,
not only unit invariants. Row 4 is that test.

| # | Scenario | Setup | Expected |
|---|---|---|---|
| 1 | **local, complete** | bare local backend, `done == planned == 3` | gate passes; `_gate_scope` reached; DONE; archive committed; worktree removed |
| 2 | **SSH, complete** | ssh backend, whole-worktree collect, `done == planned` | archive contains the state snapshot **and** `logs/` fetched from the remote before `rm -rf`; gate passes from the archive, not from the worktree |
| 3 | **unknown total** | `subtask_total IS NULL` (pre-19 row) | `NEEDS_REVIEW`, reason `unknown_total`; never DONE; branch not merged |
| 4 | **1/9 — the pilot** | 9 planned, 1 SUCCESS, spec-runner exit 0 | `NEEDS_REVIEW` "completed 1 of 9"; **base branch unchanged**; worktree preserved; archive holds all 9 task rows + logs; `stop_reason` recorded in the manifest and the message |
| 5 | **no-op completeness** | 5 planned, 5 SUCCESS of which 2 `no_op` | gate passes; message/label reads `5/5 done (2 no-op)`; empty diff handled by the existing scope gate, not by this one |
| 6 | **conflicting stop reason** | `done == planned` **and** `stop_reason="task_failed_stop"` | gate **passes** (decision 2); WARNING logged with both facts; manifest records the reason |
| 7 | **stale denominator** | `done=10`, `planned=9` (rework rewrote tasks.md) | `NEEDS_REVIEW`, reason `inconsistent`; not passed on `>=` |
| 8 | **archive failure, remote** | capture raises (unwritable `<db_dir>`) on an ssh run | `cleanup()` **not** called; remote workspace preserved; `NEEDS_REVIEW`; gate not evaluated; `archive_error` distinct from `collect_error` |
| 9 | **archive failure, local** | capture fails after a successful merge | worktree **not** removed; WARNING; workstream stays DONE |
| 10 | **atomic commit** | kill between writing and `os.replace` | only `.partial/` on disk; no `postmortem_archives` row; next run does not mistake it for evidence |
| 11 | **truncation** | logs exceed `max_archive_bytes` | archive committed, `truncated: true` + omitted count in the manifest; treated as complete for §6.5 |
| 12 | **retention** | 6 archives with `keep_per_workstream=5` | oldest pruned after the new one commits; a prune failure leaves the fresh archive intact |
| 13 | **approve resumes without spec-gen** | block, then `workstream-approve` | `resume_reason=RESUME_DELIVER`; READY dispatch jumps to `_merge_and_pr`; decomposer/spec-gen **never invoked** (assert on the spawn call, not on timing) |
| 14 | **approval invalidated by a new commit** | approve, then commit into the worktree | SHA mismatch → still `NEEDS_REVIEW`, approval not honored |
| 15 | **rework path** | block, then `workstream-rework` | `NEEDS_REVIEW -> READY` into re-decomposition, existing #124 behaviour unchanged |
| 16 | **collect failure precedes the gate** | ssh collect conflict | existing `NEEDS_REVIEW` "remote workspace preserved"; gate never runs; no archive expected |
| 17 | **totality** | — | every gate verdict has a message + a distinct reason code; unknown verdict is unrepresentable |
| 18 | **all-no-op diagnostic** | 9 planned, 9 SUCCESS, all `no_op` | gate **passes**; `workstream.completeness.all_no_op` emitted with structured fields (asserted on the JSONL record, not on a formatted string); `all_no_op: true` in the manifest; **no** notification-channel event |
| 19 | **archive root ignores cwd** | run the gate from two different working directories with the same `--db` | identical archive path both times; nothing under the project dir or `repo_path` |
| 20 | **no off switch** | project config carrying `postmortem: {enabled: false}` | config rejected as an unknown key (the invariant has no opt-out); an absent `postmortem:` block yields defaults, not "off" |

## 9. Non-goals

- **No retry classification, no dangling-deps handling** — that is #165, and
  mixing it here was explicitly excluded by the owner. This spec only makes
  `stop_reason` available to it as recorded context.
- **No resume-without-regen for involuntary interruption** — #166 (§5.4).
- **No productivity check** ("the diff is empty though tasks were done"). An
  all-no-op run is reported, not judged (§4.3); assessing substantive
  usefulness is verification's job, per §10.2.
- **No change to what DONE means once the gate passes.** The merge-gated DONE,
  the H-6 approval marker lifecycle, and Stage B verification are untouched.
- **No new notification event.** The existing `NEEDS_REVIEW` notification
  carries the block; a dedicated event would need the notify-track's
  single-owner discipline and buys nothing here.

## 10. Resolved (owner decisions, 2026-08-10)

Revision 1 raised four questions; all four are answered. Nothing here is open.

### 10.1 §5.4 boundary — confirmed

#164 ships **approve + rework only**. "Catch up the remaining tasks" is #166's
responsibility, and the second meaning of READY is not introduced inside #164.
One naming correction attaches to this answer — see the table in §5.4:
`RESUME_DELIVER` is #164's approve path (deliver as-is), and #166's
continuation gets its own constant, because a single constant meaning both
would make the exhaustive READY dispatch ambiguous exactly where it is designed
to be total.

### 10.2 All-no-op — do not block

For a completeness gate, a no-op **is** completion. A fully-no-op result is
reported through a structured event plus a manifest flag (§4.3), never blocked.
Judging substantive usefulness belongs to **verification** — Stage B's domain
verifier in Mode 2, the verifier gate in Mode 1 — not to this gate. That is the
sharper form of revision 1's recommendation: not "defer the productivity
check", but "it is already owned elsewhere".

### 10.3 Kill-switch — none

An invariant must not be globally disableable. The **audited manual approve**
(§5.2) is the emergency exit, and it is the right shape for one: per-workstream,
per-SHA, attributable, recorded in `gate_approvals`. This is a deliberate
divergence from `MAESTRO_APPROVER_DISABLED` / `MAESTRO_SPEC_RUNNER_ALLOW_UNVERIFIED`
— those switch off *optional policies*; this gate encodes an invariant, and an
env var would recreate the incident one keystroke away with no audit trail.
Test row 20 pins the absence of the switch so nobody reintroduces it as a
convenience.

### 10.4 `postmortem:` config — top-level, retention only, anchored to `db_dir`

A top-level `postmortem:` block in the project config carrying **retention/GC
settings only**. No `enabled: false` key (§6.3): an absent block means
defaults, not off. The archive root is anchored to `db_dir` and never to the
process cwd (§6.1), so archives travel with the database they describe and
recovery does not depend on where the incident run was launched from.

## 11. Dependency and sequencing

- **Depends on #169a** (merged, `16150e5`): `last_run_stop_reason` /
  `last_run_stop_detail` reach `ExecutorState`. Without it §4.2's diagnostic
  context and the manifest fields would require log parsing. The gate's
  *verdict* does not depend on it (decision 2), so a revert of #169a would
  degrade diagnosis, not correctness.
- **Independent of #169b** (the version-pin bump, trigger-gated). After the
  bump, most false-green runs never reach the gate because the exit code is
  non-zero; the gate remains the defense for the rest and for unknown totals.
- **Does not touch #165.** Shares only the `stop_reason` field, as recorded
  context.
- **Hands #166 a primitive**, not an implementation (§5.4).
