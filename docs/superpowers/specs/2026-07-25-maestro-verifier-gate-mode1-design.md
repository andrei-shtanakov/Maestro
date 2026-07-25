# Verifier Gate (Mode-1) — adversarial diff judge, aligned to Stage B

> Idea #6 (loopkit "cheap adversarial verifier") for Mode-1 tasks, built ON TOP OF
> Stage B's merged verification contracts (`maestro/domain/`, PR #105 / `346222e`).
> Date: 2026-07-25. Status: DRAFT for review (external Codex review before writing-plans).

## 1. Goal & one-line summary

Add an **opt-in, durable, fail-closed adversarial verifier gate** to the Mode-1
scheduler: after a task's deterministic `validation_cmd` passes, a cheap LLM judge
(default Haiku via the `claude` CLI, headless) reads the task's canonical scope-bounded
diff "as if it is broken" and returns a verdict. `PASS` → `DONE`; `FAIL` → the existing
validation retry context; any runner/infrastructure fault → `ERROR` → `NEEDS_REVIEW`
(fail-closed). The gate is a **third explicit task execution phase (`VERIFYING`)**.

This slice **reuses Stage B's verdict primitives and the `verification` execution phase**
but does **not** reuse Stage B's workstream-shaped wire contract (see §3): the two express
different use cases (Mode-1 task-diff judge vs Mode-2 workstream-artifact-vs-criteria).

## 2. Scope & non-goals

**In scope:** Mode-1 (`maestro run`); runs only *after* a passing `validation_cmd`;
one judge (`ClaudeDiffJudge`) behind a narrow provider Protocol; durable `verification`-phase
handle + recovery; explicit (honest) cost attribution; a small *additive* generalization
of `maestro/domain/verdict.py` (task-side models only).

**Non-goals (explicit follow-ups):**
- Mode-2 workstream verification — already shipped as Stage B (`CommandVerifier`,
  `VerificationContext`, `DomainProfile`, evidence ledger, `resume`). Untouched here.
- **Unifying the two modes into one entity-agnostic verification lifecycle.** Deferred
  until a *second* shared lifecycle invariant emerges — a shared three-value verdict
  alone does not justify collapsing `CommandVerifier`/context/ledger/resume. Revisit then.
- Strict OS-level read-only isolation (mandatory sandbox). MVP is *policy isolation*
  (§7) with a `verifier.backend` seam.
- Verifier when there is no `validation_cmd`; multi-runner (opencode/arbiter); dirty
  worktree support.

## 3. Relationship to Stage B (`maestro/domain/`) — reuse boundary

**Leave Stage B unchanged:** `CommandVerifier`, `VerificationContext`, `DomainProfile`,
`ledger.py`, `resume.py`, and the existing **workstream** `VerdictDocument`/
`VerdictIdentity`/`EchoExpectations`/`evaluate_handshake` (those carry a mandatory
`workstream_id` and an artifact-vs-criteria model — a task must not masquerade as a
workstream in that contract).

**Reuse directly (genuinely entity-agnostic in `domain/verdict.py`):**
- `VerdictValue` (`PASS`/`FAIL`/`ERROR`) and `EXIT_FOR_VERDICT` (0/1/2);
- `Finding` (criterion_id, severity, evidence, author_feedback);
- the PASS/FAIL/ERROR + fail-closed interpretation rules.

**NOT reused — `HandshakeResult` is not entity-agnostic.** Its `document` field is typed
`VerdictDocument | None` — the *workstream* document — so a frozen `HandshakeResult`
cannot carry a `TaskVerdictDocument`. Add a task-side `TaskHandshakeResult`
(`outcome: VerdictValue`, `protocol_error: str | None`, `document: TaskVerdictDocument |
None`) rather than forcing task data through the workstream shape.

**Add additively to `domain/verdict.py` (task-side, parallel to the workstream models):**
- `TaskVerdictIdentity` — task-shaped identity (see §5);
- `TaskVerdictDocument` — `schema_version: Literal[2]`, `identity: TaskVerdictIdentity`,
  `verdict: VerdictValue`, `findings: list[Finding]`;
- `TaskHandshakeResult` — the task analogue of `HandshakeResult` (carries
  `TaskVerdictDocument`);
