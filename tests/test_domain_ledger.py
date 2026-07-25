"""Tests for the evidence ledger (Stage B, Task 5).

`EvidenceLedger` is the durable attempt store: it moves verifier-written
bundles from a per-attempt staging directory into the on-disk ledger root
(indexed via the Task-3 `verification_attempts` table), and later
materializes the full bundle into a workstream's worktree for the evidence
commit.
"""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from maestro.database import Database, create_database
from maestro.domain.ledger import EvidenceLedger, IngestedAttempt, LedgerCollisionError
from maestro.domain.verdict import (
    Finding,
    HandshakeResult,
    VerdictDocument,
    VerdictIdentity,
    VerdictValue,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def db(temp_db_path: Path) -> AsyncGenerator[Database, None]:
    """Provide a connected and initialized database."""
    database = await create_database(temp_db_path)
    yield database
    await database.close()


@pytest.fixture
def ledger_root(temp_dir: Path) -> Path:
    """Provide the ledger root directory (beside the DB, per the design)."""
    return temp_dir / "evidence"


@pytest.fixture
def ledger(db: Database, ledger_root: Path) -> EvidenceLedger:
    return EvidenceLedger(db, ledger_root)


def _document(
    *,
    run_id: str = "run-1",
    attempt: int = 1,
    workstream_id: str = "w1",
    verdict: VerdictValue = VerdictValue.PASS,
    artifact_sha256: str = "a" * 64,
    findings: list[Finding] | None = None,
) -> VerdictDocument:
    return VerdictDocument(
        schema_version=2,
        identity=VerdictIdentity(
            verification_run_id=run_id,
            verification_attempt=attempt,
            rework_attempt=0,
            workstream_id=workstream_id,
            artifact="artifact.md",
            artifact_sha256=artifact_sha256,
            criteria_sha256="b" * 64,
            profile_sha256="c" * 64,
            verified_source_commit="d" * 40,
            verified_source_tree="e" * 40,
        ),
        verdict=verdict,
        findings=findings or [],
    )


def _pass_result() -> HandshakeResult:
    return HandshakeResult(
        outcome=VerdictValue.PASS,
        protocol_error=None,
        document=_document(verdict=VerdictValue.PASS),
    )


def _fail_result() -> HandshakeResult:
    return HandshakeResult(
        outcome=VerdictValue.FAIL,
        protocol_error=None,
        document=_document(
            verdict=VerdictValue.FAIL,
            findings=[
                Finding(
                    criterion_id="c1",
                    severity="major",
                    evidence="ev",
                    author_feedback="fix it",
                )
            ],
        ),
    )


def _error_result(message: str = "verifier process timed out") -> HandshakeResult:
    return HandshakeResult(
        outcome=VerdictValue.ERROR, protocol_error=message, document=None
    )


def _write_bundle(
    staging: Path,
    attempt: int,
    *,
    json_body: str = "{}",
    with_md: bool = True,
    with_raw: bool = True,
) -> None:
    nnn = f"{attempt:03d}"
    (staging / f"attempt-{nnn}.json").write_text(json_body)
    if with_md:
        (staging / f"attempt-{nnn}.md").write_text("# report\n")
    if with_raw:
        (staging / f"attempt-{nnn}.raw.txt").write_text("raw output\n")


# =============================================================================
# staging_dir
# =============================================================================


class TestStagingDir:
    def test_creates_and_returns_per_attempt_directory(
        self, ledger: EvidenceLedger
    ) -> None:
        path = ledger.staging_dir("w1", "run-1", 1)
        assert path.is_dir()

    def test_distinct_attempts_get_distinct_directories(
        self, ledger: EvidenceLedger
    ) -> None:
        p1 = ledger.staging_dir("w1", "run-1", 1)
        p2 = ledger.staging_dir("w1", "run-1", 2)
        assert p1 != p2


# =============================================================================
# ingest_attempt
# =============================================================================


class TestIngestAttempt:
    @pytest.mark.anyio
    async def test_ingests_pass_and_indexes_layout(
        self, ledger: EvidenceLedger, ledger_root: Path
    ) -> None:
        staging = ledger.staging_dir("w1", "run-1", 1)
        _write_bundle(staging, 1)

        ingested = await ledger.ingest_attempt(
            workstream_id="w1",
            run_id="run-1",
            attempt=1,
            result=_pass_result(),
            staging=staging,
        )

        assert ingested.verdict == VerdictValue.PASS
        assert ingested.attempt == 1
        assert ingested.workstream_id == "w1"
        assert ingested.anomaly is False

        expected_json = ledger_root / "w1" / "run-1" / "attempt-001.json"
        expected_md = ledger_root / "w1" / "run-1" / "attempt-001.md"
        expected_raw = ledger_root / "w1" / "run-1" / "attempt-001.raw.txt"
        assert expected_json.is_file()
        assert expected_md.is_file()
        assert expected_raw.is_file()
        assert ingested.json_path == expected_json
        assert ingested.md_path == expected_md
        assert ingested.raw_path == expected_raw

    @pytest.mark.anyio
    async def test_ingests_every_outcome_including_protocol_error(
        self, ledger: EvidenceLedger, ledger_root: Path
    ) -> None:
        staging = ledger.staging_dir("w1", "run-1", 1)
        _write_bundle(staging, 1, with_md=False, with_raw=False)

        ingested = await ledger.ingest_attempt(
            workstream_id="w1",
            run_id="run-1",
            attempt=1,
            result=_error_result("verifier process timed out or crashed"),
            staging=staging,
        )

        assert ingested.verdict == VerdictValue.ERROR
        assert ingested.protocol_error == "verifier process timed out or crashed"
        assert ingested.artifact_sha256 is None
        assert (ledger_root / "w1" / "run-1" / "attempt-001.json").is_file()

    @pytest.mark.anyio
    async def test_refuses_to_overwrite_existing_attempt_json(
        self, ledger: EvidenceLedger
    ) -> None:
        staging = ledger.staging_dir("w1", "run-1", 1)
        _write_bundle(staging, 1, json_body='{"first": true}')
        await ledger.ingest_attempt(
            workstream_id="w1",
            run_id="run-1",
            attempt=1,
            result=_pass_result(),
            staging=staging,
        )

        staging2 = ledger.staging_dir("w1", "run-1", 1)
        _write_bundle(staging2, 1, json_body='{"second": true}')

        with pytest.raises(LedgerCollisionError):
            await ledger.ingest_attempt(
                workstream_id="w1",
                run_id="run-1",
                attempt=1,
                result=_error_result("duplicate attempt"),
                staging=staging2,
            )

    @pytest.mark.anyio
    async def test_collision_leaves_original_file_untouched(
        self, ledger: EvidenceLedger, ledger_root: Path
    ) -> None:
        staging = ledger.staging_dir("w1", "run-1", 1)
        _write_bundle(staging, 1, json_body='{"first": true}')
        await ledger.ingest_attempt(
            workstream_id="w1",
            run_id="run-1",
            attempt=1,
            result=_pass_result(),
            staging=staging,
        )

        staging2 = ledger.staging_dir("w1", "run-1", 1)
        _write_bundle(staging2, 1, json_body='{"second": true}')

        with pytest.raises(LedgerCollisionError):
            await ledger.ingest_attempt(
                workstream_id="w1",
                run_id="run-1",
                attempt=1,
                result=_error_result("duplicate attempt"),
                staging=staging2,
            )

        final = ledger_root / "w1" / "run-1" / "attempt-001.json"
        assert final.read_text() == '{"first": true}'

    @pytest.mark.anyio
    async def test_missing_sidecars_flag_anomaly_not_exception(
        self, ledger: EvidenceLedger
    ) -> None:
        staging = ledger.staging_dir("w1", "run-1", 1)
        _write_bundle(staging, 1, with_md=False, with_raw=False)

        ingested = await ledger.ingest_attempt(
            workstream_id="w1",
            run_id="run-1",
            attempt=1,
            result=_pass_result(),
            staging=staging,
        )

        assert ingested.anomaly is True
        assert ingested.md_path is None
        assert ingested.raw_path is None

    @pytest.mark.anyio
    async def test_writes_are_atomic_no_leftover_tmp_files(
        self, ledger: EvidenceLedger, ledger_root: Path
    ) -> None:
        staging = ledger.staging_dir("w1", "run-1", 1)
        _write_bundle(staging, 1)

        await ledger.ingest_attempt(
            workstream_id="w1",
            run_id="run-1",
            attempt=1,
            result=_pass_result(),
            staging=staging,
        )

        dest_dir = ledger_root / "w1" / "run-1"
        leftovers = list(dest_dir.glob("*.tmp"))
        assert leftovers == []


# =============================================================================
# list_bundle
# =============================================================================


class TestListBundle:
    @pytest.mark.anyio
    async def test_lists_attempts_ordered_by_attempt(
        self, ledger: EvidenceLedger
    ) -> None:
        for attempt, result in ((1, _fail_result()), (2, _pass_result())):
            staging = ledger.staging_dir("w1", "run-1", attempt)
            _write_bundle(staging, attempt)
            await ledger.ingest_attempt(
                workstream_id="w1",
                run_id="run-1",
                attempt=attempt,
                result=result,
                staging=staging,
            )

        bundle = await ledger.list_bundle("run-1")
        assert [item.attempt for item in bundle] == [1, 2]
        assert bundle[0].verdict == VerdictValue.FAIL
        assert bundle[1].verdict == VerdictValue.PASS

    @pytest.mark.anyio
    async def test_empty_for_unknown_run(self, ledger: EvidenceLedger) -> None:
        assert await ledger.list_bundle("no-such-run") == []


# =============================================================================
# materialize
# =============================================================================


class TestMaterialize:
    @pytest.mark.anyio
    async def test_writes_all_attempts_into_worktree(
        self, ledger: EvidenceLedger, temp_dir: Path
    ) -> None:
        for attempt, result in ((1, _fail_result()), (2, _pass_result())):
            staging = ledger.staging_dir("w1", "run-1", attempt)
            _write_bundle(staging, attempt)
            await ledger.ingest_attempt(
                workstream_id="w1",
                run_id="run-1",
                attempt=attempt,
                result=result,
                staging=staging,
            )

        worktree = temp_dir / "worktree"
        worktree.mkdir()
        created = await ledger.materialize(
            run_id="run-1", worktree=worktree, evidence_root="evidence"
        )

        dest_dir = worktree / "evidence" / "run-1"
        assert (dest_dir / "attempt-001.json").is_file()
        assert (dest_dir / "attempt-001.md").is_file()
        assert (dest_dir / "attempt-001.raw.txt").is_file()
        assert (dest_dir / "attempt-002.json").is_file()
        assert len(created) == 6
        assert all(p.is_file() for p in created)

    @pytest.mark.anyio
    async def test_idempotent_rerun_tolerates_identical_content(
        self, ledger: EvidenceLedger, temp_dir: Path
    ) -> None:
        staging = ledger.staging_dir("w1", "run-1", 1)
        _write_bundle(staging, 1)
        await ledger.ingest_attempt(
            workstream_id="w1",
            run_id="run-1",
            attempt=1,
            result=_pass_result(),
            staging=staging,
        )

        worktree = temp_dir / "worktree"
        worktree.mkdir()
        first = await ledger.materialize(
            run_id="run-1", worktree=worktree, evidence_root="evidence"
        )
        second = await ledger.materialize(
            run_id="run-1", worktree=worktree, evidence_root="evidence"
        )

        assert set(first) == set(second)

    @pytest.mark.anyio
    async def test_raises_on_divergent_existing_content(
        self, ledger: EvidenceLedger, temp_dir: Path
    ) -> None:
        staging = ledger.staging_dir("w1", "run-1", 1)
        _write_bundle(staging, 1)
        await ledger.ingest_attempt(
            workstream_id="w1",
            run_id="run-1",
            attempt=1,
            result=_pass_result(),
            staging=staging,
        )

        worktree = temp_dir / "worktree"
        worktree.mkdir()
        await ledger.materialize(
            run_id="run-1", worktree=worktree, evidence_root="evidence"
        )

        # Tamper with the already-materialized file.
        tampered = worktree / "evidence" / "run-1" / "attempt-001.json"
        tampered.write_text('{"tampered": true}')

        with pytest.raises(LedgerCollisionError):
            await ledger.materialize(
                run_id="run-1", worktree=worktree, evidence_root="evidence"
            )


# =============================================================================
# mark_materialized
# =============================================================================


class TestMarkMaterialized:
    @pytest.mark.anyio
    async def test_marks_all_attempts_of_run_materialized(
        self, ledger: EvidenceLedger, db: Database
    ) -> None:
        staging = ledger.staging_dir("w1", "run-1", 1)
        _write_bundle(staging, 1)
        await ledger.ingest_attempt(
            workstream_id="w1",
            run_id="run-1",
            attempt=1,
            result=_pass_result(),
            staging=staging,
        )

        await ledger.mark_materialized("run-1")

        rows = await db.list_verification_attempts("run-1")
        assert all(row.materialized for row in rows)


# =============================================================================
# latest_fail
# =============================================================================


class TestLatestFail:
    def test_returns_highest_attempt_fail(self, ledger: EvidenceLedger) -> None:
        bundle = [
            IngestedAttempt(
                run_id="run-1",
                attempt=1,
                workstream_id="w1",
                verdict=VerdictValue.FAIL,
                protocol_error=None,
                artifact_sha256=None,
                json_path=Path("/e/attempt-001.json"),
                md_path=None,
                raw_path=None,
                materialized=False,
                anomaly=True,
            ),
            IngestedAttempt(
                run_id="run-1",
                attempt=2,
                workstream_id="w1",
                verdict=VerdictValue.ERROR,
                protocol_error="boom",
                artifact_sha256=None,
                json_path=Path("/e/attempt-002.json"),
                md_path=None,
                raw_path=None,
                materialized=False,
                anomaly=True,
            ),
            IngestedAttempt(
                run_id="run-1",
                attempt=3,
                workstream_id="w1",
                verdict=VerdictValue.FAIL,
                protocol_error=None,
                artifact_sha256=None,
                json_path=Path("/e/attempt-003.json"),
                md_path=None,
                raw_path=None,
                materialized=False,
                anomaly=True,
            ),
        ]

        result = ledger.latest_fail(bundle)
        assert result is not None
        assert result.attempt == 3

    def test_returns_none_when_no_fail_present(self, ledger: EvidenceLedger) -> None:
        bundle = [
            IngestedAttempt(
                run_id="run-1",
                attempt=1,
                workstream_id="w1",
                verdict=VerdictValue.PASS,
                protocol_error=None,
                artifact_sha256="a" * 64,
                json_path=Path("/e/attempt-001.json"),
                md_path=None,
                raw_path=None,
                materialized=False,
                anomaly=True,
            )
        ]

        assert ledger.latest_fail(bundle) is None

    def test_returns_none_for_empty_bundle(self, ledger: EvidenceLedger) -> None:
        assert ledger.latest_fail([]) is None
