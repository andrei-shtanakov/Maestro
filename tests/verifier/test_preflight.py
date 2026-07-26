"""Eager global fail-loud docker preflight for verifier.backend=docker.

Covers the cheap, daemon-free halt-matrix rows (spec §6.1): missing/blank/
CRLF ANTHROPIC_API_KEY, docker unreachable, image absent (no auto-pull). The
`claude --version` probe + image-ID inspect are exercised only in the
docker-gated integration test (Task 10).
"""

import pytest

from maestro.verifier.docker_config import VerifierDockerConfig
from maestro.verifier.preflight import (
    VerifierPreflightError,
    run_verifier_docker_preflight,
)


pytestmark = pytest.mark.anyio
_DIGEST = "example.com/img@sha256:" + "a" * 64


def _cfg():
    return VerifierDockerConfig(image=_DIGEST, user="1000:1000")


class _FakeDocker:
    def __init__(self, *, version=True, image=True):
        self._v, self._i = version, image

    async def version_ok(self):
        return self._v

    async def image_exists(self, image):
        return self._i


async def test_missing_api_key_halts():
    with pytest.raises(VerifierPreflightError, match="ANTHROPIC_API_KEY"):
        await run_verifier_docker_preflight(
            _cfg(),
            docker=_FakeDocker(),  # type: ignore[arg-type]
            env={},
        )


async def test_blank_api_key_halts():
    with pytest.raises(VerifierPreflightError):
        await run_verifier_docker_preflight(
            _cfg(),
            docker=_FakeDocker(),  # type: ignore[arg-type]
            env={"ANTHROPIC_API_KEY": "  "},
        )


async def test_api_key_with_newline_halts():
    with pytest.raises(VerifierPreflightError):
        await run_verifier_docker_preflight(
            _cfg(),
            docker=_FakeDocker(),  # type: ignore[arg-type]
            env={"ANTHROPIC_API_KEY": "sk\nx"},
        )


async def test_api_key_with_nul_halts():
    """Halt matrix (spec §6.1) enumerates NUL/CR/LF individually; NUL alone
    must halt even without an accompanying newline."""
    with pytest.raises(VerifierPreflightError):
        await run_verifier_docker_preflight(
            _cfg(),
            docker=_FakeDocker(),  # type: ignore[arg-type]
            env={"ANTHROPIC_API_KEY": "sk\x00x"},
        )


async def test_api_key_with_carriage_return_halts():
    """Halt matrix (spec §6.1): a lone CR (no LF) must also halt."""
    with pytest.raises(VerifierPreflightError):
        await run_verifier_docker_preflight(
            _cfg(),
            docker=_FakeDocker(),  # type: ignore[arg-type]
            env={"ANTHROPIC_API_KEY": "sk\rx"},
        )


async def test_docker_unreachable_halts():
    with pytest.raises(VerifierPreflightError, match="docker"):
        await run_verifier_docker_preflight(
            _cfg(),
            docker=_FakeDocker(version=False),  # type: ignore[arg-type]
            env={"ANTHROPIC_API_KEY": "sk-x"},
        )


async def test_image_absent_halts_no_pull():
    with pytest.raises(VerifierPreflightError, match="image"):
        await run_verifier_docker_preflight(
            _cfg(),
            docker=_FakeDocker(image=False),  # type: ignore[arg-type]
            env={"ANTHROPIC_API_KEY": "sk-x"},
        )


async def test_scheduler_preflight_halts_on_missing_key(monkeypatch):
    """Scheduler._check_verifier_model halts (SchedulerError) when the
    verifier backend is docker and ANTHROPIC_API_KEY is missing.

    `_check_verifier_model` is `async def` (see module docstring in
    scheduler.py for why: it always executes from inside an already-running
    event loop via `_arm_workdirs()` <- `run()`, so it must never call
    `asyncio.run()` itself) and is awaited directly here.
    """
    import maestro.scheduler as S
    from maestro.models import VerifierConfig
    from maestro.scheduler import Scheduler, SchedulerError

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    sch = Scheduler.__new__(Scheduler)
    sch._verifier = VerifierConfig(
        backend="docker", model="m", docker=_cfg(), runner="claude"
    )
    sch._verifier_docker_cli = _FakeDocker()  # type: ignore[assignment]
    monkeypatch.setattr(
        sch, "_emit_event", lambda *_args, **_kwargs: None, raising=False
    )
    monkeypatch.setattr(S, "load_catalog", lambda: None, raising=True)
    monkeypatch.setattr(
        S, "resolve_verifier_model", lambda _cfg, _cat: "m", raising=True
    )

    with pytest.raises(SchedulerError, match="preflight"):
        await sch._check_verifier_model()


def _raising_preflight_spy(*_args, **_kwargs):
    """Fails the test if the docker preflight is ever invoked."""
    raise AssertionError(
        "run_verifier_docker_preflight must not be called for a "
        "non-docker/absent verifier backend"
    )


async def test_local_backend_skips_docker_preflight(monkeypatch):
    """`verifier.backend == 'local'` must never reach the docker preflight.

    Guards against a regression that would silently re-add a docker call
    on the local path (spec §6 says the preflight is docker-only).
    """
    import maestro.scheduler as S
    from maestro.models import VerifierConfig
    from maestro.scheduler import Scheduler

    monkeypatch.setattr(S, "run_verifier_docker_preflight", _raising_preflight_spy)
    sch = Scheduler.__new__(Scheduler)
    sch._verifier = VerifierConfig(backend="local", model="m", runner="claude")
    sch._verifier_docker_cli = None
    monkeypatch.setattr(
        sch, "_emit_event", lambda *_args, **_kwargs: None, raising=False
    )
    monkeypatch.setattr(S, "load_catalog", lambda: None, raising=True)
    monkeypatch.setattr(
        S, "resolve_verifier_model", lambda _cfg, _cat: "m", raising=True
    )

    await sch._check_verifier_model()  # must return cleanly, no AssertionError


async def test_absent_verifier_skips_docker_preflight(monkeypatch):
    """`self._verifier is None` must never reach the docker preflight."""
    import maestro.scheduler as S
    from maestro.scheduler import Scheduler

    monkeypatch.setattr(S, "run_verifier_docker_preflight", _raising_preflight_spy)
    sch = Scheduler.__new__(Scheduler)
    sch._verifier = None

    await sch._check_verifier_model()  # must return cleanly, no AssertionError
