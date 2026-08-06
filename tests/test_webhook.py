"""Tests for the generic webhook notification channel.

Delivery semantics under test (owner-approved design, 2026-08-06):
at-least-once within a live process and graceful shutdown; best-effort
across a hard crash. Managed bounded queue + worker, drained with a
deadline at `aclose()`; overflow and undrained events are always visible.
"""

import asyncio
import json
import logging

import httpx
import pytest

from maestro.models import WorkstreamStatus
from maestro.notifications.base import Notification, NotificationEvent
from maestro.notifications.webhook import (
    RETRY_AFTER_CAP_SECONDS,
    SCHEMA_VERSION,
    WebhookNotifier,
    compute_retry_delay,
)


URL = "https://hooks.example.test/secret-token-abc123/maestro"


def _pr_notification() -> Notification:
    return Notification(
        event=NotificationEvent.WORKSTREAM_PR_CREATED,
        subject_id="ws-001",
        subject_title="Auth refactor",
        entity_kind="workstream",
        status=WorkstreamStatus.PR_CREATED,
        message="stderr fragment with a credential: token=SUPERSECRET",
        url="https://github.com/o/r/pull/1",
    )


def _started_notification() -> Notification:
    return Notification(
        event=NotificationEvent.WORKSTREAM_STARTED,
        subject_id="ws-001",
        subject_title="Auth refactor",
        entity_kind="workstream",
        status=WorkstreamStatus.RUNNING,
        message="diagnostic text",
    )


class _Recorder:
    """MockTransport handler recording requests, scripted responses."""

    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = responses

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        item = self._responses[index]
        if isinstance(item, Exception):
            raise item
        return item


def _notifier(
    handler: _Recorder,
    **kwargs: object,
) -> WebhookNotifier:
    kwargs.setdefault("backoffs", (0.0, 0.0))
    kwargs.setdefault("jitter_seconds", 0.0)
    return WebhookNotifier(
        URL,
        transport=httpx.MockTransport(handler),
        **kwargs,  # type: ignore[arg-type]
    )


# =============================================================================
# Envelope contract
# =============================================================================


async def test_envelope_schema_and_allowlist() -> None:
    """v1 envelope: versioned schema, allowlisted fields, message never sent."""
    handler = _Recorder([httpx.Response(200)])
    notifier = _notifier(handler)
    assert await notifier.send(_pr_notification()) is True
    await notifier.aclose()

    assert len(handler.requests) == 1
    request = handler.requests[0]
    body = json.loads(request.content)
    assert body["schema"] == SCHEMA_VERSION
    assert body["event"] == "workstream_pr_created"
    assert body["subject_id"] == "ws-001"
    assert body["subject_title"] == "Auth refactor"
    assert body["entity_kind"] == "workstream"
    assert body["status"] == "pr_created"
    assert body["url"] == "https://github.com/o/r/pull/1"
    # message may carry stderr/reasons/credentials — never forwarded in v1
    assert body["message"] is None
    assert "SUPERSECRET" not in request.content.decode()
    assert body["event_id"]
    assert body["occurred_at"].endswith("Z")
    assert request.headers["content-type"] == "application/json"
    assert request.headers["idempotency-key"] == body["event_id"]


async def test_envelope_url_null_for_non_pr_events() -> None:
    handler = _Recorder([httpx.Response(200)])
    notifier = _notifier(handler)
    await notifier.send(_started_notification())
    await notifier.aclose()

    body = json.loads(handler.requests[0].content)
    assert body["event"] == "workstream_started"
    assert body["url"] is None
    assert body["message"] is None


async def test_event_id_and_occurred_at_stable_across_retries() -> None:
    handler = _Recorder([httpx.Response(500), httpx.Response(500), httpx.Response(200)])
    notifier = _notifier(handler)
    await notifier.send(_pr_notification())
    await notifier.aclose()

    assert len(handler.requests) == 3
    bodies = [json.loads(r.content) for r in handler.requests]
    assert len({b["event_id"] for b in bodies}) == 1
    assert len({b["occurred_at"] for b in bodies}) == 1
    keys = {r.headers["idempotency-key"] for r in handler.requests}
    assert keys == {bodies[0]["event_id"]}


# =============================================================================
# Retry policy
# =============================================================================


async def test_no_retry_on_permanent_4xx(caplog: pytest.LogCaptureFixture) -> None:
    handler = _Recorder([httpx.Response(400, text="bad request: token=LEAK")])
    notifier = _notifier(handler)
    with caplog.at_level(logging.WARNING):
        await notifier.send(_pr_notification())
        await notifier.aclose()
    assert len(handler.requests) == 1
    assert any("webhook" in r.message for r in caplog.records)


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_retryable_statuses_are_retried(status: int) -> None:
    handler = _Recorder([httpx.Response(status), httpx.Response(200)])
    notifier = _notifier(handler)
    await notifier.send(_pr_notification())
    await notifier.aclose()
    assert len(handler.requests) == 2


async def test_connect_error_retried_then_gives_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = _Recorder([httpx.ConnectError(f"cannot reach {URL}")])
    notifier = _notifier(handler)
    with caplog.at_level(logging.WARNING):
        await notifier.send(_pr_notification())
        await notifier.aclose()
    assert len(handler.requests) == 3  # MAX_ATTEMPTS
    failure = [r for r in caplog.records if "delivery failed" in r.message]
    assert failure and "3" in failure[0].getMessage()


