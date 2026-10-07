# Security Policy

## Reporting a vulnerability

Do **not** open a public issue for security problems. Report privately through
[GitHub Security Advisories](https://github.com/pkomot/ENTERPRISE_SHA_COMPLIANCE_LANGGRAPH_AGENT/security/advisories/new).

Please include affected versions, reproduction steps and impact. You can expect an acknowledgement within 5 working days.

## Scope and known considerations

- **SSRF:** `fhir_base_url` is facility-supplied. Deployments must restrict outbound traffic (allow-list or egress proxy).
- **Credentials:** `SHA_AUDIT_REGISTRY_BEARER_TOKEN` is sent only to configured registries, never to FHIR hosts. Keep it in a secret store, never in code or `.env` files committed to git.
- **Personal data:** Audit state and checkpoints may contain facility and inspector identifiers. Protect checkpoint storage and apply retention rules consistent with the Data Protection Act, 2019.
- **LLM input:** Registry remarks are passed to the LLM for extraction. Outputs are schema-validated and cannot change scores, but treat them as untrusted text.
