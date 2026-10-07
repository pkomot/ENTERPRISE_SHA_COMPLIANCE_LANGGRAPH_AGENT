from __future__ import annotations

import time

import httpx

from sha_compliance_agent.graph import ComplianceAuditService
from sha_compliance_agent.mock_registries import MockRegistryServer
from sha_compliance_agent.resilience import BreakerState
from sha_compliance_agent.schemas import Registry
from tests.conftest import COMPLIANT, MORATORIUM, UPCODING

DECISION = {
    "action": "OVERRIDE_COMPLIANT",
    "inspector_id": "SHA-INSP-0042",
    "justification": "Registration evidence verified on site; registry not yet updated.",
}


def _checks(state: dict, branch_key: str) -> dict[str, dict]:
    return {c["check_id"]: c for c in state[branch_key]["checks"]}


async def test_compliant_facility_completes_without_interrupt(
    service: ComplianceAuditService,
) -> None:
    thread_id = service.new_thread_id()
    events = [e async for e in service.stream_audit(COMPLIANT, thread_id=thread_id)]

    assert not [e for e in events if e.kind == "interrupt"]
    state = (await service.get_state(thread_id)).values
    assert state["report"]["final_verdict"] == "COMPLIANT"
    assert state["scorecard"]["compliance_index"] == 1.0
    assert state["audit_errors"] == []
    assert "# SHA Facility Compliance Audit" in state["report"]["markdown"]


async def test_streaming_emits_tokens_and_report_chunks(service: ComplianceAuditService) -> None:
    events = [e async for e in service.stream_audit(COMPLIANT, thread_id=service.new_thread_id())]
    tokens = [e.data for e in events if e.kind == "token"]
    custom = [e.data["type"] for e in events if e.kind == "custom"]

    assert len(tokens) > 3, "narrative should stream token-by-token"
    assert custom[0] == "report.scorecard_json"
    assert "report.markdown_chunk" in custom


async def test_parallel_branches_both_log(service: ComplianceAuditService) -> None:
    _, state = await service.run_to_completion(COMPLIANT)
    nodes = [entry["node"] for entry in state["audit_log"]]
    assert "audit_odpc_node" in nodes and "audit_kmpdc_node" in nodes


async def test_moratorium_interrupts_and_resumes(service: ComplianceAuditService) -> None:
    thread_id, state = await service.run_to_completion(MORATORIUM)
    snapshot = await service.get_state(thread_id)

    assert snapshot.next == ("human_review_interrupt",)
    assert state["scorecard"]["sha_non_compliant"] is True
    assert set(state["scorecard"]["moratorium_triggers"]) >= {
        "odpc.controller_registered",
        "dha.hmis_certified",
    }

    async for _ in service.resume_with_decision(DECISION, thread_id=thread_id):
        pass
    final = (await service.get_state(thread_id)).values
    assert final["report"]["final_verdict"] == "COMPLIANT"
    assert final["review"]["inspector_id"] == "SHA-INSP-0042"
    assert final["scorecard"]["verdict"] == "NON_COMPLIANT"  # automated verdict preserved


async def test_invalid_decision_reprompts(service: ComplianceAuditService) -> None:
    thread_id, _ = await service.run_to_completion(MORATORIUM)
    events = [
        e async for e in service.resume_with_decision({"action": "APPROVE"}, thread_id=thread_id)
    ]
    reprompt = [e for e in events if e.kind == "interrupt"]
    assert reprompt and "previous_error" in reprompt[0].data

    async for _ in service.resume_with_decision(DECISION, thread_id=thread_id):
        pass
    final = (await service.get_state(thread_id)).values
    assert final["status"] == "COMPLETED"
    assert any(e["kind"] == "invalid_decision" for e in final["audit_errors"])


async def test_upcoding_is_critical(service: ComplianceAuditService) -> None:
    _, state = await service.run_to_completion(UPCODING)
    level = _checks(state, "kmpdc_result")["kmpdc.level_matches"]
    assert level["status"] == "FAIL"
    assert "kmpdc.level_matches" in state["scorecard"]["critical_violations"]


async def test_invalid_payload_routes_to_fallback(service: ComplianceAuditService) -> None:
    _, state = await service.run_to_completion(
        {"facility_id": "x", "kmpdc_reg": "y", "declared_level": 9}
    )
    assert state["status"] == "COMPLETED"
    assert state["report"]["final_verdict"] == "AUDIT_INCOMPLETE"
    fields = {e["field"] for e in state["audit_errors"]}
    assert {"facility_id", "kmpdc_reg", "declared_level"} <= fields
    assert state.get("odpc_result") is None


