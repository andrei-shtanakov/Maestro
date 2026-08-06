"""Gates-in-DAG runtime (steward WS-006 handoff, items M-1..M-3).

Guard hooks for two workstream transition edges: **ex-ante** before
READY -> RUNNING (classify the declared ``workstream.scope``) and **ex-post**
before RUNNING -> MERGING (classify the actual diff, catch scope violations).
Tiers come from ``steward risk-classify`` — a single source of truth; Maestro
never computes risk itself (DESIGN-610/612).

Semantics (DESIGN-606, fail-closed): a mandatory gate with a missing or
errored verdict blocks the transition — at every tier. A blocked workstream
routes to NEEDS_REVIEW with an approval marker in ``error_message``; an
operator approves via ``maestro workstream-approve``, which records the
(workstream, phase, sha) approval durably (see Gates v1.3 below). A new
commit changes the SHA and invalidates the approval (DESIGN-608, M-3).
Gates v1.2 (H-6/H-7): an approved ex-post block resumes at the ex-post edge
(see orchestrator._try_resume_ex_post); the infra-path exclusion covers only
maestro-prefixed artifacts, and the approval marker in error_message clears
only at DONE. Every evaluation appends a verdict-record to
``logs/<ULID>/gate_verdicts.jsonl`` (M-1); records are addressable via
EvidenceRef ``kind=gate-verdict``. Gates whose enforcement point lies outside
these two edges (branch protection, PR reviews) are recorded as advisory
annotations, not blocks (M-2) — their transition belongs to the git host, not
to Maestro's table. Gates v1.3 (H-9): the ``gate_approvals`` DB table (written
by ``maestro workstream-approve`` in one transaction) is the single authority
for "was this (workstream, phase, sha) approved". The marker in
``error_message`` is operator UX and the H-6 resume-position signal only; the
verdict store is pure evidence — neither grants approval.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from maestro._vendor.obs import current_pipeline_id
from maestro.changed_paths import changed_paths_since
from maestro.gate_approvals import (
    APPROVAL_MARKER_PREFIX,
    BLOCK_REASON_PREFIX,
    ApprovalMarker,
    build_approval_marker,
    parse_approval_marker,
    preserve_approval_marker,
)


if TYPE_CHECKING:
    from maestro.models import GatesConfig


__all__ = [
    "APPROVAL_MARKER_PREFIX",
    "BLOCK_REASON_PREFIX",
    "ApprovalMarker",
    "GateDecision",
    "GateKeeper",
    "GateVerdictRecord",
    "parse_approval_marker",
    "pipeline_log_dir",
    "preserve_approval_marker",
]

_STEWARD_ENV = "MAESTRO_STEWARD_BIN"

# Gates steward may demand that are enforced beyond Maestro's two edges:
# recorded as advisory annotations here, enforced by the git host / CI there.
_DEFERRED_GATES = {
    "git.required_reviews": "enforced at the PR stage (branch protection)",
    "human.transition_approval": "enforced at the PR stage (human merge)",
    "steward.gate_check": "runs in the target repo's CI, not at this edge",
}


class GateVerdictRecord(BaseModel):
    """One appended line of logs/<ULID>/gate_verdicts.jsonl (DESIGN-607).

    This is Maestro's INTERNAL run-level audit format
    (`maestro.gate-verdict-record/v1`) — not steward's canonical
    `contracts/gate-verdicts/v1` findings-bundle. The explicit `schema`
    discriminator on every line keeps the two from being mistaken for
    one contract (approver spec §7.3). `not_run` marks an approver
    observation: the evaluator did not run and the gate still blocks —
    semantically neutral, never a waiver.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    record_schema: str = Field(
        default="maestro.gate-verdict-record/v1",
        alias="schema",
        serialization_alias="schema",
    )
    gate_id: str
    obligation: Literal["mandatory", "advisory"]
    verdict: Literal["pass", "fail", "waived", "error", "missing", "not_run"]
    tier: str | None = None
    phase: Literal["ex_ante", "ex_post"]
    sha: str | None = None
    risk_model_version: str | None = None
    ts: str
    workstream_id: str
    note: str | None = None


class GateDecision(BaseModel):
    """Outcome of one guard evaluation.

    `tier`/`flags`/`paths` carry the classification data the approver
    hook persists at block time (`gate_block_contexts`, #137 §7.1) —
    additive, unset on classification errors.
    """

    allow: bool
    reason: str | None = None
    records: list[GateVerdictRecord] = Field(default_factory=list)
    tier: str | None = None
    flags: list[str] = Field(default_factory=list)
    paths: list[str] | None = None


