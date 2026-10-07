# Contributing

Thanks for helping improve the SHA Compliance LangGraph Agent.

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,anthropic]"
pytest -q
```

## Workflow

1. Create a branch from `main` (`feat/...`, `fix/...`, `docs/...`).
2. Keep changes focused, and add or update tests in `tests/` for any behaviour change.
3. Run the checks before pushing:
   ```bash
   ruff check . && ruff format --check . && pytest -q
   ```
4. Open a pull request using the template. CI must be green.

## Code conventions

- **Async everywhere.** Nodes and I/O must be `async`. Never block the event loop (use `asyncio.to_thread` for file or CPU work).
- **JSON-safe state.** Store `model_dump(mode="json")` output in `AuditState`, not Pydantic instances or enums.
- **Deterministic scoring.** Add or change checks only through `CHECK_CATALOG` in `nodes.py`, keep the weights summing to 1.0, and never let LLM output change a score or verdict.
- **Fail soft.** Network nodes use `@guarded_node`. New external calls go through `RegistryGateway.fetch` so they get timeouts, breakers and caching.
- **Interrupts.** Never catch `GraphBubbleUp`.
- **Synthetic data only.** Do not commit real facility, patient or practitioner data, credentials, or real registry responses.

## Regulatory changes

When a change reflects a new or amended regulation, cite the instrument (Act, Regulation or Gazette Notice) in the PR
description and update `REGULATION_CATALOG` in `schemas.py`.
