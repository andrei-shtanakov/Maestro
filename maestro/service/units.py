"""launchd/systemd unit generation and the install preflight (spec §2, §3.6).

The units are deliberately dumb: they start `maestro service run`, and
every lifecycle decision lives in the wrapper. What this module *does*
own is the part that silently breaks unattended runs — the environment.
launchd/systemd start with no shell profile and often no usable PATH,
and Maestro's spawners inherit that environment, so "works in my
terminal, silently fails at 03:00" is the default outcome unless the
installer refuses to write a unit it knows cannot authenticate.
"""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


__all__ = [
    "CREDENTIAL_ENV_BY_HARNESS",
    "DEFAULT_CREDENTIAL_STORES",
    "ENV_FILE_MODE",
    "PreflightError",
    "PreflightResult",
    "UnitSpec",
    "ensure_env_file",
    "preflight_environment",
    "probe_environment",
    "render_launchd",
    "render_systemd",
    "unit_name",
]

ENV_FILE_MODE = 0o600

# Maestro itself never calls a model API — it spawns harness CLIs, and
# each one authenticates itself. So there is **no** blanket credential
# requirement: an API key in the environment and the CLI's own credential
# store are equally valid, and demanding the former would refuse a
# perfectly working `claude` login.
#
# (The one place Maestro really needs `ANTHROPIC_API_KEY` is the Mode-1
# Docker verifier, where the judge runs with `inherit_env=False` and the
# key must be passed explicitly — that path has its own runtime
# preflight in `maestro/verifier/preflight.py` and is out of scope here.)
CREDENTIAL_ENV_BY_HARNESS = {
    "claude_code": ["ANTHROPIC_API_KEY"],
    "codex_cli": ["OPENAI_API_KEY"],
}

# Where those CLIs keep their own credentials when the user logged in
# instead of exporting a key. Presence is what we can honestly check;
# whether a *background* session can read it is the keychain caveat we
# document rather than pretend to verify.
DEFAULT_CREDENTIAL_STORES = [
    Path.home() / ".claude.json",
    Path.home() / ".claude",
]

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

_ENV_FILE_HEADER = """\
# Maestro service environment — read by the generated launchd/systemd unit.
#
# Put the credentials an unattended run needs here, one KEY=value per
# line. Scheduled runs get no shell profile, so anything your terminal
# sets in ~/.zshrc must be repeated here. This file is yours: Maestro
# creates it, tightens its permissions to 0600, and never writes values
# into it.
"""


class PreflightError(RuntimeError):
    """The environment cannot support an unattended run (spec §3.6)."""


@dataclass(frozen=True)
class PreflightResult:
    """Resolved absolute paths for the generated unit."""

    maestro_bin: str
    path: str


@dataclass(frozen=True)
class UnitSpec:
    """Everything the generated unit needs, all resolved at install time."""

    project: str
    stage: Literal["orchestrate", "review"]
    config_path: Path
    db_path: Path
    maestro_bin: str
    path: str
    env_file: Path
    log_dir: Path
    schedule: str | None = None
    every_minutes: int | None = None


def unit_name(
    project: str, stage: str, *, platform: Literal["launchd", "systemd"]
) -> str:
    """Per (project, stage) unit identifier — the two never collide."""
    slug = _UNSAFE.sub("-", project).strip("-") or "project"
    if platform == "launchd":
        return f"com.maestro.{slug}.{stage}"
    return f"maestro-{slug}-{stage}"


def _program_arguments(spec: UnitSpec) -> list[str]:
    return [
        spec.maestro_bin,
        "service",
        "run",
        str(spec.config_path),
        "--stage",
        spec.stage,
        "--db",
        str(spec.db_path),
    ]


def _parse_schedule(schedule: str) -> tuple[int, int]:
    hour, _, minute = schedule.partition(":")
    return int(hour), int(minute)


