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
from typing import Literal, get_args

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


# --- ADR-ECO-003 enum vocabularies (INTERIM vendored copies) ---------------
#
# Both live here, together, on purpose: they belong to the SAME upstream
# document and neither is Maestro's to define. ADR-ECO-003 publishes them only
# as prose — an inline comment in its example TOML, mirrored in the conformance
# set's README — so every consumer hand-copies them and consumers can now drift
# on the vocabulary itself, one storey above the drift the conformance set was
# built to catch. A machine-readable vocabulary derived from the ADR is
# requested from the set's owner; these constants are the INTERIM stand-in and
# should be deleted when it lands — devtools#51,
# @id:catalog-enum-vocabulary-machine-readable.
#
# Kept adjacent so the pair cannot drift apart: a status vocabulary in a
# pydantic Literal and a kind vocabulary in a loose constant, declared in two
# different places, is the setup for the next invisible divergence.
# tests/test_catalog.py pins both against the vendored set's valid fixtures.
#
# The asymmetry in enforcement is deliberate, not an oversight: an unknown
# status is a hard schema reject (it decides whether an enrollment is legal),
# an unknown kind is a warning (V7 class "flag" — Maestro never launches from
# Plane 2, so an unfamiliar kind costs nothing until someone routes to it).
ModelStatus = Literal["active", "deprecated", "retired"]
MODEL_STATUSES: frozenset[str] = frozenset(get_args(ModelStatus))
HARNESS_KINDS: frozenset[str] = frozenset({"cli", "api-baseline", "local"})


class CatalogModel(BaseModel):
    """Plane 1 model entry."""

    vendor: str
    status: ModelStatus = "active"
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

    ``kind`` stays a free string at the schema level and is checked at load
    time instead, as a V7 warning against HARNESS_KINDS. The split is the point:
    an unknown ``kind`` must be *flagged*, not rejected, because Maestro never
    launches from Plane 2 — an unfamiliar kind is information, not an
    obstruction. Upstream shipped ``warn/v7-unknown-kind.toml`` (devtools#47) to
    make exactly this observable, which is the decision the previous revision of
    this docstring said such a fixture would force.
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


def _reference_checks_armed(catalog: Catalog) -> bool:
    """Whether V1/V5 can be evaluated: Plane 2 DECLARED, entries or not.

    Presence of the key, never truthiness of the mapping. A ``[harnesses]``
    header with no entries declares zero harnesses, so every enrollment
    references an undeclared harness and V1 fires for each — canon from
    devtools#47, ``fixtures/invalid/v1-empty-harnesses.toml``. Maestro read a
    bare header as schema scaffolding before that ruling; the argument was that
    the shipped catalog template teaches an empty ``[models]`` header, and it
    does not carry, because ``[models]`` is REQUIRED and ``[harnesses]`` is not
    — nobody writes a scaffolding header for an optional table.

    An ABSENT plane is still unarmed, and that remains a hole rather than a
    decision: ``maestro models init`` emits no Plane 2 at all, so on catalogs it
    scaffolds the harness reference is unverified. load_catalog() announces that
    case; closing it means teaching the template to emit the plane
    (@id:models-init-harnesses-plane).
    """
    return "harnesses" in catalog.model_fields_set


def check_catalog_references(catalog: Catalog) -> tuple[list[str], list[str]]:
    """Rule checks V1..V7 over a parsed catalog (rule vocabulary: the shared
    conformance set, ``tests/fixtures/catalog-conformance/v1/README.md``).

    V1..V6 are referential — Plane 3 against Planes 1 and 2. V7 is not: it
    checks a single field against a vocabulary, and only for
    ``harnesses.*.kind``, an unknown model ``status`` having already failed
    CatalogModel's schema before this function is reached.

    Returns ``(errors, warnings)``. Errors mean the catalog contradicts itself
    and nobody can use it — the caller raises CatalogMalformed, which halts the
    run. Partial acceptance is deliberately not an option: routing over a
    silently-pruned agent set is the failure this check exists to prevent.

    V1/V5 arming is delegated to _reference_checks_armed — an ABSENT Plane 2
    leaves them unevaluated, which is a hole and is announced as one by the
    caller.
    """
    errors: list[str] = []
    warnings: list[str] = []
    armed = _reference_checks_armed(catalog)
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

    for name, harness in catalog.harnesses.items():
        if harness.kind and harness.kind not in HARNESS_KINDS:
            warnings.append(
                f"V7: harnesses.{name}.kind = '{harness.kind}' is outside the "
                f"ADR-ECO-003 vocabulary "
                f"({', '.join(sorted(HARNESS_KINDS))}) — if the ADR added the "
                f"value, refresh HARNESS_KINDS in maestro/catalog.py"
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
    if not _reference_checks_armed(catalog) and catalog.agents:
        _obs_log.info(
            "catalog.reference_checks_not_armed",
            path=str(path),
            unarmed=["V1", "V5"],
            reason="no [harnesses] plane declared — harness references are unverifiable",
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
