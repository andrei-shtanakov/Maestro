"""Tests for the approver_cmd hook (#137).

Spec: docs/superpowers/specs/2026-08-06-expost-approver-cmd-design.md
(approved revision 4). Sections here mirror the spec's §10 testing plan:
contract/handshake, DB layer (migration 20), bounded subprocess runner.
Orchestrator wiring is covered in tests/test_approver_orchestrator.py.
"""

import json
import sys
from pathlib import Path

import pytest

from maestro.approver import (
    APPROVAL_REQUEST_SCHEMA,
    APPROVAL_VERDICT_SCHEMA,
    ApprovalVerdict,
    AuthorInfo,
    BlockContext,
    CmdOutcome,
    EchoFields,
    build_request_envelope,
    run_approver_cmd,
    validate_verdict,
)


ECHO = EchoFields(
    approval_run_id="01RUN",
    workstream_id="ws-006",
    phase="ex_post",
    sha="a" * 40,
)


def _doc(**overrides: object) -> dict:
    doc: dict = {
        "schema": APPROVAL_VERDICT_SCHEMA,
        "approval_run_id": "01RUN",
        "workstream_id": "ws-006",
        "phase": "ex_post",
        "sha": "a" * 40,
        "verdict": "PASS",
        "summary": "consensus: benign",
        "findings": [],
        "critics": [
            {
                "name": "codex-critic",
                "harness": "codex_cli",
                "model": "gpt-5.4",
                "verdict": "PASS",
            }
        ],
        "cost_usd": 0.42,
    }
    doc.update(overrides)
    return doc


def _raw(**overrides: object) -> bytes:
    return json.dumps(_doc(**overrides)).encode()


# =============================================================================
# Handshake matrix (§5.3 / §10)
# =============================================================================


def test_valid_document_passes() -> None:
    result = validate_verdict(_raw(), ECHO, author_model="claude-sonnet-5")
    assert isinstance(result, ApprovalVerdict)
    assert result.verdict == "PASS"
    assert result.cost_usd == 0.42
    assert result.schema_version == APPROVAL_VERDICT_SCHEMA


def test_each_echo_field_mismatch_is_protocol_error() -> None:
    for field, bad in [
        ("approval_run_id", "01OTHER"),
        ("workstream_id", "ws-999"),
        ("phase", "ex_ante"),
        ("sha", "b" * 40),
    ]:
        result = validate_verdict(_raw(**{field: bad}), ECHO, author_model=None)
        assert isinstance(result, str), field
        assert field in result


def test_wrong_schema_is_protocol_error() -> None:
    result = validate_verdict(_raw(schema="something/v9"), ECHO, author_model=None)
    assert isinstance(result, str)
    assert "schema" in result


def test_malformed_json_and_garbage_are_protocol_errors() -> None:
    for raw in [b"", b"not json", _raw() + b"trailing", b'{"partial": ']:
        assert isinstance(validate_verdict(raw, ECHO, author_model=None), str)


def test_unknown_verdict_value_is_protocol_error() -> None:
    assert isinstance(
        validate_verdict(_raw(verdict="MAYBE"), ECHO, author_model=None), str
    )


def test_empty_critics_is_protocol_error() -> None:
    result = validate_verdict(_raw(critics=[]), ECHO, author_model=None)
    assert isinstance(result, str)
    assert "critics" in result


def test_declared_critic_matching_author_model_is_protocol_error() -> None:
    result = validate_verdict(_raw(), ECHO, author_model="gpt-5.4")
    assert isinstance(result, str)
    assert "author" in result


def test_unknown_author_model_passes_vacuously() -> None:
    # Mode-2 v1: author model is null — comparison passes (documented).
    result = validate_verdict(_raw(), ECHO, author_model=None)
    assert isinstance(result, ApprovalVerdict)


def test_command_error_verdict_is_respected() -> None:
    result = validate_verdict(_raw(verdict="ERROR"), ECHO, author_model=None)
    assert isinstance(result, ApprovalVerdict)
    assert result.verdict == "ERROR"


# =============================================================================
# Field limits (§5.4)
# =============================================================================


def test_over_limit_summary_is_protocol_error() -> None:
    assert isinstance(
        validate_verdict(_raw(summary="x" * 2001), ECHO, author_model=None), str
    )


def test_over_limit_findings_are_protocol_errors() -> None:
    finding = {"severity": "major", "title": "t", "detail": "d"}
    too_many = [finding] * 51
    assert isinstance(
        validate_verdict(_raw(findings=too_many), ECHO, author_model=None), str
    )
    big_detail = [{"severity": "major", "title": "t", "detail": "d" * 4001}]
    assert isinstance(
        validate_verdict(_raw(findings=big_detail), ECHO, author_model=None), str
    )


def test_over_limit_critics_is_protocol_error() -> None:
    critic = {
        "name": "c",
        "harness": "h",
        "model": "m",
        "verdict": "PASS",
    }
    assert isinstance(
        validate_verdict(_raw(critics=[critic] * 9), ECHO, author_model=None), str
    )


def test_unknown_top_level_field_is_protocol_error() -> None:
    assert isinstance(validate_verdict(_raw(surprise=1), ECHO, author_model=None), str)


