# Verifier Gate (Mode-1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, durable, fail-closed adversarial LLM verifier gate to the Mode-1 scheduler — after a passing `validation_cmd`, a cheap Claude/Haiku judge reads the task's scope-bounded diff and gates `DONE`.

**Architecture:** A third durable task phase `VERIFYING` (`execution_phase="verification"`, reused from Stage B) between `VALIDATING` and `DONE`. A narrow `JudgeRunner` Protocol with one impl `ClaudeDiffJudge`, running `claude -p` one-shot through the execution layer. Reuses Stage B's entity-agnostic verdict primitives (`VerdictValue`, `Finding`) and adds task-side models additively; never touches the workstream verification stack.

**Tech Stack:** Python 3.12+, uv, pydantic, aiosqlite, Typer, git, the merged `maestro/domain/` (Stage B, #105/#106) and `maestro/execution/` layers.

**Spec (SSOT):** `docs/superpowers/specs/2026-07-25-maestro-verifier-gate-mode1-design.md` (commit 762f966). Read it. Section refs below (§N) point to it.

## Global Constraints

- **Do NOT touch Stage B code**: `CommandVerifier`, `VerificationContext`, `DomainProfile`, `maestro/domain/ledger.py`, `resume.py`, and the workstream `VerdictIdentity`/`VerdictDocument`/`EchoExpectations`/`evaluate_handshake` are frozen. Additions to `domain/verdict.py` are strictly additive (new symbols; existing ones byte-unchanged).
- **Reuse the `verification` execution phase** — already in the CHECK (Stage B migration 15). No new phase, no phase migration.
- **`verifier.backend` is `Literal["local"]`** this slice (§7). Reject anything else at config parse.
- **Read-only is policy isolation, not OS isolation** (§7): scratch cwd, `collect=none`, repo path not passed, envelope on stdin — but never *claim* architectural read-only for `backend: local`.
- **Fail-closed everywhere**: any runner/infra/config fault → `ERROR` → `NEEDS_REVIEW`; `ERROR` is formed by the runner, never emitted by the model (§9). The model's raw payload is only `{verdict, findings}`; the provider seals identity (§6, provider binding — not model echo).
- **Migrations are append-only**: new migrations are **16** (`tasks.verifier_baseline_sha`) and **17** (`task_costs.execution_phase` + `model`). Never edit migrations ≤15.
- **Verifier runs only after a passing `validation_cmd`.** No `validation_cmd` or no `verifier:` block → `VALIDATING → DONE` exactly as today (byte-for-byte).
- **Testing:** FOREGROUND `uv run pytest` only (a workspace watchdog kills backgrounded pytest); use `-o faulthandler_timeout=60`. **Any test building a `Database` MUST close it via a fixture (`yield d; await d.close()`)** — an unclosed aiosqlite connection = ResourceWarning-as-error + ~120s hang. `uv` only, never pip. `uv run pyrefly check` (0) + `uv run ruff format . && uv run ruff check .` after every task.

## File Structure

- `maestro/domain/verdict.py` — **modify (additive)**: task-side verdict models + `evaluate_task_document`.
- `maestro/verifier/` — **new package**:
  - `config.py` — `resolve_verifier_model` + catalog status check.
  - `diff.py` — deterministic scope-bounded patch/manifest builder + identity hashes.
  - `envelope.py` — the stdin `VerifierInput` envelope + raw-payload schema/validator.
  - `prompt.py` — judge instructions + pinned fake-done taxonomy + `profile_sha256`.
  - `judge.py` — `TaskVerificationContext`, `JudgeRunner` Protocol, `ClaudeDiffJudge`.
- `maestro/models.py` — `TaskStatus.VERIFYING` + transitions; `VerifierConfig`; `Task.verifier_baseline_sha`.
- `maestro/config.py` — parse/validate the `verifier:` block.
- `maestro/event_log.py` — 4 verifier events.
- `maestro/transitions.py` — `TASK_EFFECTS[VERIFYING] = StatusEffect()` (no-op, event=None; satisfies the effect-table totality invariant, fires no auto-event).
- `maestro/database.py` — migrations 16/17 + baseline CRUD + phase/model cost columns.
- `maestro/scheduler.py` — the gate in `_handle_task_completion`; baseline capture; reservation lifecycle; verifier cost row; recovery; events.
- `maestro/cli.py` — requeue handle-fence on the Mode-1 `NEEDS_REVIEW → READY` path.

---

### Task 1: Task-side verdict primitives + `evaluate_task_document`

**Files:**
- Modify: `maestro/domain/verdict.py`
- Test: `tests/test_task_verdict.py`

**Interfaces:**
- Consumes: `VerdictValue`, `Finding` (existing, reused verbatim).
- Produces: `TaskVerdictIdentity`, `TaskVerdictDocument`, `TaskHandshakeResult`, `TaskIdentityExpectations`, `evaluate_task_document(json_path: Path, expected: TaskIdentityExpectations) -> TaskHandshakeResult`.

- [ ] **Step 1: Write failing tests** (`tests/test_task_verdict.py`)

```python
import json
from pathlib import Path

from maestro.domain.verdict import (
    Finding, VerdictValue,
    TaskVerdictIdentity, TaskVerdictDocument, TaskHandshakeResult,
    TaskIdentityExpectations, evaluate_task_document,
)


def _identity(**over):
    base = dict(
        task_id="t1", verification_run_id="r1", verification_attempt=1,
        artifact="task-diff:t1", artifact_sha256="a" * 64,
        criteria_sha256="b" * 64, profile_sha256="c" * 64,
        verified_source_commit="d" * 40, verified_scope_sha256="e" * 64,
    )
    base.update(over)
    return TaskVerdictIdentity(**base)


def _expected(**over):
    base = dict(
        task_id="t1", verification_run_id="r1", verification_attempt=1,
        artifact="task-diff:t1", artifact_sha256="a" * 64,
        criteria_sha256="b" * 64, profile_sha256="c" * 64,
        verified_source_commit="d" * 40, verified_scope_sha256="e" * 64,
    )
    base.update(over)
    return TaskIdentityExpectations(**base)


def _write(tmp_path, doc: dict) -> Path:
    p = tmp_path / "verdict.json"
    p.write_text(json.dumps(doc))
    return p


def test_valid_pass(tmp_path):
    doc = TaskVerdictDocument(schema_version=2, identity=_identity(), verdict=VerdictValue.PASS)
    p = _write(tmp_path, json.loads(doc.model_dump_json()))
    res = evaluate_task_document(p, _expected())
    assert res.outcome == VerdictValue.PASS
    assert res.document is not None and res.document.verdict == VerdictValue.PASS


def test_valid_fail_is_fail_not_error(tmp_path):
    """A FAIL verdict is FAIL — there is NO exit-code comparison (unlike Stage B)."""
    doc = TaskVerdictDocument(
        schema_version=2, identity=_identity(), verdict=VerdictValue.FAIL,
        findings=[Finding(criterion_id="stub", severity="high", evidence="x", author_feedback="fix y")],
    )
    p = _write(tmp_path, json.loads(doc.model_dump_json()))
    assert evaluate_task_document(p, _expected()).outcome == VerdictValue.FAIL


def test_identity_mismatch_is_error(tmp_path):
    doc = TaskVerdictDocument(schema_version=2, identity=_identity(artifact_sha256="f" * 64), verdict=VerdictValue.PASS)
    p = _write(tmp_path, json.loads(doc.model_dump_json()))
    res = evaluate_task_document(p, _expected())  # expected artifact_sha256 = "a"*64
    assert res.outcome == VerdictValue.ERROR and res.document is None


def test_missing_and_garbage_are_error(tmp_path):
    assert evaluate_task_document(tmp_path / "nope.json", _expected()).outcome == VerdictValue.ERROR
    bad = tmp_path / "bad.json"; bad.write_text("{not json")
    assert evaluate_task_document(bad, _expected()).outcome == VerdictValue.ERROR


def test_stage_b_workstream_models_untouched():
    from maestro.domain.verdict import VerdictIdentity, EchoExpectations, evaluate_handshake  # noqa: F401
    # workstream identity still mandates workstream_id/rework_attempt
    import inspect
    assert "workstream_id" in inspect.getsource(VerdictIdentity)
```

Run: `uv run pytest tests/test_task_verdict.py -v -o faulthandler_timeout=60` → Expected: FAIL (import errors — new symbols absent).

- [ ] **Step 2: Implement the additive models + evaluator** in `maestro/domain/verdict.py` (append after the existing symbols; do not edit existing ones).

```python
class TaskVerdictIdentity(BaseModel):
    """Task-shaped identity — provider-computed, never model-supplied (§5).

    No `workstream_id`/`rework_attempt` (those are Mode-2). `verified_scope_sha256`
    is the honest scope-state pin (NOT a git tree — non-overlapping tasks may
    legally touch other paths in parallel).
    """
    model_config = ConfigDict(frozen=True)
    task_id: str
    verification_run_id: str
    verification_attempt: int = Field(ge=1)
    artifact: str
    artifact_sha256: str
    criteria_sha256: str
    profile_sha256: str
    verified_source_commit: str
    verified_scope_sha256: str


class TaskVerdictDocument(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: Literal[2]
    identity: TaskVerdictIdentity
    verdict: VerdictValue
    findings: list[Finding] = Field(default_factory=list)


class TaskIdentityExpectations(BaseModel):
    """The provider-computed identity the sealed document must carry (§6 binding)."""
    model_config = ConfigDict(frozen=True)
    task_id: str
    verification_run_id: str
    verification_attempt: int
    artifact: str
    artifact_sha256: str
    criteria_sha256: str
    profile_sha256: str
    verified_source_commit: str
    verified_scope_sha256: str


class TaskHandshakeResult(BaseModel):
    """Task analogue of HandshakeResult — carries a TaskVerdictDocument."""
    model_config = ConfigDict(frozen=True)
    outcome: VerdictValue
    protocol_error: str | None = None
    document: TaskVerdictDocument | None = None


def _task_error(message: str) -> TaskHandshakeResult:
    return TaskHandshakeResult(outcome=VerdictValue.ERROR, protocol_error=message, document=None)


def evaluate_task_document(
    json_path: Path, expected: TaskIdentityExpectations
) -> TaskHandshakeResult:
    """Validate the sealed task verdict document (provider binding, §6): file
    present + parseable + schema-valid + identity == provider-computed identity.
    Returns the payload verdict (PASS/FAIL) or ERROR. NO exit-code comparison
    (§3 transport/semantic split — the Claude CLI exits 0 on any answer)."""
    if not json_path.is_file():
        return _task_error(f"verdict file missing: {json_path}")
    try:
        document = TaskVerdictDocument.model_validate(json.loads(json_path.read_text()))
    except (ValueError, ValidationError, OSError) as exc:
        return _task_error(f"verdict file invalid: {exc}")
    ident = document.identity
    mismatches = [
        name for name, got, want in (
            ("task_id", ident.task_id, expected.task_id),
            ("verification_run_id", ident.verification_run_id, expected.verification_run_id),
            ("verification_attempt", ident.verification_attempt, expected.verification_attempt),
            ("artifact", ident.artifact, expected.artifact),
            ("artifact_sha256", ident.artifact_sha256, expected.artifact_sha256),
            ("criteria_sha256", ident.criteria_sha256, expected.criteria_sha256),
            ("profile_sha256", ident.profile_sha256, expected.profile_sha256),
            ("verified_source_commit", ident.verified_source_commit, expected.verified_source_commit),
            ("verified_scope_sha256", ident.verified_scope_sha256, expected.verified_scope_sha256),
        ) if got != want
    ]
    if mismatches:
        return _task_error(f"identity mismatch: {', '.join(mismatches)}")
    return TaskHandshakeResult(outcome=document.verdict, protocol_error=None, document=document)
```

- [ ] **Step 3: Run tests** → PASS. `uv run pyrefly check` (0), `uv run ruff format . && uv run ruff check .`.
- [ ] **Step 4: Commit** — `feat(verifier): task-side verdict primitives + evaluate_task_document`.

---

### Task 2: Verifier config + isolated model resolution

**Files:**
- Modify: `maestro/models.py` (add `VerifierConfig`), `maestro/config.py` (parse `verifier:`).
- Create: `maestro/verifier/__init__.py`, `maestro/verifier/config.py`.
- Test: `tests/test_verifier_config.py`, `tests/test_resolve_verifier_model.py`.

**Interfaces:**
- Produces: `VerifierConfig(runner: Literal["claude"], model: str | None, timeout_seconds: int = 120, max_diff_bytes: int = 100_000, backend: Literal["local"] = "local")`; `resolve_verifier_model(cfg: VerifierConfig, catalog) -> str`.
- Consumes: `maestro/catalog.py` for the model status/identity check (read the catalog API before writing — mirror how `resolve_model` validates status).

- [ ] **Step 1: Write failing tests.** Model-resolution precedence `verifier.model → MAESTRO_VERIFIER_MODEL → fail loud`; NEVER `MAESTRO_CLAUDE_MODEL` / catalog-default; resolved model absent from catalog → error; `retired`/`unknown` → error; `deprecated` → warning (assert it does not raise). Config: `backend` other than `"local"` → ValidationError; missing `model` AND no env → resolution fails loud (parse may allow `model=None`, resolution enforces).

```python
import pytest
from maestro.models import VerifierConfig
from maestro.verifier.config import resolve_verifier_model, VerifierModelError

def test_backend_must_be_local():
    with pytest.raises(Exception):
        VerifierConfig(runner="claude", model="claude-haiku-4-5", backend="docker")

def test_precedence_config_wins(monkeypatch, fake_catalog):
    monkeypatch.setenv("MAESTRO_VERIFIER_MODEL", "env-model")
    cfg = VerifierConfig(runner="claude", model="claude-haiku-4-5")
    assert resolve_verifier_model(cfg, fake_catalog) == "claude-haiku-4-5"

def test_env_fallback(monkeypatch, fake_catalog):
    monkeypatch.setenv("MAESTRO_VERIFIER_MODEL", "claude-haiku-4-5")
    cfg = VerifierConfig(runner="claude", model=None)
    assert resolve_verifier_model(cfg, fake_catalog) == "claude-haiku-4-5"

def test_never_uses_claude_model_env(monkeypatch, fake_catalog):
    monkeypatch.setenv("MAESTRO_CLAUDE_MODEL", "expensive-main")
    monkeypatch.delenv("MAESTRO_VERIFIER_MODEL", raising=False)
    cfg = VerifierConfig(runner="claude", model=None)
    with pytest.raises(VerifierModelError):
        resolve_verifier_model(cfg, fake_catalog)

def test_unknown_or_retired_model_errors(fake_catalog):
    cfg = VerifierConfig(runner="claude", model="ghost-model")
    with pytest.raises(VerifierModelError):
        resolve_verifier_model(cfg, fake_catalog)
```

(Define a `fake_catalog` fixture that reports one healthy model, one `deprecated`, one `retired`; mirror the real catalog's status API — read `maestro/catalog.py` first.)

- [ ] **Step 2: Implement** `VerifierConfig` (models.py) + `resolve_verifier_model` (verifier/config.py) with a `VerifierModelError`. Resolution: `name = cfg.model or os.environ.get("MAESTRO_VERIFIER_MODEL")`; `if not name: raise VerifierModelError(...)`; look up `name` in the catalog; `retired`/absent → raise; `deprecated` → `logger.warning`; return `name`. Wire `verifier:` parsing into `maestro/config.py` (optional block; absent → `None`).
- [ ] **Step 3: Run tests** → PASS. pyrefly + ruff.
- [ ] **Step 4: Commit** — `feat(verifier): VerifierConfig + isolated resolve_verifier_model`.

---

### Task 3: `TaskStatus.VERIFYING` + transitions + events

**Files:**
- Modify: `maestro/models.py` (`TaskStatus`, `valid_transitions`), `maestro/event_log.py` (`EventType`).
- Test: `tests/test_verifying_transitions.py`.

**Interfaces:**
- Produces: `TaskStatus.VERIFYING = "verifying"`; transitions `VALIDATING: {VERIFYING, DONE, FAILED, NEEDS_REVIEW}`, `VERIFYING: {DONE, FAILED, NEEDS_REVIEW}`; `EventType.VERIFIER_STARTED/VERIFIER_PASSED/VERIFIER_FAILED/VERIFIER_ERROR`.
- Set `TASK_EFFECTS[VERIFYING] = StatusEffect()` (no-op, event=None) — keeps the effect-table totality invariant (`test_effect_tables_are_total`) green while firing no auto-event; §4 events are emitted explicitly so they carry `execution_id`.

- [ ] **Step 1: Write failing tests** — `VALIDATING.can_transition_to(VERIFYING/DONE/NEEDS_REVIEW)` all True; `VERIFYING.can_transition_to(DONE/FAILED/NEEDS_REVIEW)` True, `VERIFYING → RUNNING` False; the four `EventType` members exist; `TASK_EFFECTS.get(TaskStatus.VERIFYING) is None`.
- [ ] **Step 2: Implement** the enum member, the two transition-map edits, the four events.
- [ ] **Step 3: Run** → PASS. pyrefly + ruff.
- [ ] **Step 4: Commit** — `feat(verifier): TaskStatus.VERIFYING + transitions + lifecycle events`.

---

### Task 4: Migrations 16 (baseline) + 17 (cost phase/model) + CRUD

**Files:**
- Modify: `maestro/database.py` (SCHEMA_SQL defaults; ordered migration list; two migration methods; `Task` row read/write for `verifier_baseline_sha`; cost write with phase/model), `maestro/models.py` (`Task.verifier_baseline_sha: str | None = None`; `TaskCost.execution_phase`/`model` if `TaskCost` is a model).
- Test: `tests/test_db_migration_verifier.py`, extend `tests/test_database.py` journal lists.

**Interfaces:**
- Produces: `tasks.verifier_baseline_sha TEXT` (nullable); `task_costs.execution_phase TEXT NOT NULL DEFAULT 'task'` + `task_costs.model TEXT`; a way to set/read the baseline (`update_task` already persists the whole Task row — add the column to the INSERT/UPDATE/SELECT lists) and to write a cost row with a phase/model.

- [ ] **Step 1: Write migration-first tests** (mirror `tests/test_db_migration_tasks_validation_default.py` — hand-build a pre-16 DB, apply, assert). Assert: fresh DB has both columns with correct defaults; an upgraded DB (versions 1..15 journaled) gains them; existing task rows get `verifier_baseline_sha=NULL`, existing cost rows get `execution_phase='task'`; journal lists in `test_database.py` extended to include `(16, ...)` and `(17, ...)`. Both migrations are plain `ADD COLUMN` (mirror migration 11 — **not** a table rebuild; `tasks`/`task_costs` add columns fine).
- [ ] **Step 2: Implement** SCHEMA_SQL column additions, `_migrate_tasks_verifier_baseline_sha` (16) and `_migrate_task_costs_phase_model` (17) — each `PRAGMA table_info`-guarded `ALTER TABLE ... ADD COLUMN` (idempotent, no FK dance). Register `(16, ...)`, `(17, ...)`. Thread `verifier_baseline_sha` through `create_task`/`update_task`/`_row_to_task`. Add a cost-write path that accepts `execution_phase`/`model`.
- [ ] **Step 3: Run** the migration tests + `tests/test_database.py` → PASS. pyrefly + ruff.
- [ ] **Step 4: Commit** — `feat(verifier): migrations 16/17 (baseline sha, cost phase+model) + CRUD`.

---

### Task 5: Deterministic scope-bounded patch/manifest builder + identity hashes

**Files:**
- Create: `maestro/verifier/diff.py`
- Test: `tests/test_verifier_diff.py`

**Interfaces:**
- Produces:
  - `build_scope_patch(worktree: Path, baseline_sha: str, scope: list[str], *, max_bytes: int) -> ScopePatch` where `ScopePatch` has `patch_bytes: bytes`, `manifest: list[PathEntry]` (path + status added/modified/deleted/binary). Raises `PatchTooLargeError` / `BinaryChangeError` (both map to §9 ERROR upstream).
  - `compute_identity(task, patch: ScopePatch) -> tuple[artifact_sha256, criteria_sha256, verified_scope_sha256]` per §5 (criteria = SHA-256 of canonical JSON `{title, prompt, validation_cmd, normalized_scope}` sorted keys; artifact = SHA-256 of the canonical patch envelope; verified_scope = SHA-256 of the scope-bounded envelope).
- Consumes: nothing from later tasks.

- [ ] **Step 1: Write failing tests** using a real temp git repo (init, commit baseline, make in-scope + out-of-scope + untracked-in-scope + deleted changes). Assert: patch includes in-scope tracked + untracked deterministically, excludes out-of-scope; `--no-ext-diff`/stable order → byte-identical across two runs; a binary in-scope change → `BinaryChangeError`; an oversize patch → `PatchTooLargeError`; `compute_identity` is stable and criteria hash changes when `validation_cmd` changes.
- [ ] **Step 2: Implement** with `subprocess`/`git`: `git -c core.quotepath=false diff --no-ext-diff <baseline> -- <scope...>` for tracked; `git ls-files --others --exclude-standard -- <scope...>` for untracked (render deterministically, e.g. `git add -N` into a temp `GIT_INDEX_FILE` copy then diff, so the working index is not mutated — §5); detect binary via git's `Binary files ... differ` / `--numstat` `-` markers; enforce `max_bytes`; build the manifest and the hashes.
- [ ] **Step 3: Run** → PASS. pyrefly + ruff.
- [ ] **Step 4: Commit** — `feat(verifier): deterministic scope-bounded patch/manifest + identity hashes`.

---

### Task 6: VerifierInput envelope + raw-payload schema + judge prompt/profile

**Files:**
- Create: `maestro/verifier/envelope.py`, `maestro/verifier/prompt.py`
- Test: `tests/test_verifier_envelope.py`

**Interfaces:**
- Produces:
  - `build_envelope(ctx, patch: ScopePatch, acceptance: dict) -> str` — the stdin blob (task context + manifest + patch), deterministic.
  - `RAW_PAYLOAD_SCHEMA` + `parse_raw_payload(text: str) -> RawVerdict` (`{verdict: "pass"|"fail", findings: [...]}`, strict `additionalProperties: false`; anything else raises → ERROR upstream).
  - `JUDGE_PROMPT` (adversarial "read the diff as if broken" + the pinned fake-done taxonomy enum) and `profile_sha256()` = SHA-256 of `(prompt version + raw schema + taxonomy)`.
- Consumes: `ScopePatch` (Task 5), `TaskVerificationContext` (Task 7 — define the envelope to take the fields, not the class, to avoid a cycle).

- [ ] **Step 1: Write failing tests** — envelope is deterministic for fixed inputs; `parse_raw_payload` accepts `{"verdict":"pass","findings":[]}` and a valid fail-with-findings; rejects extra keys, missing verdict, wrong verdict enum, non-list findings (→ raises); `profile_sha256()` is stable and changes if the taxonomy list changes.
- [ ] **Step 2: Implement** the envelope (JSON with sorted keys), the strict raw-payload validator (a pydantic model with `extra="forbid"`), the prompt + taxonomy + profile hash.
- [ ] **Step 3: Run** → PASS. pyrefly + ruff.
- [ ] **Step 4: Commit** — `feat(verifier): stdin envelope + strict raw-payload schema + judge prompt/profile`.

---

### Task 7: `ClaudeDiffJudge` provider (transport/semantic split, provider binding, finalize)

**Files:**
- Create: `maestro/verifier/judge.py`
- Test: `tests/test_claude_diff_judge.py`

**Interfaces:**
- Produces: `TaskVerificationContext` (§5 fields); `JudgeRunner` Protocol (`async def verify(ctx) -> TaskHandshakeResult`); `ClaudeDiffJudge(model, backend, *, timeout_seconds, db=None)`.
- Consumes: `build_scope_patch`/`compute_identity` (T5), `build_envelope`/`parse_raw_payload`/`JUDGE_PROMPT`/`profile_sha256` (T6), `evaluate_task_document`/`TaskVerdictDocument`/`TaskIdentityExpectations`/`TaskHandshakeResult` (T1), the execution layer (`ExecutionRequest`, `backend.run`, `finalize_handle`, `Database.start_execution`/`update_execution_handle_launch`).

**Reference:** mirror `maestro/domain/verifier.py::CommandVerifier` for the durable pre-spawn CAS + `update_execution_handle_launch` shape, but: (a) `entity_kind="task"`; (b) the CAS is the **atomic `validating → verifying`** mint (done by the scheduler in Task 8 — the judge receives an already-`VERIFYING` task and self-loops its handle, OR the scheduler passes the minted `execution_id`; keep the handle persistence in the scheduler if cleaner and have the judge take an `execution_id`); (c) stdin envelope + scratch cwd + `collect=none`; (d) `finalize_handle` is the SOLE `wait()` owner (§6.5) — do not call `handle.wait()` then finalize.

- [ ] **Step 1: Write failing tests** with a fake backend/handle (mirror `tests/test_command_verifier.py` fakes). Cases: valid `pass` payload → `TaskHandshakeResult(PASS)` with sealed document whose identity == provider-computed; valid `fail` → FAIL (NOT error); CLI exit≠0 → ERROR (transport, before parse); timeout → ERROR; malformed/extra-key raw payload → ERROR; model output that omits identity fields still yields a sealed doc (provider binds identity — assert the model was never asked for identity); `finalize_handle` drove the handle terminal→collected→cleaned (assert on the fake); a `backend.run()` raising after pre-spawn persist reconciles the placeholder (no orphan open handle).
- [ ] **Step 2: Implement** `ClaudeDiffJudge.verify` per §6 steps 3–5 (the scheduler owns steps 1–2 preflight+CAS; the judge runs the claude request, transport-checks, parses raw payload, seals the `TaskVerdictDocument` from `compute_identity`, writes `out_json`, `evaluate_task_document`, finalizes). Build the `ExecutionRequest`: `argv=["claude","-p",JUDGE_PROMPT,"--output-format","json","--model",model]`, `stdin=envelope`, `capture_output=True`, `collect=CollectPolicy(mode="none")`, `workdir=<scratch>`, `backend_id=backend.id`, `execution_phase` label `"verification"`.
- [ ] **Step 3: Run** → PASS. pyrefly + ruff.
- [ ] **Step 4: Commit** — `feat(verifier): ClaudeDiffJudge provider (transport/semantic split, provider binding, durable finalize)`.

---

### Task 8: Scheduler gate — envelope preflight + atomic VERIFYING CAS + outcome routing

**Files:**
- Modify: `maestro/scheduler.py` (`_handle_task_completion` around `:1645`; `_spawn_task` for baseline capture; a new `_run_verifier` helper).
- Test: `tests/test_scheduler_verifier_gate.py`.

**Interfaces:**
- Consumes: `VerifierConfig`/`resolve_verifier_model` (T2), `ClaudeDiffJudge`/`TaskVerificationContext` (T7), `build_scope_patch`/`compute_identity` (T5), `build_envelope`/`profile_sha256` (T6), `Task.verifier_baseline_sha` (T4), events (T3).
- Produces: the wired gate.

**Wiring (exact):** in `_handle_task_completion`, when `validation_result.success` is True (currently `scheduler.py:1645`): if the run has a `verifier:` config AND `task.validation_cmd`, call `await self._run_verifier(task_id, task, running_task)` INSTEAD of the direct `VALIDATING → DONE`; otherwise keep today's DONE path (+ `_auto_commit_task`). `_run_verifier`:
1. **Envelope preflight (still `VALIDATING`)**: `build_scope_patch(worktree, task.verifier_baseline_sha, task.scope, max_bytes=cfg.max_diff_bytes)` + `compute_identity`. On `PatchTooLargeError`/`BinaryChangeError`/dirty-tree/empty-scope/git-failure → emit `VERIFIER_ERROR`, `self._transition(task_id, NEEDS_REVIEW, expected_status=VALIDATING)`, return (no `VERIFIER_STARTED`).
2. **Atomic CAS**: `execution_id = uuid`; `await self._db.start_execution(entity_kind="task", entity_id=task_id, expected_status="validating", running_status="verifying", execution_id=..., backend_id=<verifier backend>, transport_ref=<placeholder>, attempt=task.retry_count+1, execution_phase="verification")`; emit `VERIFIER_STARTED` (with `execution_id`); dispatch.
3. Run `ClaudeDiffJudge(...).verify(ctx)` → `TaskHandshakeResult`.
4. Route: `PASS` → `self._transition(task_id, DONE, expected_status=VERIFYING)` + `_auto_commit_task(task)` + `_build_outcome`/report + `VERIFIER_PASSED`; `FAIL` → fold `document.findings[].author_feedback` into an error message and call `self._handle_validation_failure(task_id, task, msg, <ValidationResult-like>)` + `VERIFIER_FAILED`; `ERROR` → `self._transition(task_id, NEEDS_REVIEW, expected_status=VERIFYING)` + `VERIFIER_ERROR`.

Baseline capture: in `_spawn_task`, when a verifier-enabled task is dispatched AND `task.verifier_baseline_sha is None`, record the current `git rev-parse HEAD` of the workdir into `verifier_baseline_sha` (once; never overwrite) — and enforce the **clean-worktree precondition here** (dirty at first dispatch → route to NEEDS_REVIEW, §5.1).

- [ ] **Step 1: Write failing tests** with a fake judge injected: PASS→DONE (+auto-commit called); FAIL→retry path (task back toward READY, findings in error context); ERROR→NEEDS_REVIEW; pre-CAS preflight error → NEEDS_REVIEW from VALIDATING with only `VERIFIER_ERROR`; no `verifier:` block → `VALIDATING → DONE` unchanged; baseline captured once at first dispatch and preserved across a retry.
- [ ] **Step 2: Implement** `_run_verifier` + the `:1645` branch + baseline capture in `_spawn_task`.
- [ ] **Step 3: Run** the new test + `tests/test_scheduler.py` → PASS. pyrefly + ruff.
- [ ] **Step 4: Commit** — `feat(verifier): scheduler gate — preflight, atomic VERIFYING CAS, PASS/FAIL/ERROR routing`.

---

### Task 9: Reservation lifecycle for verifier-enabled tasks

**Files:**
- Modify: `maestro/scheduler.py` (reservation acquire/hold/release around `:1512`; `_reconstruct_reservations` at `:761`).
- Test: `tests/test_verifier_reservation.py`.

**Interfaces:** Consumes `ReservationRegistry`/`scope_to_reservation` (existing). Produces the lifecycle-scoped hold.

- [ ] **Step 1: Write failing tests**: for a verifier-enabled task, the `(workdir, scope)` reservation is still held during VALIDATING/VERIFYING and across a retry; released on `DONE`; released on `ABANDONED`; **NOT** released on entering `NEEDS_REVIEW`; an overlapping-scope task cannot acquire while held; after a simulated restart, `_reconstruct_reservations` rebuilds the hold for a verifier task with a baseline in status `READY`/`FAILED`/`NEEDS_REVIEW` (not only from an open handle).
- [ ] **Step 2: Implement**: hold the reservation from first dispatch; move release out of the post-collect path (`:1512`) for verifier-enabled tasks to the terminal `DONE`/`ABANDONED` transitions only; widen `_reconstruct_reservations` to include verifier-enabled tasks with a baseline and a non-terminal status. Do NOT change reservation behavior for non-verifier tasks.
- [ ] **Step 3: Run** the new test + `tests/test_scheduler_reservations.py` + `tests/test_reservation_rehold_validation.py` → PASS. pyrefly + ruff.
- [ ] **Step 4: Commit** — `feat(verifier): lifecycle-scoped reservation (hold through NEEDS_REVIEW; restart reconstruction)`.

---

### Task 10: Verifier cost row + read-side phase/model breakdown

**Files:**
- Modify: `maestro/scheduler.py` (write a verifier `task_costs` row after the judge run), `maestro/cost_tracker.py` (parse Claude JSON usage for the judge model; `UNKNOWN`-not-`$0`), the `maestro costs` read path (`maestro/cli.py` / cost summary — add group-by `execution_phase` and `model`).
- Test: `tests/test_verifier_cost.py`.

**Interfaces:** Consumes the T4 cost columns. Note: `_build_outcome` (`scheduler.py:472`) already sums ALL rows of an attempt with unknown-propagation — so a verifier row written with the same `attempt` is **automatically** included in the arbiter outcome's full spend; do not special-case it there.

- [ ] **Step 1: Write failing tests**: after a judge run, a `task_costs` row exists with `execution_phase='verification'`, `agent_type=CLAUDE_CODE`, `model=<verifier model>`; `_build_outcome` for that attempt includes the verifier cost in `cost_usd` (and stays `None` if any component unknown); the `maestro costs` summary exposes a per-`execution_phase` and per-`model` breakdown; a judge response with no usage → that row's cost is `UNKNOWN` (None), not `0.0`.
- [ ] **Step 2: Implement** the verifier cost write (phase/model), the Claude-usage parse, and the read-side groupings. Keep existing totals summing all rows.
- [ ] **Step 3: Run** the new test + `tests/test_cost_tracker.py` + `tests/test_maestro_costs*.py` → PASS. pyrefly + ruff.
- [ ] **Step 4: Commit** — `feat(verifier): phase/model cost row + maestro costs by-phase/by-model read-side`.

---

### Task 11: Recovery for VERIFYING + requeue handle-fence

**Files:**
- Modify: `maestro/scheduler.py` (recovery: a task stranded in `VERIFYING` → `NEEDS_REVIEW`, selecting the verifier handle), `maestro/cli.py` (the Mode-1 `NEEDS_REVIEW → READY` requeue path — confirm whether that is `maestro retry` or `_approve_task`; `_approve_task` at `:733` only handles `AWAITING_APPROVAL`, so the verifier requeue is the `retry`/`NEEDS_REVIEW` path — fence THAT).
- Test: `tests/test_verifier_recovery.py`.

**Interfaces:** Consumes the durable handle recovery machinery (`StateRecovery`/`ExecutionBackend.probe`).

- [ ] **Step 1: Write failing tests**: a task persisted in `VERIFYING` with an open verification handle → recovery routes it to `NEEDS_REVIEW` (fail-closed), probing/holding via the verifier handle; a verifier-originated `NEEDS_REVIEW` re-queue is **rejected while the verification handle is still open** (fail-closed) and **allowed only after** the handle is terminal→cleaned; the reservation stays held across the fence.
- [ ] **Step 2: Implement** the `VERIFYING` recovery branch (mirror the `VALIDATING` recovery that prefers the validation handle) and the requeue fence on the Mode-1 `NEEDS_REVIEW → READY` command.
- [ ] **Step 3: Run** the new test + `tests/test_recovery*.py` → PASS. pyrefly + ruff.
- [ ] **Step 4: Commit** — `feat(verifier): VERIFYING recovery + requeue handle-fence`.

---

### Task 12: Docs + example + opt-in e2e + full suite

**Files:**
- Modify: `maestro/CLAUDE.md` (verifier note); create `examples/with-verifier.yaml`.
- Create: `tests/e2e/test_verifier_gate_e2e.py` (opt-in, env-gated, skips cleanly with zero subprocesses when the gate env is unset — mirror `tests/e2e/test_ssh_validation_e2e.py`).

- [ ] **Step 1** Write the opt-in e2e (gated by e.g. `MAESTRO_VERIFIER_E2E`, needs a real `claude` CLI + a cheap model; skips clean otherwise). Add `examples/with-verifier.yaml`.
- [ ] **Step 2** Update `CLAUDE.md`: the verifier gate as a third Mode-1 phase, opt-in `verifier:` block, `execution_phase="verification"` reuse, fail-closed, policy-isolation, migrations 16/17.
- [ ] **Step 3** Full suite `uv run pytest -o faulthandler_timeout=90` → green; `uv run pyrefly check` (0); `uv run ruff format . && uv run ruff check .`.
- [ ] **Step 4: Commit** — `docs(verifier): CLAUDE.md + example + opt-in e2e; full suite green`.

---

## Self-Review (controller runs before dispatch)

- **Spec coverage:** §3 (T1), §4 config/model (T2), §4 FSM/events (T3), migrations §11 (T4), §5 diff attribution (T5), §6 envelope/raw-payload/binding (T6, T7), §6 provider/finalize (T7), §4/§6/§9 gate+routing (T8), §5.3 reservation (T9), §10 cost (T10), §8 recovery+fence (T11), docs (T12). All spec sections mapped.
- **Type consistency:** `TaskHandshakeResult` is the judge/eval return everywhere (T1/T7/T8); `execution_phase="verification"` and `entity_kind="task"` consistent (T4/T7/T8); migration numbers 16/17 consistent (T4).
- **Ordering:** T1→T3 pure/foundation; T4 migrations; T5/T6 pure builders; T7 provider (needs T1/T5/T6); T8 gate (needs T2/T4/T5/T6/T7); T9/T10/T11 scheduler concerns (need T8); T12 wrap. No forward type references.
