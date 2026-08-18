"""Model catalog loader and resolution (ADR-ECO-003b).

The catalog is user configuration, not shipped in the package and not vendored
for runtime use. It is resolved from $ATP_CATALOG (XDG default path is a
follow-up). There is no baked default model: when no catalog and no override
supply a model, resolution fails loud.

Fault taxonomy is split by blast radius:
  * CatalogError (CatalogNotConfigured / CatalogMalformed) — the catalog is
    unusable for everyone; the scheduler halts the whole run.
  * HarnessModelUnresolved — this one harness cannot resolve a default; the
    scheduler sends only that task to NEEDS_REVIEW and keeps running. It is
    deliberately NOT a CatalogError.
"""

from __future__ import annotations

import difflib
import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from maestro._vendor import obs


_obs_log = obs.get_logger("maestro.catalog")

_NOT_CONFIGURED_MSG = (
    "model catalog not configured: set $ATP_CATALOG (or run 'maestro models init')"
)


class CatalogError(RuntimeError):
    """Global catalog fault — the catalog is unusable for everyone. Halts the run."""


class CatalogNotConfigured(CatalogError):
    """No catalog configured and a default is needed."""


class CatalogMalformed(CatalogError):
    """$ATP_CATALOG set, file present, but corrupt / schema-invalid."""


class HarnessModelUnresolved(RuntimeError):
    """No routable model, or >1, for this harness. Per-task; NOT a CatalogError."""


class CatalogModel(BaseModel):
    """Plane 1 model entry."""

    vendor: str
    status: Literal["active", "deprecated", "retired"] = "active"
    aliases: list[str] = []


class CatalogAgent(BaseModel):
    """Plane 3 enrollment entry (harness, model) pair."""

    harness: str
    model: str
    tested: bool = False
    routable: bool = False


class CatalogHarness(BaseModel):
    """Plane 2 harness entry. Read for reference checks only — Maestro launches
    harnesses from its own spawner registry, never from this plane.

    ``kind`` is deliberately unvalidated, and that is narrower than it looks.
    V7 (unknown enum value) spans two fields: an unknown model ``status`` IS
    rejected, by CatalogModel.status's Literal — an unknown harness ``kind`` is
    NOT, and nothing else here checks it. The current V7 fixture varies both, so
    the Literal alone carries it; a future fixture varying only ``kind`` would
    be red and would need a decision, not a quick patch. The reason to leave it
    open is ownership: the kind vocabulary (``cli | api-baseline | local``)
    belongs to ADR-ECO-003, and re-declaring a contract Maestro does not own is
    how a consumer drifts from it silently.
    """

    kind: str = ""
    routable: bool = False


class Catalog(BaseModel):
    """Parsed catalog. Plane 2 (harnesses) is read for V1/V5 only."""

    models: dict[str, CatalogModel]
    harnesses: dict[str, CatalogHarness] = {}
    agents: list[CatalogAgent] = []

    def default_model_for_harness(self, harness: str) -> str:
        """Model of the single routable [[agents]] entry for this harness.

        Raises HarnessModelUnresolved (per-task) when there is no routable entry,
        or more than one (the ADR-003a A/B window).
        """
        routable = [a.model for a in self.agents if a.harness == harness and a.routable]
        if len(routable) == 1:
            return routable[0]
        if not routable:
            raise HarnessModelUnresolved(
                f"catalog has no routable model for harness '{harness}'; "
                f"set MAESTRO_{harness.upper()}_MODEL"
            )
        raise HarnessModelUnresolved(
            f"ambiguous default for harness '{harness}': {len(routable)} "
            f"routable models ({', '.join(routable)}); set "
            f"MAESTRO_{harness.upper()}_MODEL"
        )

    def status_of(self, model: str) -> str | None:
        """Status of a model id, resolving aliases. None means unknown."""
        entry = self.models.get(model)
        if entry is not None:
            return entry.status
        for m in self.models.values():
            if model in m.aliases:
                return m.status
        return None

    def nearest_models(self, model: str, n: int = 3) -> list[str]:
        """Closest known model ids, for warning payloads."""
        return difflib.get_close_matches(model, list(self.models), n=n, cutoff=0.3)


