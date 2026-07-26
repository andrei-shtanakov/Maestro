"""Bounded config for the strict Docker verifier sandbox (spec §4.2).

Tuning knobs have hard secure defaults and are range-checked so they can
never express "no limit"; hardening flags are NOT here — they are baked into
`VerifierDockerIsolator`.
"""

from __future__ import annotations

import math
import re

from pydantic import BaseModel, ConfigDict, field_validator


_IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
_USER_RE = re.compile(r"^\d+:\d+$")
_SIZE_RE = re.compile(r"^(\d+)([bkmg]?)$", re.IGNORECASE)
_DECIMAL_RE = re.compile(r"^\d+(\.\d+)?$")

_UNIT = {"b": 1, "": 1, "k": 1024, "m": 1024**2, "g": 1024**3}

_MEM_MIN, _MEM_MAX = 128 * 1024**2, 8 * 1024**3
_TMPFS_MIN, _TMPFS_MAX = 16 * 1024**2, 1 * 1024**3
_CPUS_MIN, _CPUS_MAX = 0.1, 8.0
_PIDS_MIN, _PIDS_MAX = 16, 4096


def _parse_docker_size_bytes(value: str) -> int:
    """Parse a Docker size string (`128m`, `8g`, `512`) to bytes.

    Raises ValueError for an unparseable/exponent/empty value.
    """
    match = _SIZE_RE.match(value.strip())
    if match is None:
        raise ValueError(f"not a Docker size: {value!r}")
    return int(match.group(1)) * _UNIT[match.group(2).lower()]


class VerifierDockerConfig(BaseModel):
    """Tuning for the hardened verifier container (bounds are contract)."""

    model_config = ConfigDict(extra="forbid")

    image: str
    user: str
    memory: str = "512m"
    cpus: str = "1"
    pids_limit: int = 128
    tmpfs_size: str = "64m"

    @field_validator("image")
    @classmethod
    def _digest_pinned(cls, value: str) -> str:
        if _IMAGE_RE.match(value) is None:
            raise ValueError(
                f"image must be digest-pinned image@sha256:<64hex>: {value!r}"
            )
        return value

    @field_validator("user")
    @classmethod
    def _numeric_nonroot(cls, value: str) -> str:
        if _USER_RE.match(value) is None:
            raise ValueError(f"user must be numeric 'uid:gid': {value!r}")
        uid, gid = (int(p) for p in value.split(":"))
        if uid == 0 or gid == 0:
            raise ValueError(f"user must be non-root (uid!=0, gid!=0): {value!r}")
        return value

    @field_validator("memory")
    @classmethod
    def _memory_bounds(cls, value: str) -> str:
        if not _MEM_MIN <= _parse_docker_size_bytes(value) <= _MEM_MAX:
            raise ValueError(f"memory must be within 128m..8g: {value!r}")
        return value.strip()

    @field_validator("tmpfs_size")
    @classmethod
    def _tmpfs_bounds(cls, value: str) -> str:
        if not _TMPFS_MIN <= _parse_docker_size_bytes(value) <= _TMPFS_MAX:
            raise ValueError(f"tmpfs_size must be within 16m..1g: {value!r}")
        return value.strip()

    @field_validator("cpus")
    @classmethod
    def _cpus_bounds(cls, value: str) -> str:
        if _DECIMAL_RE.match(value.strip()) is None:
            raise ValueError(f"cpus must be a finite decimal: {value!r}")
        parsed = float(value)
        if not math.isfinite(parsed) or not _CPUS_MIN <= parsed <= _CPUS_MAX:
            raise ValueError(f"cpus must be within 0.1..8: {value!r}")
        return value.strip()

    @field_validator("pids_limit")
    @classmethod
    def _pids_bounds(cls, value: int) -> int:
        if not _PIDS_MIN <= value <= _PIDS_MAX:
            raise ValueError(f"pids_limit must be within 16..4096: {value!r}")
        return value