def test_canonical_serialization_round_trips() -> None:
    verdict = validate_verdict(_raw(), ECHO, author_model=None)
    assert isinstance(verdict, ApprovalVerdict)
    canonical = verdict.model_dump_json(by_alias=True)
    again = validate_verdict(canonical.encode(), ECHO, author_model=None)
    assert isinstance(again, ApprovalVerdict)
    assert again == verdict


# =============================================================================
# Request envelope (§5.1)
# =============================================================================


def _context() -> BlockContext:
    return BlockContext(
        tier="high",
        flags=["scope_violation"],
        block_reason="gates: human.owner_approval required (…)",
        declared_scope=["src/auth/**"],
        changed_paths=["src/auth/x.py", "docs/notes.md"],
        escaped_paths=["docs/notes.md"],
        author=AuthorInfo(harness="spec-runner", model=None),
    )


def test_envelope_shape() -> None:
    envelope = build_request_envelope(
        _context(),
        approval_run_id="01RUN",
        workstream_id="ws-006",
        phase="ex_post",
        sha="a" * 40,
        base_branch="main",
        diff="--- a/x\n+++ b/x\n",
        worktree="/tmp/wt",
        auto_approvals_used=0,
        evaluations_used=1,
    )
    assert envelope["schema"] == APPROVAL_REQUEST_SCHEMA
    assert envelope["approval_run_id"] == "01RUN"
    assert envelope["tier"] == "high"
    assert envelope["escaped_paths"] == ["docs/notes.md"]
    assert envelope["author"] == {"harness": "spec-runner", "model": None}
    assert envelope["auto_approvals_used"] == 0
    assert envelope["evaluations_used"] == 1
    json.dumps(envelope)  # must be JSON-serializable as-is


def test_block_context_round_trips_via_json() -> None:
    ctx = _context()
    again = BlockContext.model_validate_json(ctx.model_dump_json())
    assert again == ctx


# =============================================================================
# Bounded subprocess runner (§5.4 / §8.2)
# =============================================================================


def _script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "approver.py"
    path.write_text(body)
    return [sys.executable, str(path)]


_ENV = {"PATH": "/usr/bin:/bin"}


async def test_runner_success_returns_stdout(tmp_path: Path) -> None:
    argv = _script(
        tmp_path,
        "import sys, json\n"
        "req = json.load(sys.stdin)\n"
        "print(json.dumps({'echo': req['approval_run_id']}))\n",
    )
    outcome = await run_approver_cmd(
        argv,
        b'{"approval_run_id": "01RUN"}',
        timeout_seconds=30,
        max_stdout_bytes=65536,
        max_stderr_bytes=65536,
        env=_ENV,
    )
    assert outcome.error is None
    assert outcome.stdout is not None
    assert json.loads(outcome.stdout) == {"echo": "01RUN"}


async def test_runner_nonzero_exit_is_error_without_stdout(tmp_path: Path) -> None:
    argv = _script(
        tmp_path,
        "import sys\nprint('partial output')\nsys.stderr.write('boom token=X')\n"
        "sys.exit(3)\n",
    )
    outcome = await run_approver_cmd(
        argv,
        b"{}",
        timeout_seconds=30,
        max_stdout_bytes=65536,
        max_stderr_bytes=65536,
        env=_ENV,
    )
    assert outcome.error == "exit 3"
    assert outcome.stdout is None  # partial output never interpreted
    assert "boom" in outcome.stderr_tail


async def test_runner_timeout_kills_process_group(tmp_path: Path) -> None:
    argv = _script(tmp_path, "import time\ntime.sleep(60)\n")
    outcome = await run_approver_cmd(
        argv,
        b"{}",
        timeout_seconds=0.3,
        max_stdout_bytes=65536,
        max_stderr_bytes=65536,
        env=_ENV,
    )
    assert outcome.error == "timeout"
    assert outcome.stdout is None


async def test_runner_stdout_overflow_is_error(tmp_path: Path) -> None:
    argv = _script(
        tmp_path,
        "import sys\nsys.stdout.write('x' * 100000)\nsys.stdout.flush()\n",
    )
    outcome = await run_approver_cmd(
        argv,
        b"{}",
        timeout_seconds=30,
        max_stdout_bytes=1024,
        max_stderr_bytes=65536,
        env=_ENV,
    )
    assert outcome.error == "stdout_overflow"
    assert outcome.stdout is None


async def test_runner_absent_binary_is_spawn_error() -> None:
    outcome = await run_approver_cmd(
        ["/nonexistent/approver-bin"],
        b"{}",
        timeout_seconds=5,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
        env=_ENV,
    )
    assert outcome.error is not None
    assert outcome.error.startswith("spawn:")


async def test_runner_stderr_tail_truncated_to_500(tmp_path: Path) -> None:
    argv = _script(
        tmp_path,
        "import sys\nsys.stderr.write('e' * 5000)\nsys.exit(1)\n",
    )
    outcome = await run_approver_cmd(
        argv,
        b"{}",
        timeout_seconds=30,
        max_stdout_bytes=1024,
        max_stderr_bytes=65536,
        env=_ENV,
    )
    assert len(outcome.stderr_tail) == 500


async def test_runner_outcome_is_frozen() -> None:
    outcome = CmdOutcome(stdout=None, stderr_tail="", error="x")
    with pytest.raises(AttributeError):
        outcome.error = "y"  # type: ignore[misc]
