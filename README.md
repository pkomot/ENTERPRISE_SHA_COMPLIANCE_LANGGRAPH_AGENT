# ENTERPRISE_SHA_COMPLIANCE_LANGGRAPH_AGENT

An asynchronous multi-agent LangGraph system that audits Kenyan health facilities against
**Social Health Authority (SHA)** and **Digital Health Agency (DHA)** requirements. It checks
ODPC data-controller registration, KMPDC licensing and level, and DHA HMIS readiness (FHIR R4,
SNOMED CT, CIHIS). It computes a deterministic compliance index, enforces SHA moratorium rules,
and pauses for a human inspector whenever a facility is flagged.

```
START -> ingest_and_validate
           |-- invalid --> fallback_node -------------------------------+
           '-- valid ----> audit_odpc_node  --+   (parallel superstep)  |
                           audit_kmpdc_node --+--> audit_hmis_dha_node  |
                                                   -> evaluate_compliance_scorecard
                 flagged: human_review_interrupt --+    clean | fatal: fallback_node
                                                   v          v         |
                                         compile_final_audit_report <---+ -> END
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev,anthropic]"
python -m sha_compliance_agent                          # runs all demo scenarios
pytest -q
```

The demo runs offline against **synthetic in-process registries**. All facilities in the demo are fictional. It covers four scenarios:

| Scenario | What it shows |
|---|---|
| `compliant` | Parallel branches, streamed narrative and Markdown, no interrupt |
| `moratorium` | Missing ODPC registration and HMIS certification trigger an SHA moratorium, the HITL interrupt fires, and the run resumes with an inspector decision |
| `invalid` | Pydantic rejects the payload and the run goes to the `fallback_node`, giving an `AUDIT_INCOMPLETE` report |
| `degraded` | KMPDC is slowed past the 5 s timeout, so the audit serves last-known-good cache immediately, then catches the upcoding violation |

To audit your own payload:

```bash
python -m sha_compliance_agent --payload examples/facility_payload.json \
  --decision examples/inspector_decision.json --report-dir reports
```

## How the spec maps to the code

| Requirement | Where |
|---|---|
| Async nodes and tools | Every node in [`nodes.py`](sha_compliance_agent/nodes.py) and every call in [`registries.py`](sha_compliance_agent/registries.py) is `async` |
| Parallel checks | ODPC and KMPDC run as parallel graph branches (fan-out from `route_after_ingest`, join edge into DHA). Inside each node, independent lookups run under `asyncio.gather` |
| Streaming | `graph.astream(stream_mode=["updates","custom","messages"])`: LLM narrative tokens, JSON scorecard, and Markdown chunks via `runtime.stream_writer` |
| 5 s timeout and circuit breaker | `RegistryGateway.fetch` uses `asyncio.wait_for`, falls back to cache on timeout, and keeps a per-registry `CircuitBreaker` ([`resilience.py`](sha_compliance_agent/resilience.py)) |
| Pydantic v2 guardrails | `FacilityPayload`: KMHFL code, KMPDC reg format, level bounds 2–6, https-only FHIR URL. Also `InspectorDecision` and all results ([`schemas.py`](sha_compliance_agent/schemas.py)) |
| Node-level exception handling | `@guarded_node` turns failures into degraded results plus `audit_errors` records; the graph never crashes |
| `RetryPolicy(max_attempts=3, retry_on=(httpx.HTTPError, asyncio.TimeoutError))` | Applied to the three network nodes ([`graph.py`](sha_compliance_agent/graph.py)) |
| HITL | `human_review_interrupt` calls `interrupt()`, and is reached only through a conditional edge when the score is below threshold, a critical violation exists, or a critical check is unverified |
| `TypedDict` + `Annotated` reducers | `AuditState.audit_log` / `audit_errors` use `operator.add` ([`state.py`](sha_compliance_agent/state.py)) |
| Checkpointing | `MemorySaver` by default. Set `SHA_AUDIT_CHECKPOINT_DB` to use `AsyncSqliteSaver`. `ComplianceAuditService.history()` supports replay and time travel |
| Structured LLM output | `llm.with_structured_output(KmpdcRemarksExtraction)` parses KMPDC remarks; `with_structured_output(PolicyInterpretation)` produces the policy interpretation ([`llm.py`](sha_compliance_agent/llm.py)) |