def pipeline_log_dir() -> Path:
    """The pipeline's logs/<ULID>/ directory (mirrors vendored obs logic)."""
    env_dir = os.environ.get("ORCHESTRA_LOG_DIR")
    if env_dir:
        return Path(env_dir)
    pipeline = (
        current_pipeline_id()
        or os.environ.get("ORCHESTRA_PIPELINE_ID")
        or "no-pipeline"
    )
    return Path.cwd() / "logs" / pipeline


def _validated_classification(decoded: object) -> dict | str:
    """Shape-check the risk-classify output (DESIGN-610) — fail-closed.

    JSON that parses but lacks the contract shape must become an error
    verdict, not a KeyError escaping the guard.
    """
    if not isinstance(decoded, dict):
        return f"risk-classify output is not an object: {type(decoded).__name__}"
    tier = decoded.get("tier")
    if not isinstance(tier, str) or not tier:
        return "risk-classify output missing 'tier'"
    gates = decoded.get("mandatory_gates", [])
    flags = decoded.get("flags", [])
    if not isinstance(gates, list) or not all(isinstance(g, str) for g in gates):
        return "risk-classify output: 'mandatory_gates' must be a list of strings"
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        return "risk-classify output: 'flags' must be a list of strings"
    return decoded


class GateKeeper:
    """Evaluates gates at transition edges and persists verdict-records."""

    def __init__(
        self,
        config: GatesConfig,
        *,
        project: str,
        repo_path: Path,
        base_branch: str,
        log_dir: Path,
    ) -> None:
        self._config = config
        self._project = project
        self._repo_path = repo_path
        self._base_branch = base_branch
        self._log_dir = log_dir

    # ------------------------------------------------------------ public

    async def evaluate_ex_ante(
        self,
        workstream_id: str,
        scope: list[str],
        approvals: set[tuple[str, str]],
    ) -> GateDecision:
        """Guard for READY -> RUNNING: classify the declared scope (REQ-605)."""
        sha = await self._git_sha(self._repo_path)
        payload = {"project": self._project, "sha": sha, "scope": scope}
        classification = await self._classify("--declared", payload)
        return self._decide("ex_ante", workstream_id, sha, classification, approvals)

    async def evaluate_ex_post(
        self,
        workstream_id: str,
        scope: list[str],
        workspace: Path,
        approvals: set[tuple[str, str]],
    ) -> GateDecision:
        """Guard for RUNNING -> MERGING: classify the actual diff vs base."""
        sha = await self._git_sha(workspace)
        paths = await changed_paths_since(self._base_branch, "HEAD", workspace)
        payload = {
            "project": self._project,
            "sha": sha,
            "paths": paths,
            "declared_scope": scope,
        }
        classification = await self._classify("--no-fs", payload)
        decision = self._decide(
            "ex_post", workstream_id, sha, classification, approvals
        )
        decision.paths = paths
        return decision

    # ------------------------------------------------------------ decision

    def _decide(
        self,
        phase: Literal["ex_ante", "ex_post"],
        workstream_id: str,
        sha: str,
        classification: dict | str,
        approvals: set[tuple[str, str]],
    ) -> GateDecision:
        ts = datetime.now(UTC).isoformat()

        def record(
            gate_id: str,
            obligation: Literal["mandatory", "advisory"],
            verdict: Literal["pass", "fail", "waived", "error", "missing"],
            tier: str | None = None,
            risk_model_version: str | None = None,
            note: str | None = None,
        ) -> GateVerdictRecord:
            return GateVerdictRecord(
                gate_id=gate_id,
                obligation=obligation,
                verdict=verdict,
                tier=tier,
                risk_model_version=risk_model_version,
                note=note,
                phase=phase,
                sha=sha,
                ts=ts,
                workstream_id=workstream_id,
            )

        classify_gate = f"steward.risk_classify_{phase}"
        if isinstance(classification, str):  # error text — fail-closed
            records = [
                record(
                    gate_id=classify_gate,
                    obligation="mandatory",
                    verdict="error",
                    note=classification,
                )
            ]
            self._write(records)
            return GateDecision(
                allow=False,
                reason=f"gates: {classify_gate} error: {classification}",
                records=records,
            )

        tier = classification["tier"]
        model_version = classification.get("risk_model_version")
        flags = classification.get("flags", [])
        records = [
            record(
                gate_id=classify_gate,
                obligation="mandatory",
                verdict="pass",
                tier=tier,
                risk_model_version=model_version,
                note=(", ".join(flags) or None),
            )
        ]

        # Advisory annotations for gates enforced beyond these edges (M-2).
        for gate_id in classification.get("mandatory_gates", []):
            if gate_id in _DEFERRED_GATES:
                records.append(
                    record(
                        gate_id=gate_id,
                        obligation="advisory",
                        verdict="missing",
                        tier=tier,
                        risk_model_version=model_version,
                        note=_DEFERRED_GATES[gate_id],
                    )
                )
            elif gate_id == "maestro.validate_strict":
                records.append(
                    record(
                        gate_id=gate_id,
                        obligation="mandatory",
                        verdict="pass",
                        tier=tier,
                        risk_model_version=model_version,
                        note="preflight ran at orchestrator startup",
                    )
                )

        blocked_reason: str | None = None
        needs_approval = (
            tier in self._config.approval_tiers or "scope_violation" in flags
        )
        if needs_approval:
            marker = build_approval_marker(phase, sha)
            # gates v1.3 (H-9): the DB approvals set is the single authority;
            # the marker in the block message is operator UX + the H-6
            # position signal, never authorization.
            approved = (phase, sha) in approvals
            if approved:
                records.append(
                    record(
                        gate_id="human.owner_approval",
                        obligation="mandatory",
                        verdict="pass",
                        tier=tier,
                        risk_model_version=model_version,
                        note="approval recorded in gate_approvals (same sha)",
                    )
                )
            else:
                detail = (
                    f"scope violation ({', '.join(flags)})"
                    if "scope_violation" in flags
                    else f"tier={tier}"
                )
                records.append(
                    record(
                        gate_id="human.owner_approval",
                        obligation="mandatory",
                        verdict="missing",
                        tier=tier,
                        risk_model_version=model_version,
                        note=detail,
                    )
                )
                blocked_reason = (
                    f"{BLOCK_REASON_PREFIX} ({detail}); re-queue to approve. {marker}"
                )

        self._write(records)
        if blocked_reason is not None:
            return GateDecision(
                allow=False,
                reason=blocked_reason,
                records=records,
                tier=tier,
                flags=list(flags),
            )
        return GateDecision(allow=True, records=records, tier=tier, flags=list(flags))

    # ------------------------------------------------------------ steward

    def _resolve_bin(self) -> str | None:
        candidate = self._config.steward_bin or os.environ.get(_STEWARD_ENV)
        if not candidate:
            return None
        path = Path(candidate).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            return None
        return str(path)

    async def _classify(self, input_flag: str, payload: dict) -> dict | str:
        """Run `steward risk-classify`; return the parsed JSON or an error string."""
        binary = self._resolve_bin()
        if binary is None:
            return f"steward binary not found (gates.steward_bin or ${_STEWARD_ENV})"
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as handle:
            json.dump(payload, handle)
            input_path = handle.name
        cmd = [
            binary,
            "risk-classify",
            input_flag,
            input_path,
            "--profile",
            self._config.profile,
        ]
        if self._config.risk_model:
            cmd += ["--risk-model", self._config.risk_model]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                return (
                    f"risk-classify exited {process.returncode}: "
                    f"{stderr.decode(errors='replace').strip()[:500]}"
                )
            decoded = json.loads(stdout.decode())
            return _validated_classification(decoded)
        except (OSError, json.JSONDecodeError) as exc:
            return f"risk-classify failed: {exc}"
        finally:
            await asyncio.to_thread(Path(input_path).unlink, True)

    # ------------------------------------------------------------ git + store

    async def _git_sha(self, repo: Path) -> str:
        out = await self._git(repo, "rev-parse", "HEAD")
        return out.strip()

    @staticmethod
    async def _git(repo: Path, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            msg = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"git {' '.join(args)} failed: {msg}")
        return stdout.decode()

    def append_records(self, records: list[GateVerdictRecord]) -> None:
        """Append externally-built records (e.g. approver evidence, #137)."""
        self._write(records)

    def _write(self, records: list[GateVerdictRecord]) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        path = self._log_dir / "gate_verdicts.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for rec in records:
                handle.write(
                    json.dumps(rec.model_dump(by_alias=True, exclude_none=True)) + "\n"
                )
