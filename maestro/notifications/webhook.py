"""Generic webhook notification channel (envelope contract v1).

POSTs a versioned JSON envelope to a configured URL. Delivery semantics:
**at-least-once within a live process and graceful shutdown; best-effort
across a hard crash** — events are queued in memory (bounded), delivered
by a background worker with bounded retries, and drained with a deadline
at `aclose()`. There is no durable outbox yet; if that becomes necessary,
the queue is the seam to replace with a durable store (the payload
contract does not change).

Security invariants:
- The envelope forwards only allowlisted fields per event. `message` is
  never forwarded in v1 (it may carry stderr, gate/verifier reasons, or
  an accidentally printed credential); `url` only for events whose link
  is the payload (PR created).
- The webhook URL (which may embed tokens) never reaches logs — neither
  directly nor via exception strings (only exception class names are
  logged). HTTP response bodies are never logged either.
- Redirects are disabled so the URL/token can never travel to another
  host; a 3xx answer is a permanent failure.
"""

import asyncio
import logging
import random
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import ulid

from maestro.notifications.base import (
    Notification,
    NotificationChannel,
    NotificationEvent,
)


logger = logging.getLogger(__name__)

SCHEMA_VERSION = "maestro.notification/v1"
QUEUE_MAXSIZE = 256
ATTEMPT_TIMEOUT_SECONDS = 10.0
MAX_ATTEMPTS = 3
DELIVERY_DEADLINE_SECONDS = 40.0
DRAIN_DEADLINE_SECONDS = 15.0
RETRY_AFTER_CAP_SECONDS = 10.0
BACKOFF_SECONDS: tuple[float, ...] = (1.0, 4.0)
JITTER_SECONDS = 0.5

# Statuses worth another attempt; everything else in 4xx (and any 3xx,
# since redirects are disabled) is permanent.
_RETRYABLE_STATUSES = frozenset({408, 429})

# Events whose link belongs in the envelope. Everything else sends null.
_URL_EVENTS = frozenset({NotificationEvent.WORKSTREAM_PR_CREATED})


def compute_retry_delay(
    *,
    backoff: float,
    retry_after: float | None,
    remaining: float,
    jitter: float,
) -> float | None:
    """Delay before the next attempt, or None when the budget is spent.

    A server-provided Retry-After wins over the backoff but is capped at
    `RETRY_AFTER_CAP_SECONDS`; the result never exceeds the remaining
    wall-clock delivery budget.
    """
    if remaining <= 0:
        return None
    if retry_after is not None:
        delay = min(retry_after, RETRY_AFTER_CAP_SECONDS)
    else:
        delay = backoff + jitter
    return min(delay, remaining)


class _RedactUrlFilter(logging.Filter):
    """Drop httpx/httpcore records that would leak the webhook URL.

    httpx logs `HTTP Request: POST <full url> ...` at INFO — with a
    token-bearing webhook URL that is a secret leak straight into the
    durable obs log. Suppressing only records that mention this URL keeps
    all other httpx logging (e.g. the coordination clients) untouched.
    """

    def __init__(self, url: str) -> None:
        super().__init__()
        self._needles = [url]
        host = httpx.URL(url).host
        if host:
            self._needles.append(host)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(needle in message for needle in self._needles)


@dataclass(frozen=True)
class _Delivery:
    """One queued event: identity is fixed at enqueue, stable across retries."""

    event_id: str
    occurred_at: str
    notification: Notification


