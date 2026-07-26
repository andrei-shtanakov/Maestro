"""Isolated verifier-model resolution (verifier-gate Mode-1).

Deliberately separate from `maestro.catalog.resolve_model`: the verifier is
supposed to be a cheap judge, so its precedence must never read
`MAESTRO_<HARNESS>_MODEL` (that's the main-harness env var) and must never
fall back to a catalog default (`Catalog.default_model_for_harness`) — either
path could silently hand the verifier an expensive main model.

Precedence: `verifier.model -> $MAESTRO_VERIFIER_MODEL -> fail loud`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from maestro._vendor import obs


if TYPE_CHECKING:
    from maestro.catalog import Catalog
    from maestro.models import VerifierConfig


_obs_log = obs.get_logger("maestro.verifier.config")

_VERIFIER_MODEL_ENV = "MAESTRO_VERIFIER_MODEL"


class VerifierModelError(RuntimeError):
    """The verifier model could not be resolved to a healthy catalog entry.

    Raised for: no model configured and no env override; model absent from
    the catalog (unknown); model `retired`. Per-run gate fault — callers
    should route the affected task to NEEDS_REVIEW, mirroring how
    `HarnessModelUnresolved` is handled for the main routing path.
    """


def resolve_verifier_model(cfg: VerifierConfig, catalog: Catalog | None) -> str:
    """Resolve the verifier's model id under the isolated verifier precedence.

    Args:
        cfg: The parsed `verifier:` block.
        catalog: The loaded model catalog (may be `None` if unconfigured).

    Returns:
        The resolved model id.

    Raises:
        VerifierModelError: no model available (`cfg.model` and
            `$MAESTRO_VERIFIER_MODEL` both unset), no catalog to check
            against, the model is absent from the catalog (unknown), or the
            model's catalog status is `retired`. A `deprecated` status is a
            warning only — it does not raise.
    """
    name = cfg.model or os.environ.get(_VERIFIER_MODEL_ENV)
    if not name:
        raise VerifierModelError(
            "no verifier model configured: set verifier.model in config or "
            f"${_VERIFIER_MODEL_ENV}"
        )

    if catalog is None:
        raise VerifierModelError(
            f"verifier model '{name}' cannot be validated: no model catalog "
            "configured (set $ATP_CATALOG or run 'maestro models init')"
        )

    status = catalog.status_of(name)
    if status is None:
        raise VerifierModelError(
            f"verifier model '{name}' is unknown to the catalog; "
            f"nearest matches: {catalog.nearest_models(name)}"
        )
    if status == "retired":
        raise VerifierModelError(f"verifier model '{name}' is retired")
    if status == "deprecated":
        _obs_log.warning("verifier.model_deprecated", model=name)

    return name
