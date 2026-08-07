"""Unit generation and the credentials preflight (spec §2, §3.5, §3.6)."""

import plistlib
import shutil
from pathlib import Path

import pytest

from maestro.service.units import (
    ENV_FILE_MODE,
    PreflightError,
    UnitSpec,
    ensure_env_file,
    preflight_environment,
    probe_environment,
    render_launchd,
    render_systemd,
    unit_name,
)


def _spec(tmp_path: Path, **kw: object) -> UnitSpec:
    defaults: dict = {
        "project": "demo",
        "stage": "orchestrate",
        "config_path": tmp_path / "project.yaml",
        "db_path": tmp_path / "maestro.db",
        "maestro_bin": "/opt/bin/maestro",
        "path": "/opt/bin:/usr/bin:/bin",
        "env_file": tmp_path / "service.env",
        "log_dir": tmp_path / "service-logs",
        "schedule": "03:00",
        "every_minutes": None,
    }
    defaults.update(kw)
    return UnitSpec(**defaults)  # type: ignore[arg-type]


# =============================================================================
# Naming (§2): projects and stages never collide
# =============================================================================


def test_unit_names_are_per_project_and_stage() -> None:
    a = unit_name("demo", "orchestrate", platform="launchd")
    b = unit_name("demo", "review", platform="launchd")
    c = unit_name("other", "orchestrate", platform="launchd")
    assert len({a, b, c}) == 3
    assert a == "com.maestro.demo.orchestrate"
    assert unit_name("demo", "review", platform="systemd") == "maestro-demo-review"


def test_unit_names_sanitize_project() -> None:
    assert " " not in unit_name("my project", "orchestrate", platform="systemd")
    assert "/" not in unit_name("a/b", "review", platform="launchd")


# =============================================================================
# launchd
# =============================================================================


def test_launchd_plist_is_valid_and_runs_the_wrapper(tmp_path: Path) -> None:
    text = render_launchd(_spec(tmp_path))
    parsed = plistlib.loads(text.encode())
    assert parsed["Label"] == "com.maestro.demo.orchestrate"
    # The scheduler must start `service run`, never `orchestrate` directly.
    assert parsed["ProgramArguments"][:3] == [
        "/opt/bin/maestro",
        "service",
        "run",
    ]
    assert "--stage" in parsed["ProgramArguments"]
    assert parsed["RunAtLoad"] is False  # no surprise run at install
    assert parsed["StartCalendarInterval"] == {"Hour": 3, "Minute": 0}
    assert parsed["EnvironmentVariables"]["PATH"] == "/opt/bin:/usr/bin:/bin"


def test_launchd_interval_variant(tmp_path: Path) -> None:
    parsed = plistlib.loads(
        render_launchd(_spec(tmp_path, schedule=None, every_minutes=30)).encode()
    )
    assert parsed["StartInterval"] == 1800
    assert "StartCalendarInterval" not in parsed


def test_launchd_has_no_keepalive(tmp_path: Path) -> None:
    """§5: the next scheduled tick is the retry; restarts would stack runs."""
    parsed = plistlib.loads(render_launchd(_spec(tmp_path)).encode())
    assert parsed.get("KeepAlive", False) is False


# =============================================================================
# systemd
# =============================================================================


def test_systemd_service_and_timer(tmp_path: Path) -> None:
    service, timer = render_systemd(_spec(tmp_path))
    assert "ExecStart=/opt/bin/maestro service run" in service
    assert "--stage orchestrate" in service
    assert f"EnvironmentFile=-{tmp_path / 'service.env'}" in service
    assert "Environment=PATH=/opt/bin:/usr/bin:/bin" in service
    assert "Restart=" not in service  # §5: no automatic restarts
    assert "OnCalendar=*-*-* 03:00:00" in timer
    assert "Persistent=true" in timer


def test_systemd_interval_variant(tmp_path: Path) -> None:
    _service, timer = render_systemd(_spec(tmp_path, schedule=None, every_minutes=30))
    assert "OnUnitActiveSec=30min" in timer


# =============================================================================
# Env file (§3.6): user-owned, 0600, never written to by us
# =============================================================================


def test_env_file_created_empty_and_private(tmp_path: Path) -> None:
    path = tmp_path / "service.env"
    ensure_env_file(path)
    assert path.exists()
    content = path.read_text()
    assert content != ""  # a comment header explaining its purpose
    # Maestro writes no values of its own: every non-blank line is a comment.
    assert all(line.startswith("#") for line in content.splitlines() if line.strip())
    assert path.stat().st_mode & 0o777 == ENV_FILE_MODE


def test_env_file_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "service.env"
    path.write_text("ANTHROPIC_API_KEY=sk-user-value\n")
    path.chmod(0o600)
    ensure_env_file(path)
    assert "sk-user-value" in path.read_text()


def test_env_file_loose_permissions_are_tightened(tmp_path: Path) -> None:
    path = tmp_path / "service.env"
    path.write_text("SECRET=1\n")
    path.chmod(0o644)
    ensure_env_file(path)
    assert path.stat().st_mode & 0o777 == ENV_FILE_MODE


# =============================================================================
# Credential probing: Maestro itself never calls the model API
# =============================================================================


