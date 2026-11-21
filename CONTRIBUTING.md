# Contributing

Start with [CLAUDE.md](CLAUDE.md), the [architecture](docs/architecture.md), [security boundaries](docs/security.md) and [Chinese guide](README.md). Keep changes small and document the final behavior and verification limits. The UI currently uses Simplified Chinese.

## Development and verification

Use Python 3.11+ and an isolated environment:

```bash
python3 -m venv .venv
.venv/bin/pip install --no-cache-dir -r requirements-dev.txt
.venv/bin/python -IB tests/test_core.py
.venv/bin/python -IB tests/test_control.py
.venv/bin/python -IB tests/test_providers.py
.venv/bin/python -IB tests/test_google.py
.venv/bin/python -IB tests/test_app.py
.venv/bin/python -IB tests/test_ssh_transport.py
.venv/bin/python -IB tests/test_mcp_sdk.py
.venv/bin/python -IB tests/test_backup_check.py
.venv/bin/python -IB tests/test_admin_auth.py
.venv/bin/python -IB tests/test_doctor.py
.venv/bin/python -IB tests/test_release_notes.py
```

Use temporary databases, synthetic secrets and mock upstreams only. Never use or publish real credentials, account sessions, production databases, master keys, mailbox content or private repository data. Official SDK tests use a local fake provider: passing them establishes protocol behavior, not a real upstream integration.

UI changes also require the dedicated browser fixture and `tests/test_browser.py`; setup is in the Chinese guide. Deployment changes require isolated Compose validation with `tests/test_container.py`, including persistence and hardening checks. Do not point fixtures or container tests at a production instance. Remove temporary processes, datasets and build artifacts afterward.

## Adding a provider

Implement an explicit adapter and capability metadata in the existing provider architecture. Define validated settings and secret fields, a finite resource set, tool schemas and fixed operations. Keep planned catalog entries clearly unsupported until their adapters and tests exist.

Preserve these contracts:

- The server validates every scope and call. One grant binds one connection and explicit resources/tools, with a TTL of at most 24 hours. No wildcard expansion or arbitrary shell/URL forwarding.
- AI never receives raw upstream credentials. Keep connection payloads encrypted with connection-ID AAD and token hashes separate from secrets. Administrator and MCP authentication must remain separate.
- Use least-privilege upstream permissions. Pin SSH host keys; preserve OAuth state, PKCE, browser binding and expiry. Do not reuse the host's existing accounts, proxy environment or SSH agent.
- Bound network/subprocess time and output. Validate inputs, reject redirects where required, redact known secrets and keep errors generic. Treat returned content as untrusted data.
- Audit only approved metadata and outcomes, never arguments, response bodies or secrets. Connection health is the result of an explicit test, not continuous monitoring.
- Preserve single-worker revocation/disconnection semantics and legacy/scoped storage isolation. Do not silently enable legacy behavior.

Add tests for invalid settings and unknown fields, resource/tool denial, malformed arguments, timeouts/output limits, sensitive-value redaction, expiry, revocation and disconnection. Extend official SDK tests when MCP behavior changes; use a local loopback transport when testing SSH. Cover migrations and rollback behavior for storage changes. A mock success must never be described as verification against a real service.

## Pull requests

Explain the user problem, resulting behavior, security implications and exact checks run. Link any updated setup instructions or schema changes. Mark checks that were not run and why; do not claim release or client compatibility before the corresponding verification succeeds.

## Releases

Push a `vMAJOR.MINOR.PATCH` tag only after isolated acceptance. The Container release workflow reruns that tag's tests before publishing the Linux amd64 image. Pull the public image anonymously by digest and verify it in a separate Compose project before documenting availability.

Commit `docs/releases/vMAJOR.MINOR.PATCH.md` to `main` after image acceptance. The Publish release notes workflow verifies the source version and the successful image workflow triggered by the matching tag before it creates the GitHub Release. It reuses a release with identical notes and stops if the existing notes differ.