def render_launchd(spec: UnitSpec) -> str:
    """Render the LaunchAgent plist.

    `RunAtLoad=false` (installing is not running) and no `KeepAlive`:
    per §5 the next scheduled tick is the retry — automatic restarts
    would stack runs on top of each other.
    """
    plist: dict[str, object] = {
        "Label": unit_name(spec.project, spec.stage, platform="launchd"),
        "ProgramArguments": _program_arguments(spec),
        "RunAtLoad": False,
        "KeepAlive": False,
        "EnvironmentVariables": {"PATH": spec.path},
        # Stage is part of the filename: both units of one project would
        # otherwise interleave their output into the same file.
        "StandardOutPath": str(
            spec.log_dir / spec.project / f"launchd.{spec.stage}.out.log"
        ),
        "StandardErrorPath": str(
            spec.log_dir / spec.project / f"launchd.{spec.stage}.err.log"
        ),
    }
    if spec.every_minutes is not None:
        plist["StartInterval"] = spec.every_minutes * 60
    else:
        hour, minute = _parse_schedule(spec.schedule or "03:00")
        plist["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    return plistlib.dumps(plist).decode()


def render_systemd(spec: UnitSpec) -> tuple[str, str]:
    """Render the (service, timer) pair for a systemd **user** unit.

    `ExecStart` is built with `shlex.quote` per argument: a config path
    under "Application Support" would otherwise split into two words and
    the timer would fail to start the tick — silently, at 03:00.
    """
    name = unit_name(spec.project, spec.stage, platform="systemd")
    argv = " ".join(shlex.quote(arg) for arg in _program_arguments(spec))
    service = f"""\
[Unit]
Description=Maestro {spec.stage} tick for {spec.project}

[Service]
Type=oneshot
ExecStart={argv}
Environment=PATH={spec.path}
EnvironmentFile=-{spec.env_file}
"""
    if spec.every_minutes is not None:
        cadence = f"OnUnitActiveSec={spec.every_minutes}min\nOnBootSec=5min"
    else:
        hour, minute = _parse_schedule(spec.schedule or "03:00")
        cadence = f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00"
    timer = f"""\
[Unit]
Description=Schedule for {name}

[Timer]
{cadence}
Persistent=true

[Install]
WantedBy=timers.target
"""
    return service, timer


def ensure_env_file(path: Path) -> Path:
    """Create the user-owned env file if absent; always tighten to 0600.

    Never overwrites an existing file — the operator's secrets live
    there, and Maestro only guarantees the mode.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_ENV_FILE_HEADER, encoding="utf-8")
    path.chmod(ENV_FILE_MODE)
    return path


def _env_file_keys(env_file: Path | None) -> set[str]:
    if env_file is None or not env_file.exists():
        return set()
    keys: set[str] = set()
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    return keys


def probe_environment(
    *,
    harness_binaries: list[str],
    required_env: list[str] | None = None,
    env_file: Path | None = None,
    credential_stores: list[Path] | None = None,
) -> tuple[PreflightResult, list[str]]:
    """Resolve what it can and report what it cannot — never raises.

    `--dry-run` previews a unit, so an incomplete environment must not
    stop it from rendering; `preflight_environment` is the strict
    wrapper that install uses.

    Credentials are satisfied by *either* an environment/env-file entry
    *or* the harness CLI's own credential store — Maestro spawns those
    CLIs and never calls a model API itself, so a `claude` login is as
    valid as an exported key.
    """
    resolved: dict[str, str] = {}
    problems: list[str] = []
    missing: list[str] = []
    for name in harness_binaries:
        found = shutil.which(name)
        if found is None:
            missing.append(name)
        else:
            resolved[name] = found
    if missing:
        problems.append(
            f"not on PATH: {', '.join(missing)}. A scheduled unit runs without "
            "your shell profile, so every harness must resolve to an absolute "
            "path at install time. Install them, or run `maestro service "
            "install` from a shell where they resolve."
        )

    if required_env:
        available = set(os.environ) | _env_file_keys(env_file)
        absent = [key for key in required_env if key not in available]
        stores = (
            DEFAULT_CREDENTIAL_STORES
            if credential_stores is None
            else credential_stores
        )
        has_store = any(store.exists() for store in stores)
        if absent and not has_store:
            target = env_file or Path("~/.maestro/service.env")
            problems.append(
                f"no usable credentials for an unattended run: neither "
                f"{', '.join(absent)} nor a harness credential store "
                f"({', '.join(str(s) for s in stores)}). Log the harness CLI "
                f"in, or add the key to {target} (mode 0600) — a scheduled "
                "run gets no shell profile."
            )

    directories: list[str] = []
    for found in resolved.values():
        parent = str(Path(found).parent)
        if parent not in directories:
            directories.append(parent)
    for fallback in ("/usr/local/bin", "/usr/bin", "/bin"):
        if fallback not in directories:
            directories.append(fallback)
    result = PreflightResult(
        maestro_bin=resolved.get("maestro", "maestro"),
        path=":".join(directories),
    )
    return result, problems


def preflight_environment(
    *,
    harness_binaries: list[str],
    required_env: list[str] | None = None,
    env_file: Path | None = None,
    credential_stores: list[Path] | None = None,
) -> PreflightResult:
    """Strict probe for install: refuse rather than write a broken unit.

    Raises:
        PreflightError: a harness binary is not on PATH, or no credential
            source (environment, env file, or harness credential store)
            can be found for a required key.
    """
    result, problems = probe_environment(
        harness_binaries=harness_binaries,
        required_env=required_env,
        env_file=env_file,
        credential_stores=credential_stores,
    )
    if problems:
        raise PreflightError(" ".join(problems))
    return result
