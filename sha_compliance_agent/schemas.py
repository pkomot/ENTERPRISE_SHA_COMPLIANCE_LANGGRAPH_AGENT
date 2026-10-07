"""Pydantic v2 schemas: input guardrails, audit results, and LLM structured outputs."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
)

# ---------------------------------------------------------------------------
# Identifier formats
# ---------------------------------------------------------------------------
# Kenya Master Health Facility List (KMHFL) codes are numeric.
KMHFL_CODE_PATTERN = r"^\d{5,6}$"
# KMPDC facility registration number. The exact production format should be
# confirmed with KMPDC; this pattern is the single place to change it.
KMPDC_REG_PATTERN = r"^KMPDC/(?:HF|FAC)/\d{4,6}$"
INSPECTOR_ID_PATTERN = r"^SHA-INSP-\d{4,6}$"

SHA_MIN_LEVEL = 2
SHA_MAX_LEVEL = 6


class Registry(StrEnum):
    """External registries consulted during an audit."""

    ODPC = "ODPC"
    KMPDC = "KMPDC"
    CIHIS = "CIHIS"
    FHIR = "FHIR"


class CheckStatus(StrEnum):
    """Outcome of a single regulatory check."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


class DataSource(StrEnum):
    """Where the evidence for a check came from."""

    LIVE = "live"
    CACHE = "cache"
    UNAVAILABLE = "unavailable"


class Verdict(StrEnum):
    """Overall audit verdict."""

    COMPLIANT = "COMPLIANT"
    NON_COMPLIANT = "NON_COMPLIANT"
    PROVISIONAL = "PROVISIONAL"
    ESCALATED = "ESCALATED"
    AUDIT_INCOMPLETE = "AUDIT_INCOMPLETE"


# ---------------------------------------------------------------------------
# Input guardrails
# ---------------------------------------------------------------------------
class FacilityPayload(BaseModel):
    """Validated facility submission. Every audit starts from this schema."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    facility_id: Annotated[str, StringConstraints(pattern=KMHFL_CODE_PATTERN)] = Field(
        description="KMHFL facility code, e.g. '10234'."
    )
    kmpdc_reg: Annotated[str, StringConstraints(pattern=KMPDC_REG_PATTERN)] = Field(
        description="KMPDC facility registration number, e.g. 'KMPDC/HF/04512'."
    )
    declared_level: Annotated[int, Field(ge=SHA_MIN_LEVEL, le=SHA_MAX_LEVEL)] = Field(
        description="SHA contracting level declared by the facility (2-6)."
    )
    facility_name: Annotated[str, StringConstraints(min_length=3, max_length=200)] | None = None
    county: Annotated[str, StringConstraints(min_length=3, max_length=60)] | None = None
    fhir_base_url: HttpUrl | None = Field(
        default=None, description="Facility HMIS FHIR R4 base URL (https only)."
    )

    @field_validator("kmpdc_reg", mode="before")
    @classmethod
    def _normalise_kmpdc_reg(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("declared_level", mode="before")
    @classmethod
    def _reject_bool_level(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("declared_level must be an integer between 2 and 6")
        return value

    @field_validator("fhir_base_url")
    @classmethod
    def _https_only(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and value.scheme != "https":
            raise ValueError("fhir_base_url must use https")
        return value


class InspectorDecision(BaseModel):
    """Human inspector decision captured at the HITL interrupt gate."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["UPHOLD", "OVERRIDE_COMPLIANT", "OVERRIDE_NON_COMPLIANT", "ESCALATE"]
    inspector_id: Annotated[str, StringConstraints(pattern=INSPECTOR_ID_PATTERN)]
    justification: Annotated[str, StringConstraints(min_length=20, max_length=2000)]
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Audit results
# ---------------------------------------------------------------------------
class CheckResult(BaseModel):
    """A single, scored regulatory check."""

    check_id: str
    status: CheckStatus
    source: DataSource
    critical: bool
    detail: str
    evidence: dict[str, Any] = Field(default_factory=dict)


class RegistryAuditResult(BaseModel):
    """Output of one audit branch (ODPC, KMPDC or DHA)."""

    branch: Literal["ODPC", "KMPDC", "DHA"]
    checks: list[CheckResult]
    advisories: list[str] = Field(default_factory=list)
    degraded: bool = False
    latency_ms: float = 0.0
    extraction_engine: str | None = None


# ---------------------------------------------------------------------------
# LLM structured outputs
# ---------------------------------------------------------------------------
RegulationCode = Literal[
    "DPA_2019",
    "DPA_REGISTRATION_REGS_2021",
    "SHI_ACT_2023",
    "DIGITAL_HEALTH_ACT_2023",
    "MPD_ACT_CAP_253",
    "HL7_FHIR_R4",
    "SNOMED_CT",
]

REGULATION_CATALOG: dict[str, str] = {
    "DPA_2019": "Data Protection Act, 2019 (registration of data controllers and processors)",
    "DPA_REGISTRATION_REGS_2021": "Data Protection (Registration of Data Controllers and "
    "Data Processors) Regulations, 2021",
    "SHI_ACT_2023": "Social Health Insurance Act, 2023 (SHA empanelment and contracting)",
    "DIGITAL_HEALTH_ACT_2023": "Digital Health Act, 2023 (Digital Health Agency, HMIS standards)",
    "MPD_ACT_CAP_253": "Medical Practitioners and Dentists Act, Cap. 253 (KMPDC licensing)",
    "HL7_FHIR_R4": "HL7 FHIR Release 4 interoperability standard",
    "SNOMED_CT": "SNOMED CT clinical terminology binding",
}


class KmpdcRemarksExtraction(BaseModel):
    """Facts extracted from free-text KMPDC inspection remarks."""

    pending_conditions: list[str] = Field(
        default_factory=list,
        description="Licence conditions the facility has not yet satisfied.",
    )
    service_restrictions: list[str] = Field(
        default_factory=list,
        description="Services the facility is restricted or barred from offering.",
    )
    reinspection_required: bool = Field(
        default=False, description="True if the remarks state a re-inspection is pending."
    )


class RemediationAction(BaseModel):
    """A concrete corrective action for the facility."""

    action: Annotated[str, StringConstraints(min_length=5, max_length=300)]
    regulation: RegulationCode
    priority: Literal["P1", "P2", "P3"]


class PolicyInterpretation(BaseModel):
    """Plain-language interpretation of audit findings against Kenyan regulation."""

    summary: Annotated[str, StringConstraints(min_length=10, max_length=1200)]
    cited_regulations: list[RegulationCode] = Field(default_factory=list)
    remediation_actions: list[RemediationAction] = Field(default_factory=list, max_length=8)


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------
class ComplianceScorecard(BaseModel):
    """Deterministic compliance evaluation. LLM output never changes the score."""

    compliance_index: Annotated[float, Field(ge=0.0, le=1.0)]
    threshold: Annotated[float, Field(ge=0.0, le=1.0)]
    verdict: Verdict
    sha_non_compliant: bool
    moratorium_triggers: list[str] = Field(default_factory=list)
    critical_violations: list[str] = Field(default_factory=list)
    unverified_checks: list[str] = Field(default_factory=list)
    degraded_sources: list[str] = Field(default_factory=list)
    requires_human_review: bool
    interpretation: PolicyInterpretation | None = None


def humanise_check_id(check_id: str) -> str:
    """Turn 'kmpdc.license_valid' into 'KMPDC: license valid'."""
    prefix, _, rest = check_id.partition(".")
    return f"{prefix.upper()}: {re.sub(r'_', ' ', rest)}"
