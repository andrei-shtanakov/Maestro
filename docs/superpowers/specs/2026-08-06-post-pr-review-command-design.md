# `maestro review-pr` — post-PR review wrapper command — design

**Status:** proposed (revision 2)
**Date:** 2026-08-06
**Track:** notify/post-PR step 5 (owner order 2026-08-06); TODO
`@id:post-pr-command`; upstream counterpart spec-runner#102 SHIPPED
(M1 v2.18 read-only loop, M2 fix+reply, M3 post-PR stage + external
caller contract — `spec-runner review-pr`, exit `0/1/2`, idempotent
re-invocation, `--json` report).
**Owner decisions incorporated (2026-08-06):** variant (b) — a separate
wrapper command, orchestrator untouched; no new `WorkstreamStatus`;
fresh workspace materialization is admissible ONLY over durable
external state + retention of unfinished work; durable per-PR lock;
append-only audit table; design-only PR before implementation.
**Revision 2 (owner review):** Maestro-owned push-recovery for the
failed-push continuation (spec-runner's mutating phase requires strict
`local_head == remote head_sha`); machine-report dependency made
explicit — blocked on spec-runner#116 (`--json` stdout purity) with a
version pin; §7 corrected — PR identity is the positional argument plus
Maestro-side pre-invocation verification, not a config invariant; audit
semantics renamed to immutable-after-finalization with CAS-guarded
sentinel finalization; v1 declared advisory/post-delivery cleanup (not
a DAG-correctness guarantee); collision-free repo key; `--all` dedup by
(repo, pr_number); locked PRs get no run row.

## 1. Problem and boundary

Maestro creates its own PRs; the review-bot loop (verify each comment
against code, TDD-fix the valid ones, reply in threads, bounded rounds)
now lives entirely in `spec-runner review-pr`. Maestro needs a way to
*drive* that loop for Maestro-created PRs without owning any of it —
and without the foreground-orchestrator lifecycle redesign that a
synchronous post-PR stage would require.

The critical coupling this design exists to protect: spec-runner's
resumability (never process a comment twice, never reply twice,
continue bounded rounds, survive crash/exit-2) lives in its
`state_file` — by default `spec/.executor-state.db` **inside the
checkout** (spec-runner `config.py:204`). A "fresh temporary worktree,
always deleted after the call" would destroy that state and, after a
failed push, the fix commits themselves. Therefore: durable state
outside the checkout, and retention rules for unfinished work.

**Scope honesty (revision 2): advisory post-delivery cleanup.** The
command runs after `DONE` — by then Maestro has already merged the
original feature SHA into the local base branch. Review fixes move the
*PR head*, not the already-integrated local base, and may therefore
never reach dependent workstreams' baselines. v1 is explicitly an
**advisory / post-delivery cleanup** mechanism, not a DAG-correctness
guarantee; if review comments must ever block downstream work, that is
the future synchronous pre-merge stage (a separate lifecycle design),
not this command.

## 2. Command surface

```
maestro review-pr <project.yaml> <workstream-id>   # one PR
maestro review-pr <project.yaml> --all             # every eligible PR
maestro review-pr <project.yaml> --gc              # sweep closed/merged PRs
```

A wrapper/orchestration command (same config-argument pattern as
`maestro workspaces`): the GitHub review loop itself stays exclusively
in spec-runner. Cron- and operator-invocable; the orchestrator process
is not involved and no `WorkstreamStatus` changes — the workstream is
already `DONE`, the PR review is a post-factum operation on the remote
PR, and flipping DONE back would be a false product-lifecycle rollback.

## 3. Review workspace and durable state

Keyed by **(repository, PR number)** — not by workstream (a workstream's
normal worktree lifecycle ends at DONE; review has its own):

```
~/.maestro/review-workspaces/<repo>/<pr-number>/   # the checkout
~/.maestro/review-state/<repo>/<pr-number>/        # durable, survives checkout removal
    executor-state.db                              # spec-runner state_file (absolute)
    lock                                           # flock target (§6)
```

`<repo>` is a collision-free key: the sanitized `owner-repo` slug
plus a short hash of the canonical `owner/repo` string (sanitization
alone could collide, e.g. `a-b/c` vs `a/b-c`). Maestro passes the
**absolute** `state_file` to spec-runner via the generated config
(`ExecutorConfig` accepts an absolute path) — spec-runner keeps its own
idempotency state machine; Maestro never copies or interprets those
tables.

### 3.1 Materialization algorithm (fail-closed at every step)

