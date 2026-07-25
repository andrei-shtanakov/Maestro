"""Evidence ledger: durable attempt store + delivery materialization (§5, §6, §8).

Every verification attempt — PASS, FAIL, or protocol ERROR — is ingested as
append-only evidence: the verifier writes its bundle (`attempt-NNN.json` plus
optional `.md`/`.raw.txt` sidecars) into a per-attempt staging directory, and
`EvidenceLedger.ingest_attempt` moves that bundle under the ledger root and
indexes it in the `verification_attempts` table (Task 3). The root lives
beside the database (`db_path.parent / "evidence"`), never inside a
workstream's worktree, so the author cannot see it until `materialize` copies
the bundle in for the evidence commit (§7, §8).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from maestro.domain.verdict import HandshakeResult, VerdictValue


if TYPE_CHECKING:
    # Deferred: maestro.database imports maestro.models, which imports
    # maestro.domain (this package) for DomainProfile — a module-level
    # import here would be circular. The annotation-only reference is safe.
    from maestro.database import Database


class LedgerCollisionError(Exception):
    """Raised when ingest/materialize would overwrite diverging evidence.

    Evidence is append-only: an existing `attempt-NNN.json` (or an
    already-materialized file with different bytes) is never silently
    overwritten.
    """


class IngestedAttempt(BaseModel):
    """One evidence-ledger row: an ingested attempt bundle + its DB index.

    `anomaly` is derived (not persisted as its own column) — it is `True`
    whenever either sidecar file (`.md` or `.raw.txt`) is missing, which is
    a protocol-adjacent oddity but never a reason to reject the attempt
    (§5 sidecar rule).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    attempt: int
    workstream_id: str
    verdict: VerdictValue
    protocol_error: str | None
    artifact_sha256: str | None
    json_path: Path
    md_path: Path | None
    raw_path: Path | None
    materialized: bool
    anomaly: bool


def _attempt_stem(attempt: int) -> str:
    """Zero-padded 3-digit attempt stem, e.g. `attempt-001`."""
    return f"attempt-{attempt:03d}"


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    """Write `data` to `dest` atomically: tmp sibling, then an atomic rename.

    `Path.replace` wraps `os.replace` under the hood — same atomic-rename
    guarantee, ruff-preferred spelling (PTH105).
    """
    tmp_path = dest.with_name(f"{dest.name}.tmp")
    tmp_path.write_bytes(data)
    tmp_path.replace(dest)


def _move_atomic(src: Path, dest: Path) -> None:
    """Atomically place `src`'s bytes at `dest`, then remove `src`."""
    _atomic_write_bytes(dest, src.read_bytes())
    src.unlink()


