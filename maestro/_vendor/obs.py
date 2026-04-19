# Vendored from spec-runner@fa6b106be96eef2d9a4e3e0d9a970ce0e7092384 — observability contract v1
# Do not edit locally. To update: re-copy from spec-runner and bump marker.
"""Orchestra observability emitter — reference implementation.

Source of truth for `obs.py` (vendored into other Python projects).
Produces OpenTelemetry Logs Data Model JSONL, one file per PID.

Contract: see _cowork_output/observability-contract/log-schema.json
"""

from __future__ import annotations

import logging as _stdlib_logging
import os
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import ulid


_SEVERITY_NUMBER = {
    "debug": 5,
    "info": 9,
    "warning": 13,
    "warn": 13,
    "error": 17,
    "critical": 21,
    "fatal": 21,
}
_SEVERITY_TEXT = {
    5: "DEBUG",
    9: "INFO",
    13: "WARN",
    17: "ERROR",
    21: "FATAL",
}

_initialized = False


def _now_ns() -> int:
    return time.time_ns()


def _iso_micros(ns: int) -> str:
    dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def _parse_traceparent() -> tuple[str, str | None]:
    """Return (trace_id, parent_span_id). parent_span_id is None at root."""
    raw = os.environ.get("TRACEPARENT", "").strip()
    if not raw:
        return secrets.token_hex(16), None
    m = _TRACEPARENT_RE.match(raw)
    if not m:
        _stdlib_logging.getLogger(__name__).warning(
            "malformed TRACEPARENT=%r, treating as root", raw
        )
        return secrets.token_hex(16), None
    return m.group(1), m.group(2)


_DEFAULT_REDACT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "password",
        "secret",
        "authorization",
        "cookie",
        "private_key",
    }
)


def _redact(keys: frozenset[str]):
    def _walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: ("<redacted>" if k.lower() in keys else _walk(v))
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    def processor(logger, method_name, event_dict):
        return {
            k: ("<redacted>" if k.lower() in keys else _walk(v))
            for k, v in event_dict.items()
        }

    return processor


def _reshape_to_otel(project: str):
    """Final processor: rearrange structlog dict into OTel Logs DM shape."""

    def processor(logger, method_name, event_dict):
        ns = event_dict.pop("_ts_ns", _now_ns())
        # structlog passes method_name: "info", "error", etc. Use it, not event_dict.
        sev_num = _SEVERITY_NUMBER.get(method_name.lower(), 9)
        event_dict.pop("level", None)  # drop if present; method_name is authoritative
        event_name = event_dict.pop("event")

        attrs = {"event": event_name}
        for key in ("pipeline_id", "parent_span_id", "task_id", "module"):
            if key in event_dict:
                attrs[key] = event_dict.pop(key)
        attrs.update(event_dict)

        return {
            "Timestamp": str(ns),
            "ts_iso": _iso_micros(ns),
            "SeverityText": _SEVERITY_TEXT[sev_num],
            "SeverityNumber": sev_num,
            "TraceId": attrs.pop("_trace_id", "0" * 32),
            "SpanId": attrs.pop("_span_id", "0" * 16),
            "TraceFlags": "01",
            "Body": attrs.pop("_body", event_name),
            "Resource": {"service.name": project},
            "Attributes": attrs,
        }

    return processor


def _default_log_dir() -> Path:
    env_dir = os.environ.get("ORCHESTRA_LOG_DIR")
    if env_dir:
        return Path(env_dir)
    pid = os.environ.get("ORCHESTRA_PIPELINE_ID") or str(ulid.new())
    return Path.cwd() / "logs" / pid


