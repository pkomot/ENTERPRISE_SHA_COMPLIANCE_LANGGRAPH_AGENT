"""In-process synthetic registries for demos, tests and CI.

All facilities, registration numbers and certificates below are fictional.
The mock is served through ``httpx.MockTransport`` so the real gateway code
path (timeouts, breakers, caching, retries) is exercised end to end.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx

from sha_compliance_agent.config import (
    MOCK_CIHIS_BASE_URL,
    MOCK_KMPDC_BASE_URL,
    MOCK_ODPC_BASE_URL,
)


def _in_days(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def default_dataset() -> dict[str, dict[str, Any]]:
    """Synthetic registry records keyed by KMHFL facility code."""
    return {
        # Fully compliant Level 4 hospital.
        "10234": {
            "kmpdc_reg": "KMPDC/HF/04512",
            "odpc": {
                "registration_no": "ODPC/DC/2025/88213",
                "status": "REGISTERED",
                "category": "Data Controller and Processor",
                "expiry_date": _in_days(400),
            },
            "odpc_notices": [],
            "kmpdc_facility": {
                "registration_no": "KMPDC/HF/04512",
                "kmhfl_code": "10234",
                "name": "Nakuru Valley Hospital (synthetic)",
                "registered_level": 4,
                "remarks": "Annual inspection satisfactory. No outstanding conditions.",
            },
            "kmpdc_license": {"status": "ACTIVE", "expiry_date": _in_days(180)},
            "cihis": {
                "endpoint_registered": True,
                "hmis_certified": True,
                "certificate_no": "DHA/HMIS/CERT/2026/0412",
                "last_successful_sync": _in_days(-1),
            },
            "fhir": {"fhirVersion": "4.0.1", "snomed_total": 1},
        },
        # Level 3 health centre: no ODPC registration, HMIS not certified.
        "20871": {
            "kmpdc_reg": "KMPDC/HF/07719",
            "odpc": None,
            "odpc_notices": [],
            "kmpdc_facility": {
                "registration_no": "KMPDC/HF/07719",
                "kmhfl_code": "20871",
                "name": "Kibera Community Health Centre (synthetic)",
                "registered_level": 3,
                "remarks": "Licence renewed; conditions: install medical waste incinerator "
                "by Q1; maternity theatre restricted pending re-inspection.",
            },
            "kmpdc_license": {"status": "ACTIVE", "expiry_date": _in_days(90)},
            "cihis": {
                "endpoint_registered": True,
                "hmis_certified": False,
                "certificate_no": None,
                "last_successful_sync": _in_days(-30),
            },
            "fhir": {"fhirVersion": "3.0.2", "snomed_total": 0},
        },
        # Declares Level 5 but KMPDC registers it as Level 4 (upcoding risk).
        "31502": {
            "kmpdc_reg": "KMPDC/HF/11050",
            "odpc": {
                "registration_no": "ODPC/DC/2024/50117",
                "status": "REGISTERED",
                "category": "Data Controller",
                "expiry_date": _in_days(60),
            },
            "odpc_notices": [{"notice_no": "ODPC/EN/2026/031", "status": "OPEN"}],
            "kmpdc_facility": {
                "registration_no": "KMPDC/HF/11050",
                "kmhfl_code": "31502",
                "name": "Mombasa Coastline Medical Centre (synthetic)",
                "registered_level": 4,
                "remarks": "Renal unit operating; ICU not licensed.",
            },
            "kmpdc_license": {"status": "ACTIVE", "expiry_date": _in_days(30)},
            "cihis": {
                "endpoint_registered": True,
                "hmis_certified": True,
                "certificate_no": "DHA/HMIS/CERT/2026/0977",
                "last_successful_sync": _in_days(-2),
            },
            "fhir": {"fhirVersion": "4.0.1", "snomed_total": 1},
        },
    }


class MockRegistryServer:
    """Routes ODPC, KMPDC, CIHIS and facility FHIR requests to synthetic data.

    ``latency_s`` and ``fail_status`` can be changed at runtime to simulate slow
    or failing registries (keys: ``odpc``, ``kmpdc``, ``cihis``, ``fhir``).
    """

    def __init__(self, dataset: dict[str, dict[str, Any]] | None = None) -> None:
        self.dataset = dataset if dataset is not None else default_dataset()
        self.latency_s: dict[str, float] = {}
        self.fail_status: dict[str, int] = {}
        self.calls: dict[str, int] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    @staticmethod
    def fhir_base_url(facility_id: str) -> str:
        return f"https://fhir.f{facility_id}.mock/fhir"

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        service = self._service_for(request.url)
        self.calls[service] = self.calls.get(service, 0) + 1
        if delay := self.latency_s.get(service):
            await asyncio.sleep(delay)
        if status := self.fail_status.get(service):
            return httpx.Response(status, json={"error": "simulated upstream failure"})
        return self._route(service, request)

    @staticmethod
    def _service_for(url: httpx.URL) -> str:
        host = url.host
        for name, base in (
            ("odpc", MOCK_ODPC_BASE_URL),
            ("kmpdc", MOCK_KMPDC_BASE_URL),
            ("cihis", MOCK_CIHIS_BASE_URL),
        ):
            if host == urlsplit(base).hostname:
                return name
        return "fhir" if host.endswith(".mock") and host.startswith("fhir.") else "unknown"

    def _route(self, service: str, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        not_found = httpx.Response(404, json={"error": "not found"})

        if service == "odpc":
            if path.endswith("/enforcement-notices"):
                record = self.dataset.get(params.get("facility_id", ""))
                notices = record["odpc_notices"] if record else []
                return httpx.Response(
                    200, json={"facility_id": params.get("facility_id"), "notices": notices}
                )
            facility_id = path.rsplit("/", 1)[-1]
            record = self.dataset.get(facility_id)
            if not record or record["odpc"] is None:
                return not_found
            return httpx.Response(200, json={"facility_id": facility_id, **record["odpc"]})

        if service == "kmpdc":
            reg = params.get("registration_no", "")
            record = next((r for r in self.dataset.values() if r["kmpdc_reg"] == reg), None)
            if record is None:
                return not_found
            if path.endswith("/licenses"):
                return httpx.Response(200, json={"registration_no": reg, **record["kmpdc_license"]})
            return httpx.Response(200, json=record["kmpdc_facility"])

        if service == "cihis":
            facility_id = path.split("/")[-2]
            record = self.dataset.get(facility_id)
            if not record:
                return not_found
            return httpx.Response(200, json={"facility_id": facility_id, **record["cihis"]})

        if service == "fhir":
            facility_id = request.url.host.split(".")[1].removeprefix("f")
            record = self.dataset.get(facility_id)
            if not record:
                return not_found
            if path.endswith("/metadata"):
                return httpx.Response(
                    200,
                    json={
                        "resourceType": "CapabilityStatement",
                        "status": "active",
                        "kind": "instance",
                        "fhirVersion": record["fhir"]["fhirVersion"],
                        "format": ["application/fhir+json"],
                    },
                )
            if path.endswith("/CodeSystem"):
                return httpx.Response(
                    200,
                    json={
                        "resourceType": "Bundle",
                        "type": "searchset",
                        "total": record["fhir"]["snomed_total"],
                    },
                )

        return not_found
