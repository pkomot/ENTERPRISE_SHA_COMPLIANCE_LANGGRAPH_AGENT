"""Fault-tolerance primitives: circuit breaker, TTL cache and the node guard.

The node guard is what reconciles LangGraph ``RetryPolicy`` with the
"never crash the graph" requirement:

* Transient network errors are re-raised while attempts remain, so the node's
  ``RetryPolicy`` retries them with backoff.
* On the final attempt, or for any non-transient error, the guard converts the
  exception into a degraded state update plus a structured ``audit_errors``
  record. The graph keeps running.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Generic, TypeVar

import httpx
from langgraph.errors import GraphBubbleUp

from sha_compliance_agent.state import AuditState, error_entry, log_entry

if TYPE_CHECKING:
    from langgraph.runtime import Runtime

    from sha_compliance_agent.context import AuditContext

logger = logging.getLogger(__name__)

TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (httpx.HTTPError, asyncio.TimeoutError)

V = TypeVar("V")


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Consecutive-failure circuit breaker.

    After ``failure_threshold`` consecutive failures the breaker opens and
    callers skip the network entirely (serving cache or degraded state) until
    ``reset_timeout_s`` elapses. One probe call is then allowed (half-open).
    """

    name: str
    failure_threshold: int = 3
    reset_timeout_s: float = 30.0
    clock: Callable[[], float] = time.monotonic
    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _probe_in_flight: bool = field(default=False, init=False)

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self.clock() - self._opened_at >= self.reset_timeout_s:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allow_request(self) -> bool:
        """Whether a network call may be attempted now."""
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.HALF_OPEN and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        self._probe_in_flight = False
        if self._failures >= self.failure_threshold or self._opened_at is not None:
            if self._opened_at is None:
                logger.warning("circuit breaker '%s' opened", self.name)
            self._opened_at = self.clock()


class TTLCache(Generic[V]):
    """Minimal async-safe in-process TTL cache for last-known-good registry data.

    Swap for Redis or similar when running multiple workers.
    """

    def __init__(self, ttl_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._items: dict[str, tuple[float, V]] = {}

    def get(self, key: str) -> V | None:
        item = self._items.get(key)
        if item is None:
            return None
        stored_at, value = item
        if self._clock() - stored_at > self._ttl_s:
            self._items.pop(key, None)
            return None
        return value

    def set(self, key: str, value: V) -> None:
        self._items[key] = (self._clock(), value)

    def __len__(self) -> int:
        return len(self._items)


NodeFn = Callable[[AuditState, "Runtime[AuditContext]"], Awaitable[dict[str, Any]]]
FailureHandler = Callable[[AuditState, BaseException], dict[str, Any]]


def guarded_node(node_name: str, on_failure: FailureHandler) -> Callable[[NodeFn], NodeFn]:
    """Wrap a node so failures degrade state instead of crashing the graph.

    Args:
        node_name: Name recorded in error and log entries.
        on_failure: Builds the degraded state update for an unrecoverable error.
    """

    def decorator(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        async def wrapper(state: AuditState, runtime: Runtime[AuditContext]) -> dict[str, Any]:
            try:
                return await fn(state, runtime)
            except GraphBubbleUp:
                # Interrupts and other control-flow signals must reach LangGraph.
                raise
            except TRANSIENT_ERRORS as exc:
                info = runtime.execution_info
                attempt = info.node_attempt if info is not None else 1
                max_attempts = runtime.context.settings.retry_max_attempts
                if attempt < max_attempts:
                    logger.info(
                        "%s transient failure (attempt %d/%d): %r",
                        node_name,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    raise
                return _degrade(
                    node_name, state, exc, on_failure, kind="retries_exhausted", attempt=attempt
                )
            except Exception as exc:  # noqa: BLE001 - deliberate catch-all guardrail
                logger.exception("%s failed with a non-transient error", node_name)
                return _degrade(node_name, state, exc, on_failure, kind="unhandled_exception")

        return wrapper

    return decorator


def _degrade(
    node_name: str,
    state: AuditState,
    exc: BaseException,
    on_failure: FailureHandler,
    *,
    kind: str,
    **extra: Any,
) -> dict[str, Any]:
    update = dict(on_failure(state, exc))
    update["audit_errors"] = [
        *update.get("audit_errors", []),
        error_entry(node_name, exc, kind=kind, recoverable=False, **extra),
    ]
    update["audit_log"] = [
        *update.get("audit_log", []),
        log_entry(node_name, "degraded", reason=kind),
    ]
    return update