class WebhookNotifier(NotificationChannel):
    """Webhook channel with a managed bounded queue and delivery worker.

    `send()` means *accepted for delivery* (or False on overflow /
    closed); actual delivery happens in the background. `aclose()` drains
    the queue with a bounded deadline, then closes the shared HTTP client.
    """

    def __init__(
        self,
        url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        queue_maxsize: int = QUEUE_MAXSIZE,
        backoffs: tuple[float, ...] = BACKOFF_SECONDS,
        jitter_seconds: float = JITTER_SECONDS,
        drain_deadline: float = DRAIN_DEADLINE_SECONDS,
    ) -> None:
        """Initialize the channel.

        Args:
            url: Webhook endpoint (may embed a token — never logged).
            transport: Optional httpx transport override (tests).
            queue_maxsize: Bound of the in-memory delivery queue.
            backoffs: Per-retry base delays (len >= MAX_ATTEMPTS - 1).
            jitter_seconds: Max uniform jitter added to each backoff.
            drain_deadline: Seconds `aclose()` waits for the queue to drain.

        Raises:
            ValueError: If `backoffs` has fewer than MAX_ATTEMPTS - 1
                entries (it would IndexError mid-delivery otherwise).
        """
        if len(backoffs) < MAX_ATTEMPTS - 1:
            msg = (
                f"backoffs needs at least {MAX_ATTEMPTS - 1} entries "
                f"(one per retry), got {len(backoffs)}"
            )
            raise ValueError(msg)
        self._url = url
        self._client = httpx.AsyncClient(
            timeout=ATTEMPT_TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=transport,
        )
        self._queue: asyncio.Queue[_Delivery] = asyncio.Queue(maxsize=queue_maxsize)
        self._backoffs = backoffs
        self._jitter_seconds = jitter_seconds
        self._drain_deadline = drain_deadline
        self._worker: asyncio.Task[None] | None = None
        self._in_flight = 0
        self._closed = False
        self._redact_filter = _RedactUrlFilter(url)
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).addFilter(self._redact_filter)

    @property
    def channel_type(self) -> str:
        """Return channel type identifier."""
        return "webhook"

    def is_available(self) -> bool:
        """Check if the channel can accept notifications."""
        return bool(self._url) and not self._closed

    async def send(self, notification: Notification) -> bool:
        """Accept a notification for background delivery.

        Returns:
            True when queued (accepted for delivery — NOT yet delivered);
            False when the channel is closed or the bounded queue is full
            (overflow is logged, never silent).
        """
        if not self.is_available():
            return False
        if self._worker is None:
            self._worker = asyncio.create_task(self._worker_loop())
        delivery = _Delivery(
            event_id=str(ulid.new()),
            occurred_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            notification=notification,
        )
        try:
            self._queue.put_nowait(delivery)
        except asyncio.QueueFull:
            logger.warning(
                "webhook queue overflow: dropped event=%s subject=%s",
                notification.event.value,
                notification.subject_id,
            )
            return False
        return True

    async def aclose(self) -> None:
        """Drain the queue (bounded) and close the shared HTTP client.

        After the drain deadline, remaining deliveries are recorded as
        undelivered (visible, never silent) and the worker is cancelled.
        """
        if self._closed:
            return
        self._closed = True
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._queue.join(), self._drain_deadline)
            except TimeoutError:
                undelivered = self._queue.qsize() + self._in_flight
                logger.warning(
                    "webhook drain deadline reached: %d undelivered event(s)",
                    undelivered,
                )
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
        await self._client.aclose()
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).removeFilter(self._redact_filter)

    def _envelope(self, delivery: _Delivery) -> dict[str, str | None]:
        """Render the v1 envelope: allowlisted fields only, keys stable."""
        n = delivery.notification
        return {
            "schema": SCHEMA_VERSION,
            "event_id": delivery.event_id,
            "event": n.event.value,
            "occurred_at": delivery.occurred_at,
            "subject_id": n.subject_id,
            "subject_title": n.subject_title,
            "entity_kind": n.entity_kind,
            "status": n.status.value,
            "message": None,  # never forwarded in v1 (may carry secrets)
            "url": n.url if n.event in _URL_EVENTS else None,
        }

    async def _worker_loop(self) -> None:
        while True:
            delivery = await self._queue.get()
            self._in_flight += 1
            try:
                await self._deliver(delivery)
            except asyncio.CancelledError:
                raise
            except Exception:
                # _deliver only raises on programming errors; keep the
                # worker alive but visible.
                logger.exception("webhook worker error (event dropped)")
            finally:
                self._in_flight -= 1
                self._queue.task_done()

    async def _deliver(self, delivery: _Delivery) -> None:
        """One delivery: bounded attempts inside a wall-clock budget."""
        n = delivery.notification
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DELIVERY_DEADLINE_SECONDS
        last_error = "unknown"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            retry_after: float | None = None
            try:
                response = await self._client.post(
                    self._url,
                    json=self._envelope(delivery),
                    headers={"Idempotency-Key": delivery.event_id},
                )
            except httpx.HTTPError as exc:
                # Never log str(exc): httpx embeds the URL in messages.
                last_error = type(exc).__name__
            else:
                if response.is_success:
                    return
                last_error = f"HTTP {response.status_code}"
                if not self._is_retryable(response.status_code):
                    logger.warning(
                        "webhook delivery permanently rejected: event=%s "
                        "subject=%s error=%s",
                        n.event.value,
                        n.subject_id,
                        last_error,
                    )
                    return
                retry_after = self._parse_retry_after(response)
            if attempt == MAX_ATTEMPTS:
                break
            delay = compute_retry_delay(
                backoff=self._backoffs[attempt - 1],
                retry_after=retry_after,
                remaining=deadline - loop.time(),
                jitter=random.uniform(0, self._jitter_seconds),
            )
            if delay is None:
                break  # wall-clock budget spent
            await asyncio.sleep(delay)
        logger.warning(
            "webhook delivery failed: event=%s subject=%s attempts=%d error=%s",
            n.event.value,
            n.subject_id,
            attempt,
            last_error,
        )

    @staticmethod
    def _is_retryable(status_code: int) -> bool:
        return status_code >= 500 or status_code in _RETRYABLE_STATUSES

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        """Parse a seconds-valued Retry-After; HTTP-dates fall back to None."""
        raw = response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value >= 0 else None
