# `maestro review-pr` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Implement the approved spec
`docs/superpowers/specs/2026-08-06-post-pr-review-command-design.md`
(revision 3, approved 2026-08-06) — a wrapper command driving
`spec-runner review-pr` for Maestro-created PRs.

**Architecture:** New `maestro/review_pr.py` (pure helpers: repo key,
PR-ref parsing, precondition classification, report validation) +
`maestro/review_workspace.py` (materialization, flock, push recovery) +
migration 21 (`post_pr_review_runs`) + CLI command. The orchestrator is
NOT touched.

**Tech Stack:** Python 3.12, pydantic v2, aiosqlite, `gh` CLI for the
GitHub API (already a dependency of PRManager), `fcntl.flock`, Typer.

## Global Constraints (verbatim from spec + upstream recon)

- Minimum spec-runner: **2.21.0** (json purity, #116/#117; payload gains
  `exit_code`). Enforced by a preflight version gate before any
  invocation — reuse `parse_spec_runner_version` from
  `maestro/spec_runner.py`; bump nothing else (the existing Mode-2 pin
  stays 2.16.0; this is a *command-scoped* higher floor).
- Upstream JSON payload keys (2.21.0): `repo`, `pr_number`, `head_sha`,
  `new_comments`, `comments`, `counts`, `needs_human`, `exit_code`; on
  exit 1: `{repo, pr_number, error, exit_code}` (repo/pr_number may be
  null).
- Upstream exits: `0` complete, `1` fail-closed, `2` NEEDS_HUMAN.
  Maestro CLI exits: 0/1/2 pass-through + **3 = already running**.
- Paths: `~/.maestro/review-workspaces/<key>/<pr>/`,
  `~/.maestro/review-state/<key>/<pr>/{executor-state.db,lock}` where
  `<key>` = sanitized `owner-repo` + `-` + first 8 hex of sha256 of the
  canonical `owner/repo`.
- Invocation: `spec-runner review-pr <canonical-pr-url> --json` with cwd
  = review workspace; forced config keys: absolute `state_file`,
  `review_pr.post_pr: off`, project root = workspace.
- Audit outcomes: `complete | needs_human | infra_error`; `reason` is a
  detail column; finalization is CAS on `finished_at IS NULL`.

---

### Task 1: Pure helpers (`maestro/review_pr.py`)

**Files:** Create `maestro/review_pr.py`; Test `tests/test_review_pr.py`.

**Produces:** `repo_key(owner_repo) -> str`; `parse_pr_url(url) ->
PrRef(owner, repo, number, canonical_url)`; `ReviewReport` model +
`validate_report(raw, expected_exit) -> ReviewReport | str`;
`classify_precondition(local_head, remote_head, ancestor: bool, dirty:
bool) -> Literal["ready","continuation","dirty","diverged"]`;
`outcome_for_exit(code) -> str`.

- [ ] Failing tests: key collisions (`a-b/c` vs `a/b-c` differ), URL
      parsing (valid/invalid/trailing paths), report validation
      (exit-1 shape with nulls, exit-0/2 shape, garbage, exit_code
      mismatch with the process exit), precondition matrix, exit map.
- [ ] Implement; run; commit.

### Task 2: Migration 21 + DB APIs

**Files:** Modify `maestro/database.py`; tests in `tests/test_review_pr.py`
+ journal tripwires in `tests/test_database.py` (…, 21; count 21).

**Produces:** `insert_review_run(...) -> None` (sentinel),
`finalize_review_run(review_run_id, *, exit_code, outcome, reason,
report_json, output_head_sha)` — CAS on `finished_at IS NULL`,
`list_unfinished_review_runs()`, `list_review_runs(workstream_id)`.

- [ ] Failing tests: fresh+upgrade, sentinel then CAS finalize, second
      finalize is a no-op (returns False), unfinished listing, per-ws
      history ordering.
- [ ] Implement; run; commit.

### Task 3: Review workspace + lock + push recovery (`maestro/review_workspace.py`)

**Files:** Create `maestro/review_workspace.py`; tests in
`tests/test_review_workspace.py` (real git repos + a fake `gh`).

**Produces:** `ReviewPaths` (workspace/state/lock dirs),
`PrLock` (flock context manager, raises `AlreadyRunning`),
`fetch_pr_meta(pr_ref) -> PrMeta` (via `gh api`), `materialize(...) ->
Path`, `recover_push(...)`, `cleanup(exit_code, ...)`.

- [ ] Failing tests: lock held → second acquire raises; lock released on
      release/process exit; materialize creates then restores; each
      precondition refusal (draft, closed, dirty, diverged/force-push,
      wrong head repo); continuation → plain push → metadata refresh →
      exact HEAD; push rejected → no invocation; retention matrix.
- [ ] Implement; run; commit.

### Task 4: CLI command + version gate + notifications

**Files:** Modify `maestro/cli.py`, `maestro/notifications/base.py`
(3 events), `maestro/transitions.py` (none — events fire directly from
the command, not from a status transition); tests in
`tests/test_review_pr_cli.py`.

- [ ] Failing tests: version gate blocks < 2.21.0 (no invocation, no
      run row); happy path exit 0 → complete + notification + cleanup;
      exit 2 → needs_human, workspace kept, CLI exit 2; exit 1 →
      infra_error, CLI exit 1; locked → exit 3, no row; `--all`
      dedup + aggregation; `--gc` only after closed/merged.
- [ ] Implement; run; commit.

### Task 5: Docs

- [ ] CHANGELOG, README section, CLAUDE.md commands block; format,
      lint, pyrefly, foreground pytest of all touched files.
