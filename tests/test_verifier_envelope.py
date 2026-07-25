"""Tests for `maestro.verifier.envelope` + `maestro.verifier.prompt`
(verifier-gate Mode-1, design §5/§6): the deterministic judge stdin envelope,
the strict raw-payload schema, and the judge prompt/profile hash.
"""

from __future__ import annotations

import copy
import json
from typing import TypedDict, cast

import pytest

from maestro.verifier.diff import PathEntry, ScopePatch
from maestro.verifier.envelope import (
    RAW_PAYLOAD_SCHEMA,
    RawPayloadError,
    build_envelope,
    parse_raw_payload,
)
from maestro.verifier.prompt import (
    FAKE_DONE_TAXONOMY,
    JUDGE_PROMPT,
    JUDGE_PROMPT_VERSION,
    _canonical_profile_payload,
    profile_sha256,
)


def _sample_patch() -> ScopePatch:
    return ScopePatch(
        patch_bytes=(
            b"diff --git a/in_scope/a.txt b/in_scope/a.txt\n"
            b"index 0000000..1111111 100644\n"
            b"--- a/in_scope/a.txt\n"
            b"+++ b/in_scope/a.txt\n"
            b"@@ -1 +1 @@\n"
            b"-line1\n"
            b"+line1 changed\n"
        ),
        manifest=[
            PathEntry(path="in_scope/a.txt", status="modified"),
            PathEntry(path="in_scope/new.txt", status="added"),
        ],
    )


class _EnvelopeKwargs(TypedDict):
    task_id: str
    title: str
    prompt: str
    validation_cmd: str | None
    scope: list[str]
    patch: ScopePatch
    artifact_sha256: str
    criteria_sha256: str
    verified_scope_sha256: str


def _sample_envelope_kwargs() -> _EnvelopeKwargs:
    return {
        "task_id": "TASK-001",
        "title": "Add widget",
        "prompt": "Implement the widget feature.",
        "validation_cmd": "pytest -k widget",
        "scope": ["in_scope/"],
        "patch": _sample_patch(),
        "artifact_sha256": "a" * 64,
        "criteria_sha256": "b" * 64,
        "verified_scope_sha256": "c" * 64,
    }


class TestBuildEnvelope:
    """Determinism + content of the stdin blob handed to the judge."""

    def test_deterministic_for_fixed_inputs(self) -> None:
        first = build_envelope(**_sample_envelope_kwargs())
        second = build_envelope(**_sample_envelope_kwargs())

        assert first == second

    def test_is_valid_json_with_sorted_keys(self) -> None:
        blob = build_envelope(**_sample_envelope_kwargs())

        parsed = json.loads(blob)
        assert parsed["task"]["task_id"] == "TASK-001"
        assert parsed["task"]["title"] == "Add widget"
        assert parsed["task"]["prompt"] == "Implement the widget feature."
        assert parsed["task"]["validation_cmd"] == "pytest -k widget"
        assert parsed["task"]["scope"] == ["in_scope/"]
        assert parsed["identity"] == {
            "artifact_sha256": "a" * 64,
            "criteria_sha256": "b" * 64,
            "verified_scope_sha256": "c" * 64,
        }
        assert parsed["manifest"] == [
            {"path": "in_scope/a.txt", "status": "modified"},
            {"path": "in_scope/new.txt", "status": "added"},
        ]
        assert "line1 changed" in parsed["patch"]

        # Keys sorted at every object level (compact separators too).
        assert ", " not in blob
        assert ": " not in blob

    def test_changes_when_patch_content_changes(self) -> None:
        base = build_envelope(**_sample_envelope_kwargs())

        kwargs = _sample_envelope_kwargs()
        other_patch = _sample_patch()
        other_patch = ScopePatch(
            patch_bytes=other_patch.patch_bytes + b"\n+extra line\n",
            manifest=other_patch.manifest,
        )
        kwargs["patch"] = other_patch
        changed = build_envelope(**kwargs)

        assert base != changed

    def test_manifest_is_sorted_regardless_of_input_order(self) -> None:
        kwargs = _sample_envelope_kwargs()
        patch = kwargs["patch"]
        reordered = ScopePatch(
            patch_bytes=patch.patch_bytes,
            manifest=list(reversed(patch.manifest)),
        )
        kwargs["patch"] = reordered

        blob = build_envelope(**kwargs)
        original_blob = build_envelope(**_sample_envelope_kwargs())

        assert blob == original_blob


