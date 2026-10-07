# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- MIT License.
- README overhaul with Mermaid architecture diagrams, a scoring table and programmatic usage.
- Contributing guide, security policy, issue and PR templates, Dependabot configuration.

### Changed
- GitHub Actions bumped to Node 24-based `checkout@v7`, `setup-python@v7` and `upload-artifact@v7`.

## [0.1.0] - 2026-10-07

### Added
- Async LangGraph audit graph: `ingest_and_validate`, parallel `audit_odpc_node` / `audit_kmpdc_node`,
  `audit_hmis_dha_node`, `evaluate_compliance_scorecard`, `human_review_interrupt`, `fallback_node`,
  `compile_final_audit_report`.
- Deterministic compliance index with SHA moratorium rules (ODPC registration, DHA HMIS certification).
- Resilience: 5 s per-call timeouts, circuit breakers, last-known-good cache, `RetryPolicy` on network nodes,
  guarded nodes that degrade instead of crashing.
- Structured LLM extraction (`with_structured_output`) with a deterministic offline fallback, and streamed narratives.
- `MemorySaver` / `AsyncSqliteSaver` checkpointing with history for replay.
- Synthetic in-process registries, 33 tests, and the GitHub Actions CI and demo workflow.