async def test_timeout_falls_back_to_cache_without_retry(
    service: ComplianceAuditService,
    mock_server: MockRegistryServer,
) -> None:
    await service.run_to_completion(COMPLIANT)  # warm cache
    mock_server.latency_s["kmpdc"] = 1.0  # > 0.3s timeout
    mock_server.calls.clear()

    started = time.perf_counter()
    _, state = await service.run_to_completion(COMPLIANT)
    elapsed = time.perf_counter() - started

    kmpdc = _checks(state, "kmpdc_result")
    assert kmpdc["kmpdc.license_valid"]["source"] == "cache"
    assert kmpdc["kmpdc.license_valid"]["status"] == "PASS"
    assert state["kmpdc_result"]["degraded"] is True
    assert mock_server.calls["kmpdc"] == 2  # one attempt per lookup, no retries
    assert elapsed < 1.0


async def test_timeout_without_cache_retries_then_degrades(mock_server: MockRegistryServer) -> None:
    from sha_compliance_agent.config import AuditSettings
    from sha_compliance_agent.llm import LLMService

    # Breaker threshold above the attempt count isolates RetryPolicy behaviour.
    settings = AuditSettings(
        http_timeout_s=0.3,
        retry_initial_interval_s=0.01,
        breaker_failure_threshold=100,
        llm_model=None,
    )
    mock_server.latency_s["odpc"] = 1.0
    async with ComplianceAuditService(
        settings, transport=mock_server.transport(), llm=LLMService(None, timeout_s=5)
    ) as svc:
        _, state = await svc.run_to_completion(COMPLIANT)

    assert mock_server.calls["odpc"] == 2 * 3  # 2 lookups x 3 attempts
    odpc = _checks(state, "odpc_result")
    assert {c["status"] for c in odpc.values()} == {"UNVERIFIED"}
    assert any(
        e["kind"] == "retries_exhausted" and e["node"] == "audit_odpc_node"
        for e in state["audit_errors"]
    )
    assert state["scorecard"]["verdict"] == "PROVISIONAL"


async def test_circuit_breaker_short_circuits_retries(
    service: ComplianceAuditService,
    mock_server: MockRegistryServer,
) -> None:
    mock_server.latency_s["odpc"] = 1.0
    _, state = await service.run_to_completion(COMPLIANT)

    # Breaker (threshold 3) opens during attempt 2; attempt 3 makes no network call.
    assert mock_server.calls["odpc"] == 4
    assert service.context.gateway.breaker(Registry.ODPC).state is BreakerState.OPEN
    odpc = _checks(state, "odpc_result")
    assert {c["source"] for c in odpc.values()} == {"unavailable"}
    assert state["scorecard"]["verdict"] == "PROVISIONAL"


async def test_http_5xx_retries_then_degrades(
    service: ComplianceAuditService,
    mock_server: MockRegistryServer,
) -> None:
    mock_server.fail_status["cihis"] = 503
    _, state = await service.run_to_completion(COMPLIANT)
    dha = _checks(state, "hmis_result")
    assert dha["dha.hmis_certified"]["status"] == "UNVERIFIED"
    assert mock_server.calls["cihis"] == 3


async def test_non_transient_error_degrades_without_crash(
    service: ComplianceAuditService,
    mock_server: MockRegistryServer,
) -> None:
    original = mock_server._route

    def broken(service_name: str, request: httpx.Request) -> httpx.Response:
        if service_name == "kmpdc":
            return httpx.Response(200, content=b"<html>not json</html>")
        return original(service_name, request)

    mock_server._route = broken  # type: ignore[method-assign]
    _, state = await service.run_to_completion(COMPLIANT)
    assert any(
        e["kind"] == "unhandled_exception" and e["node"] == "audit_kmpdc_node"
        for e in state["audit_errors"]
    )
    assert state["scorecard"]["verdict"] == "PROVISIONAL"


async def test_checkpoint_history_supports_replay(service: ComplianceAuditService) -> None:
    thread_id, _ = await service.run_to_completion(COMPLIANT)
    history = await service.history(thread_id)
    assert len(history) >= 6
    steps = [snap.metadata["step"] for snap in history]
    assert steps == sorted(steps, reverse=True)
    before_scoring = next(s for s in history if s.next == ("evaluate_compliance_scorecard",))
    assert before_scoring.values.get("scorecard") is None


async def test_registry_token_not_sent_to_fhir_hosts(mock_server: MockRegistryServer) -> None:
    from sha_compliance_agent.config import AuditSettings
    from sha_compliance_agent.llm import LLMService

    seen: dict[str, str | None] = {}
    inner = mock_server._handle

    async def spy(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("authorization")
        return await inner(request)

    settings = AuditSettings(registry_bearer_token="secret-token", llm_model=None)
    async with ComplianceAuditService(
        settings, transport=httpx.MockTransport(spy), llm=LLMService(None, timeout_s=5)
    ) as svc:
        await svc.run_to_completion(COMPLIANT)

    assert seen["odpc.registry.mock"] == "Bearer secret-token"
    assert seen["fhir.f10234.mock"] is None
