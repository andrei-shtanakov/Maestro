"""Judge stdin envelope + strict raw-payload schema (verifier-gate Mode-1).

The envelope (`build_envelope`) is the deterministic JSON blob piped to the
judge's stdin (design §6.3): task context + scope-bounded manifest + patch.
It never carries the model's own prompt (that is `maestro.verifier.prompt`)
and it deliberately does NOT import `TaskVerificationContext` (Task 7) — that
would create an import cycle, since the provider (Task 7) is the one that
builds a context and then calls into this module. Callers pass plain fields
plus the already-computed identity hashes (`maestro.verifier.diff.
compute_identity`).

The raw payload schema (`RawVerdict`) is what the model itself is asked to
produce: **only** `{verdict, findings}` — never identity/hash/control
fields (design §6.3: "the model is not asked to reproduce any control /
identity / hash field"). Validation is strict at the top level
(`extra="forbid"`) so an unexpected key, a missing/invalid verdict, or a
non-list `findings` all raise rather than silently coercing — the caller
(the provider) maps any raise into the fail-closed `ERROR` outcome (design
§9).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


if TYPE_CHECKING:
    from maestro.verifier.diff import ScopePatch


class RawPayloadError(ValueError):
    """The judge's raw stdout payload is missing, malformed, or schema-invalid.

    Maps to a §9 fail-closed `ERROR` upstream — callers must not swallow
    this or attempt to coerce a partial verdict out of it.
    """


class RawFinding(BaseModel):
    """One finding as the model itself must shape it — strict, minimal.

    Mirrors the fields `maestro.domain.verdict.Finding` needs, so the
    provider (Task 7) can build a `Finding` directly from each entry.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str
    severity: str
    evidence: str
    author_feedback: str


class RawVerdict(BaseModel):
    """The model's raw payload: `{verdict: "pass"|"fail", findings: [...]}`.

    Strict on the top-level shape (`extra="forbid"`) — the model is never
    asked to reproduce identity/hash/control fields; those are authored by
    the provider (Task 7), never by the model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["pass", "fail"]
    findings: list[RawFinding] = Field(default_factory=list)


RAW_PAYLOAD_SCHEMA: dict[str, object] = RawVerdict.model_json_schema()


def parse_raw_payload(text: str) -> RawVerdict:
    """Strictly parse+validate the judge's raw stdout payload.

    Args:
        text: The raw text the judge process produced (expected to be a
            single JSON object).

    Returns:
        The validated `RawVerdict`.

    Raises:
        RawPayloadError: `text` is not valid JSON, or the parsed object
            fails the strict `RawVerdict` schema (extra key, missing or
            invalid `verdict`, non-list `findings`, or a malformed finding).
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RawPayloadError(f"raw verifier payload is not valid JSON: {exc}") from exc

    try:
        return RawVerdict.model_validate(data)
    except ValidationError as exc:
        raise RawPayloadError(
            f"raw verifier payload failed schema validation: {exc}"
        ) from exc


def build_envelope(
    *,
    task_id: str,
    title: str,
    prompt: str,
    validation_cmd: str | None,
    scope: list[str],
    patch: ScopePatch,
    artifact_sha256: str,
    criteria_sha256: str,
    verified_scope_sha256: str,
) -> str:
    """Build the deterministic stdin envelope handed to the judge.

    Determinism is load-bearing: identical arguments must always produce a
    byte-identical string (the envelope, and by extension the judge's
    input, is audited via the identity hashes computed alongside it).

    Args:
        task_id: The task's id.
        title: The task's title.
        prompt: The task's own prompt/instructions text.
        validation_cmd: The task's validation command, if any.
        scope: The task's scope pathspecs (deduped + sorted here).
        patch: The scope-bounded patch + manifest (`maestro.verifier.diff.
            build_scope_patch`).
        artifact_sha256: The precomputed artifact identity hash.
        criteria_sha256: The precomputed criteria identity hash.
        verified_scope_sha256: The precomputed verified-scope identity hash.

    Returns:
        A compact, sorted-key JSON string.
    """
    envelope = {
        "task": {
            "task_id": task_id,
            "title": title,
            "prompt": prompt,
            "validation_cmd": validation_cmd,
            "scope": sorted(set(scope)),
        },
        "identity": {
            "artifact_sha256": artifact_sha256,
            "criteria_sha256": criteria_sha256,
            "verified_scope_sha256": verified_scope_sha256,
        },
        "manifest": [
            {"path": entry.path, "status": entry.status}
            for entry in sorted(patch.manifest, key=lambda e: e.path)
        ],
        "patch": patch.patch_bytes.decode("utf-8"),
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"))