def init_logging(
    project: str,
    *,
    level: str | None = None,
    log_dir: Path | None = None,
    redact_keys: list[str] | None = None,
) -> None:
    global _initialized
    _initialized = False
    structlog.contextvars.clear_contextvars()
    _initialized = True

    log_dir = log_dir or _default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    output_path = log_dir / f"{project}-{os.getpid()}.jsonl"

    pipeline_id = os.environ.get("ORCHESTRA_PIPELINE_ID") or str(ulid.new())
    trace_id, parent_span_id = _parse_traceparent()

    # When TRACEPARENT carries an external parent span, use it as the initial
    # _span_id so the first obs.span() child correctly sets parent_span_id to
    # the remote caller's span — preserving cross-process OTel trace linkage.
    initial_span_id = (
        parent_span_id if parent_span_id is not None else secrets.token_hex(8)
    )
    bind_kwargs: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "_trace_id": trace_id,
        "_span_id": initial_span_id,
    }
    if parent_span_id is not None:
        bind_kwargs["parent_span_id"] = parent_span_id
    structlog.contextvars.bind_contextvars(**bind_kwargs)

    env_extra = os.environ.get("ORCHESTRA_REDACT_KEYS", "")
    env_keys = {k.strip().lower() for k in env_extra.split(",") if k.strip()}
    param_keys = {k.lower() for k in (redact_keys or [])}
    all_redact = frozenset(_DEFAULT_REDACT_KEYS | env_keys | param_keys)

    level_name = (level or os.environ.get("ORCHESTRA_LOG_LEVEL") or "info").lower()
    min_level = {"debug": 10, "info": 20, "warning": 30, "error": 40}.get(
        level_name, 20
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _redact(all_redact),
            _reshape_to_otel(project),
            structlog.processors.JSONRenderer(sort_keys=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
        logger_factory=structlog.WriteLoggerFactory(file=output_path.open("a")),
        cache_logger_on_first_use=True,
    )


def get_logger(module: str | None = None) -> structlog.BoundLogger:
    return structlog.get_logger(module=module) if module else structlog.get_logger()


class Span:
    def __init__(self, span_id: str, parent_span_id: str | None, trace_id: str):
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.trace_id = trace_id
        self._attrs: dict[str, Any] = {}

    def set_attrs(self, **attrs: Any) -> None:
        self._attrs.update(attrs)


def _exc_to_dict(exc: BaseException) -> dict[str, Any]:
    d: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        d["caused_by"] = _exc_to_dict(cause)
    return d


@contextmanager
def span(event: str, **attrs: Any) -> Iterator[Span]:
    log = get_logger()
    ctx = structlog.contextvars.get_contextvars()
    parent_span_id = ctx.get("_span_id")
    trace_id = ctx.get("_trace_id", "0" * 32)

    new_span_id = secrets.token_hex(8)
    sp = Span(new_span_id, parent_span_id, trace_id)

    # push new span, parent_span_id
    structlog.contextvars.bind_contextvars(
        _span_id=new_span_id,
        parent_span_id=parent_span_id,
    )
    log.info(f"{event}.started", **attrs)
    try:
        yield sp
    except BaseException as exc:
        log.error(f"{event}.failed", error=_exc_to_dict(exc), **sp._attrs)
        raise
    else:
        log.info(f"{event}.ended", **sp._attrs)
    finally:
        # restore previous span context
        structlog.contextvars.unbind_contextvars("_span_id", "parent_span_id")
        if parent_span_id is not None:
            structlog.contextvars.bind_contextvars(_span_id=parent_span_id)


def child_env() -> dict[str, str]:
    """Env-var dict to merge into subprocess env= for trace propagation."""
    ctx = structlog.contextvars.get_contextvars()
    trace_id = ctx.get("_trace_id", "0" * 32)
    span_id = ctx.get("_span_id", "0" * 16)
    pipeline_id = ctx.get("pipeline_id", "")
    env = {
        "TRACEPARENT": f"00-{trace_id}-{span_id}-01",
        "ORCHESTRA_PIPELINE_ID": pipeline_id,
    }
    log_dir = os.environ.get("ORCHESTRA_LOG_DIR")
    if log_dir:
        env["ORCHESTRA_LOG_DIR"] = str(Path(log_dir).resolve())
    return env


def current_trace_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("_trace_id")


def current_span_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("_span_id")


def current_pipeline_id() -> str | None:
    return structlog.contextvars.get_contextvars().get("pipeline_id")
