"""Pure builders for the SSH+Docker path (Phase 2c): the remote `docker run`
argv (with remote paths) and the effective-user resolution. No container
lifecycle here — that lives in `ssh_docker_probe.ContainerOps`.
"""

from collections.abc import Mapping

from maestro.execution.ssh_cli import SshCli


def build_docker_run_argv(
    *,
    execution_id: str,
    entity_kind: str,
    entity_id: str,
    attempt: int,
    backend_id: str,
    image: str,
    remote_repo: str,
    remote_root: str,
    remote_env_file: str,
    effective_user: str,
    network: str,
    memory: str | None,
    cpus: str | None,
    inline_env: Mapping[str, str],
    has_secret_env_file: bool,
    inner_argv: list[str],
) -> tuple[list[str], dict[str, str]]:
    """Build the remote `docker run` argv + its expected label set.

    Deliberately omits `--rm` so a killed/crashed container stays inspectable
    as recovery evidence. Secrets are delivered only via `--env-file`
    (when present); non-secret `inline_env` (trace + explicit env) is inlined
    as `-e KEY=VALUE`. The label set mirrors the local `DockerIsolator`.
    """
    name = f"maestro-{execution_id}"
    labels = {
        "maestro.execution_id": execution_id,
        "maestro.entity_kind": entity_kind,
        "maestro.entity_id": entity_id,
        "maestro.attempt": str(attempt),
        "maestro.backend_id": backend_id,
    }
    argv: list[str] = [
        "docker",
        "run",
        "--name",
        name,
        "--cidfile",
        f"{remote_root}/cid",
        "-v",
        f"{remote_repo}:/work",
        "-w",
        "/work",
        "--network",
        network,
        "--user",
        effective_user,
    ]
    if memory:
        argv += ["--memory", memory]
    if cpus:
        argv += ["--cpus", cpus]
    if has_secret_env_file:
        argv += ["--env-file", remote_env_file]
    for key, value in inline_env.items():
        argv += ["-e", f"{key}={value}"]
    for key, value in labels.items():
        argv += ["--label", f"{key}={value}"]
    argv.append(image)
    argv += list(inner_argv)
    return argv, labels


async def resolve_effective_user(ssh: SshCli, configured_user: str | None) -> str:
    """Return `configured_user` if set, else the remote SSH user's uid:gid.

    Raises RuntimeError if the remote id probe fails (fail-fast: we must know
    the ownership the container will write with before it runs).
    """
    if configured_user:
        return configured_user
    uid = await ssh.run(["id", "-u"])
    gid = await ssh.run(["id", "-g"])
    if uid.returncode != 0 or gid.returncode != 0:
        raise RuntimeError(
            f"could not resolve remote uid:gid (id -u rc={uid.returncode}, "
            f"id -g rc={gid.returncode})"
        )
    return f"{uid.stdout.strip()}:{gid.stdout.strip()}"
