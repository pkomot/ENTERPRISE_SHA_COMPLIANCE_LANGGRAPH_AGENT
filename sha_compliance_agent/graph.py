"""Graph assembly, checkpointing and the high-level audit service.

Topology::

    START -> ingest_and_validate
               |-- invalid --> fallback_node --------------------------+
               '-- valid ----> audit_odpc_node  --+                    |
                               audit_kmpdc_node --+-> audit_hmis_dha_node
                                                       -> evaluate_compliance_scorecard
                     +---------------------------------------+---------+
                     | flagged                    | clean    | fatal
                     v                            |          v
               human_review_interrupt ----------->+    fallback_node
                                                  v          |
                                    compile_final_audit_report <-+ -> END
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, RetryPolicy, StateSnapshot

from sha_compliance_agent.config import AuditSettings
from sha_compliance_agent.context import AuditContext
from sha_compliance_agent.llm import SUMMARY_TAG, LLMService
from sha_compliance_agent.nodes import (
    NETWORK_NODES,
    audit_hmis_dha_node,
    audit_kmpdc_node,
    audit_odpc_node,
    compile_final_audit_report,
    evaluate_compliance_scorecard,
    fallback_node,
    human_review_interrupt,
    ingest_and_validate,
    route_after_ingest,
    route_after_scorecard,
)
from sha_compliance_agent.registries import RegistryGateway
from sha_compliance_agent.resilience import TRANSIENT_ERRORS
from sha_compliance_agent.schemas import InspectorDecision
from sha_compliance_agent.state import AuditInput, AuditState


def network_retry_policy(settings: AuditSettings) -> RetryPolicy:
    """RetryPolicy for nodes that call external registries."""
    return RetryPolicy(
        max_attempts=settings.retry_max_attempts,
        initial_interval=settings.retry_initial_interval_s,
        backoff_factor=2.0,
        max_interval=10.0,
        jitter=True,
        retry_on=TRANSIENT_ERRORS,
    )


def build_graph(settings: AuditSettings) -> StateGraph:
    """Construct the (uncompiled) audit StateGraph."""
    builder = StateGraph(AuditState, context_schema=AuditContext, input_schema=AuditInput)
    retry = network_retry_policy(settings)

    builder.add_node("ingest_and_validate", ingest_and_validate)
    network_nodes = {
        "audit_odpc_node": audit_odpc_node,
        "audit_kmpdc_node": audit_kmpdc_node,
        "audit_hmis_dha_node": audit_hmis_dha_node,
    }
    for name in NETWORK_NODES:
        builder.add_node(name, network_nodes[name], retry_policy=retry)
    builder.add_node("evaluate_compliance_scorecard", evaluate_compliance_scorecard)
    builder.add_node("human_review_interrupt", human_review_interrupt)
    builder.add_node("fallback_node", fallback_node)
    builder.add_node("compile_final_audit_report", compile_final_audit_report)

    builder.add_edge(START, "ingest_and_validate")
    builder.add_conditional_edges(
        "ingest_and_validate",
        route_after_ingest,
        ["audit_odpc_node", "audit_kmpdc_node", "fallback_node"],
    )
    # Join: DHA audit runs once both parallel branches have written their results.
    builder.add_edge(["audit_odpc_node", "audit_kmpdc_node"], "audit_hmis_dha_node")
    builder.add_edge("audit_hmis_dha_node", "evaluate_compliance_scorecard")
    builder.add_conditional_edges(
        "evaluate_compliance_scorecard",
        route_after_scorecard,
        ["human_review_interrupt", "compile_final_audit_report", "fallback_node"],
    )
    builder.add_edge("human_review_interrupt", "compile_final_audit_report")
    builder.add_edge("fallback_node", "compile_final_audit_report")
    builder.add_edge("compile_final_audit_report", END)
    return builder


StreamKind = Literal["node_update", "custom", "token", "interrupt"]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One normalised streaming event from an audit run."""

    kind: StreamKind
    node: str | None
    data: Any


