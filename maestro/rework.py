"""Operator-initiated workstream rework (#124) — data types and validation.

Spec: docs/superpowers/specs/2026-08-05-workstream-rework-design.md.
The DB transaction itself lives in `Database.record_workstream_rework`;
this module holds the operator-facing validation that runs BEFORE it
(liveness proof, refresh validation, HEAD reading) and the addendum
builder used by the orchestrator's resume dispatch.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RefreshEvidence:
    """Audit evidence of a `--refresh-from` description/scope refresh."""

    config_path: str
    config_hash: str  # sha256 over the exact bytes parsed
    old_description: str
    new_description: str
    old_scope: list[str]
    new_scope: list[str]


class ReworkRefused(Exception):
    """Fail-closed refusal; message is operator-facing."""
