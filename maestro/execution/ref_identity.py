"""Classify a persisted execution transport_ref into its (transport, isolation)
identity — the recovery SSOT for whether a resolved backend still matches the
run that minted the handle. Pure/no I/O."""

import json
from typing import NamedTuple


class RefIdentity(NamedTuple):
    """A persisted transport_ref's classified (transport, isolation) identity."""

    transport: str  # "local" | "ssh" | "unknown"
    isolation: str  # "bare" | "docker" | "unknown"


def ref_identity(transport_ref: str) -> RefIdentity:
    """Classify a persisted `transport_ref` string (pure, no I/O).

    `local_pid:<pid>` -> local/bare; `docker:<name>` -> local/docker
    (including the legacy backend named literally `"docker"`, whose
    placeholder `docker:maestro-<id>` happens to already match its real
    ref); a versioned SSH JSON ref (`{"transport": "ssh", "isolation": ...}`)
    -> ssh/<isolation> (unknown isolation if the field is absent or not one
    of `"bare"|"docker"`); anything else (a placeholder like
    `"sandbox:maestro-x"`, malformed JSON, or an empty string) -> unknown/unknown.
    """
    if transport_ref.startswith("local_pid:"):
        return RefIdentity("local", "bare")
    if transport_ref.startswith("docker:"):
        return RefIdentity("local", "docker")
    try:
        obj = json.loads(transport_ref)
    except (ValueError, TypeError):
        return RefIdentity("unknown", "unknown")
    if isinstance(obj, dict) and obj.get("transport") == "ssh":
        iso = obj.get("isolation")
        return RefIdentity("ssh", iso if iso in ("bare", "docker") else "unknown")
    return RefIdentity("unknown", "unknown")