class ComplianceAuditService:
    """Owns the gateway, LLM, checkpointer and compiled graph for audit runs.

    Use as an async context manager::

        async with ComplianceAuditService(settings) as service:
            async for event in service.stream_audit(payload, thread_id="audit-1"):
                ...
    """

    def __init__(
        self,
        settings: AuditSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        llm: LLMService | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._llm = llm
        self._checkpointer = checkpointer
        self._stack = contextlib.AsyncExitStack()
        self.graph: CompiledStateGraph | None = None
        self.context: AuditContext | None = None

    async def __aenter__(self) -> ComplianceAuditService:
        gateway = await self._stack.enter_async_context(
            RegistryGateway(self.settings, transport=self._transport)
        )
        llm = self._llm or LLMService.from_settings(self.settings)
        checkpointer = self._checkpointer or await self._open_checkpointer()
        self.context = AuditContext(settings=self.settings, gateway=gateway, llm=llm)
        self.graph = build_graph(self.settings).compile(checkpointer=checkpointer)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._stack.aclose()

    async def _open_checkpointer(self) -> BaseCheckpointSaver:
        if not self.settings.checkpoint_db:
            return MemorySaver()
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        return await self._stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(self.settings.checkpoint_db)
        )

    @staticmethod
    def new_thread_id() -> str:
        return f"sha-audit-{uuid.uuid4()}"

    @staticmethod
    def thread_config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    def _require_ready(self) -> tuple[CompiledStateGraph, AuditContext]:
        if self.graph is None or self.context is None:
            raise RuntimeError("ComplianceAuditService must be used inside 'async with'")
        return self.graph, self.context

    async def stream_audit(
        self, payload: dict[str, Any], *, thread_id: str
    ) -> AsyncIterator[AuditEvent]:
        """Start an audit and stream node updates, custom report chunks and LLM tokens."""
        async for event in self._stream({"payload": payload}, thread_id):
            yield event

    async def resume_with_decision(
        self, decision: InspectorDecision | dict[str, Any], *, thread_id: str
    ) -> AsyncIterator[AuditEvent]:
        """Resume a run paused at the inspector gate."""
        value = (
            decision.model_dump(mode="json")
            if isinstance(decision, InspectorDecision)
            else decision
        )
        async for event in self._stream(Command(resume=value), thread_id):
            yield event

    async def _stream(self, graph_input: Any, thread_id: str) -> AsyncIterator[AuditEvent]:
        graph, context = self._require_ready()
        async for mode, chunk in graph.astream(
            graph_input,
            self.thread_config(thread_id),
            context=context,
            stream_mode=["updates", "custom", "messages"],
        ):
            if mode == "updates":
                for node, update in chunk.items():
                    if node == "__interrupt__":
                        for item in update:
                            yield AuditEvent("interrupt", None, item.value)
                    else:
                        yield AuditEvent("node_update", node, update)
            elif mode == "custom":
                yield AuditEvent("custom", None, chunk)
            elif mode == "messages":
                message, metadata = chunk
                if SUMMARY_TAG in (metadata.get("tags") or []) and message.text:
                    yield AuditEvent("token", metadata.get("langgraph_node"), message.text)

    async def get_state(self, thread_id: str) -> StateSnapshot:
        graph, _ = self._require_ready()
        return await graph.aget_state(self.thread_config(thread_id))

    async def history(self, thread_id: str) -> list[StateSnapshot]:
        """All checkpoints for a thread, newest first (for replay / time travel)."""
        graph, _ = self._require_ready()
        return [s async for s in graph.aget_state_history(self.thread_config(thread_id))]

    async def run_to_completion(
        self, payload: dict[str, Any], *, thread_id: str | None = None
    ) -> tuple[str, AuditState]:
        """Run without streaming consumers; returns (thread_id, final or paused state)."""
        thread_id = thread_id or self.new_thread_id()
        async for _ in self.stream_audit(payload, thread_id=thread_id):
            pass
        return thread_id, (await self.get_state(thread_id)).values
