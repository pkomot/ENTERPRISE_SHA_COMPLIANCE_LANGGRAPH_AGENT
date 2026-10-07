"""Async registry gateway with per-call timeouts, circuit breakers and cache fallback.

Every outbound HTTP call goes through :meth:`RegistryGateway.fetch`:

1. If the registry's circuit is open, skip the network and serve last-known-good
   cache (or an ``unavailable`` marker) immediately.
2. Otherwise call the endpoint under ``asyncio.wait_for(..., http_timeout_s)``.
3. On timeout or HTTP error, serve cache immediately if present; if not,
   re-raise so the node's ``RetryPolicy`` can retry.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import BaseModel, Field

from sha_compliance_agent.config import AuditSettings
from sha_compliance_agent.resilience import TRANSIENT_ERRORS, CircuitBreaker, TTLCache
from sha_compliance_agent.schemas import DataSource, Registry
from sha_compliance_agent.state import utc_now_iso


class RegistryResponse(BaseModel):
    """Normalised result of one registry lookup."""

    registry: Registry
    url: str
    source: DataSource
    found: bool | None = Field(
        description="True/False when the registry answered; None if unknown."
    )
    status_code: int | None = None
    data: dict[str, Any] | None = None
    latency_ms: float = 0.0
    fetched_at: str = Field(default_factory=utc_now_iso)
    degraded_reason: str | None = None


class RegistryGateway:
    """Shared async HTTP gateway for ODPC, KMPDC, CIHIS and facility FHIR servers."""

    def __init__(
        self,
        settings: AuditSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        # Registry credentials are attached per request and never sent to
        # facility-supplied FHIR hosts.
        self._registry_auth: dict[str, str] = (
            {"Authorization": f"Bearer {settings.registry_bearer_token}"}
            if settings.registry_bearer_token
            else {}
        )
        # httpx's own timeout sits just above the asyncio deadline as a backstop.
        self._client = httpx.AsyncClient(
            transport=transport,
            headers={"Accept": "application/json, application/fhir+json"},
            timeout=httpx.Timeout(settings.http_timeout_s + 1.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            follow_redirects=False,
        )
        self._breakers = {
            registry: CircuitBreaker(
                name=registry.value,
                failure_threshold=settings.breaker_failure_threshold,
                reset_timeout_s=settings.breaker_reset_timeout_s,
            )
            for registry in Registry
        }
        self._cache: TTLCache[RegistryResponse] = TTLCache(settings.cache_ttl_s)

    def breaker(self, registry: Registry) -> CircuitBreaker:
        return self._breakers[registry]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> RegistryGateway:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def fetch(
        self,
        registry: Registry,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> RegistryResponse:
        """GET a registry resource with timeout, circuit breaker and cache fallback.

        Raises:
            httpx.HTTPError | asyncio.TimeoutError: when the call fails and no
                cached value exists, so the calling node's RetryPolicy can retry.
        """
        cache_key = f"{registry}:{httpx.URL(url, params=params)}"
        breaker = self._breakers[registry]

        if not breaker.allow_request():
            return self._fallback(registry, url, cache_key, reason="circuit_open")

        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self._client.get(
                    url,
                    params=params,
                    headers=None if registry is Registry.FHIR else self._registry_auth,
                ),
                timeout=self._settings.http_timeout_s,
            )
            if response.status_code != httpx.codes.NOT_FOUND:
                response.raise_for_status()
        except TRANSIENT_ERRORS as exc:
            breaker.record_failure()
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached.model_copy(
                    update={"source": DataSource.CACHE, "degraded_reason": type(exc).__name__}
                )
            raise

        breaker.record_success()
        found = response.status_code != httpx.codes.NOT_FOUND
        result = RegistryResponse(
            registry=registry,
            url=url,
            source=DataSource.LIVE,
            found=found,
            status_code=response.status_code,
            data=response.json() if found else None,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        self._cache.set(cache_key, result)
        return result

    def _fallback(
        self, registry: Registry, url: str, cache_key: str, *, reason: str
    ) -> RegistryResponse:
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached.model_copy(update={"source": DataSource.CACHE, "degraded_reason": reason})
        return RegistryResponse(
            registry=registry,
            url=url,
            source=DataSource.UNAVAILABLE,
            found=None,
            degraded_reason=reason,
        )

    # ------------------------------------------------------------------
    # Typed registry lookups
    # ------------------------------------------------------------------
    async def odpc_data_controller(self, facility_id: str) -> RegistryResponse:
        return await self.fetch(
            Registry.ODPC, f"{self._settings.odpc_base_url}/data-controllers/{facility_id}"
        )

    async def odpc_enforcement_notices(self, facility_id: str) -> RegistryResponse:
        return await self.fetch(
            Registry.ODPC,
            f"{self._settings.odpc_base_url}/enforcement-notices",
            params={"facility_id": facility_id},
        )

    async def kmpdc_facility(self, kmpdc_reg: str) -> RegistryResponse:
        return await self.fetch(
            Registry.KMPDC,
            f"{self._settings.kmpdc_base_url}/facilities",
            params={"registration_no": kmpdc_reg},
        )

    async def kmpdc_license(self, kmpdc_reg: str) -> RegistryResponse:
        return await self.fetch(
            Registry.KMPDC,
            f"{self._settings.kmpdc_base_url}/licenses",
            params={"registration_no": kmpdc_reg},
        )

    async def cihis_readiness(self, facility_id: str) -> RegistryResponse:
        return await self.fetch(
            Registry.CIHIS, f"{self._settings.cihis_base_url}/facilities/{facility_id}/readiness"
        )

    async def fhir_capability_statement(self, fhir_base_url: str) -> RegistryResponse:
        return await self.fetch(Registry.FHIR, f"{fhir_base_url.rstrip('/')}/metadata")

    async def fhir_snomed_codesystem(self, fhir_base_url: str) -> RegistryResponse:
        return await self.fetch(
            Registry.FHIR,
            f"{fhir_base_url.rstrip('/')}/CodeSystem",
            params={"url": "http://snomed.info/sct", "_summary": "count"},
        )
