"""R-06b M4 contract tests — JSONSchema validation on Maestro side.

Schema lives at Maestro/contracts/benchmark/report_benchmark-v1.schema.json.
Both Maestro (this file) and arbiter (Rust tests/contract_test.rs)
validate against it. Schema is the single source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, RefResolver

from maestro.benchmark.arbiter_report import _build_wire_payload
from maestro.benchmark.models import BenchmarkResult, BenchmarkTaskResult
from tests.fakes.benchmark_score import evaluated_semantics


SCHEMA_PATH = (
    Path(__file__).parents[1]
    / "contracts"
    / "benchmark"
    / "report_benchmark-v1.schema.json"
)


@pytest.fixture(scope="module")
def schema() -> dict:
    """Load the shared JSONSchema from disk."""
    with SCHEMA_PATH.open() as f:
        return json.load(f)


@pytest.fixture(scope="module")
def request_validator(schema: dict) -> Draft202012Validator:
    """Validator for the Request sub-schema, with full $ref resolution."""
    resolver = RefResolver.from_schema(schema)
    return Draft202012Validator(
        schema["definitions"]["Request"],
        resolver=resolver,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


@pytest.fixture(scope="module")
def response_validator(schema: dict) -> Draft202012Validator:
    """Validator for the Response sub-schema, with full $ref resolution."""
    resolver = RefResolver.from_schema(schema)
    return Draft202012Validator(
        schema["definitions"]["Response"],
        resolver=resolver,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def test_schema_file_exists() -> None:
    """Schema file must exist at the canonical contract path."""
    assert SCHEMA_PATH.exists(), f"schema file missing at {SCHEMA_PATH}"


def test_schema_is_valid_jsonschema(schema: dict) -> None:
    """Schema itself must be a valid JSON Schema (meta-validation)."""
    Draft202012Validator.check_schema(schema)


def test_pydantic_payload_validates_against_schema(
    request_validator: Draft202012Validator,
) -> None:
    """Canonical Pydantic-serialized payload must satisfy the Request sub-schema."""
    result = BenchmarkResult(
        run_id="r1",
        benchmark_id="b",
        agent_id="claude_code",
        score=0.85,
        score_components={"accuracy": 0.85},
        semantics=evaluated_semantics(),
        per_task=[
            BenchmarkTaskResult(
                task_index=0,
                prompt="p",
                response="r",
                duration_seconds=1.0,
                task_type="bugfix",
                score=0.9,
            )
        ],
        duration_seconds=10.0,
    )
    payload = _build_wire_payload(result, max_per_task=200)
    data = json.loads(payload.model_dump_json())
    errors = list(request_validator.iter_errors(data))
    assert not errors, f"validation errors: {[e.message for e in errors]}"


def test_missing_required_field_fails_validation(
    request_validator: Draft202012Validator,
) -> None:
    """Payload missing required fields must fail Request validation."""
    data = {"payload_version": "1.0.0", "run_id": "r1"}
    errors = list(request_validator.iter_errors(data))
    assert errors, "expected validation errors for missing required fields"


def test_response_created_validates(
    response_validator: Draft202012Validator,
) -> None:
    """Response with status=created must satisfy the Response sub-schema."""
    resp = {"status": "created", "run_id": "r1"}
    errors = list(response_validator.iter_errors(resp))
    assert not errors, f"errors: {[e.message for e in errors]}"


def test_response_duplicate_validates(
    response_validator: Draft202012Validator,
) -> None:
    """Response with status=duplicate must satisfy the Response sub-schema."""
    resp = {"status": "duplicate", "run_id": "r1"}
    errors = list(response_validator.iter_errors(resp))
    assert not errors


def test_response_unknown_status_fails(
    response_validator: Draft202012Validator,
) -> None:
    """Response with an unknown status value must fail validation."""
    resp = {"status": "weird", "run_id": "r1"}
    errors = list(response_validator.iter_errors(resp))
    assert errors


def test_unknown_optional_fields_in_payload_accepted(
    request_validator: Draft202012Validator,
) -> None:
    """Adding optional fields in v1.1+ must not break v1.0 validation.

    Policy: additive optional fields don't require payload_version bump.
    Producer is strict (Pydantic extra='forbid'); consumer (arbiter JSONSchema
    additionalProperties: true) is liberal. Asymmetric trust = safe evolution.
    """
    payload = {
        "payload_version": "1.0.0",
        "run_id": "r",
        "benchmark_id": "b",
        "agent_id": "a",
        "ts": "2026-05-23T12:00:00Z",
        "score": 0.5,
        "score_components": {},
        "duration_seconds": 1.0,
        "per_task": [],
        "per_task_total_count": 0,
        "per_task_truncated": False,
        "future_field": "added in v1.1",
    }
    errors = list(request_validator.iter_errors(payload))
    assert not errors, f"unknown optional field rejected: {[e.message for e in errors]}"


def test_unknown_response_fields_dont_crash_helper() -> None:
    """Helper must tolerate arbiter responses with extra info fields."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from maestro.benchmark.arbiter_report import report_benchmark_to_arbiter
    from maestro.benchmark.models import BenchmarkResult

    mock_client = MagicMock()
    mock_client.report_benchmark_raw = AsyncMock(
        return_value={
            "status": "created",
            "run_id": "x",
            "server_advice": "future info",
            "queue_depth": 42,
        }
    )
    result = BenchmarkResult(
        run_id="x",
        benchmark_id="b",
        agent_id="a",
        score=0.5,
        semantics=evaluated_semantics(),
        per_task=[],
        duration_seconds=1.0,
    )
    returned = asyncio.run(report_benchmark_to_arbiter(result, mock_client))
    assert returned.report_status == "ok"
