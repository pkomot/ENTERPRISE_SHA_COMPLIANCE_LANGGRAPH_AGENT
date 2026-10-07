from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from sha_compliance_agent.config import AuditSettings
from sha_compliance_agent.graph import ComplianceAuditService
from sha_compliance_agent.llm import LLMService
from sha_compliance_agent.mock_registries import MockRegistryServer


@pytest.fixture
def settings() -> AuditSettings:
    """Fast settings: short timeouts and retry intervals, offline LLM."""
    return AuditSettings(
        http_timeout_s=0.3,
        retry_initial_interval_s=0.01,
        retry_max_attempts=3,
        llm_model=None,
    )


@pytest.fixture
def mock_server() -> MockRegistryServer:
    return MockRegistryServer()


@pytest.fixture
async def service(
    settings: AuditSettings, mock_server: MockRegistryServer
) -> AsyncIterator[ComplianceAuditService]:
    async with ComplianceAuditService(
        settings,
        transport=mock_server.transport(),
        llm=LLMService(None, timeout_s=settings.llm_timeout_s),
    ) as svc:
        yield svc


def payload(facility_id: str, kmpdc_reg: str, level: int, **extra: object) -> dict[str, object]:
    return {
        "facility_id": facility_id,
        "kmpdc_reg": kmpdc_reg,
        "declared_level": level,
        "fhir_base_url": MockRegistryServer.fhir_base_url(facility_id),
        **extra,
    }


COMPLIANT = payload("10234", "KMPDC/HF/04512", 4)
MORATORIUM = payload("20871", "KMPDC/HF/07719", 3)
UPCODING = payload("31502", "KMPDC/HF/11050", 5)