class EvidenceLedger:
    """Durable store for verification-attempt evidence bundles.

    Layout: `<root>/<workstream_id>/<run_id>/attempt-NNN.json|.md|.raw.txt`.
    The DB (`verification_attempts`) is the index; the files are the
    evidence of record.
    """

    def __init__(self, db: Database, root: Path) -> None:
        """Bind the ledger to its DB index and its on-disk root directory."""
        self._db = db
        self._root = root

    def staging_dir(self, workstream_id: str, run_id: str, attempt: int) -> Path:
        """Return (creating if needed) the staging dir for one attempt.

        The verifier writes `<staging_dir>/attempt-NNN.json` (and optional
        sidecars) here; `ingest_attempt` later moves that bundle under the
        durable root.
        """
        path = self._root / workstream_id / run_id / f".staging-{attempt:03d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def ingest_attempt(
        self,
        *,
        workstream_id: str,
        run_id: str,
        attempt: int,
        result: HandshakeResult,
        staging: Path,
    ) -> IngestedAttempt:
        """Move one attempt bundle from staging under the ledger root.

        Ingests every outcome — PASS, FAIL, and protocol ERROR alike — as
        append-only evidence. Refuses to overwrite an existing
        `attempt-NNN.json` (raises `LedgerCollisionError`); missing `.md`/
        `.raw.txt` sidecars are recorded as `anomaly=True` on the returned
        row rather than raised.
        """
        stem = _attempt_stem(attempt)
        dest_dir = self._root / workstream_id / run_id
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest_json = dest_dir / f"{stem}.json"
        if dest_json.exists():
            msg = (
                f"attempt {attempt} for run {run_id!r} is already ingested "
                f"at {dest_json}"
            )
            raise LedgerCollisionError(msg)

        src_json = staging / f"{stem}.json"
        _move_atomic(src_json, dest_json)

        dest_md = self._move_optional_sidecar(
            staging / f"{stem}.md", dest_dir, stem, ".md"
        )
        dest_raw = self._move_optional_sidecar(
            staging / f"{stem}.raw.txt", dest_dir, stem, ".raw.txt"
        )

        artifact_sha256 = (
            result.document.identity.artifact_sha256 if result.document else None
        )

        await self._db.insert_verification_attempt(
            run_id=run_id,
            attempt=attempt,
            workstream_id=workstream_id,
            verdict=result.outcome,
            json_path=str(dest_json),
            protocol_error=result.protocol_error,
            artifact_sha256=artifact_sha256,
            md_path=str(dest_md) if dest_md else None,
            raw_path=str(dest_raw) if dest_raw else None,
        )

        return IngestedAttempt(
            run_id=run_id,
            attempt=attempt,
            workstream_id=workstream_id,
            verdict=result.outcome,
            protocol_error=result.protocol_error,
            artifact_sha256=artifact_sha256,
            json_path=dest_json,
            md_path=dest_md,
            raw_path=dest_raw,
            materialized=False,
            anomaly=dest_md is None or dest_raw is None,
        )

    @staticmethod
    def _move_optional_sidecar(
        src: Path, dest_dir: Path, stem: str, suffix: str
    ) -> Path | None:
        """Move an optional sidecar file if present; `None` if missing."""
        if not src.is_file():
            return None
        dest = dest_dir / f"{stem}{suffix}"
        _move_atomic(src, dest)
        return dest

    async def list_bundle(self, run_id: str) -> list[IngestedAttempt]:
        """List every ingested attempt of a run, oldest first."""
        rows = await self._db.list_verification_attempts(run_id)
        return [
            IngestedAttempt(
                run_id=row.run_id,
                attempt=row.attempt,
                workstream_id=row.workstream_id,
                verdict=VerdictValue(row.verdict),
                protocol_error=row.protocol_error,
                artifact_sha256=row.artifact_sha256,
                json_path=Path(row.json_path),
                md_path=Path(row.md_path) if row.md_path else None,
                raw_path=Path(row.raw_path) if row.raw_path else None,
                materialized=row.materialized,
                anomaly=row.md_path is None or row.raw_path is None,
            )
            for row in rows
        ]

    async def materialize(
        self, *, run_id: str, worktree: Path, evidence_root: str
    ) -> list[Path]:
        """Copy every ingested attempt of a run into the worktree.

        Writes `<worktree>/<evidence_root>/<run_id>/attempt-NNN.*` for all
        files of every attempt and returns the full list of paths (created
        or already present). Idempotent: an existing destination file with
        identical bytes is left alone; one with different bytes raises
        `LedgerCollisionError` rather than silently overwriting evidence.
        """
        bundle = await self.list_bundle(run_id)
        dest_dir = worktree / evidence_root / run_id
        dest_dir.mkdir(parents=True, exist_ok=True)

        created: list[Path] = []
        for item in bundle:
            stem = _attempt_stem(item.attempt)
            for src, suffix in (
                (item.json_path, ".json"),
                (item.md_path, ".md"),
                (item.raw_path, ".raw.txt"),
            ):
                if src is None:
                    continue
                dest = dest_dir / f"{stem}{suffix}"
                self._materialize_one(src, dest)
                created.append(dest)
        return created

    @staticmethod
    def _materialize_one(src: Path, dest: Path) -> None:
        """Copy `src` to `dest`, tolerating byte-identical re-runs."""
        data = src.read_bytes()
        if dest.exists():
            if dest.read_bytes() == data:
                return
            msg = f"materialized evidence at {dest} diverges from the ledger"
            raise LedgerCollisionError(msg)
        _atomic_write_bytes(dest, data)

    async def mark_materialized(self, run_id: str) -> None:
        """Mark every ingested attempt of a run as materialized."""
        await self._db.mark_attempts_materialized(run_id)

    def latest_fail(self, bundle: list[IngestedAttempt]) -> IngestedAttempt | None:
        """Return the highest-attempt FAIL row of `bundle`, or `None`."""
        fails = [item for item in bundle if item.verdict == VerdictValue.FAIL]
        if not fails:
            return None
        return max(fails, key=lambda item: item.attempt)