1. From the workstream's `pr_url`, query the GitHub API for: PR state
   and draft flag, head repository, head ref, head SHA, and push
   permission.
2. Create **or restore** the dedicated worktree on the PR head branch
   (a `git worktree` of the project's `repo_path`; `git fetch` first).
3. Fail-closed preconditions, all mandatory:
   - PR is open and not draft;
   - the checkout is clean (unless it holds a recognized local
     continuation, below);
   - local HEAD equals the expected PR head SHA, **or** is a saved
     local continuation: the remote head is an ancestor of local HEAD
     (fix commits made locally that failed to push). Any other
     divergence (e.g. a remote force-push) → refuse with instructions;
     **no force-reset of a local continuation without an explicit
     operator decision** — a `--discard-local` flag discards it, is
     audited, and is never the default;
   - the branch belongs to the expected head repository;
   - push permission present (for the fix path).
4. **Push-recovery for a local continuation (revision 2).** spec-runner
   refuses to mutate unless strictly `local_head == remote head_sha` —
   so a saved continuation would exit 1 forever if handed over as-is.
   Maestro owns the reconciliation: (a) confirm the remote head still
   equals the saved expected remote SHA (the ancestor); (b) push the
   local continuation to the PR branch itself — a **plain push against
   that verified expected SHA, never force**; (c) re-read the PR
   metadata; (d) only once `remote head_sha == local HEAD`, proceed.
   This composes cleanly with spec-runner's own state: the fixes are
   already recorded there and the replies are not yet published, so the
   next `review-pr` call completes the reply phase idempotently without
   re-processing comments. A failed recovery push → `infra_error`,
   workspace kept (§4).
5. Invoke `spec-runner review-pr <canonical-pr-url> --json` in the
   workspace with the forced config (§7).

## 4. Cleanup and retention policy

Not "keep nothing" — **"don't keep a finished checkout; always keep
durable state and unfinished work"**:

| Exit | Worktree | Durable state |
|------|----------|---------------|
| `0` complete | removable (removed by default) | **kept** — a future bot round / new head resumes from it |
| `2` needs_human | **kept** — a human may need to inspect/fix in place | kept |
| `1` infra_error | kept if local commits or dirty evidence exist; a clean workspace may be recreated | kept |
| PR closed/merged | removed by `--gc` only, after verifying remote state | removed by `--gc` |

A workspace kept dirty after `infra_error` intentionally FAILS the
clean-checkout precondition on the next invocation (§3.1) — the
operator must either commit the changes (turning them into a
recognized commit-based continuation) or discard them with the audited
`--discard-local`. The retention exists to preserve evidence, not to
bypass the precondition.

`--gc` is the only path that deletes durable state, and only after
confirming via the GitHub API that the PR is closed or merged. No TTL
auto-cleanup in v1 (a GC sweep is explicit and auditable).

## 5. Audit: append-only `post_pr_review_runs` (migration 21)

One field on `workstreams` is not enough — the history of runs must
survive. New run-record table — **immutable after finalization**
(revision 2 wording: the sentinel row IS updated once, by the CAS
finalization below; a finalized row is never rewritten):

```sql
CREATE TABLE post_pr_review_runs (
    review_run_id       TEXT PRIMARY KEY,          -- ULID
    workstream_id       TEXT NOT NULL,
    pr_url              TEXT NOT NULL,
    repo                TEXT NOT NULL,
    pr_number           INTEGER NOT NULL,
    input_head_sha      TEXT,
    output_head_sha     TEXT,
    started_at          TIMESTAMP NOT NULL,
    finished_at         TIMESTAMP,
    exit_code           INTEGER,
    outcome             TEXT CHECK (outcome IN
                          ('complete','needs_human','infra_error')),
    reason              TEXT,                      -- e.g. 'interrupted'; detail under outcome
    report_json         TEXT,                      -- spec-runner --json, verbatim-validated
    workspace_path      TEXT,
    spec_runner_version TEXT
);
```

- A row is inserted (`started_at`, no `finished_at`) **before** the
  spec-runner invocation — the crash sentinel. Every finalization
  (normal or orphan-recovery) is a **CAS UPDATE guarded on
  `finished_at IS NULL`** — two concurrent recovery passes can never
  rewrite an outcome. Orphaned rows finalize with
  `outcome='infra_error', reason='interrupted'` — interruption is a
  *reason* under the `infra_error` outcome, never a distinct outcome
  value (spec-runner's own state remains the authority on review
  progress — an interrupted Maestro row never blocks re-invocation,
  the per-PR lock does that while alive).
- A new bot round (new head SHA) is simply a new audit row. `DONE`
  stays untouched.
- A current-state read-model (latest row per PR) may be surfaced in
  `maestro workstreams` later; the history is the contract.

### 5.1 Exit mapping

| spec-runner exit | outcome | Maestro CLI behavior |
|------------------|---------|----------------------|
| `0` | `complete` | notify (`post_pr_review_complete`), cleanup allowed, exit 0 |
| `2` | `needs_human` | the wrapper itself succeeded; notify (`post_pr_review_needs_human`), keep workspace, exit 2 |
| `1` | `infra_error` | notify (`post_pr_review_error`), keep workspace/state per §4, exit 1 |

Notifications go through the existing manager (desktop + webhook): three
new `NotificationEvent`s named above; the webhook envelope carries the
PR URL (the `url` field precedent from PR_CREATED).

## 6. Concurrency: durable per-PR lock

Identity: **(repo, pr_number)**. Mechanism: `flock` on
`~/.maestro/review-state/<repo>/<pr-number>/lock` held for the whole
fetch → fix → push → reply cycle — OS-released on process death (no
stale-lock protocol needed), advisory, same-host (cross-host locking is
out of scope in v1 and noted as such). A second caller (cron vs
operator) gets a clear **"already running"** message and a distinct
exit code `3` — it never spawns a second spec-runner.

### 6.1 `--all` semantics

- Candidates: workstreams with non-empty `pr_url`; open PRs only by
  default; **deduplicated by (repo, pr_number)** — distinct workstream
  rows can in principle reference one PR, which must yield one run.
- **Sequential** processing in v1 (bounded concurrency later if ever
  needed).
- One PR's failure never hides the others' results: every candidate is
  attempted, each gets its own audit row.
- Aggregated report (table; `--json` for machines) and aggregated exit:
  `1` if any infra_error, else `2` if any needs_human, else `0`.
  A locked PR is reported in the JSON as `skipped/already_running`,
  does not affect the aggregate, and — like every §6 lock refusal —
  creates **no** `post_pr_review_runs` row (nothing ran).

## 7. Config: overlay for limits, harness-owned invariants

User-tunable review limits (`max_rounds`, `max_comments`,
`max_changed_lines`, `max_cost_usd`, `max_wall_minutes`,
`allowed_bots`) flow through the **existing
`SpecRunnerConfig.extra_executor_config`** deep-merge overlay — no new
Maestro surface. But Maestro force-sets, after the
overlay merge (harness-owned, user values ignored with a warning):

- the review-workspace `project_root`;
- the **absolute** `state_file` (§3) — a user override here would
  silently destroy resumability;
- Mode-2 review invariants (`post_pr: off` — the stage inside
  spec-runner's own flow must not double-fire when Maestro drives the
  loop externally);
- the expected PR identity.

## 8. Non-goals

- No new `WorkstreamStatus` (incl. the `PR_REVIEWED` suggested by the
  spec-runner contract doc — state lives in the audit/read-model).
- No approving or merging (spec-runner's own invariant; ADR-ECO-004 —
  master stays human).
- No synchronous post-PR stage inside `maestro orchestrate` (possible
  future opt-in; this contract would not change).
- No cross-host locking; no TTL auto-GC.

## 9. Testing plan (implementation PR)

- Workspace materialization: create vs restore; each fail-closed
  precondition (draft, closed, dirty-without-continuation, diverged
  head / remote force-push refused, wrong head repo); local
  continuation accepted (remote head ancestor of local HEAD);
  `--discard-local` audited and non-default.
- Durable state: absolute `state_file` passed; state survives worktree
  removal; forced config keys win over user overlay (with warning);
  `post_pr` forced off.
- Retention matrix (§4) per exit code, incl. exit-1-with-local-commits
  kept vs clean recreated; `--gc` removes only after remote
  closed/merged confirmation and never touches open PRs.
- Audit: sentinel row before invocation; orphaned row finalized
  `infra_error/interrupted` on next call; report_json captured; new
  head SHA → new row; migration 21 fresh+upgrade, journal tripwires.
- Lock: second concurrent caller exits 3 without spawning; lock
  released on process death (kill the holder, re-acquire).
- Exit mapping + notifications: 0/2/1 → events and CLI codes; `--all`
  aggregation (mixed outcomes, locked-skip, one failure not hiding
  others).
- Zero-change: no CLI invocation → orchestrator behavior byte-identical
  (no orchestrator edits at all in this feature).
