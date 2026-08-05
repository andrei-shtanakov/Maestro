# Spec-runner version gate (issue #122: harness-owned paths) — design

Date: 2026-08-05
Status: approved (variant A of the comparative design; decision recorded below)
Issue: #122 `scope-gate-harness-owned-paths` (battle-testing pilot, finding M-03)
Counterpart: spec-runner#96 (CLOSED — fixed in spec-runner v2.16.0, commit `762a5b7`)

## Problem

spec-runner 2.15.x writes `spec/.gitignore` to protect its runtime state
(spec-runner#62) and commits it with the first subtask's auto-commit. Under
Maestro orchestration this puts a file no agent chose to create into the
workstream diff, and the ex-post scope gate correctly flags it as a scope
escape: on the kapelle S2 run both parallel workstreams went to NEEDS_REVIEW
with all subtasks green, twice, solely because of this file.

Both tools are right by their own contracts; the missing piece was a shared
notion of **harness-owned paths**.

## Considered options

| Option | Verdict |
| --- | --- |
| **A. Rely on the upstream fix; enforce spec-runner >= 2.16.0 in preflight** | **Chosen.** Zero gate changes, zero new holes, the shim-free path. |
| B. Add `spec/.gitignore` to `_ORCHESTRATOR_MANAGED` (content-blind exclusion) | Rejected: under >= 2.16 the harness no longer commits the file, so any diff touching it is author-chosen — excluding it would make such a commit invisible to the gate with *any* content. Degrades the gate exactly where it should watch. |
| C. Content-aware exclusion (drop only when content == what `ensure_runtime_gitignore` writes) | Rejected: couples Maestro to the byte format of a foreign constant (`RUNTIME_GITIGNORE_ENTRIES`, would need a vendored pinned copy), breaks on any upstream addition, heavy machinery for a window closed by an upgrade. |
| D. Versioned compatibility rule (B or C active only for detected 2.15.x) | Rejected: needs both version detection *and* shim code; the same version detection in A simply says "upgrade" and the problem disappears entirely. |
| E. Maestro pre-creates/commits the gitignore during worktree prep | Rejected (dead end): 2.15 commits the file anyway when it is not in HEAD, and a Maestro-made commit on the feature branch still shows in the diff from merge-base — E does not work without B. |

Decision (2026-08-05, owner): **A, severity `error`** — closes #122 without
weakening the scope gate and without compatibility code for an
already-fixed version.

## The harness-owned paths convention (documented contract)

Two halves, one per tool:

1. **Harness side (spec-runner >= 2.16.0):** runtime files live under paths
   covered by the harness-written `spec/.gitignore`; the gitignore itself is
   written but *excluded from auto-commits* — unless the user tracks it in
   HEAD, in which case it is deliberately the user's file.
2. **Orchestrator side (Maestro):** the changed-paths source excludes only
   Maestro's *own* managed prefixes (`_ORCHESTRATOR_MANAGED`:
   `spec/maestro-*`, `spec/.maestro-*`, `spec/.executor-*`). Nothing else.

Corollaries:

- `spec/.gitignore` is **not** added to `_ORCHESTRATOR_MANAGED`. With a
  compatible spec-runner, its presence in a diff means the author (or the
  user) put it there — the scope gate must see it.
- A user-tracked `spec/.gitignore` is a user file, checked by the normal
  scope rules like any other path.

## Design

### Minimum version and where it is enforced

- Minimum compatible version: **spec-runner >= 2.16.0**. The doc pin
  `SPEC_RUNNER_REQUIRED_VERSION` in `maestro/spec_runner.py` is bumped to
  `"2.16.0"` and becomes the single source for the gate (the "runtime
  version gate" hardening its comment used to defer).
- New preflight check `_check_spec_runner_version()` in
  `maestro/preflight.py`, wired into `validate_project` next to the existing
  `--spec-prefix` capability probe (the `check_fs` block). Both
  `maestro validate` and the fail-fast preflight inside
  `maestro orchestrate` run it **before any worktree is created or any
  state is mutated**.
- The capability probe stays: the version states the known contract, the
  probe checks the actually installed executable. Both must pass.

### Fail-closed matrix

`spec-runner --version` is invoked with a timeout; the output is parsed
strictly. All of the following produce a blocking `error` (code
`spec-runner-version-unsupported`):

- parsed version < 2.16.0;
- output that does not match the expected format (unknown/dev versions are
  **not** guessed);
- missing binary, non-zero exit, timeout, OS error.

The error message contains: the found version (or the reason none could be
determined), the required minimum, an upgrade command
(`uv tool upgrade spec-runner` / `uv add --dev spec-runner --upgrade-package
spec-runner`), and the one-line reason: *versions 2.15.x may commit the
harness-owned `spec/.gitignore` into task commits, which the ex-post scope
gate flags as a scope escape*.

### Version parsing

`parse_spec_runner_version(output) -> tuple[int, int, int] | None` in
`maestro/spec_runner.py`:

- accepts the ordinary CLI format: `spec-runner 2.16.0` (surrounding
  whitespace tolerated, first line of output considered);
- returns `None` for anything else — including dev/rc/local suffixes
  (`2.16.0.dev1`, `2.16.0rc1`, `2.16.0+local`). No guessing.

### Override for unpublished builds

Explicit, documented escape hatch for local development against an
unreleased spec-runner: environment variable
`MAESTRO_SPEC_RUNNER_ALLOW_UNVERIFIED=1` downgrades the version-check error
to a `warning` (never silent — the finding stays visible in the report).
The capability probe is NOT affected by the override and remains mandatory.

### Legacy 2.15.x (emergency path, not a supported mode)

For users stuck on 2.15.x the previously observed workarounds — declaring
`spec/.gitignore` in every workstream's scope, or `maestro
workstream-approve` after the block — remain possible but are documented as
an emergency path only. The supported path is the upgrade.

## Tests

- 2.15.x → blocked (`error`, message carries found + required version);
- 2.16.0 and a newer compatible version → pass;
- malformed / empty / dev-suffixed output → blocked;
- missing binary / timeout → blocked;
- override env set → warning instead of error; capability probe still
  enforced independently;
- `spec/.gitignore` stays visible to the changed-paths source
  (`_orchestrator_managed("spec/.gitignore") is False`).

## Out of scope

- Any change to gate semantics, `_ORCHESTRATOR_MANAGED`, or `scope_gate.py`.
- Content inspection of `spec/.gitignore`.
- A general version-negotiation protocol with spec-runner (the existing
  capability-probe pattern remains the tool for feature detection).
