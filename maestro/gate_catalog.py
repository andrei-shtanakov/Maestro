"""The vendored steward gate catalog and the `gate_id` namespace rule (#160).

steward owns the `GC-` namespace and the `obligation` axis; Maestro owns its
own `<namespace>.<name>` ids and the `enforcement` axis. That split is an
owner ruling (maestro#160 / steward#63, 2026-08-12), and the two halves of it
are asymmetric in a way worth stating:

- A **canonical** `GC-*` id must resolve in the vendored catalog. An id this
  copy does not know is fail-closed — never a pass, never an invented record.
  Anything else would let an unknown governance gate disappear silently, which
  is the one outcome a governance gate exists to prevent.
- A **producer** id is validated by *shape* and never resolved. steward defines
  nothing outside `GC-*`, so asking the catalog about `maestro.validate_strict`
  would be asking the wrong authority; catalog membership is decided only by
  resolving the id, never inferred from a field being present.

The leading segment names the originating *tool*, not the owner: the
`steward.risk_classify_*` records written here are Maestro-owned ids, and
steward does not claim them.

**Vendored, not referenced.** The catalog is a pinned copy under
`resources/gate_catalog/upstream/` (mirroring upstream's own paths so the
provenance test is a plain path map). Runtime never reads the sibling checkout
— that directory does not exist for anyone who installed this package. The
patterns and reserved tokens are read *from the vendored file* rather than
re-declared here: steward publishes them in the catalog precisely because
consumers vendor the file and not the loader, and a second copy in Python
would be free to drift from the one guarantee it is supposed to carry.
"""

import re
from functools import lru_cache
from importlib import resources
from importlib.abc import Traversable
from typing import Literal

import yaml


STEWARD_VENDORED_FROM_SHA = "afd192f0706c708920c07514e4ec558dd66f5951"
"""steward commit the catalog and the normative README were vendored from.

The pin the catalog owner issued on maestro#160 after steward#63 merged. Kept
next to the copy so a stale vendoring shows up in review rather than as a
governance decision made against a catalog nobody has read in months.
"""

VENDORED_FILE_SHA256 = {
    "profiles/gate-catalog.yaml": (
        "27771bb1aebdc92c6822b26d692682b44e9ce5dc96201a1abc332ad6a3f1bda7"
    ),
    "contracts/gate-verdicts/v1/README.md": (
        "c04fb660da05a5de7acb4d681ce6b387c838669e4f8a42eea15576bfd28d5492"
    ),
}
"""Digest per vendored file — copy-integrity, checkable without the sibling.

Distinct from provenance: this catches an edit to our copy, which the
provenance test cannot see in a checkout where steward is absent (CI), and
which is the more likely accident of the two.
"""

_UPSTREAM_ROOT = "upstream"


class GateCatalogError(Exception):
    """Base for gate_id namespace violations."""


class GateIdMalformed(GateCatalogError):
    """The id belongs to neither the canonical nor the producer namespace."""


class UnknownCanonicalGate(GateCatalogError):
    """A `GC-*` id this vendored catalog copy does not contain.

    Fail-closed by contract: the writer must reject rather than degrade to a
    pass. In practice it means either the pin is stale or something emitted a
    gate id it does not own.
    """


def vendored_file(relative: str) -> Traversable:
    """One vendored upstream file, addressed by its path in steward's repo.

    A `Traversable`, not a `Path`: `read_text`/`read_bytes` is all any caller
    needs, and materialising a filesystem path would assume the package is
    always unpacked on disk.
    """
    root = resources.files("maestro.resources.gate_catalog")
    return root.joinpath(_UPSTREAM_ROOT, *relative.split("/"))


@lru_cache(maxsize=1)
def _catalog() -> dict:
    """The vendored catalog, parsed once."""
    text = vendored_file("profiles/gate-catalog.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


@lru_cache(maxsize=1)
def _namespaces() -> tuple[re.Pattern[str], re.Pattern[str]]:
    patterns = _catalog()["gate_id_namespaces"]
    return (
        re.compile(patterns["canonical_pattern"]),
        re.compile(patterns["producer_pattern"]),
    )


def catalog_version() -> int:
    """Composition version of the vendored catalog (bumps on gate changes)."""
    return int(_catalog()["version"])


@lru_cache(maxsize=1)
def canonical_gate_ids() -> frozenset[str]:
    """Every `GC-*` id the vendored catalog defines, active or not.

    Deliberately not filtered by `status`: this set answers "does the catalog
    know this id", and a deprecated gate is known. Whether a known gate still
    applies is a policy question for the run, not an identity question.
    """
    return frozenset(_catalog()["gates"])


@lru_cache(maxsize=1)
def _reserved_obligation_tokens() -> frozenset[str]:
    return frozenset(_catalog()["obligation_reserved_tokens"])


def classify_gate_id(gate_id: str) -> Literal["canonical", "producer"]:
    """Which namespace `gate_id` belongs to, refusing anything else.

    Args:
        gate_id: The id about to be written to a verdict record.

    Returns:
        `"canonical"` for a `GC-*` id present in the vendored catalog,
        `"producer"` for a well-formed `<namespace>.<name>` id.

    Raises:
        UnknownCanonicalGate: A `GC-*` id this catalog copy does not define.
        GateIdMalformed: An id matching neither namespace.
    """
    canonical, producer = _namespaces()
    if canonical.fullmatch(gate_id):
        if gate_id not in canonical_gate_ids():
            raise UnknownCanonicalGate(
                f"gate id {gate_id!r} is in steward's reserved GC- namespace but "
                f"is not defined by the vendored catalog "
                f"(steward@{STEWARD_VENDORED_FROM_SHA[:7]}, version "
                f"{catalog_version()}); refusing fail-closed. Re-vendor the "
                f"catalog if steward has since minted it."
            )
        return "canonical"
    if producer.fullmatch(gate_id):
        return "producer"
    raise GateIdMalformed(
        f"gate id {gate_id!r} matches neither the canonical namespace "
        f"({canonical.pattern}) nor the producer namespace ({producer.pattern})"
    )


CANONICAL_GATE_ID_PATTERN = _namespaces()[0]
"""steward's closed namespace, as published by the vendored catalog."""

PRODUCER_GATE_ID_PATTERN = _namespaces()[1]
"""Producer-owned namespace: `<namespace>.<name>`, lowercase-anchored."""

RESERVED_OBLIGATION_TOKENS = _reserved_obligation_tokens()
"""Tokens the catalog will never accept as an `obligation` value.

They are `mandatory` / `advisory` — Maestro's own `enforcement` vocabulary.
steward barred them in its loader so the two axes cannot collide by name; the
mirror is here so that guarantee is visible from this side too.
"""
