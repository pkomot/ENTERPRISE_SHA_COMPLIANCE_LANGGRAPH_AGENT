"""Graph state schema and the helpers that build log and error records.

State values are JSON-safe dicts (not Pydantic instances) so every checkpoint
serialises cleanly with both MemorySaver and AsyncSqliteSaver.
"""

from __future__ import annotations

import operator
import traceback
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict


class AuditInput(TypedDict):
    """Public input contract for one audit run."""

    payload: dict[str, Any]


class AuditState(TypedDict, total=False):
    """Full audit state.

    ``audit_log`` and ``audit_errors`` use ``operator.add`` reducers so the
    parallel ODPC and KMPDC branches can append in the same superstep without
    overwriting each other.
    """

    payload: dict[str, Any]
    facility: dict[str, Any] | None
    odpc_result: dict[str, Any] | None
    kmpdc_result: dict[str, Any] | None
    hmis_result: dict[str, Any] | None
    scorecard: dict[str, Any] | None
    review: dict[str, Any] | None
    report: dict[str, Any] | None
    status: str
    fatal_error: bool
    audit_log: Annotated[list[dict[str, Any]], operator.add]
    audit_errors: Annotated[list[dict[str, Any]], operator.add]


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def log_entry(node: str, event: str, **data: Any) -> dict[str, Any]:
    """Build one structured audit-trail record."""
    return {"ts": utc_now_iso(), "node": node, "event": event, **data}


def error_entry(
    node: str,
    error: BaseException | str,
    *,
    kind: str,
    recoverable: bool,
    **data: Any,
) -> dict[str, Any]:
    """Build one structured error record for ``state["audit_errors"]``."""
    if isinstance(error, BaseException):
        message = f"{type(error).__name__}: {error}"
        trace = "".join(traceback.format_exception_only(type(error), error)).strip()
    else:
        message, trace = error, None
    return {
        "ts": utc_now_iso(),
        "node": node,
        "kind": kind,
        "recoverable": recoverable,
        "message": message,
        "trace": trace,
        **data,
    }
