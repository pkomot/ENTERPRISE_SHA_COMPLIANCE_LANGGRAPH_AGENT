"""Run-scoped dependencies injected into nodes via LangGraph's ``Runtime.context``."""

from __future__ import annotations

from dataclasses import dataclass

from sha_compliance_agent.config import AuditSettings
from sha_compliance_agent.llm import LLMService
from sha_compliance_agent.registries import RegistryGateway


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Dependencies shared by every node in a run. Never checkpointed."""

    settings: AuditSettings
    gateway: RegistryGateway
    llm: LLMService