def test_no_credential_is_required_when_the_cli_can_authenticate_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Maestro spawns harness CLIs; they authenticate themselves.

    A `claude` login (its own credential store) is a working setup, and
    install must not demand an API key the normal path never reads.
    """
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = tmp_path / ".claude.json"
    store.write_text("{}")
    result = preflight_environment(
        harness_binaries=["maestro"],
        required_env=["ANTHROPIC_API_KEY"],
        credential_stores={"ANTHROPIC_API_KEY": [store]},
    )
    assert result.maestro_bin.endswith("maestro")


def test_refuses_only_when_no_credential_source_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(PreflightError) as exc:
        preflight_environment(
            harness_binaries=["maestro"],
            required_env=["ANTHROPIC_API_KEY"],
            credential_stores={"ANTHROPIC_API_KEY": [tmp_path / "absent.json"]},
        )
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_env_var_alone_is_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    result = preflight_environment(
        harness_binaries=["maestro"],
        required_env=["ANTHROPIC_API_KEY"],
        credential_stores={"ANTHROPIC_API_KEY": [tmp_path / "absent.json"]},
    )
    assert result.path


def test_probe_reports_problems_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--dry-run` needs to preview a unit even when the environment is
    incomplete — a preview must not be gated like a real install."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    result, problems = probe_environment(harness_binaries=["maestro", "nope"])
    assert problems  # reported...
    assert result.maestro_bin  # ...but a best-effort result is still usable


def test_a_login_store_vouches_only_for_its_own_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (Copilot, PR #156): a claude login says nothing about
    an OpenAI credential — stores are keyed by the variable they stand
    in for, not pooled into one boolean."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    claude_store = tmp_path / ".claude.json"
    claude_store.write_text("{}")
    stores = {
        "ANTHROPIC_API_KEY": [claude_store],
        "OPENAI_API_KEY": [tmp_path / "no-codex"],
    }

    # The claude key is satisfied by its own store...
    preflight_environment(
        harness_binaries=["maestro"],
        required_env=["ANTHROPIC_API_KEY"],
        credential_stores=stores,
    )
    # ...but it must not vouch for the OpenAI one.
    with pytest.raises(PreflightError) as exc:
        preflight_environment(
            harness_binaries=["maestro"],
            required_env=["ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
            credential_stores=stores,
        )
    assert "OPENAI_API_KEY" in str(exc.value)
    assert "ANTHROPIC_API_KEY" not in str(exc.value)


def test_a_key_with_no_known_store_needs_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--require-env CUSTOM_TOKEN can only come from env or the env file."""
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.delenv("CUSTOM_TOKEN", raising=False)
    with pytest.raises(PreflightError, match="CUSTOM_TOKEN"):
        preflight_environment(
            harness_binaries=["maestro"], required_env=["CUSTOM_TOKEN"]
        )


# =============================================================================
# Preflight (§3.6): refuse, don't warn
# =============================================================================


def test_preflight_resolves_binaries_to_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    result = preflight_environment(harness_binaries=["maestro", "spec-runner"])
    assert result.maestro_bin == "/opt/bin/maestro"
    assert "/opt/bin" in result.path.split(":")


def test_preflight_refuses_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name == "spec-runner" else f"/b/{name}"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(PreflightError, match="spec-runner"):
        preflight_environment(harness_binaries=["maestro", "spec-runner"])


def test_preflight_refuses_missing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/b/{name}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(PreflightError) as exc:
        preflight_environment(
            harness_binaries=["maestro"],
            required_env=["ANTHROPIC_API_KEY"],
            # No env var AND no credential store — the only genuine
            # "cannot authenticate" state.
            credential_stores={"ANTHROPIC_API_KEY": [tmp_path / "absent.json"]},
        )
    # The message must tell the operator how to fix it, not just fail.
    assert (
        "service.env" in str(exc.value) or "log the harness" in str(exc.value).lower()
    )


def test_preflight_accepts_credentials_from_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/b/{name}")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env_file = tmp_path / "service.env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-from-file\n")
    result = preflight_environment(
        harness_binaries=["maestro"],
        required_env=["ANTHROPIC_API_KEY"],
        env_file=env_file,
    )
    assert result.maestro_bin.endswith("maestro")


# =============================================================================
# Copilot review (PR #154): quoting and per-stage logs
# =============================================================================


def test_systemd_execstart_quotes_paths_with_spaces(tmp_path: Path) -> None:
    """An unquoted path would split into two words and the timer would
    silently fail to start the tick."""
    spaced = tmp_path / "Application Support" / "project.yaml"
    spaced.parent.mkdir(parents=True)
    service, _timer = render_systemd(_spec(tmp_path, config_path=spaced))
    exec_line = next(
        line for line in service.splitlines() if line.startswith("ExecStart=")
    )
    assert "'" in exec_line or '"' in exec_line
    assert str(spaced) in exec_line.replace("'", "")


def test_launchd_log_paths_include_the_stage(tmp_path: Path) -> None:
    """Both units of one project must not interleave into one log file."""
    orchestrate = plistlib.loads(render_launchd(_spec(tmp_path)).encode())
    review = plistlib.loads(render_launchd(_spec(tmp_path, stage="review")).encode())
    assert orchestrate["StandardOutPath"] != review["StandardOutPath"]
    assert "orchestrate" in orchestrate["StandardOutPath"]
    assert "review" in review["StandardErrorPath"]
