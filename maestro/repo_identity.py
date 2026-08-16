"""Canonical repository identity for state layout (spec §3).

A repository is named by its remote — host, owner, name — never by a
filesystem path and never by the operator-chosen `project:` field.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class IdentityError(Exception):
    """Identity could not be established; the run must refuse to start."""


# Hosts whose owner/repo names are case-insensitive.
_CASE_INSENSITIVE_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org"})

_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")
_URL_LIKE = re.compile(
    r"^(?P<scheme>https?|ssh|git)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+)$"
)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class RepoKey:
    host: str
    owner: str
    repo: str
    local: bool = False

    def as_path_parts(self) -> tuple[str, ...]:
        """Path segments under `projects/`. Local keys are two segments."""
        if self.local:
            return ("_local", self.repo)
        return (self.host, self.owner, self.repo)


def _fold(host: str, owner: str, repo: str) -> tuple[str, str, str]:
    host = host.lower()
    if host in _CASE_INSENSITIVE_HOSTS:
        return host, owner.lower(), repo.lower()
    return host, owner, repo


def parse_remote_url(url: str) -> RepoKey:
    """Parse a git remote into a `RepoKey`, or raise `IdentityError`."""
    text = (url or "").strip()
    if not text:
        raise IdentityError("empty remote URL")

    match = _URL_LIKE.match(text) or _SCP_LIKE.match(text)
    if match is None:
        raise IdentityError(f"cannot parse remote URL: {url!r}")

    host = match.group("host")

    # Reject non-git schemes (e.g., file://)
    if host == "file":
        raise IdentityError(f"cannot parse remote URL: {url!r}")
    path = match.group("path").strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]

    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise IdentityError(f"remote URL has no owner/repo: {url!r}")

    owner, repo = parts[-2], parts[-1]
    if _UNSAFE.search(owner) or _UNSAFE.search(repo) or repo in {".", ".."}:
        raise IdentityError(f"remote URL yields unsafe path segments: {url!r}")

    host, owner, repo = _fold(host, owner, repo)
    return RepoKey(host=host, owner=owner, repo=repo)


def identity_from_remote_url(url: str) -> RepoKey:
    """Alias for `parse_remote_url`, for callers that read better this way."""
    return parse_remote_url(url)


def local_key(repo_path: Path) -> RepoKey:
    """Identity for a checkout with no remote — a local fingerprint (spec §3.3).

    The hash is over the canonical *git common dir*, so worktrees of one
    repository resolve together while two unrelated checkouts that happen to
    share a basename do not.
    """
    common = _git_output(
        repo_path, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    common_path = Path(common).resolve()
    digest = hashlib.sha256(str(common_path).encode()).hexdigest()[:12]
    name = _UNSAFE.sub("-", common_path.parent.name).strip("-") or "repo"
    return RepoKey(host="_local", owner="", repo=f"{name}-{digest}", local=True)


def identity_from_config(config: object) -> RepoKey:
    """Identity of the repository a config names (spec §3.2, §3.3).

    **One rule, two ways of reaching the same fact.** The identity is always
    the repository's `origin` remote, parsed by `parse_remote_url`; the two
    config models differ only in whether they carry that remote themselves:

    - `OrchestratorConfig.repo_url` *declares* it, so it is parsed directly
      (§3.2);
    - `ProjectConfig.repo` is a local path, and a path is never identity
      (ADR-ECO-007 D2), so that checkout's `origin` is read at runtime and
      parsed by the very same rule (§3.3).

    A config that names neither is a refusal, never a fallback to `project:`
    (§3.4): that would be a silent downgrade to the ambiguous identifier this
    layout exists to remove.
    """
    repo_url = getattr(config, "repo_url", None)
    if isinstance(repo_url, str):
        return parse_remote_url(repo_url)
    repo = getattr(config, "repo", None)
    if isinstance(repo, str):
        return identity_from_checkout(Path(repo).expanduser())
    raise IdentityError(
        "config names no repository: it has neither `repo_url` (spec §3.2) "
        "nor `repo` (spec §3.3)"
    )


def identity_from_checkout(repo_path: Path) -> RepoKey:
    """Identity for Mode 1: the checkout's `origin`, else a local key."""
    try:
        url = _git_output(repo_path, "remote", "get-url", "origin")
    except IdentityError:
        # No `origin` is resolvable (spec §3.4) — not a refusal.
        return local_key(repo_path)
    return parse_remote_url(url)


def _git_output(repo_path: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise IdentityError(f"git {' '.join(args)} failed in {repo_path}") from exc
    return proc.stdout.strip()
