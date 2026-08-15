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
    digest = hashlib.sha256(str(Path(common).resolve()).encode()).hexdigest()[:12]
    name = _UNSAFE.sub("-", repo_path.resolve().name).strip("-") or "repo"
    return RepoKey(host="_local", owner="", repo=f"{name}-{digest}", local=True)


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