async def test_redirect_is_permanent_not_followed() -> None:
    handler = _Recorder(
        [httpx.Response(302, headers={"location": "https://evil.test/steal"})]
    )
    notifier = _notifier(handler)
    await notifier.send(_pr_notification())
    await notifier.aclose()
    # one request, no follow, no retry
    assert len(handler.requests) == 1
    assert handler.requests[0].url == httpx.URL(URL)


def test_compute_retry_delay_uses_backoff() -> None:
    assert compute_retry_delay(
        backoff=1.0, retry_after=None, remaining=100.0, jitter=0.25
    ) == pytest.approx(1.25)


def test_compute_retry_delay_respects_retry_after_bounded_by_cap() -> None:
    assert (
        compute_retry_delay(backoff=1.0, retry_after=3.0, remaining=100.0, jitter=0.0)
        == 3.0
    )
    assert (
        compute_retry_delay(backoff=1.0, retry_after=600.0, remaining=100.0, jitter=0.0)
        == RETRY_AFTER_CAP_SECONDS
    )


def test_compute_retry_delay_never_exceeds_remaining_budget() -> None:
    assert (
        compute_retry_delay(backoff=4.0, retry_after=None, remaining=2.0, jitter=0.0)
        == 2.0
    )
    assert (
        compute_retry_delay(backoff=1.0, retry_after=None, remaining=0.0, jitter=0.0)
        is None
    )


# =============================================================================
# Queue lifecycle: overflow, drain, shutdown
# =============================================================================


async def test_graceful_shutdown_drains_queue() -> None:
    handler = _Recorder([httpx.Response(200)])
    notifier = _notifier(handler)
    for _ in range(5):
        assert await notifier.send(_pr_notification()) is True
    await notifier.aclose()
    assert len(handler.requests) == 5


async def test_queue_overflow_is_visible(caplog: pytest.LogCaptureFixture) -> None:
    gate = asyncio.Event()

    async def stalled(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return httpx.Response(200)

    notifier = WebhookNotifier(
        URL,
        transport=httpx.MockTransport(stalled),
        queue_maxsize=1,
        backoffs=(0.0, 0.0),
        jitter_seconds=0.0,
        drain_deadline=0.1,
    )
    with caplog.at_level(logging.WARNING):
        assert await notifier.send(_pr_notification()) is True  # worker picks up
        await asyncio.sleep(0.05)  # let the worker dequeue and stall
        assert await notifier.send(_pr_notification()) is True  # fills queue
        assert await notifier.send(_pr_notification()) is False  # overflow
    assert any("overflow" in r.message for r in caplog.records)
    gate.set()
    await notifier.aclose()


async def test_drain_deadline_bounds_shutdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = asyncio.Event()

    async def stalled(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return httpx.Response(200)

    notifier = WebhookNotifier(
        URL,
        transport=httpx.MockTransport(stalled),
        backoffs=(0.0, 0.0),
        jitter_seconds=0.0,
        drain_deadline=0.1,
    )
    await notifier.send(_pr_notification())
    await notifier.send(_pr_notification())
    with caplog.at_level(logging.WARNING):
        await notifier.aclose()  # must return despite the stalled handler
    undrained = [r for r in caplog.records if "undelivered" in r.message]
    assert undrained and "2" in undrained[0].getMessage()
    gate.set()


async def test_short_backoffs_rejected_at_construction() -> None:
    """Too few backoffs would IndexError mid-delivery and drop the event."""
    with pytest.raises(ValueError, match="backoffs"):
        WebhookNotifier(URL, backoffs=(1.0,))


async def test_send_after_close_is_rejected() -> None:
    handler = _Recorder([httpx.Response(200)])
    notifier = _notifier(handler)
    await notifier.aclose()
    assert notifier.is_available() is False
    assert await notifier.send(_pr_notification()) is False


async def test_client_closed_after_aclose() -> None:
    handler = _Recorder([httpx.Response(200)])
    notifier = _notifier(handler)
    await notifier.send(_pr_notification())
    await notifier.aclose()
    assert notifier._client.is_closed


# =============================================================================
# Redaction: URL and response body never reach logs
# =============================================================================


async def test_url_and_response_body_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = _Recorder(
        [
            httpx.ConnectError(f"connection to {URL} refused"),
            httpx.Response(400, text="response body with token=RESPLEAK"),
        ]
    )
    # first notification exhausts retries on connect errors; use two sends
    notifier = _notifier(handler)
    with caplog.at_level(logging.DEBUG):
        await notifier.send(_pr_notification())
        await notifier.aclose()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "secret-token-abc123" not in logged
    assert "hooks.example.test" not in logged
    assert "RESPLEAK" not in logged


async def test_permanent_failure_response_body_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = _Recorder([httpx.Response(422, text="body token=RESPLEAK")])
    notifier = _notifier(handler)
    with caplog.at_level(logging.DEBUG):
        await notifier.send(_pr_notification())
        await notifier.aclose()
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "RESPLEAK" not in logged
    assert "422" in logged  # the status code itself is fine and useful