- `TaskIdentityExpectations` — the Maestro-**computed** identity the provider *seals* into
  the document (task-shaped: **no `workstream_id`, no `rework_attempt`** — #106 added
  `rework_attempt` to the *workstream* echo/handshake, confirming these must stay
  separate). This is **not** a "model echo": unlike Stage B (where an external verifier
  process re-emits Maestro's values into its own output), the Claude payload never carries
  identity/hash fields — the provider authors them (see §6).
- `evaluate_task_document(json_path, expected) -> TaskHandshakeResult` — validates the
  **sealed** document (file present, parseable, schema-valid, its identity == the
  provider-computed `TaskIdentityExpectations`) and returns `outcome = document.verdict`
  (`PASS`/`FAIL`), or `ERROR` on any problem. This is an **integrity check of the sealed
  artifact (provider binding)**, not a model echo. **It does NOT compare the verdict
  against a process exit code** — see the transport/semantic split below. Deliberately **not** named `*_handshake`: Stage B's `evaluate_handshake` is a
  *process/file* handshake where the external command's exit code is a fail-closed
  backstop (`exit == EXIT_FOR_VERDICT[verdict]`). That check is invalid for the Claude
  CLI, which exits **0 whenever it successfully returns an answer** — regardless of
  whether the model's payload says PASS or FAIL. Reusing the Stage B table would turn
  every semantic FAIL into an `ERROR`.

**Transport vs semantic split (load-bearing):**
- **Transport** (the provider, *before* document eval): the Claude CLI must exit `0` and
  not time out, else `ERROR`. This is the process-level backstop for the judge.
- **Semantic**: the strict model payload carries `PASS | FAIL`. The provider parses it,
  builds the sealed `TaskVerdictDocument`, and `evaluate_task_document` validates the
  document + the provider-bound identity — it never re-derives the verdict from Claude's
  exit code.

**Reuse the `verification` execution phase as-is:** `execution_phase="verification"` was
delivered by Stage B (migration 15, CHECK widened). Mode-1 verifier handles use it with
`entity_kind="task"`. **No new phase and no phase migration.**

## 4. Lifecycle, state machine & config

```
RUNNING → VALIDATING → VERIFYING → DONE
                          │
                          ├→ FAILED → READY (retry)  |  → NEEDS_REVIEW
                          └→ NEEDS_REVIEW            (ERROR, fail-closed)
```
- **`TaskStatus.VERIFYING`** added (`maestro/models.py`). `tasks.status` is `TEXT` with no
  CHECK → **no DB migration** for the status value.
- Transitions: `VALIDATING: {VERIFYING, DONE, FAILED, NEEDS_REVIEW}`; `VERIFYING: {DONE,
  FAILED, NEEDS_REVIEW}`. The `DONE` edge is kept for the no-verifier path; the new
  **`VALIDATING → NEEDS_REVIEW`** edge exists for an **envelope-preflight `ERROR` that
  occurs before the atomic CAS** (dirty worktree / empty scope / oversize / binary / git
  failure — §6.1), where the task is still `VALIDATING` and the judge never started.
  Verifier-disabled or no `validation_cmd` → `VALIDATING → DONE` exactly as today.
- **Atomic `VALIDATING → VERIFYING` + durable handle (no crash window).** The status
  change and the placeholder verifier handle are minted in **one CAS**, reusing
  `Database.start_execution` the way durable validation already folds `RUNNING →
  VALIDATING` into its atomic mint:
  `start_execution(expected_status="validating", running_status="verifying",
  execution_phase="verification", entity_kind="task", ...)`. After the CAS commits, the
  dispatcher fires. There is no window where the task is `VERIFYING` but the handle is
  absent.
- **Events are all emitted explicitly (with the verifier `execution_id`); `TASK_EFFECTS[
  VERIFYING]` is left EMPTY.** An entry effect fires on the status transition and cannot
  see the `execution_id` minted by the same CAS, so putting `VERIFIER_STARTED` there would
  emit it without the id (violating §10's "every verifier event carries the execution
  id"). Instead: emit `VERIFIER_STARTED` right after the atomic CAS (it now has the id),
  and `VERIFIER_PASSED/FAILED/ERROR` from the outcome-handler — alongside the dispatcher's
  normal `TASK_COMPLETED/TASK_FAILED/TASK_NEEDS_REVIEW` on the exit transitions.
  `TASK_EFFECTS` stays entry-based and single-effect (`maestro/transitions.py:29`); we do
  not extend it.

Opt-in only via an explicit `verifier:` block (absent → gate disabled, today's behavior
byte-for-byte):

```yaml
verifier:
  runner: claude            # only allowed value this slice
  model: claude-haiku-4-5   # REQUIRED (or MAESTRO_VERIFIER_MODEL)
  timeout_seconds: 120
  max_diff_bytes: 100000
  backend: local            # MVP: Literal["local"] ONLY (see §7)
```

**Model resolution — dedicated `resolve_verifier_model`, isolated from the main
precedence:** `verifier.model → MAESTRO_VERIFIER_MODEL → FAIL LOUD`. Never
`MAESTRO_CLAUDE_MODEL`, never the ordinary catalog default (either could pick an
expensive main model). The resolved model must exist in the catalog: `retired`/`unknown`
→ error; `deprecated` → at least a warning. A missing verifier model when the block is
present is a fail-loud config error at scheduler start.

## 5. Task identity & diff attribution

**`TaskVerdictIdentity` / `TaskIdentityExpectations` fields (honest, task-shaped;
provider-computed, never model-supplied):**
- `task_id` — a first-class field (NOT reusing `workstream_id`);
- `verification_run_id`, `verification_attempt` (≥1);
- `artifact` — a stable logical name: `"task-diff:<task_id>"`;
- `artifact_sha256` — SHA-256 of the **canonical scope-bounded patch envelope** (the
  exact bytes handed to the judge);
- `criteria_sha256` — SHA-256 of a **canonical JSON of the task's own fields** (there is
  no separate acceptance field on `TaskConfig`, `maestro/models.py:423`):
  `{"title", "prompt", "validation_cmd", "normalized_scope"}` with sorted keys /
  normalized scope order;
- `profile_sha256` — SHA-256 of the **judge policy**: prompt version + verdict schema +
  the pinned fake-done taxonomy (so a policy change is detectable/auditable);
- `verified_source_commit` — the worktree HEAD at verification time (provenance only);
- `verified_scope_sha256` — SHA-256 of the **canonical scope-bounded envelope** (the
  honest scope-state pin). **Not a full Git tree**: a full tree would flap when a
  *non-overlapping* task legally changes other paths in parallel, so the identity binds
  only the in-scope content the judge actually saw. (This closes the earlier
  tree-or-snapshot "OR": there is one honest choice.)

**Diff attribution invariants (all mandatory when `verifier:` is set):**
1. **Clean-worktree precondition is checked ONCE, before the FIRST dispatch** (when the
   baseline is captured) — **not** on every dispatch. After a failed attempt the tree is
   legitimately dirty; re-checking cleanliness at each dispatch would make the task
   self-block on its own first-attempt changes. A dirty tree at first dispatch →
   fail-closed config/runtime error → `NEEDS_REVIEW`, never a silent skip.
2. **`task.scope` mandatory and non-empty** (the judge input is scope-bounded).
3. **Reservation is held across the whole task lifecycle, not per-execution.** Today the
   scheduler releases the `(workdir, scope)` reservation right after execution/collect,
   *before* validation/verification (`maestro/scheduler.py:1512`) — that leaves a window
   where an overlapping task mutates the scope *between attempts* and pollutes the
   cumulative diff. For a verifier-enabled task the reservation must be **held from the
   first dispatch through validation, verification, every retry, AND `NEEDS_REVIEW`** —
   and **released only at a truly terminal outcome: `DONE` or `ABANDONED`**.
   - **`NEEDS_REVIEW` is resumable, so it must NOT release the scope.** An operator can
     re-queue `NEEDS_REVIEW → READY`; if the scope were freed on entering review, another
     task could change it and the resumed task would keep verifying against a stale
     baseline. `NEEDS_REVIEW → READY` preserves the reservation. An operator who wants to
     free the scope without finishing must move the task to `ABANDONED` first (the only
     non-`DONE` release path).
   - **Restart reconstruction**: after a crash, the held reservation must be rebuilt for
     any verifier-enabled task that has a baseline and an **unfinished, non-terminal
     status — including `READY`, `FAILED`, and `NEEDS_REVIEW`**, not only tasks with an
     *open durable handle* (today's SSH-handle-only reconstruction,
     `_reconstruct_reservations`). A between-attempts / in-review crash otherwise drops
     the reservation and reopens the pollution window.
4. **Baseline captured once** — the task-baseline git SHA is recorded at the **first**
   dispatch and never overwritten on retries (judge sees the task's cumulative
   contribution vs pre-task state). Persisted in **`tasks.verifier_baseline_sha`**
   (migration 16, nullable `ADD COLUMN`).
5. **Deterministic patch** — built `--no-ext-diff`, `core.quotepath=false`, stable path
   order/normalization; includes the tracked diff **and** a deterministic representation
   of new untracked in-scope files; deleted/binary paths listed in the manifest.
6. **Size/binary → never silent** — patch over `max_diff_bytes` or containing binary
   changes → `ERROR` (fail-closed → `NEEDS_REVIEW`).

**`TaskVerificationContext`** (the Mode-1 analogue of Stage B's `VerificationContext`):
`task_id`, `run_id`, `attempt`, `worktree`, `out_json` (Maestro-assigned), plus the
computed `artifact_sha256`/`criteria_sha256`/`profile_sha256`/`verified_source_commit`/
`verified_scope_sha256`.

## 6. Provider contract (`ClaudeDiffJudge`)

A narrow Mode-1 provider Protocol, single implementation:

```
async def verify(self, ctx: TaskVerificationContext) -> TaskHandshakeResult
```

`ClaudeDiffJudge.verify`:
1. **Envelope preflight — BEFORE the CAS, while the task is still `VALIDATING`.** Build the
   deterministic scope-bounded patch envelope + acceptance context (§5) and compute the
   identity hashes. Every *deterministic input* fault is detected here — dirty worktree,
   empty scope, patch over `max_diff_bytes`, binary changes, git failure — and yields an
   `ERROR` **while the task is still `VALIDATING`**, routed `VALIDATING → NEEDS_REVIEW`
   (§4/§9) with **only `VERIFIER_ERROR`** emitted (no `VERIFIER_STARTED` — the judge never
   started). This is exactly why the FSM needs a `VALIDATING → NEEDS_REVIEW` edge.
2. **Enter `VERIFYING` + mint the handle in one atomic CAS** (only once the envelope is
   valid): `start_execution(expected_status="validating", running_status="verifying",
   execution_phase="verification", entity_kind="task", ...)` (§4) — no window where the
   task is `VERIFYING` with no handle. **Emit `VERIFIER_STARTED`** (it now carries the
   minted `execution_id`); `update_execution_handle_launch` records the backend
   `transport_ref`. (Improves on Stage B's `CommandVerifier` self-loop CAS, which assumes
   the entity is *already* verifying.)
3. Runs `claude -p "<judge instructions + RAW-payload schema>" --output-format json
   --model <resolved>` through the execution layer (`capture_output=True`, `collect=none`,
   `cwd=<empty scratch>`, `stdin=<envelope>`, `backend=verifier.backend`). The envelope
   (task context + patch) is on **stdin**; argv carries only CLI/model/output flags — the
   diff is never in argv (ARG_MAX, process list). **The model's raw payload schema is only
   `{verdict: "pass"|"fail", findings: [...]}`** (strict, `additionalProperties: false`).
   The model is **not** asked to reproduce any control / identity / hash field.
4. **Transport check first**: CLI timed out or exited non-zero → `ERROR` (before parsing).
   Otherwise strictly validate the raw payload; then the **provider binds** the result —
   it seals a `TaskVerdictDocument` = the *provider-computed* `TaskVerdictIdentity` + the
   model's `verdict`/`findings`, writes it to `ctx.out_json`, and runs
   `evaluate_task_document(ctx.out_json, expected)` → `TaskHandshakeResult`. This is an
   **integrity check of the sealed document (provider binding), NOT a model echo**: the
   identity is authored by Maestro and never by the model, so nothing tautological is
   asked of the judge. `ERROR` is formed by the runner (timeout, launch/model/catalog
   failure, non-zero CLI, absent/garbage/schema-invalid raw payload, sealed-document
   integrity mismatch) — never by the model; the verdict comes from the payload, not
   Claude's exit code (§3 split). A valid payload always maps to `PASS`/`FAIL`.
5. **Mandatory durable finalization** — for **every** post-CAS outcome (PASS, FAIL, ERROR,
   timeout, cancellation) the verifier handle is driven through the full durable lifecycle:
   `wait → terminal → collect(none) → collected → cleanup → cleaned`.
   **`finalize_handle` is the SOLE owner of `wait()`** — the provider must not call a
   separate `handle.wait()` and then finalize as a second operation; finalization owns the
   single wait. A local judge that stops at a bare `wait()` leaves an **open execution
   handle**, and recovery would see a false unfinished run for a task that actually
   completed. Finalization runs in a `finally`/guaranteed path. **Spawn-failure
   reconciliation**: if `backend.run()` fails *after* the pre-spawn placeholder handle was
   persisted (step 2) but before/at launch, the placeholder must be reconciled (terminal→
   cleaned or explicitly failed) so no orphan placeholder lingers — same posture as the
   orchestrator's spawn→persist crash window.

Note: unlike Stage B's `CommandVerifier` (where the *external command* writes the full
verdict document itself), here the model returns only `{verdict, findings}` and the
*provider* seals the identity and evaluates the document — the same fail-closed rules,
adapted to an LLM that must never be trusted to reproduce control fields.

## 7. Read-only posture (named honestly)

MVP is **policy isolation, not OS isolation.** The judge runs in an empty scratch `cwd`
outside the project worktree, Claude tools disabled, repo path never passed;
`collect=none` so nothing it writes is applied. A bare local process *can* still read the
filesystem — so this spec does **not** claim architectural read-only for `backend: local`.

**`verifier.backend` is `Literal["local"]` in this slice.** The ordinary Docker isolator
does not add `docker run -i` (`maestro/execution/isolators.py:124`), so the **stdin
envelope would never reach a container** — the current isolator cannot carry the judge's
input. A strict Docker sandbox is a real follow-up but needs its own verifier-oriented
mount + stdin contract (bind only scratch/input, pipe the envelope in), not just flipping
a config value. The `verifier.backend` field exists as the seam; MVP validates it to
`"local"` and rejects anything else, so the config shape survives to the sandbox slice.

## 8. Durability & recovery

Durable, own handle (`entity_kind="task"`, `execution_phase="verification"`). A task
stranded in **`VERIFYING`** selects the verifier handle. **Any stranded verifier →
`NEEDS_REVIEW`** (fail-closed), whether the judge is live, dead, or state-unclear. The MVP
deliberately has **no auto-re-run** even for a proven-dead judge: re-dispatching a task
that is *already* in `VERIFYING` would need its own scheduler dispatch path (the main loop
only dispatches `READY` tasks), which this slice does not define. The conservative rule is
simpler and safe — the operator re-queues from `NEEDS_REVIEW` (which, per §5.3, preserves
the scope reservation). GC follows the existing transport-aware path. (A future
auto-re-run fast-path is possible once a VERIFYING-redispatch path exists — see §12.)

**Requeue handle-fence (else recovery is fail-closed only until the first `approve`).**
A verifier-originated `NEEDS_REVIEW → READY` re-queue must be **gated on the verification
handle being reconciled to a terminal/cleaned state first**. Otherwise, if the judge was
live/state-unclear, a plain re-queue would start a fresh task attempt while a judge
subprocess (and its open handle) is still around:
- the operator action (`approve`/re-queue) **fails closed while the verification handle is
  still open** — it is allowed only after the handle is terminal→cleaned;
- a live/unclear judge is first terminated/killed **or** explicitly operator-acknowledged,
  then GC-cleaned under the guarded path;
- the `(workdir, scope)` reservation is **held throughout** (§5.3), so no overlapping task
  can enter the scope during reconciliation.

## 9. Verdict routing & error taxonomy (fail-closed)

The originating state matters: **envelope-preflight** faults happen *before* the CAS (task
still `VALIDATING`, judge never started → only `VERIFIER_ERROR`); everything else happens
*after* the CAS (task `VERIFYING`, `VERIFIER_STARTED` already emitted).

| Condition | From | Outcome | Routing / events |
|---|---|---|---|
| dirty worktree / empty scope / diff > `max_diff_bytes` / binary / git failure (envelope preflight, §6.1) | `VALIDATING` | ERROR | `VALIDATING → NEEDS_REVIEW`; `VERIFIER_ERROR` only (no `STARTED`) |
| valid payload, `pass` | `VERIFYING` | PASS | `VERIFYING → DONE`; `VERIFIER_PASSED` |
| valid payload, `fail` | `VERIFYING` | FAIL | `VERIFYING → FAILED` → existing retry context; `findings[].author_feedback` folded into retry feedback; `VERIFIER_FAILED` |
| timeout / launch / model-unresolved / catalog error | `VERIFYING` | ERROR | `VERIFYING → NEEDS_REVIEW`; `VERIFIER_ERROR` |
| non-zero CLI, absent/garbage/schema-invalid raw payload, sealed-document integrity mismatch | `VERIFYING` | ERROR | `VERIFYING → NEEDS_REVIEW`; `VERIFIER_ERROR` |

ERROR is never masked as a substantive `FAIL` (an infra fault is not "the code is wrong").

## 10. Cost & observability — schema extension (measuring judge cheapness is a goal)

Measuring the judge's actual cheapness is an **acceptance criterion** of this slice, so
the cost cut must be real, not overclaimed. Two current-schema facts make a naive claim
false:
- `ExecutionRequest` does **not** create a cost row on its own; cost is written by the
  scheduler's `_record_cost`.
- `TaskCost.agent_type` is the **closed `AgentType` enum** (`maestro/models.py:905`) that
  also defines the allowed *task harnesses* — so a `"verifier:claude"` marker cannot be
  added there without polluting harness identity. And the cost roll-up **sums all rows of
  one `(task_id, attempt)`** (`maestro/scheduler.py:469`), so a plain extra `CLAUDE_CODE`
  row would be **merged into the author attempt's cost** — a "not merged" claim would be
  false.

**MVP choice — extend storage (first option) AND define the read-side/consumer split.**

*Storage (migration 17):* add **`task_costs.execution_phase`** (`'task' | 'validation' |
'verification'`, default `'task'`) and **`task_costs.model`** (nullable TEXT). The verifier
provider parses the Claude JSON `usage`/cost and writes a judge cost row with
`execution_phase='verification'`, `agent_type=CLAUDE_CODE` (honest — the judge *is*
Claude), and `model=<resolved verifier model>`.

*Consumers (a storage column alone does not create a cut — `TaskOutcome` still carries a
single `cost_usd`, so `_build_outcome` cannot express the split by itself):*
- **Arbiter outcome** → the **full actual attempt spend** (author + validation +
  verification summed). If **any** component's cost is unknown, the total is `UNKNOWN`
  (never silently treated as `$0`). The judge cost is *included*, not hidden.
- **`maestro costs` (read-side)** → gains **group-by `execution_phase`** and
  **group-by `model`** breakdowns. *These* are what prove the judge is cheap — the
  per-phase/per-model cut lives in the reporting query, not in `TaskOutcome`.
- **Existing totals** → keep summing all rows (no lost judge cost; back-compatible).
- **`execution_phase`/`model` are NOT a dedup key.** Two verifier runs of the same phase
  (e.g. a retry) are **two real spends** and both count. If write-idempotency is ever
  needed, it must key on the verifier **`execution_id`** (or a dedicated unique cost
  identity), never on `(task_id, attempt, phase)` alone.
- **Absent usage in an otherwise-correct response → cost `UNKNOWN`, not `$0`**
  (`reported_cost_usd=None`, same convention as opencode/arbiter).

Verifier lifecycle events (`VERIFIER_STARTED/PASSED/FAILED/ERROR`) are recorded in the
event log, correlated by `task_id` + the verifier `execution_id`.

## 11. Migrations & touched components

- **Migration 16** — `ALTER TABLE tasks ADD COLUMN verifier_baseline_sha TEXT` (nullable;
  same shape as migration 11). *(No `execution_phase` migration — `"verification"` already
  shipped in Stage B migration 15.)*
- **Migration 17** — `ALTER TABLE task_costs ADD COLUMN execution_phase TEXT NOT NULL
  DEFAULT 'task'` + `ADD COLUMN model TEXT` (for the phase-keyed judge cost cut, §10).
- `maestro/domain/verdict.py` — **additive** `TaskVerdictIdentity`, `TaskVerdictDocument`,
  `TaskHandshakeResult`, `TaskIdentityExpectations`, `evaluate_task_document` (Stage B
  models untouched).
- `maestro/verifier/` (new) — `TaskVerificationContext`, the provider Protocol,
  `ClaudeDiffJudge`, `resolve_verifier_model`, deterministic patch/manifest builder,
  the VerifierInput envelope + Claude-output → `TaskVerdictDocument` mapping, the transport
  check, and the durable finalize + spawn-failure reconciliation (§6.5).
- `maestro/models.py` — `TaskStatus.VERIFYING`; transitions
  `VALIDATING: {VERIFYING, DONE, FAILED, NEEDS_REVIEW}` (the `NEEDS_REVIEW` edge is for the
  pre-CAS envelope-preflight ERROR, §4/§6.1) and `VERIFYING: {DONE, FAILED, NEEDS_REVIEW}`;
  `VerifierConfig` (`backend: Literal["local"]`, no `idempotent`); `Task.verifier_baseline_sha`.
- `maestro/scheduler.py` — `_handle_task_completion` runs the **envelope preflight while
  still `VALIDATING`** (ERROR → `VALIDATING → NEEDS_REVIEW`), then the **atomic
  `VALIDATING → VERIFYING` CAS** (§4/§6); first-dispatch clean check + baseline capture;
  **lifecycle-scoped reservation** (hold through validation/verification/retries/
  `NEEDS_REVIEW`, release only at `DONE`/`ABANDONED`; restart reconstruction incl.
  `READY`/`FAILED`/`NEEDS_REVIEW`, §5.3); the verifier cost row; explicit
  `VERIFIER_STARTED` (post-CAS) + `VERIFIER_PASSED/FAILED/ERROR` emits; recovery for
  `VERIFYING` (any strand → `NEEDS_REVIEW`, §8); `finalize_handle` as the sole `wait()`
  owner (§6.5).
- **Requeue handle-fence (§8)** — the `approve`/re-queue path (`maestro approve` /
  `NEEDS_REVIEW → READY`) must reject a verifier-originated re-queue while the verification
  handle is still open; allowed only after terminal→cleaned reconciliation.
- `maestro/database.py` — migrations 16 + 17; `verifier_baseline_sha` CRUD; `execution_phase`
  /`model` on cost writes.
- **Cost consumers (§10)** — the arbiter-outcome cost path sums author+validation+
  verification (unknown-propagating); `maestro costs` read-side gains group-by
  `execution_phase` and `model`.
- `maestro/config.py` — parse/validate the `verifier:` block (reject `backend != "local"`).
- `maestro/event_log.py` — the four verifier events.
- `maestro/transitions.py` — **`TASK_EFFECTS[VERIFYING]` left EMPTY** (§4; events are
  emitted explicitly so they carry the `execution_id`).

## 12. Open questions / future work

- **Mode-1/Mode-2 convergence (variant 2)** — revisit only after a *second* shared
  lifecycle invariant appears; then consider generalizing `VerificationContext`/provider
  to entity-agnostic and collapsing the task/workstream verdict models.
- **Strict OS isolation** — a Docker `verifier.backend` with its own mount + `docker run
  -i` stdin contract (the ordinary isolator can't pipe the envelope; §7).
- **Dirty-worktree support** — a persisted content snapshot (not a bare SHA).
- **Auto-re-run of a proven-dead judge** — needs a scheduler dispatch path for tasks
  *already* in `VERIFYING` (the main loop only dispatches `READY`); once that exists, a
  pure read→verdict judge can safely fast-path re-run instead of `NEEDS_REVIEW` (§8).
- **Multi-runner** — `OpenCodeJudge` (open models), arbiter role-routing (#5).
- **Pinned fake-done taxonomy** — the enum list + definitions, in-repo, hashed into
  `profile_sha256`.
