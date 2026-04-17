"""Structural tests for the vendored ArbiterClient.

These verify the module exists and exposes the expected surface. Behavior
is exercised via FakeArbiterClient in later tasks.
"""

from maestro.coordination import arbiter_client


class TestVendoringHeader:
    def test_vendor_commit_pinned(self) -> None:
        assert arbiter_client.ARBITER_VENDOR_COMMIT == "861534e"

    def test_required_version_pinned(self) -> None:
        assert arbiter_client.ARBITER_MCP_REQUIRED_VERSION == "0.1.0"


class TestPublicAPI:
    def test_client_class_exists(self) -> None:
        assert hasattr(arbiter_client, "ArbiterClient")
        assert hasattr(arbiter_client, "ArbiterClientConfig")

    def test_dto_classes_exist(self) -> None:
        assert hasattr(arbiter_client, "RouteDecisionDTO")
        assert hasattr(arbiter_client, "OutcomeResultDTO")
