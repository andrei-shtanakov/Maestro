#!/usr/bin/env python3
"""Scripted stub verifier — the CI-tier workhorse for CommandVerifier tests
(Stage B design §10). Dependency-free (stdlib only) so it runs as a plain
subprocess under LocalBackend, with no LLM and no external tools involved.

Each invocation reads a JSON list of directives (``--script``) and a cursor
file persisted next to it (``<script>.cursor``) to know which directive to
apply this time, then writes a verdict-v2 document to ``--out`` and exits
with the contract-matching code (0=PASS, 1=FAIL, 2=ERROR) — unless the
directive asks for a specific protocol violation instead.

Directive shapes (one list element per invocation, consumed in order):

    {"verdict": "PASS" | "FAIL" | "ERROR"}
        Write a fully valid document with this verdict; exit with the
        matching contract code.
    {"mode": "missing_file"}
        Never write ``--out`` at all.
    {"mode": "exit_mismatch"}
        Write a valid PASS document but exit 1 (PASS expects exit 0).
    {"mode": "hang"}
        Sleep past any caller-side timeout before doing anything else.
    {"mode": "dirty_worktree"}
        Drop a stray untracked file in the current directory (the worktree,
        since the caller's ExecutionRequest.workdir is the worktree), then
        write a valid PASS document and exit 0.
    {"mode": "wrong_echo", "field": "<identity field name>"}
        Write a document whose given identity field diverges from what the
        caller expects.
    {"mode": "wrong_artifact_sha"}
        Write a document whose ``artifact_sha256`` does not match the real
        artifact file's sha256.

Echo values (``profile_sha256``, ``verified_source_commit``,
``verified_source_tree``) are read from the ``MAESTRO_*`` env vars the
caller sets; ``verification_run_id``/``verification_attempt``/``artifact``
are read from argv; ``workstream_id`` is read from ``--workstream-id`` (not
one of Maestro's templated argv placeholders — a real per-workstream
verifier command would hardcode its own topic the same way).
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


EXIT_FOR_VERDICT = {"PASS": 0, "FAIL": 1, "ERROR": 2}
_HANG_SECONDS = 5.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--criteria", required=True)
    parser.add_argument("--verification-run-id", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--workstream-id", required=True)
    return parser.parse_args()


def _next_directive(script_path: Path) -> dict[str, Any]:
    """Pop the next directive, advancing the persistent cursor file."""
    directives = json.loads(script_path.read_text())
    cursor_path = script_path.parent / f"{script_path.name}.cursor"
    index = int(cursor_path.read_text().strip()) if cursor_path.is_file() else 0
    if index >= len(directives):
        sys.stderr.write(f"stub_verifier: no directive left at index {index}\n")
        sys.exit(2)
    cursor_path.write_text(str(index + 1))
    directive = directives[index]
    assert isinstance(directive, dict)
    return directive


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def _base_identity(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "verification_run_id": args.verification_run_id,
        "verification_attempt": args.attempt,
        "rework_attempt": 0,
        "workstream_id": args.workstream_id,
        "artifact": args.artifact,
        "artifact_sha256": _sha256_file(Path(args.artifact)),
        "criteria_sha256": _sha256_file(Path(args.criteria)),
        "profile_sha256": os.environ.get("MAESTRO_PROFILE_SHA256", ""),
        "verified_source_commit": os.environ.get("MAESTRO_VERIFIED_SOURCE_COMMIT", ""),
        "verified_source_tree": os.environ.get("MAESTRO_VERIFIED_SOURCE_TREE", ""),
    }


def _write_document(
    out_path: Path, identity: dict[str, Any], verdict: str, findings: list[dict]
) -> None:
    _atomic_write(
        out_path,
        {
            "schema_version": 2,
            "identity": identity,
            "verdict": verdict,
            "findings": findings,
        },
    )


def _findings_for(verdict: str) -> list[dict[str, Any]]:
    if verdict != "FAIL":
        return []
    return [
        {
            "criterion_id": "synthesis",
            "severity": "major",
            "evidence": "stub-injected finding",
            "author_feedback": "Fix the thing the stub flagged.",
        }
    ]


def _handle_mode(mode: str, directive: dict[str, Any], args: argparse.Namespace) -> int:
    """Apply a `{"mode": ...}` directive; return the process exit code."""
    out_path = Path(args.out)

    if mode == "missing_file":
        return 0

    if mode == "hang":
        time.sleep(_HANG_SECONDS)
        _write_document(out_path, _base_identity(args), "PASS", [])
        return 0

    if mode == "exit_mismatch":
        _write_document(out_path, _base_identity(args), "PASS", [])
        return 1  # PASS expects exit 0 — deliberate mismatch.

    if mode == "dirty_worktree":
        Path("stub-verifier-left-this.tmp").write_text("uninvited\n")
        _write_document(out_path, _base_identity(args), "PASS", [])
        return 0

    if mode == "wrong_echo":
        identity = _base_identity(args)
        field = directive["field"]
        if field == "verification_attempt":
            identity[field] = identity[field] + 1
        else:
            identity[field] = f"wrong-{identity[field]}"
        _write_document(out_path, identity, "PASS", [])
        return 0

    if mode == "wrong_artifact_sha":
        identity = _base_identity(args)
        identity["artifact_sha256"] = "f" * 64
        _write_document(out_path, identity, "PASS", [])
        return 0

    sys.stderr.write(f"stub_verifier: unknown mode {mode!r}\n")
    return 2


def main() -> None:
    args = _parse_args()
    directive = _next_directive(Path(args.script))

    if "mode" in directive:
        sys.exit(_handle_mode(directive["mode"], directive, args))

    verdict = directive["verdict"]
    identity = _base_identity(args)
    _write_document(Path(args.out), identity, verdict, _findings_for(verdict))
    sys.exit(EXIT_FOR_VERDICT[verdict])


if __name__ == "__main__":
    main()
