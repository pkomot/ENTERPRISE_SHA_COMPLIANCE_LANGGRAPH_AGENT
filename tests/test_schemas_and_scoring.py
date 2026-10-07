from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from sha_compliance_agent.config import AuditSettings
from sha_compliance_agent.nodes import CHECK_CATALOG, score_checks
from sha_compliance_agent.resilience import BreakerState, CircuitBreaker, TTLCache
from sha_compliance_agent.schemas import (
    CheckResult,
    CheckStatus,
    DataSource,
    FacilityPayload,
    InspectorDecision,
    Verdict,
)


def test_payload_normalises_kmpdc_reg() -> None:
    facility = FacilityPayload.model_validate(
        {"facility_id": "10234", "kmpdc_reg": " kmpdc/hf/04512 ", "declared_level": 4}
    )
    assert facility.kmpdc_reg == "KMPDC/HF/04512"


@pytest.mark.parametrize(
    "overrides",
    [
        {"declared_level": 1},
        {"declared_level": 7},
        {"declared_level": True},
        {"facility_id": "ABC12"},
        {"kmpdc_reg": "12345"},
        {"fhir_base_url": "http://insecure.example/fhir"},
        {"unexpected": "field"},
    ],
)
def test_payload_rejects_invalid_input(overrides: dict[str, object]) -> None:
    base = {"facility_id": "10234", "kmpdc_reg": "KMPDC/HF/04512", "declared_level": 4}
    with pytest.raises(ValidationError):
        FacilityPayload.model_validate({**base, **overrides})


def test_inspector_decision_requires_justification() -> None:
    with pytest.raises(ValidationError):
        InspectorDecision(
            action="OVERRIDE_COMPLIANT", inspector_id="SHA-INSP-0001", justification="ok"
        )


def test_check_weights_sum_to_one() -> None:
    assert math.isclose(sum(s.weight for s in CHECK_CATALOG.values()), 1.0)


def _checks(**status: CheckStatus) -> list[CheckResult]:
    return [
        CheckResult(
            check_id=cid,
            status=status.get(cid.replace(".", "__"), CheckStatus.PASS),
            source=DataSource.LIVE,
            critical=spec.critical,
            detail="",
        )
        for cid, spec in CHECK_CATALOG.items()
    ]


def test_all_pass_is_compliant() -> None:
    sc = score_checks(_checks(), 0.75)
    assert sc.verdict is Verdict.COMPLIANT
    assert sc.compliance_index == 1.0
    assert not sc.requires_human_review


def test_missing_odpc_triggers_moratorium_even_with_high_score() -> None:
    sc = score_checks(_checks(odpc__controller_registered=CheckStatus.FAIL), 0.5)
    assert sc.sha_non_compliant
    assert sc.verdict is Verdict.NON_COMPLIANT
    assert sc.requires_human_review
    assert "odpc.controller_registered" in sc.moratorium_triggers


def test_missing_hmis_certification_triggers_moratorium() -> None:
    sc = score_checks(_checks(dha__hmis_certified=CheckStatus.FAIL), 0.5)
    assert sc.sha_non_compliant
    assert sc.moratorium_triggers == ["dha.hmis_certified"]


def test_unverified_critical_check_is_provisional() -> None:
    sc = score_checks(_checks(kmpdc__license_valid=CheckStatus.UNVERIFIED), 0.5)
    assert sc.verdict is Verdict.PROVISIONAL
    assert sc.requires_human_review


def test_non_critical_failure_below_threshold_is_non_compliant() -> None:
    sc = score_checks(_checks(dha__snomed_ct_binding=CheckStatus.FAIL), 0.99)
    assert sc.verdict is Verdict.NON_COMPLIANT
    assert not sc.critical_violations


def test_circuit_breaker_opens_and_half_opens() -> None:
    now = [0.0]
    breaker = CircuitBreaker("t", failure_threshold=2, reset_timeout_s=10, clock=lambda: now[0])
    breaker.record_failure()
    assert breaker.allow_request()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN and not breaker.allow_request()
    now[0] = 11
    assert breaker.allow_request()  # single half-open probe
    assert not breaker.allow_request()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_ttl_cache_expires() -> None:
    now = [0.0]
    cache: TTLCache[int] = TTLCache(5, clock=lambda: now[0])
    cache.set("k", 1)
    assert cache.get("k") == 1
    now[0] = 6
    assert cache.get("k") is None


def test_live_mode_rejects_mock_urls() -> None:
    with pytest.raises(ValidationError):
        AuditSettings(mode="live")


def test_settings_from_env() -> None:
    s = AuditSettings.from_env({"SHA_AUDIT_HTTP_TIMEOUT_S": "2.5", "ANTHROPIC_API_KEY": "x"})
    assert s.http_timeout_s == 2.5
    assert s.llm_model == "anthropic:claude-sonnet-5-5"
    assert (
        AuditSettings.from_env(
            {"SHA_AUDIT_LLM_MODEL": "offline", "ANTHROPIC_API_KEY": "x"}
        ).llm_model
        is None
    )