### Design decisions worth knowing

- **Node retries vs. "never crash".** Transient errors are re-raised while
  `runtime.execution_info.node_attempt < max_attempts`, so `RetryPolicy` does the retrying. On the final
  attempt the guard converts the error into `UNVERIFIED` checks. When cache exists, a timeout returns cached data
  immediately with no retry wait.
- **The LLM is advisory only.** The compliance index and verdict are fully deterministic
  (`CHECK_CATALOG` weights in `nodes.py`). The LLM only extracts remarks, interprets policy, and writes the
  narrative. Cited regulations are constrained to a fixed catalogue (a `Literal` type). Without an
  API key, or if the LLM times out or fails, a deterministic engine produces the same Pydantic types.
- **Conditional interrupt.** The spec mentions `interrupt_before=["final_approval_node"]`. A static breakpoint
  pauses on every run, so the gate uses dynamic `interrupt()` in a node reachable only through a
  conditional edge. Invalid inspector decisions are re-prompted up to 3 times.
- **Moratorium rule.** If ODPC registration is missing or expired, or DHA HMIS certification is missing, the run sets `sha_non_compliant=true`
  and returns `NON_COMPLIANT` regardless of the score.
- **Security.** The registry bearer token is never sent to facility-supplied FHIR hosts, and FHIR URLs must be
  https.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `SHA_AUDIT_MODE` | `mock` | `live` requires real registry base URLs |
| `SHA_AUDIT_ODPC_BASE_URL` / `_KMPDC_` / `_CIHIS_` | mock hosts | Registry endpoints |
| `SHA_AUDIT_REGISTRY_BEARER_TOKEN` | – | Auth header for registry calls only |
| `SHA_AUDIT_HTTP_TIMEOUT_S` | `5.0` | Per-call deadline |
| `SHA_AUDIT_RETRY_MAX_ATTEMPTS` | `3` | RetryPolicy attempts |
| `SHA_AUDIT_COMPLIANCE_THRESHOLD` | `0.75` | Review threshold |
| `SHA_AUDIT_BREAKER_FAILURE_THRESHOLD` / `_RESET_TIMEOUT_S` | `3` / `30` | Circuit breaker |
| `SHA_AUDIT_CACHE_TTL_S` | `86400` | Last-known-good cache TTL |
| `SHA_AUDIT_LLM_MODEL` | – | Any `init_chat_model` string, e.g. `anthropic:claude-sonnet-5-5`; `offline` forces the deterministic engine |
| `ANTHROPIC_API_KEY` | – | If set and no model is given, uses `anthropic:claude-sonnet-5-5` |
| `SHA_AUDIT_CHECKPOINT_DB` | – | SQLite path for `AsyncSqliteSaver` |

## GitHub Actions

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs ruff and pytest on Python 3.11–3.13, then runs the demo. It writes the
Markdown reports to the job summary and uploads them as an artifact. Add an `ANTHROPIC_API_KEY` repository secret to use
Claude in the demo job. To run a single scenario, use **Actions → CI → Run workflow**.

## Before production

- **Registry APIs are placeholders.** ODPC, KMPDC and CIHIS do not publish the REST contracts assumed here.
  Adapt the typed lookups in `RegistryGateway` and the response parsing in `nodes.py` to the real integration
  agreements.
- **Confirm identifier formats.** `KMPDC_REG_PATTERN` and `INSPECTOR_ID_PATTERN` in `schemas.py` are assumptions.
- **Use shared state for multiple workers.** Replace the in-process `TTLCache` and `MemorySaver` with Redis and
  Postgres or SQLite checkpointers.
- **Restrict outbound FHIR calls.** `fhir_base_url` is facility-supplied. Restrict egress (an allow-list or proxy) to prevent SSRF.
- **Get the weights signed off.** The weights in `CHECK_CATALOG` and the 0.75 threshold are illustrative and need SHA policy approval.