def check_catalog_references(catalog: Catalog) -> tuple[list[str], list[str]]:
    """Referential checks V1..V6 over a parsed catalog (rule vocabulary: the
    shared conformance set, ``tests/fixtures/catalog-conformance/v1/README.md``).

    Returns ``(errors, warnings)``. Errors mean the catalog contradicts itself
    and nobody can use it — the caller raises CatalogMalformed, which halts the
    run. Partial acceptance is deliberately not an option: routing over a
    silently-pruned agent set is the failure this check exists to prevent.

    **V1 and V5 are armed only when Plane 2 carries at least one harness.** That
    is a hole, not a decision: catalogs scaffolded by ``maestro models init``
    carry no ``[harnesses]`` table at all, so on most real catalogs "harness
    reference is checked" is simply untrue. An absent plane means
    *unverifiable*, never *valid* — the caller says so out loud, and emitting
    the plane from the scaffold template is the actual fix.

    A ``[harnesses]`` header present but EMPTY counts as unarmed too, and that
    part IS a decision. Pydantic can tell the two apart (``model_fields_set``),
    so the conflation is a choice, not an oversight: the shipped catalog
    template instructs users to "keep this table header even when empty" for
    ``[models]``, so a bare header is this ecosystem's idiom for schema
    scaffolding, not an assertion that zero harnesses exist. Reading it as the
    latter would make every ``[[agents]]`` row a V1 violation and reject the
    whole catalog because someone typed a section header. The shared fixture
    set has no case for it (v1 carries no empty-plane fixture), so this is
    Maestro's reading until the contract owner canonises one.
    """
    errors: list[str] = []
    warnings: list[str] = []
    armed = bool(catalog.harnesses)
    seen: set[tuple[str, str]] = set()

    for agent in catalog.agents:
        agent_id = f"{agent.harness}@{agent.model}"

        if armed and agent.harness not in catalog.harnesses:
            errors.append(f"V1: {agent_id} — harness not declared in [harnesses.*]")

        status = catalog.status_of(agent.model)
        if status is None:
            errors.append(f"V2: {agent_id} — model not declared in [models.*]")
        elif status == "retired":
            errors.append(f"V3: {agent_id} — references a retired model")
        elif status == "deprecated":
            warnings.append(f"V6: {agent_id} — references a deprecated model")

        pair = (agent.harness, agent.model)
        if pair in seen:
            errors.append(f"V4: {agent_id} — duplicate enrollment")
        seen.add(pair)

        harness = catalog.harnesses.get(agent.harness)
        if armed and harness is not None and agent.routable and not harness.routable:
            errors.append(
                f"V5: {agent_id} — routable enrollment on a non-routable harness"
            )

    return errors, warnings


def resolve_catalog_path() -> Path | None:
    """Resolve the catalog file path. $ATP_CATALOG only for now.

    XDG default path ($XDG_CONFIG_HOME/<eco>/agents-catalog.toml) is a follow-up
    gated on the ratified <eco> namespace.
    """
    env_path = os.environ.get("ATP_CATALOG")
    return Path(env_path) if env_path else None


def load_catalog() -> Catalog | None:
    """Load and validate the catalog.

    Returns None for both "no catalog" cases: $ATP_CATALOG unset, or set but the
    file is absent (a path typo must not crash a routed run). Raises
    CatalogMalformed when the file is present but corrupt / schema-invalid, or
    when it contradicts itself referentially (V1..V5).

    Note: "set but absent -> None" diverges from ADR-ECO-003b D2, which requires
    a loud missing-file error. The expectation is accepted, not disputed; the
    fix is a breaking change against Maestro's 2026-07-02 decision and is
    tracked separately. tests/test_catalog_conformance.py holds the xfail that
    makes fixing it silently impossible.
    """
    path = resolve_catalog_path()
    if path is None:
        return None
    if not path.is_file():
        _obs_log.info("catalog.path_absent", path=str(path))
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        catalog = Catalog.model_validate(data)
    except (tomllib.TOMLDecodeError, ValidationError, OSError) as exc:
        raise CatalogMalformed(f"catalog is corrupt ({path}): {exc}") from exc

    errors, warnings = check_catalog_references(catalog)
    for warning in warnings:
        _obs_log.warning("catalog.reference_warning", path=str(path), finding=warning)
    if not catalog.harnesses and catalog.agents:
        _obs_log.info(
            "catalog.reference_checks_not_armed",
            path=str(path),
            unarmed=["V1", "V5"],
            reason="no [harnesses.*] plane — harness references are unverifiable",
        )
    if errors:
        raise CatalogMalformed(
            f"catalog is inconsistent ({path}): " + "; ".join(errors)
        )
    return catalog


def resolve_model(
    routed: str | None,
    env_var: str,
    harness: str,
    catalog: Catalog | None,
) -> tuple[str, str]:
    """Resolve the model to run and its source. Precedence: routed > env >
    catalog-default. An empty ``routed`` is treated as absent (guards against a
    degenerate ``"<harness>@"`` id producing an empty ``--model``).

    Raises CatalogNotConfigured (GLOBAL → halt) when the default path is reached
    with no catalog. Propagates HarnessModelUnresolved (PER-TASK) from
    default_model_for_harness for no-routable / ambiguous harnesses.
    """
    if routed:
        return routed, "routed"
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val, "env"
    if catalog is None:
        raise CatalogNotConfigured(_NOT_CONFIGURED_MSG)
    return catalog.default_model_for_harness(harness), "catalog"


def warn_on_model_status(model: str, source: str, catalog: Catalog | None) -> None:
    """Coherence check against the catalog only (never provider reality — that is
    the CLI's job). No-op when catalog is None. Grades by status: retired → loud,
    deprecated → light, active → silent — for every source. The unknown → soft
    branch is the only source-gated one (skipped for source == 'catalog', where
    membership is tautological). Never blocks the spawn.
    """
    if catalog is None:
        return
    status = catalog.status_of(model)
    if status == "retired":
        _obs_log.warning(
            "agent.model_retired",
            model=model,
            source=source,
            nearest=catalog.nearest_models(model),
        )
    elif status == "deprecated":
        _obs_log.warning("agent.model_deprecated", model=model, source=source)
    elif status is None and source != "catalog":
        _obs_log.info(
            "agent.model_unknown",
            model=model,
            source=source,
            nearest=catalog.nearest_models(model),
        )
    # active → silent
