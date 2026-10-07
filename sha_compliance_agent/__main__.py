"""Runnable demonstration: ``python -m sha_compliance_agent``.

In mock mode (the default) the agent audits synthetic facilities through
in-process registries, exercising parallel branches, streaming, the HITL
gate, timeout-to-cache fallback, the FallbackNode and checkpoint history.
Inspector decisions in the demo are scripted.

Options:
    --scenario NAME     compliant | moratorium | invalid | degraded | all (default)
    --payload FILE      audit a facility from a JSON file instead of the demo set
    --decision FILE     inspector decision JSON used if --payload run is flagged
    --report-dir DIR    write each final report as JSON and Markdown
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from sha_compliance_agent.config import AuditSettings
from sha_compliance_agent.graph import AuditEvent, ComplianceAuditService
from sha_compliance_agent.mock_registries import MockRegistryServer
from sha_compliance_agent.schemas import InspectorDecision

DEMO_PAYLOADS: dict[str, dict[str, Any]] = {
    "compliant": {
        "facility_id": "10234",
        "kmpdc_reg": "kmpdc/hf/04512",
        "declared_level": 4,
        "facility_name": "Nakuru Valley Hospital (synthetic)",
        "county": "Nakuru",
        "fhir_base_url": MockRegistryServer.fhir_base_url("10234"),
    },
    "moratorium": {
        "facility_id": "20871",
        "kmpdc_reg": "KMPDC/HF/07719",
        "declared_level": 3,
        "facility_name": "Kibera Community Health Centre (synthetic)",
        "county": "Nairobi",
        "fhir_base_url": MockRegistryServer.fhir_base_url("20871"),
    },
    "invalid": {
        "facility_id": "ABC",
        "kmpdc_reg": "12345",
        "declared_level": 7,
        "fhir_base_url": "http://insecure.example/fhir",
    },
    "degraded": {
        "facility_id": "31502",
        "kmpdc_reg": "KMPDC/HF/11050",
        "declared_level": 5,
        "facility_name": "Mombasa Coastline Medical Centre (synthetic)",
        "county": "Mombasa",
        "fhir_base_url": MockRegistryServer.fhir_base_url("31502"),
    },
}

SCRIPTED_DECISIONS: dict[str, dict[str, Any]] = {
    "moratorium": {
        "action": "UPHOLD",
        "inspector_id": "SHA-INSP-0042",
        "justification": "ODPC registration and DHA HMIS certification both absent; "
        "moratorium upheld pending remediation.",
    },
    "degraded": {
        "action": "ESCALATE",
        "inspector_id": "SHA-INSP-0107",
        "justification": "Declared level exceeds KMPDC register; KMPDC data served from "
        "cache. Escalating to the SHA claims integrity unit.",
    },
}


def _print_event(event: AuditEvent) -> None:
    if event.kind == "token":
        print(event.data, end="", flush=True)
    elif event.kind == "node_update":
        update = event.data or {}
        status = update.get("status")
        print(f"\n  [node] {event.node}" + (f" -> {status}" if status else ""), flush=True)
    elif event.kind == "custom" and event.data.get("type") == "report.scorecard_json":
        sc = event.data["data"]
        print(
            f"\n  [stream] scorecard: verdict={sc['final_verdict']} "
            f"index={sc['scorecard']['compliance_index']:.2f}\n  [stream] narrative: ",
            end="",
            flush=True,
        )
    elif event.kind == "interrupt":
        print(
            f"\n  [HITL] inspector review required: verdict={event.data['verdict']} "
            f"moratorium={event.data['moratorium_triggers']} "
            f"critical={event.data['critical_violations']}",
            flush=True,
        )


async def audit(
    service: ComplianceAuditService,
    name: str,
    payload: dict[str, Any],
    decision: dict[str, Any] | None,
    report_dir: Path | None,
) -> dict[str, Any] | None:
    thread_id = service.new_thread_id()
    print(f"\n=== Scenario: {name} (thread {thread_id}) ===")
    interrupted = False
    async for event in service.stream_audit(payload, thread_id=thread_id):
        _print_event(event)
        interrupted |= event.kind == "interrupt"

    if interrupted:
        if decision is None:
            print("\n  [HITL] no decision supplied; run left paused at the inspector gate.")
            return None
        InspectorDecision.model_validate(decision)
        print(f"  [HITL] resuming with scripted decision: {decision['action']}")
        async for event in service.resume_with_decision(decision, thread_id=thread_id):
            _print_event(event)

    state = (await service.get_state(thread_id)).values
    report = state.get("report") or {}
    print(
        f"\n  [done] final verdict: {report.get('final_verdict')} | "
        f"errors: {len(state.get('audit_errors', []))} | "
        f"checkpoints: {len(await service.history(thread_id))}"
    )

    if report_dir and report.get("markdown"):
        await asyncio.to_thread(_write_report, report_dir, f"{name}-{report['audit_id']}", report)
    return report


def _write_report(report_dir: Path, stem: str, report: dict[str, Any]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / f"{stem}.md").write_text(report["markdown"], encoding="utf-8")
    (report_dir / f"{stem}.json").write_text(
        json.dumps({k: v for k, v in report.items() if k != "markdown"}, indent=2, default=str),
        encoding="utf-8",
    )


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sha_compliance_agent",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--scenario", default="all", choices=["all", *DEMO_PAYLOADS])
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = AuditSettings.from_env()
    mock = MockRegistryServer() if settings.mode == "mock" else None

    print(
        f"SHA compliance agent | mode={settings.mode} | "
        f"llm={settings.llm_model or 'offline (deterministic)'} | "
        f"checkpointer={'sqlite' if settings.checkpoint_db else 'memory'}"
    )

    async with ComplianceAuditService(
        settings, transport=mock.transport() if mock else None
    ) as service:
        if args.payload:
            payload = json.loads(args.payload.read_text(encoding="utf-8"))
            decision = (
                json.loads(args.decision.read_text(encoding="utf-8")) if args.decision else None
            )
            report = await audit(service, "custom", payload, decision, args.report_dir)
            return 0 if report else 2

        if mock is None:
            parser.error("demo scenarios require SHA_AUDIT_MODE=mock; use --payload in live mode")

        names = list(DEMO_PAYLOADS) if args.scenario == "all" else [args.scenario]
        for name in names:
            if name == "degraded":
                # Warm the last-known-good cache, then slow KMPDC past the timeout.
                await service.run_to_completion(DEMO_PAYLOADS[name])
                mock.latency_s["kmpdc"] = settings.http_timeout_s + 1.0
                print(
                    f"\n  [chaos] KMPDC latency set to {mock.latency_s['kmpdc']:.1f}s "
                    f"(timeout {settings.http_timeout_s:.1f}s); expecting cache fallback"
                )
            await audit(
                service, name, DEMO_PAYLOADS[name], SCRIPTED_DECISIONS.get(name), args.report_dir
            )
            mock.latency_s.clear()
    return 0


def cli() -> None:
    """Console-script entry point."""
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    cli()
