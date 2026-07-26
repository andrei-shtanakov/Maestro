"""Docker-gated integration test for the strict verifier sandbox.

Exercises the FULL verifier hardening stack — `VerifierDockerIsolator` ->
`LocalBackend` -> a real `docker run` -> a real hardened container —
against a trivial `alpine` image (digest-pinned at test time). The command
run inside the container is a plain `sh -c <assertion script>`, NEVER
`claude` (no auth, no spend). This proves the security contract
end-to-end in one shot: non-root uid, read-only root + writable/noexec
tmpfs (the runtime lives in the image, not unpacked to `/scratch`), stdin
envelope delivery via `-i`, the eager env-file unlink (`after_spawn`), and
full container cleanup.

A single cheap, fast gate keeps this module docker-free when the daemon is
unreachable: `_docker_ok()` (evaluated once, at collection time, via the
module-level `pytestmark`) runs `docker info` under a bounded timeout and
returns False on ANY failure (missing binary, unreachable daemon, timeout)
— it never raises and never hangs. A machine with the docker CLI
installed but its daemon down (this dev environment) skips the whole
module here, fast, with no subprocess left behind. Every OTHER
docker-shelling helper below (`docker pull`, `docker inspect`, `docker
ps`) only ever runs from inside a test body, which itself only runs once
`_docker_ok()` has already gated the module in.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from maestro.execution.docker_cli import DockerCli
from maestro.execution.finalize import finalize_handle
from maestro.execution.local import LocalBackend
from maestro.execution.models import CollectPolicy, ExecutionRequest
from maestro.verifier.docker_backend import (
    ANTHROPIC_ENV_KEY,
    VerifierDockerIsolator,
    verifier_exec_dir,
)
from maestro.verifier.docker_config import VerifierDockerConfig


def _docker_ok() -> bool:
    """True only if the docker CLI is present AND its daemon answers.

    Deliberately probed with a real `docker info` call rather than just
    `shutil.which("docker")`: the CLI binary can be installed while the
    daemon itself is unreachable (exactly this dev environment), and a
    `which`-only guard would then try to run docker commands and
    fail/hang instead of skipping cleanly. Any failure mode (missing
    binary, unreachable daemon, timeout) is treated the same way: not
    available, never raise.
    """
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=10
            ).returncode
            == 0
        )
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_ok(), reason="docker unavailable"),
    pytest.mark.anyio,
]

_IMAGE_TAG = "alpine"
_ENVELOPE_MARKER = "MAESTRO_TEST_ENVELOPE_V1"


def _digest_for(image_tag: str) -> str:
    """Resolve a locally-present image tag to a digest-pinned ref.

    Pulls first (a cheap no-op once the image is cached locally), then
    reads the first RepoDigest so the isolator's `image` field is a real
    `repo@sha256:<hex>` — the only form `VerifierDockerConfig` accepts.
    Skips the test outright if no repo digest is available (e.g. an image
    with no registry-assigned digest) rather than falling back to an
    Id-based reference, which is not a valid `image@sha256:` form.
    """
    subprocess.run(["docker", "pull", image_tag], check=True, capture_output=True)
    out = subprocess.run(
        ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", image_tag],
        capture_output=True,
        text=True,
    )
    ref = out.stdout.strip()
    if "@sha256:" not in ref:
        pytest.skip("no repo digest for test image; pull one first")
    return ref


def _container_names_for(execution_id: str) -> str:
    """`docker ps -a` container names carrying this maestro.execution_id label."""
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=maestro.execution_id={execution_id}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _assertion_script() -> str:
    """A single `sh -c` script asserting the whole hardening contract.

    Exits non-zero on the FIRST failing assertion (a distinct code per
    check, for debuggability); exits 0 only if every assertion holds:
      1 - stdin envelope was not delivered (proves `-i` wiring)
      2 - uid is not the configured non-root uid
      3 - /scratch (tmpfs) is not writable
      4 - /root (read-only root) was unexpectedly writable
      5 - copying a binary into /scratch failed (setup issue, not a check)
      6 - a binary copied into /scratch executed despite noexec /scratch
    """
    return "; ".join(
        [
            "read -r line",
            f'test "$line" = "{_ENVELOPE_MARKER}" || exit 1',
            'test "$(id -u)" = "1000" || exit 2',
            "touch /scratch/write_ok 2>/dev/null || exit 3",
            "touch /root/write_should_fail 2>/dev/null && exit 4",
            "cp /bin/sh /scratch/sh_copy 2>/dev/null || exit 5",
            "chmod +x /scratch/sh_copy 2>/dev/null || exit 5",
            '/scratch/sh_copy -c "exit 0" 2>/dev/null && exit 6',
            "exit 0",
        ]
    )


async def test_hardened_container_enforces_full_security_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One real hardened `alpine` run proves uid, read-only root, writable
    noexec tmpfs, stdin-via-`-i`, the eager env-file unlink, and full
    container teardown — all against a real docker daemon.
    """
    # A fake, non-secret value: only its PRESENCE (not its content) drives
    # VerifierDockerIsolator to plan a real --env-file, so the eager-unlink
    # assertion below actually exercises something.
    monkeypatch.setenv(ANTHROPIC_ENV_KEY, "sk-test-not-a-real-secret")

    digest = _digest_for(_IMAGE_TAG)
    exec_root = tmp_path / "exec-root"
    exec_root.mkdir()
    workdir = tmp_path / "wd"
    workdir.mkdir()
    execution_id = "verifier-itest-1"

    cfg = VerifierDockerConfig(image=digest, user="1000:1000")
    isolator = VerifierDockerIsolator(cfg, exec_root=exec_root)

    # Sanity, pure, no I/O: no project bind mount is ever planned.
    plan = isolator.prepare(
        ExecutionRequest(
            run_id="verify-itest-1-plan",
            argv=["sh", "-c", "true"],
            workdir=workdir,
            log_path=workdir / "plan.log",
            collect=CollectPolicy(mode="none"),
            execution_id=execution_id,
            entity_kind="task",
            attempt=1,
            backend_id="verifier-docker",
        ),
        trace_env={},
        host_env=dict(os.environ),
    )
    assert not any(tok == "-v" for tok in plan.argv)

    backend = LocalBackend(isolator, backend_id="verifier-docker", docker=DockerCli())
    req = ExecutionRequest(
        run_id="verify-itest-1",
        argv=["sh", "-c", _assertion_script()],
        workdir=workdir,
        log_path=workdir / "judge.log",
        stdin=_ENVELOPE_MARKER + "\n",
        capture_output=True,
        timeout_seconds=60.0,
        collect=CollectPolicy(mode="none"),
        execution_id=execution_id,
        entity_kind="task",
        attempt=1,
        backend_id="verifier-docker",
    )

    handle = await backend.run(req)

    # after_spawn's eager unlink (spec §3.5/§7.3): the credential env-file
    # must already be gone as soon as `run()` returns — not merely by the
    # time final cleanup runs.
    env_file = verifier_exec_dir(exec_root, execution_id) / "env"
    assert not env_file.exists(), "env-file must be unlinked by after_spawn"

    result = await handle.wait()
    assert result.exit_code == 0, (
        f"assertion script failed (exit={result.exit_code}); "
        f"stdout={result.stdout_tail!r} stderr={result.stderr_tail!r}"
    )

    fin = await finalize_handle(handle)
    assert fin.cleaned is True
    assert _container_names_for(execution_id) == ""
