"""Graph nodes for the SHA / DHA compliance audit workflow.

Every node is ``async`` and receives ``(state, runtime)``; dependencies come from
``runtime.context`` (:class:`~sha_compliance_agent.context.AuditContext`).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import ValidationError

from sha_compliance_agent.context import AuditContext
from sha_compliance_agent.registries import RegistryResponse
from sha_compliance_agent.resilience import guarded_node
from sha_compliance_agent.schemas import (
    REGULATION_CATALOG,
    CheckResult,
    CheckStatus,
    ComplianceScorecard,
    DataSource,
    FacilityPayload,
    InspectorDecision,
    KmpdcRemarksExtraction,
    PolicyInterpretation,
    RegistryAuditResult,
    RegulationCode,
    RemediationAction,
    Verdict,
    humanise_check_id,
)
from sha_compliance_agent.state import AuditState, error_entry, log_entry, utc_now_iso

# ---------------------------------------------------------------------------
# Check catalogue: the single source of truth for weights and severity.
# ---------------------------------------------------------------------------
Branch = Literal["ODPC", "KMPDC", "DHA"]


@dataclass(frozen=True, slots=True)
class CheckSpec:
    branch: Branch
    weight: float
    critical: bool
    moratorium: bool
    regulation: RegulationCode
    remediation: str


CHECK_CATALOG: dict[str, CheckSpec] = {
    "odpc.controller_registered": CheckSpec(
        "ODPC",
        0.20,
        True,
        True,
        "DPA_2019",
        "Register the facility as a data controller/processor with the ODPC.",
    ),
    "odpc.registration_current": CheckSpec(
        "ODPC",
        0.10,
        True,
        True,
        "DPA_REGISTRATION_REGS_2021",
        "Renew the ODPC registration certificate before expiry.",
    ),
    "odpc.no_enforcement_notices": CheckSpec(
        "ODPC",
        0.05,
        False,
        False,
        "DPA_2019",
        "Resolve open ODPC enforcement notices and file proof of compliance.",
    ),
    "kmpdc.facility_registered": CheckSpec(
        "KMPDC", 0.10, True, False, "MPD_ACT_CAP_253", "Register the facility with KMPDC."
    ),
    "kmpdc.identity_matches": CheckSpec(
        "KMPDC",
        0.05,
        True,
        False,
        "MPD_ACT_CAP_253",
        "Reconcile the KMHFL code on the KMPDC register with the SHA submission.",
    ),
    "kmpdc.license_valid": CheckSpec(
        "KMPDC",
        0.15,
        True,
        False,
        "MPD_ACT_CAP_253",
        "Renew the KMPDC facility licence and clear any suspension.",
    ),
    "kmpdc.level_matches": CheckSpec(
        "KMPDC",
        0.10,
        True,
        False,
        "SHI_ACT_2023",
        "Re-declare the SHA contracting level to match the KMPDC-registered level.",
    ),
    "dha.hmis_certified": CheckSpec(
        "DHA",
        0.10,
        True,
        True,
        "DIGITAL_HEALTH_ACT_2023",
        "Obtain Digital Health Agency HMIS certification.",
    ),
    "dha.fhir_r4": CheckSpec(
        "DHA",
        0.08,
        True,
        False,
        "HL7_FHIR_R4",
        "Upgrade the HMIS API to HL7 FHIR R4 (4.0.x) and publish a CapabilityStatement.",
    ),
    "dha.snomed_ct_binding": CheckSpec(
        "DHA",
        0.04,
        False,
        False,
        "SNOMED_CT",
        "Bind clinical terminology to SNOMED CT on the facility FHIR server.",
    ),
    "dha.cihis_endpoint_ready": CheckSpec(
        "DHA",
        0.03,
        False,
        False,
        "DIGITAL_HEALTH_ACT_2023",
        "Register the CIHIS endpoint and restore daily synchronisation.",
    ),
}

CIHIS_MAX_SYNC_AGE = timedelta(days=7)
NETWORK_NODES = ("audit_odpc_node", "audit_kmpdc_node", "audit_hmis_dha_node")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _check(
    check_id: str,
    ok: bool | None,
    response: RegistryResponse | None,
    detail: str,
    **evidence: Any,
) -> CheckResult:
    spec = CHECK_CATALOG[check_id]
    status = (
        CheckStatus.UNVERIFIED if ok is None else (CheckStatus.PASS if ok else CheckStatus.FAIL)
    )
    source = response.source if response is not None else DataSource.LIVE
    if response is not None and response.degraded_reason:
        evidence["degraded_reason"] = response.degraded_reason
    return CheckResult(
        check_id=check_id,
        status=status,
        source=source,
        critical=spec.critical,
        detail=detail,
        evidence=evidence,
    )


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _unverified_branch(branch: Branch, reason: str) -> dict[str, Any]:
    checks = [
        CheckResult(
            check_id=check_id,
            status=CheckStatus.UNVERIFIED,
            source=DataSource.UNAVAILABLE,
            critical=spec.critical,
            detail=f"Not verified: {reason}",
        )
        for check_id, spec in CHECK_CATALOG.items()
        if spec.branch == branch
    ]
    return RegistryAuditResult(branch=branch, checks=checks, degraded=True).model_dump(mode="json")


def _branch_failure(branch: Branch, key: str) -> Any:
    def handler(_state: AuditState, exc: BaseException) -> dict[str, Any]:
        return {key: _unverified_branch(branch, type(exc).__name__)}

    return handler


def _finish_branch(
    branch: Branch,
    checks: list[CheckResult],
    started: float,
    *,
    advisories: list[str] | None = None,
    engine: str | None = None,
) -> RegistryAuditResult:
    return RegistryAuditResult(
        branch=branch,
        checks=checks,
        advisories=advisories or [],
        degraded=any(c.source is not DataSource.LIVE for c in checks),
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        extraction_engine=engine,
    )


# ---------------------------------------------------------------------------
# 1. ingest_and_validate
# ---------------------------------------------------------------------------
async def ingest_and_validate(state: AuditState, runtime: Runtime[AuditContext]) -> dict[str, Any]:
    """Validate the raw payload against :class:`FacilityPayload`."""
    try:
        facility = FacilityPayload.model_validate(state.get("payload") or {})
    except ValidationError as exc:
        errors = [
            error_entry(
                "ingest_and_validate",
                f"{'.'.join(str(p) for p in err['loc']) or 'payload'}: {err['msg']}",
                kind="input_validation",
                recoverable=False,
                field=".".join(str(p) for p in err["loc"]),
            )
            for err in exc.errors()
        ]
        return {
            "facility": None,
            "status": "INVALID_INPUT",
            "audit_errors": errors,
            "audit_log": [log_entry("ingest_and_validate", "rejected", error_count=len(errors))],
        }
    return {
        "facility": facility.model_dump(mode="json"),
        "status": "VALIDATED",
        "audit_log": [
            log_entry(
                "ingest_and_validate",
                "validated",
                facility_id=facility.facility_id,
                declared_level=facility.declared_level,
                llm_online=runtime.context.llm.online,
            )
        ],
    }


def route_after_ingest(state: AuditState) -> list[str] | str:
    """Fan out to the parallel regulatory branches, or divert invalid input."""
    if not state.get("facility"):
        return "fallback_node"
    return ["audit_odpc_node", "audit_kmpdc_node"]


# ---------------------------------------------------------------------------
# 2a. audit_odpc_node (parallel branch)
# ---------------------------------------------------------------------------
@guarded_node("audit_odpc_node", _branch_failure("ODPC", "odpc_result"))
async def audit_odpc_node(state: AuditState, runtime: Runtime[AuditContext]) -> dict[str, Any]:
    """Verify ODPC data-controller registration and open enforcement notices."""
    started = time.perf_counter()
    facility_id = state["facility"]["facility_id"]
    gateway = runtime.context.gateway

    controller, notices = await asyncio.gather(
        gateway.odpc_data_controller(facility_id),
        gateway.odpc_enforcement_notices(facility_id),
    )

    record = controller.data or {}
    registered: bool | None = (
        None
        if controller.found is None
        else bool(controller.found and record.get("status") == "REGISTERED")
    )
    expiry = _parse_date(record.get("expiry_date"))
    current: bool | None = (
        None
        if registered is None
        else bool(registered and expiry is not None and expiry >= date.today())
    )
    open_notices = (
        None
        if notices.found is None
        else [n for n in (notices.data or {}).get("notices", []) if n.get("status") == "OPEN"]
    )

    checks = [
        _check(
            "odpc.controller_registered",
            registered,
            controller,
            "Registered with ODPC"
            if registered
            else "No ODPC data-controller registration found"
            if registered is False
            else "ODPC registry unavailable",
            registration_no=record.get("registration_no"),
        ),
        _check(
            "odpc.registration_current",
            current,
            controller,
            f"Certificate valid until {expiry}"
            if current
            else "Registration missing or expired"
            if current is False
            else "Expiry could not be verified",
            expiry_date=record.get("expiry_date"),
        ),
        _check(
            "odpc.no_enforcement_notices",
            None if open_notices is None else not open_notices,
            notices,
            "No open enforcement notices"
            if open_notices == []
            else f"{len(open_notices)} open enforcement notice(s)"
            if open_notices
            else "Enforcement notices could not be checked",
            open_notices=open_notices or [],
        ),
    ]
    result = _finish_branch("ODPC", checks, started)
    return {
        "odpc_result": result.model_dump(mode="json"),
        "audit_log": [
            log_entry(
                "audit_odpc_node",
                "completed",
                latency_ms=result.latency_ms,
                degraded=result.degraded,
            )
        ],
    }


# ---------------------------------------------------------------------------
# 2b. audit_kmpdc_node (parallel branch)
# ---------------------------------------------------------------------------
REMARKS_SYSTEM_PROMPT = (
    "You extract facts from Kenya Medical Practitioners and Dentists Council (KMPDC) "
    "facility inspection remarks. Only report conditions, restrictions and re-inspection "
    "requirements that are explicitly stated. Do not infer or invent facts."
)


def _deterministic_remarks(remarks: str) -> KmpdcRemarksExtraction:
    clauses = [c.strip(" .") for c in remarks.replace(":", ";").split(";") if c.strip(" .")]
    lowered = remarks.lower()
    return KmpdcRemarksExtraction(
        pending_conditions=[
            c
            for c in clauses
            if any(k in c.lower() for k in ("install", "by q", "pending", "must", "required"))
            and "restrict" not in c.lower()
        ],
        service_restrictions=[
            c
            for c in clauses
            if any(k in c.lower() for k in ("restrict", "not licensed", "barred"))
        ],
        reinspection_required="re-inspection" in lowered or "reinspection" in lowered,
    )


@guarded_node("audit_kmpdc_node", _branch_failure("KMPDC", "kmpdc_result"))
async def audit_kmpdc_node(state: AuditState, runtime: Runtime[AuditContext]) -> dict[str, Any]:
    """Cross-reference KMPDC registration, licence validity and operational level."""
    started = time.perf_counter()
    facility = state["facility"]
    gateway, llm = runtime.context.gateway, runtime.context.llm

    register, licence = await asyncio.gather(
        gateway.kmpdc_facility(facility["kmpdc_reg"]),
        gateway.kmpdc_license(facility["kmpdc_reg"]),
    )

    record = register.data or {}
    registered = register.found
    identity_ok = (
        None
        if registered is None
        else bool(registered and record.get("kmhfl_code") == facility["facility_id"])
    )
    licence_data = licence.data or {}
    licence_expiry = _parse_date(licence_data.get("expiry_date"))
    licence_ok = (
        None
        if licence.found is None
        else bool(
            licence.found
            and licence_data.get("status") == "ACTIVE"
            and licence_expiry is not None
            and licence_expiry >= date.today()
        )
    )

    declared = facility["declared_level"]
    registered_level = record.get("registered_level")
    advisories: list[str] = []
    if registered is None:
        level_ok, level_detail = None, "KMPDC registry unavailable"
    elif not isinstance(registered_level, int):
        level_ok, level_detail = False, "No registered level on KMPDC record"
    elif declared > registered_level:
        level_ok = False
        level_detail = (
            f"Declared SHA Level {declared} exceeds KMPDC-registered Level "
            f"{registered_level} (upcoding risk)"
        )
    else:
        level_ok, level_detail = (
            True,
            f"Declared Level {declared}; registered Level {registered_level}",
        )
        if declared < registered_level:
            advisories.append(
                f"Facility under-declares its level ({declared} vs registered {registered_level})."
            )

    checks = [
        _check(
            "kmpdc.facility_registered",
            registered,
            register,
            "Facility on KMPDC register"
            if registered
            else "Registration number not found on KMPDC register"
            if registered is False
            else "KMPDC registry unavailable",
            registration_no=facility["kmpdc_reg"],
        ),
        _check(
            "kmpdc.identity_matches",
            identity_ok,
            register,
            "KMHFL code matches KMPDC record"
            if identity_ok
            else "KMHFL code does not match KMPDC record"
            if identity_ok is False
            else "Identity could not be verified",
            registry_kmhfl_code=record.get("kmhfl_code"),
        ),
        _check(
            "kmpdc.license_valid",
            licence_ok,
            licence,
            f"Licence ACTIVE until {licence_expiry}"
            if licence_ok
            else "Licence missing, inactive or expired"
            if licence_ok is False
            else "Licence status unavailable",
            licence_status=licence_data.get("status"),
            expiry_date=licence_data.get("expiry_date"),
        ),
        _check(
            "kmpdc.level_matches",
            level_ok,
            register,
            level_detail,
            declared_level=declared,
            registered_level=registered_level,
        ),
    ]

    engine = None
    if remarks := (record.get("remarks") or "").strip():
        extraction, engine = await llm.extract(
            KmpdcRemarksExtraction,
            system=REMARKS_SYSTEM_PROMPT,
            content=f"Inspection remarks:\n{remarks}",
            fallback=lambda: _deterministic_remarks(remarks),
        )
        advisories += [f"KMPDC condition pending: {c}" for c in extraction.pending_conditions]
        advisories += [f"KMPDC service restriction: {r}" for r in extraction.service_restrictions]
        if extraction.reinspection_required:
            advisories.append("KMPDC re-inspection pending.")

    result = _finish_branch("KMPDC", checks, started, advisories=advisories, engine=engine)
    return {
        "kmpdc_result": result.model_dump(mode="json"),
        "audit_log": [
            log_entry(
                "audit_kmpdc_node",
                "completed",
                latency_ms=result.latency_ms,
                degraded=result.degraded,
            )
        ],
    }


# ---------------------------------------------------------------------------
# 3. audit_hmis_dha_node
# ---------------------------------------------------------------------------
async def _none() -> None:
    return None


@guarded_node("audit_hmis_dha_node", _branch_failure("DHA", "hmis_result"))
async def audit_hmis_dha_node(state: AuditState, runtime: Runtime[AuditContext]) -> dict[str, Any]:
    """Evaluate DHA integration: HMIS certification, FHIR R4, SNOMED CT and CIHIS."""
    started = time.perf_counter()
    facility = state["facility"]
    gateway = runtime.context.gateway
    fhir_url: str | None = facility.get("fhir_base_url")

    cihis, capability, snomed = await asyncio.gather(
        gateway.cihis_readiness(facility["facility_id"]),
        gateway.fhir_capability_statement(fhir_url) if fhir_url else _none(),
        gateway.fhir_snomed_codesystem(fhir_url) if fhir_url else _none(),
    )

    readiness = cihis.data or {}
    certified = (
        None if cihis.found is None else bool(cihis.found and readiness.get("hmis_certified"))
    )
    last_sync = _parse_date(readiness.get("last_successful_sync"))
    cihis_ok = (
        None
        if cihis.found is None
        else bool(
            cihis.found
            and readiness.get("endpoint_registered")
            and last_sync is not None
            and date.today() - last_sync <= CIHIS_MAX_SYNC_AGE
        )
    )

    if capability is None:
        fhir_ok, fhir_detail, fhir_version = False, "No FHIR base URL declared", None
    elif capability.found is None:
        fhir_ok, fhir_detail, fhir_version = None, "FHIR endpoint unavailable", None
    else:
        statement = capability.data or {}
        fhir_version = statement.get("fhirVersion")
        fhir_ok = bool(
            capability.found
            and statement.get("resourceType") == "CapabilityStatement"
            and str(fhir_version).startswith("4.0")
        )
        fhir_detail = (
            f"CapabilityStatement reports FHIR {fhir_version}"
            if capability.found
            else "No CapabilityStatement at /metadata"
        )

    if snomed is None:
        snomed_ok, snomed_detail = False, "No FHIR base URL declared"
    elif snomed.found is None:
        snomed_ok, snomed_detail = None, "FHIR terminology endpoint unavailable"
    else:
        total = (snomed.data or {}).get("total") or 0
        snomed_ok = bool(snomed.found and total > 0)
        snomed_detail = (
            "SNOMED CT CodeSystem bound" if snomed_ok else "SNOMED CT CodeSystem not found"
        )

    checks = [
        _check(
            "dha.hmis_certified",
            certified,
            cihis,
            f"Certified ({readiness.get('certificate_no')})"
            if certified
            else "No DHA HMIS certification"
            if certified is False
            else "CIHIS registry unavailable",
            certificate_no=readiness.get("certificate_no"),
        ),
        _check("dha.fhir_r4", fhir_ok, capability, fhir_detail, fhir_version=fhir_version),
        _check("dha.snomed_ct_binding", snomed_ok, snomed, snomed_detail),
        _check(
            "dha.cihis_endpoint_ready",
            cihis_ok,
            cihis,
            "CIHIS endpoint registered and syncing"
            if cihis_ok
            else "CIHIS endpoint unregistered or sync stale"
            if cihis_ok is False
            else "CIHIS readiness unavailable",
            last_successful_sync=readiness.get("last_successful_sync"),
        ),
    ]
    result = _finish_branch("DHA", checks, started)
    return {
        "hmis_result": result.model_dump(mode="json"),
        "audit_log": [
            log_entry(
                "audit_hmis_dha_node",
                "completed",
                latency_ms=result.latency_ms,
                degraded=result.degraded,
            )
        ],
    }


# ---------------------------------------------------------------------------
# 4. evaluate_compliance_scorecard
# ---------------------------------------------------------------------------
POLICY_SYSTEM_PROMPT = (
    "You are a Kenyan health-sector compliance analyst for the Social Health Authority. "
    "Interpret the audit findings below. Cite only regulation codes from the provided "
    "catalogue. Do not change or second-guess the numerical score or verdict. "
    "Recommend concrete, prioritised remediation actions (P1 = blocks SHA contracting)."
)


def collect_checks(state: AuditState) -> list[CheckResult]:
    """All checks from the three branches; missing branches become UNVERIFIED."""
    checks: list[CheckResult] = []
    for branch, key in (("ODPC", "odpc_result"), ("KMPDC", "kmpdc_result"), ("DHA", "hmis_result")):
        raw = state.get(key) or _unverified_branch(branch, "branch did not run")
        checks += RegistryAuditResult.model_validate(raw).checks
    return checks


def score_checks(checks: list[CheckResult], threshold: float) -> ComplianceScorecard:
    """Deterministic scoring and SHA moratorium enforcement."""
    by_id = {c.check_id: c for c in checks}
    index = sum(
        spec.weight
        for cid, spec in CHECK_CATALOG.items()
        if (c := by_id.get(cid)) is not None and c.status is CheckStatus.PASS
    )
    failed = [c for c in checks if c.status is CheckStatus.FAIL]
    unverified = [c for c in checks if c.status is CheckStatus.UNVERIFIED]

    moratorium = [c.check_id for c in failed if CHECK_CATALOG[c.check_id].moratorium]
    critical = [c.check_id for c in failed if c.critical]
    unverified_critical = [c for c in unverified if c.critical]

    if moratorium or critical:
        verdict = Verdict.NON_COMPLIANT
    elif unverified_critical:
        verdict = Verdict.PROVISIONAL
    elif index < threshold:
        verdict = Verdict.NON_COMPLIANT
    else:
        verdict = Verdict.COMPLIANT

    return ComplianceScorecard(
        compliance_index=round(min(index, 1.0), 4),
        threshold=threshold,
        verdict=verdict,
        sha_non_compliant=bool(moratorium),
        moratorium_triggers=moratorium,
        critical_violations=critical,
        unverified_checks=[c.check_id for c in unverified],
        degraded_sources=sorted(
            {f"{c.check_id}:{c.source}" for c in checks if c.source is not DataSource.LIVE}
        ),
        requires_human_review=bool(index < threshold or critical or unverified_critical),
    )


def _deterministic_interpretation(
    scorecard: ComplianceScorecard, checks: list[CheckResult]
) -> PolicyInterpretation:
    failing = [c for c in checks if c.status is not CheckStatus.PASS]
    actions = [
        RemediationAction(
            action=CHECK_CATALOG[c.check_id].remediation,
            regulation=CHECK_CATALOG[c.check_id].regulation,
            priority="P1" if CHECK_CATALOG[c.check_id].moratorium else "P2" if c.critical else "P3",
        )
        for c in failing
    ]
    actions.sort(key=lambda a: a.priority)
    summary = (
        f"Verdict {scorecard.verdict} at compliance index {scorecard.compliance_index:.2f} "
        f"(threshold {scorecard.threshold:.2f}). "
        + (
            f"SHA moratorium triggered by {', '.join(scorecard.moratorium_triggers)}. "
            if scorecard.sha_non_compliant
            else ""
        )
        + (
            f"{len(scorecard.unverified_checks)} check(s) could not be verified."
            if scorecard.unverified_checks
            else ""
        )
    ).strip()
    return PolicyInterpretation(
        summary=summary,
        cited_regulations=sorted({a.regulation for a in actions}),
        remediation_actions=actions[:8],
    )


def _scorecard_failure(_state: AuditState, _exc: BaseException) -> dict[str, Any]:
    return {"fatal_error": True, "status": "SCORECARD_FAILED"}


@guarded_node("evaluate_compliance_scorecard", _scorecard_failure)
async def evaluate_compliance_scorecard(
    state: AuditState, runtime: Runtime[AuditContext]
) -> dict[str, Any]:
    """Aggregate branch results, compute the compliance index and apply moratorium rules."""
    settings, llm = runtime.context.settings, runtime.context.llm
    checks = collect_checks(state)
    scorecard = score_checks(checks, settings.compliance_threshold)

    engine = None
    if any(c.status is not CheckStatus.PASS for c in checks):
        findings = [
            c.model_dump(mode="json", include={"check_id", "status", "critical", "detail"})
            for c in checks
            if c.status is not CheckStatus.PASS
        ]
        interpretation, engine = await llm.extract(
            PolicyInterpretation,
            system=POLICY_SYSTEM_PROMPT,
            content=json.dumps(
                {
                    "facility": state["facility"],
                    "scorecard": scorecard.model_dump(mode="json", exclude={"interpretation"}),
                    "findings": findings,
                    "regulation_catalogue": REGULATION_CATALOG,
                },
                indent=2,
            ),
            fallback=lambda: _deterministic_interpretation(scorecard, checks),
        )
        scorecard = scorecard.model_copy(update={"interpretation": interpretation})

    return {
        "scorecard": scorecard.model_dump(mode="json"),
        "status": "SCORED",
        "audit_log": [
            log_entry(
                "evaluate_compliance_scorecard",
                "scored",
                compliance_index=scorecard.compliance_index,
                verdict=scorecard.verdict.value,
                requires_human_review=scorecard.requires_human_review,
                interpretation_engine=engine,
            )
        ],
    }


def route_after_scorecard(state: AuditState) -> str:
    """Send flagged facilities to the inspector gate; everything else to the report."""
    if state.get("fatal_error") or not state.get("scorecard"):
        return "fallback_node"
    if state["scorecard"]["requires_human_review"]:
        return "human_review_interrupt"
    return "compile_final_audit_report"


# ---------------------------------------------------------------------------
# 5. human_review_interrupt (HITL gate)
# ---------------------------------------------------------------------------
MAX_REVIEW_PROMPTS = 3


async def human_review_interrupt(
    state: AuditState, runtime: Runtime[AuditContext]
) -> dict[str, Any]:
    """Pause for an SHA inspector decision. Resume with ``Command(resume=<decision dict>)``.

    Invalid decisions are rejected and the inspector is re-prompted (up to
    ``MAX_REVIEW_PROMPTS`` times) without leaving the node.
    """
    scorecard = state["scorecard"]
    request: dict[str, Any] = {
        "type": "inspector_review_required",
        "facility": state["facility"],
        "verdict": scorecard["verdict"],
        "compliance_index": scorecard["compliance_index"],
        "moratorium_triggers": scorecard["moratorium_triggers"],
        "critical_violations": scorecard["critical_violations"],
        "unverified_checks": scorecard["unverified_checks"],
        "allowed_actions": ["UPHOLD", "OVERRIDE_COMPLIANT", "OVERRIDE_NON_COMPLIANT", "ESCALATE"],
        "decision_schema": InspectorDecision.model_json_schema(),
    }
    errors: list[dict[str, Any]] = []
    for prompt_no in range(1, MAX_REVIEW_PROMPTS + 1):
        raw = interrupt(request)
        try:
            decision = InspectorDecision.model_validate(raw)
        except ValidationError as exc:
            errors.append(
                error_entry(
                    "human_review_interrupt",
                    exc,
                    kind="invalid_decision",
                    recoverable=True,
                    prompt_no=prompt_no,
                )
            )
            request = {**request, "previous_error": str(exc)}
            continue
        return {
            "review": decision.model_dump(mode="json"),
            "status": "REVIEWED",
            "audit_errors": errors,
            "audit_log": [
                log_entry(
                    "human_review_interrupt",
                    "decision_recorded",
                    action=decision.action,
                    inspector_id=decision.inspector_id,
                )
            ],
        }

    return {
        "review": None,
        "status": "REVIEW_INVALID",
        "audit_errors": [
            *errors,
            error_entry(
                "human_review_interrupt",
                "No valid inspector decision; automated verdict stands",
                kind="review_abandoned",
                recoverable=False,
            ),
        ],
        "audit_log": [log_entry("human_review_interrupt", "review_abandoned")],
    }


# ---------------------------------------------------------------------------
# FallbackNode
# ---------------------------------------------------------------------------
async def fallback_node(state: AuditState, runtime: Runtime[AuditContext]) -> dict[str, Any]:
    """Deterministic terminal path for invalid input or a failed scorecard."""
    scorecard = ComplianceScorecard(
        compliance_index=0.0,
        threshold=runtime.context.settings.compliance_threshold,
        verdict=Verdict.AUDIT_INCOMPLETE,
        sha_non_compliant=False,
        requires_human_review=False,
        interpretation=PolicyInterpretation(
            summary="The audit could not be completed. Correct the errors listed in this "
            "report and resubmit; no compliance determination has been made.",
        ),
    )
    return {
        "scorecard": scorecard.model_dump(mode="json"),
        "status": "FALLBACK",
        "audit_log": [
            log_entry(
                "fallback_node", "audit_incomplete", error_count=len(state.get("audit_errors", []))
            )
        ],
    }


# ---------------------------------------------------------------------------
# 6. compile_final_audit_report
# ---------------------------------------------------------------------------
NARRATIVE_SYSTEM_PROMPT = (
    "Write a concise (max 150 words) executive summary of this SHA facility compliance audit "
    "for an SHA contracting officer. State the verdict, the decisive findings, and the next "
    "steps. Do not introduce findings that are not in the data."
)

_OVERRIDE_VERDICTS = {
    "OVERRIDE_COMPLIANT": Verdict.COMPLIANT,
    "OVERRIDE_NON_COMPLIANT": Verdict.NON_COMPLIANT,
    "ESCALATE": Verdict.ESCALATED,
}


def final_verdict(state: AuditState) -> str:
    """Automated verdict, adjusted by any inspector decision."""
    automated = state["scorecard"]["verdict"]
    review = state.get("review")
    if not review:
        return automated
    override = _OVERRIDE_VERDICTS.get(review["action"])
    return override.value if override else automated


def _render_markdown(report: dict[str, Any]) -> list[str]:
    """Render the report as Markdown sections (streamed one by one)."""
    facility = report["facility"] or {}
    sc = report["scorecard"]
    sections = [
        f"# SHA Facility Compliance Audit\n\n"
        f"**Facility:** {facility.get('facility_name') or '-'} "
        f"(KMHFL `{facility.get('facility_id', '-')}`)  \n"
        f"**KMPDC Reg:** `{facility.get('kmpdc_reg', '-')}` | "
        f"**Declared Level:** {facility.get('declared_level', '-')}  \n"
        f"**Audit ID:** `{report['audit_id']}` | **Generated:** {report['generated_at']}\n",
        f"## Verdict: {report['final_verdict']}\n\n"
        f"| Metric | Value |\n|---|---|\n"
        f"| Compliance index | {sc['compliance_index']:.2f} / threshold {sc['threshold']:.2f} |\n"
        f"| Automated verdict | {sc['verdict']} |\n"
        f"| SHA moratorium | {'YES' if sc['sha_non_compliant'] else 'No'} |\n"
        f"| Critical violations | {len(sc['critical_violations'])} |\n"
        f"| Unverified checks | {len(sc['unverified_checks'])} |\n",
        f"## Executive Summary\n\n{report['narrative']}\n",
    ]

    rows = [
        f"| {r['branch']} | {humanise_check_id(c['check_id'])} | {c['status']} | "
        f"{'yes' if c['critical'] else ''} | {c['source']} | {c['detail']} |"
        for r in report["registry_results"]
        if r
        for c in r["checks"]
    ]
    if rows:
        sections.append(
            "## Checks\n\n| Branch | Check | Status | Critical | Source | Detail |\n"
            "|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n"
        )

    advisories = [a for r in report["registry_results"] if r for a in r.get("advisories", [])]
    if advisories:
        sections.append("## Advisories\n\n" + "\n".join(f"- {a}" for a in advisories) + "\n")

    interp = sc.get("interpretation") or {}
    if interp.get("remediation_actions"):
        sections.append(
            "## Remediation Plan\n\n"
            + "\n".join(
                f"- **{a['priority']}** {a['action']} _({REGULATION_CATALOG[a['regulation']]})_"
                for a in interp["remediation_actions"]
            )
            + "\n"
        )

    if review := report["inspector_review"]:
        sections.append(
            f"## Inspector Decision\n\n**{review['action']}** by `{review['inspector_id']}` "
            f"at {review['decided_at']}\n\n> {review['justification']}\n"
        )

    if report["errors"]:
        sections.append(
            "## Audit Errors\n\n"
            + "\n".join(f"- `{e['node']}` [{e['kind']}] {e['message']}" for e in report["errors"])
            + "\n"
        )
    return sections


def _report_failure(state: AuditState, exc: BaseException) -> dict[str, Any]:
    return {
        "report": {
            "final_verdict": Verdict.AUDIT_INCOMPLETE.value,
            "markdown": None,
            "error": f"{type(exc).__name__}: {exc}",
        },
        "status": "REPORT_FAILED",
    }


@guarded_node("compile_final_audit_report", _report_failure)
async def compile_final_audit_report(
    state: AuditState, runtime: Runtime[AuditContext]
) -> dict[str, Any]:
    """Assemble the final report, streaming JSON, narrative tokens and Markdown."""
    write = runtime.stream_writer
    info = runtime.execution_info
    scorecard = state["scorecard"]

    report: dict[str, Any] = {
        "audit_id": (info.thread_id if info else None) or "unthreaded",
        "generated_at": utc_now_iso(),
        "facility": state.get("facility") or state.get("payload"),
        "final_verdict": final_verdict(state),
        "scorecard": scorecard,
        "registry_results": [
            state.get("odpc_result"),
            state.get("kmpdc_result"),
            state.get("hmis_result"),
        ],
        "inspector_review": state.get("review"),
        "errors": state.get("audit_errors", []),
    }
    write(
        {
            "type": "report.scorecard_json",
            "data": {
                "audit_id": report["audit_id"],
                "final_verdict": report["final_verdict"],
                "scorecard": scorecard,
            },
        }
    )

    interpretation = scorecard.get("interpretation") or {}
    fallback_text = interpretation.get("summary") or (
        f"Facility assessed {report['final_verdict']} with compliance index "
        f"{scorecard['compliance_index']:.2f}. All regulatory checks passed."
    )
    if report["inspector_review"]:
        fallback_text += (
            f" Inspector {report['inspector_review']['inspector_id']} recorded "
            f"{report['inspector_review']['action']}; final verdict "
            f"{report['final_verdict']}."
        )
    report["narrative"] = await runtime.context.llm.stream_narrative(
        system=NARRATIVE_SYSTEM_PROMPT,
        content=json.dumps(
            {k: report[k] for k in ("facility", "final_verdict", "scorecard", "inspector_review")},
            default=str,
        ),
        fallback_text=fallback_text,
    )

    sections = _render_markdown(report)
    for section in sections:
        write({"type": "report.markdown_chunk", "text": section})
    report["markdown"] = "\n".join(sections)

    return {
        "report": report,
        "status": "COMPLETED",
        "audit_log": [
            log_entry(
                "compile_final_audit_report",
                "report_compiled",
                final_verdict=report["final_verdict"],
            )
        ],
    }