class TestParseRawPayload:
    """Strict top-level schema: `{verdict, findings}`, `extra='forbid'`."""

    def test_accepts_pass_with_empty_findings(self) -> None:
        result = parse_raw_payload(json.dumps({"verdict": "pass", "findings": []}))

        assert result.verdict == "pass"
        assert result.findings == []

    def test_accepts_valid_fail_with_findings(self) -> None:
        payload = {
            "verdict": "fail",
            "findings": [
                {
                    "criterion_id": "no-stub",
                    "severity": "high",
                    "evidence": "function body is `return True`",
                    "author_feedback": "Implement the real logic, not a stub.",
                }
            ],
        }
        result = parse_raw_payload(json.dumps(payload))

        assert result.verdict == "fail"
        assert len(result.findings) == 1
        finding = result.findings[0]
        assert finding.criterion_id == "no-stub"
        assert finding.severity == "high"
        assert finding.evidence == "function body is `return True`"
        assert finding.author_feedback == "Implement the real logic, not a stub."

    def test_rejects_extra_top_level_key(self) -> None:
        payload = {"verdict": "pass", "findings": [], "confidence": 0.9}

        with pytest.raises(RawPayloadError):
            parse_raw_payload(json.dumps(payload))

    def test_rejects_missing_verdict(self) -> None:
        with pytest.raises(RawPayloadError):
            parse_raw_payload(json.dumps({"findings": []}))

    def test_rejects_wrong_verdict_value(self) -> None:
        with pytest.raises(RawPayloadError):
            parse_raw_payload(json.dumps({"verdict": "maybe", "findings": []}))

    def test_rejects_non_list_findings(self) -> None:
        with pytest.raises(RawPayloadError):
            parse_raw_payload(json.dumps({"verdict": "pass", "findings": "none"}))

    def test_rejects_malformed_json(self) -> None:
        with pytest.raises(RawPayloadError):
            parse_raw_payload("not json at all")

    def test_rejects_finding_with_extra_key(self) -> None:
        payload = {
            "verdict": "fail",
            "findings": [
                {
                    "criterion_id": "x",
                    "severity": "low",
                    "evidence": "e",
                    "author_feedback": "f",
                    "extra_field": "nope",
                }
            ],
        }

        with pytest.raises(RawPayloadError):
            parse_raw_payload(json.dumps(payload))


class TestJudgePromptAndProfile:
    """Adversarial prompt + pinned taxonomy + profile hash."""

    def test_prompt_mentions_taxonomy_ids(self) -> None:
        for entry in FAKE_DONE_TAXONOMY:
            assert entry["id"] in JUDGE_PROMPT

    def test_taxonomy_ids_are_unique(self) -> None:
        ids = [entry["id"] for entry in FAKE_DONE_TAXONOMY]
        assert len(ids) == len(set(ids))
        assert len(ids) > 0

    def test_raw_payload_schema_is_a_dict(self) -> None:
        assert isinstance(RAW_PAYLOAD_SCHEMA, dict)

    def test_profile_sha256_is_stable(self) -> None:
        first = profile_sha256()
        second = profile_sha256()

        assert first == second
        assert len(first) == 64
        int(first, 16)  # valid hex

    def test_profile_sha256_changes_if_taxonomy_changes(self) -> None:
        payload = _canonical_profile_payload()
        mutated = copy.deepcopy(payload)
        taxonomy = cast("list[dict[str, str]]", mutated["taxonomy"])
        mutated["taxonomy"] = [
            *taxonomy,
            {"id": "new_pattern", "definition": "a brand new fake-done pattern"},
        ]

        import hashlib

        original_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        mutated_hash = hashlib.sha256(
            json.dumps(mutated, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        assert original_hash == profile_sha256()
        assert mutated_hash != profile_sha256()

    def test_profile_sha256_changes_if_prompt_version_changes(self) -> None:
        payload = _canonical_profile_payload()
        mutated = copy.deepcopy(payload)
        prompt_version = cast("str", mutated["prompt_version"])
        mutated["prompt_version"] = prompt_version + "-modified"

        import hashlib

        mutated_hash = hashlib.sha256(
            json.dumps(mutated, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        assert mutated_hash != profile_sha256()

    def test_prompt_version_is_nonempty_string(self) -> None:
        assert isinstance(JUDGE_PROMPT_VERSION, str)
        assert JUDGE_PROMPT_VERSION
