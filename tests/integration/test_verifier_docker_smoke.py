"""Opt-in authenticated smoke test for the strict verifier Docker sandbox
(spec `docs/superpowers/specs/2026-07-26-verifier-docker-sandbox-design.md`
§6.3/§8) — default-OFF, gated behind BOTH an explicit env flag AND real
docker availability.

Task 10's `test_verifier_docker_integration.py` proves the hardening
contract (read-only root, noexec tmpfs, non-root uid, stdin delivery,
cleanup) against a synthetic `sh -c` assertion script — it never runs
`claude`, so it never spends tokens and never proves the authenticated
code path survives the sandbox. This module is the other half: it runs a
REAL `ClaudeDiffJudge.verify()` — a real `claude` CLI process, with a real
`ANTHROPIC_API_KEY`, through the real `verifier-docker` backend
(`VerifierDockerIsolator` -> `LocalBackend` -> `docker run`) — over a tiny,
real, scope-bounded diff envelope, and asserts a well-formed
`TaskHandshakeResult` (PASS or FAIL — NOT ERROR).

IMPORTANT — the noexec proof boundary (spec §6.3): `claude --version`
(the cheap preflight probe) proves only that the CLI *starts* under
`--read-only` + noexec tmpfs + non-root; it does NOT prove the
authenticated code path never lazily unpacks an executable into
`/scratch`. THIS smoke closes that gap. If it ever fails under noexec —
the judge process crashes, hangs, or degrades to `ERROR` because the
sandbox interferes with something the authenticated `claude` runtime
needs — that is NOT a license to silently drop or loosen `noexec`/
`--read-only`. It is a signal to STOP and force an explicit threat-model
decision (spec §6.3) about how the authenticated code path and the
hardening contract can coexist. Never quietly weaken the sandbox just to
make this test pass.

Requires (all opt-in; none of this is expected to be present in CI/dev):
  - `MAESTRO_VERIFIER_SMOKE=1` — the explicit opt-in flag.
  - A reachable docker daemon.
  - `MAESTRO_VERIFIER_SMOKE_IMAGE` — a digest-pinned image
    (`repo@sha256:<hex>`, `VerifierDockerConfig`'s only accepted form)
    with the `claude` CLI installed.
  - `ANTHROPIC_API_KEY` exported in the host environment — consumed via
    the isolator's `--env-file` handoff (spec §3.5/§7.3), never baked into
    the image.
  - Optionally `MAESTRO_VERIFIER_SMOKE_MODEL` (defaults to the cheap
    `claude-haiku-4-5`) to control judge spend.

Skip-guard order is deliberate (cheapest first, spec §8 house rule: the
default suite stays green with zero docker probing for this file): the
env-flag gate is checked FIRST, with no subprocess involved at all: an
unset `MAESTRO_VERIFIER_SMOKE` short-circuits the `and` below and skips
before `_docker_ok()` (which shells out to `docker info`) is ever called.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from maestro.domain.verdict import VerdictValue
from maestro.execution.docker_cli import DockerCli
from maestro.execution.local import LocalBackend
from maestro.verifier.diff import build_scope_patch, compute_identity
from maestro.verifier.docker_backend import ANTHROPIC_ENV_KEY, VerifierDockerIsolator
from maestro.verifier.docker_config import VerifierDockerConfig
from maestro.verifier.envelope import build_envelope
from maestro.verifier.judge import ClaudeDiffJudge, TaskVerificationContext
from maestro.verifier.prompt import profile_sha256
from tests.integration.test_verifier_docker_integration import _docker_ok


_SMOKE_ENV_VAR = "MAESTRO_VERIFIER_SMOKE"
_IMAGE_ENV_VAR = "MAESTRO_VERIFIER_SMOKE_IMAGE"
_MODEL_ENV_VAR = "MAESTRO_VERIFIER_SMOKE_MODEL"
_DEFAULT_MODEL = "claude-haiku-4-5"  # cheap judge model; keeps smoke spend low

_SMOKE_ENABLED = os.environ.get(_SMOKE_ENV_VAR) == "1"

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.integration,
    pytest.mark.skipif(
        not _SMOKE_ENABLED,
        reason=f"opt-in authenticated smoke; set {_SMOKE_ENV_VAR}=1",
    ),
    # `_docker_ok()` shells out to `docker info`; the `and` short-circuits
    # so it is only ever called once `_SMOKE_ENABLED` is already True —
    # the default (flag unset) run never probes docker for this module.
    pytest.mark.skipif(
        _SMOKE_ENABLED and not _docker_ok(),
        reason="docker unavailable",
    ),
    pytest.mark.anyio,
]

_TASK_ID = "smoke-verifier-docker-1"


@dataclass
class _SmokeTask:
    """Minimal `maestro.verifier.diff.TaskLike` for `compute_identity`."""

    title: str
    prompt: str
    validation_cmd: str | None
    scope: list[str]


def _git(args: list[str], *, cwd: Path) -> str:
    """Run a `git` command in `cwd`, returning stripped stdout. Raises on failure."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _make_repo_with_scope_bounded_change(worktree: Path) -> tuple[str, list[str]]:
    """Build a tiny real git repo with one committed baseline + one in-scope
    edit on top of it. Returns `(baseline_sha, scope)`.
    """
    worktree.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], cwd=worktree)
    _git(["config", "user.email", "smoke@example.invalid"], cwd=worktree)
    _git(["config", "user.name", "Maestro Verifier Smoke"], cwd=worktree)

    target = worktree / "greeter.py"
    target.write_text('def greet(name: str) -> str:\n    return f"Hello, {name}!"\n')
    _git(["add", "greeter.py"], cwd=worktree)
    _git(["commit", "-q", "-m", "baseline"], cwd=worktree)
    baseline_sha = _git(["rev-parse", "HEAD"], cwd=worktree)

    # A tiny, real, in-scope edit — something small enough for a cheap
    # model to judge quickly, but a genuine diff (not a no-op).
    target.write_text(
        "def greet(name: str) -> str:\n"
        '    """Return a friendly greeting for `name`."""\n'
        '    return f"Hello, {name}!"\n'
    )
    return baseline_sha, ["greeter.py"]


