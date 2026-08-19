"""Vendored ADR-ECO-003 enum vocabulary, shipped inside the package.

The conformance set it comes from lives in `tests/fixtures/`, which is not in
the wheel — runtime therefore cannot read it, and a source-checkout-relative
path would work only for developers. This copy is the one the loader reads;
`tests/test_catalog_conformance.py` asserts the two are byte-identical, so the
pin covers both.
"""
