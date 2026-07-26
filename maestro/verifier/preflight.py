"""Eager, global fail-loud preflight for verifier.backend=docker (spec §6).

Runs once, at scheduler start, BEFORE any task: validates the credential,
the docker daemon, image presence (never auto-pulled), and that `claude
--version` succeeds under the identical hardened security profile the
production judge launch uses. Any halt-matrix row raises
`VerifierPreflightError`; the scheduler turns that into a global
`SchedulerError` so the run never starts with a broken verifier docker path
(never a silent gate-disable).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import TYPE_CHECKING

from maestro.verifier.docker_backend import (
    ANTHROPIC_ENV_KEY,
    _security_flags,
    _synthetic_env,
)


if TYPE_CHECKING:
    from collections.abc import Mapping

    from maestro.execution.docker_cli import DockerCli
    from maestro.verifier.docker_config import VerifierDockerConfig

_FORBIDDEN_CHARS = ("\x00", "\r", "\n")
_PROBE_NAME_PREFIX = "maestro-verify-preflight"
_INSPECT_TIMEOUT_S = 10.0


class VerifierPreflightError(RuntimeError):
    """A verifier docker preflight halt (spec §6.1). Global fail-loud."""


def _check_api_key(env: Mapping[str, str]) -> None:
    """Raise unless `ANTHROPIC_API_KEY` is present, non-blank, and clean.

    "Clean" means no NUL/CR/LF — those can never be a valid credential and
    would otherwise surface as a confusing downstream docker/CLI failure
    instead of a clear preflight halt.
    """
    value = env.get(ANTHROPIC_ENV_KEY)
    if value is None or not value.strip():
        raise VerifierPreflightError(
            f"{ANTHROPIC_ENV_KEY} is required and non-empty for verifier.backend=docker"
        )
    if any(char in value for char in _FORBIDDEN_CHARS):
        raise VerifierPreflightError(
            f"{ANTHROPIC_ENV_KEY} must not contain NUL/CR/LF characters"
        )


async def run_verifier_docker_preflight(
    cfg: VerifierDockerConfig,
    *,
    docker: DockerCli,
    env: Mapping[str, str],
    timeout_s: float = 20.0,
) -> str:
    """Run every §6.1 halt-matrix check; return the inspected image ID.

    Raises `VerifierPreflightError` on any halt row: missing/blank/dirty
    credential, docker daemon unreachable, image absent (never auto-pulled),
    or the `claude --version` probe missing/non-zero under the hardened
    profile. The probe itself never receives the credential (no
    `ANTHROPIC_API_KEY`, no `--env-file`), runs under a unique per-invocation
    container name (so a stale container from a prior crashed/timed-out
    invocation can never collide with this one via `docker run --name`
    conflict), and always runs with `--rm` plus a guaranteed best-effort
    kill+remove in a `finally` so nothing leaks a container regardless of
    which path (success, halt, timeout) is taken.
    """
    _check_api_key(env)
    if not await docker.version_ok():
        raise VerifierPreflightError("docker daemon unreachable")
    if not await docker.image_exists(cfg.image):
        raise VerifierPreflightError(f"image absent (no auto-pull): {cfg.image}")

    name = f"{_PROBE_NAME_PREFIX}-{uuid.uuid4().hex}"
    uid, gid = cfg.user.split(":")
    # Reproduce production's runtime env (synthetic HOME/TMPDIR/XDG_* -> the
    # writable /scratch tmpfs). Without these, `claude --version` could fail
    # under --read-only (no writable HOME) even though the real judge, which
    # DOES set them, would succeed — a false-negative preflight halt. These
    # are Maestro-authored constants, not credentials, so the probe stays
    # credential-free.
    synthetic_env_flags: list[str] = []
    for key, value in _synthetic_env().items():
        synthetic_env_flags += ["-e", f"{key}={value}"]
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--label",
        "maestro.verify_preflight=1",
        *_security_flags(cfg, uid, gid),
        *synthetic_env_flags,
        cfg.image,
        "claude",
        "--version",
    ]
    await _run_version_probe(argv, docker=docker, name=name, timeout_s=timeout_s)
    return await _inspect_image_id(cfg.image)


async def _run_version_probe(
    argv: list[str], *, docker: DockerCli, name: str, timeout_s: float
) -> None:
    """Run `claude --version` under the hardened profile; raise on failure.

    No credential is passed (no env, no `--env-file`). Cleanup is
    guaranteed via a `finally`: `--rm` handles the normal-exit case, and the
    `finally` block best-effort `docker kill`/`docker rm`s the uniquely
    named container regardless of whether this function returns normally,
    times out, or raises — belt-and-suspenders so a preflight failure never
    leaves a container behind to poison the next invocation's `--name`.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise VerifierPreflightError(f"docker CLI not found: {exc}") from exc

    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        except TimeoutError as exc:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise VerifierPreflightError(
                "claude --version probe timed out under the hardened profile"
            ) from exc

        if proc.returncode != 0:
            raise VerifierPreflightError(
                "claude --version failed under the hardened profile "
                f"(exit={proc.returncode}); image/CLI/hardening incompatible"
            )
    finally:
        with contextlib.suppress(Exception):
            await docker.kill(name)
        with contextlib.suppress(Exception):
            await docker.rm(name)


async def _inspect_image_id(image: str) -> str:
    """`docker image inspect --format {{.Id}}` for the audit log line.

    Strictly best-effort: it must NEVER raise or hang (it is not a halt row —
    the preflight has already passed by the time it runs). Any failure —
    the docker binary missing (`FileNotFoundError`), a stalled daemon
    (timeout), or a non-zero exit — yields "" rather than turning an audit
    step into a hard failure.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:
        return ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_INSPECT_TIMEOUT_S)
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()
            await proc.wait()
        return ""
    if proc.returncode != 0:
        return ""
    return out.decode("utf-8", "replace").strip()