async def test_real_judge_under_hardened_sandbox(tmp_path: Path) -> None:
    """End-to-end: a real `claude` judge PASS/FAIL under noexec/read-only,
    with `ANTHROPIC_API_KEY` delivered via the isolator's env-file handoff.

    See the module docstring: an ERROR/crash outcome here must NOT be
    read as "noexec doesn't work, drop it" — it must be escalated as an
    explicit threat-model decision (spec §6.3).
    """
    if not os.environ.get(ANTHROPIC_ENV_KEY):
        pytest.skip(f"{ANTHROPIC_ENV_KEY} not set; smoke needs a real credential")
    image = os.environ.get(_IMAGE_ENV_VAR)
    if not image:
        pytest.skip(
            f"{_IMAGE_ENV_VAR} not set; smoke needs a digest-pinned image "
            "(repo@sha256:<hex>) with the claude CLI installed"
        )
    model = os.environ.get(_MODEL_ENV_VAR, _DEFAULT_MODEL)

    worktree = tmp_path / "repo"
    baseline_sha, scope = _make_repo_with_scope_bounded_change(worktree)
    task_like = _SmokeTask(
        title="Add a docstring to greet()",
        prompt="Add a short one-line docstring to the greet() function.",
        validation_cmd=None,
        scope=scope,
    )

    patch = build_scope_patch(worktree, baseline_sha, scope, max_bytes=100_000)
    artifact_sha256, criteria_sha256, verified_scope_sha256 = compute_identity(
        task_like, patch
    )
    envelope = build_envelope(
        task_id=_TASK_ID,
        title=task_like.title,
        prompt=task_like.prompt,
        validation_cmd=task_like.validation_cmd,
        scope=scope,
        patch=patch,
        artifact_sha256=artifact_sha256,
        criteria_sha256=criteria_sha256,
        verified_scope_sha256=verified_scope_sha256,
    )
    verified_source_commit = _git(["rev-parse", "HEAD"], cwd=worktree)

    exec_root = tmp_path / "exec-root"
    exec_root.mkdir()
    cfg = VerifierDockerConfig(image=image, user="1000:1000")
    isolator = VerifierDockerIsolator(cfg, exec_root=exec_root)
    backend = LocalBackend(isolator, backend_id="verifier-docker", docker=DockerCli())

    ctx = TaskVerificationContext(
        task_id=_TASK_ID,
        run_id=f"verify-{_TASK_ID}-1",
        attempt=1,
        execution_id="smoke-exec-1",
        worktree=worktree,
        out_json=tmp_path / "verdict.json",
        envelope=envelope,
        artifact_sha256=artifact_sha256,
        criteria_sha256=criteria_sha256,
        profile_sha256=profile_sha256(),
        verified_source_commit=verified_source_commit,
        verified_scope_sha256=verified_scope_sha256,
    )
    judge = ClaudeDiffJudge(model=model, backend=backend, timeout_seconds=180)

    result = await judge.verify(ctx)

    assert result.outcome in (VerdictValue.PASS, VerdictValue.FAIL), (
        "authenticated judge must resolve to a real verdict under noexec/"
        "read-only, never ERROR — an ERROR/crash here is NOT a license to "
        "silently drop noexec; escalate a threat-model decision (spec "
        f"§6.3) instead. protocol_error={result.protocol_error!r} "
        f"raw={result.raw_result_envelope!r}"
    )
    assert result.document is not None
    assert result.document.identity.task_id == _TASK_ID
