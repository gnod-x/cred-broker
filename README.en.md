# Cred Broker

[完整中文指南](README.md) · [Security model](docs/security.md) · [Contributing](CONTRIBUTING.md)

A self-hosted authorization gateway for AI tools. Keep upstream credentials in the broker and give AI a revocable, short-lived Bearer token scoped to one connection, explicit resources and tools. Credentials are encrypted at rest; no API returns them to AI.

The console uses Simplified Chinese. This is a single-instance project for individuals and small teams, licensed under MIT. v0.3.1 is the first implementation batch of the November 25 plan (T00–T03), not completion of all 17 task cards. It adds fail-closed storage startup, exact HTTP ingress and bounded body reads, transactional local audit/state changes, and explicit unknown outcomes. See the [validation record](docs/validation.md) for candidate/publication evidence. The [v0.3.0 release notes](docs/releases/v0.3.0.md) remain historical release evidence.

| Provider | Supported operations |
| --- | --- |
| Gmail | Search/read mail using your Google OAuth client; gmail.readonly only |
| SSH | Read selected systemd status/logs using a dedicated non-root identity and pinned host key |
| GitHub | Read selected repository files, issues and PRs using a GitHub App |
| Instabox | Read selected machines; a separate optional key enables fixed uptime/disk/memory diagnostics |

Feishu and Instagram are catalog placeholders. Model accounts and model API authorization are outside this project.

## Deployment and ingress

Requires Docker Engine and Compose v2. Confirm that the target image has actually passed publication acceptance before deployment; a version in compose.yaml alone is not release evidence. Unpublished candidates can be built and tested in isolation.

```bash
git clone https://github.com/gnod-x/cred-broker.git
cd cred-broker
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
# Put the generated value in .env as CB_ADMIN_TOKEN.
docker compose up -d --pull always --no-build
docker compose ps
```

Open http://localhost:8765. Compose maps the host port to loopback. When changing CB_HOST_PORT, explicitly add the external origins to CB_ALLOWED_ORIGINS, for example ["http://localhost:18765","http://127.0.0.1:18765"]. Use compose up -d --force-recreate after environment changes; restart does not reload Compose environment settings.

Host must match an allowed authority. Any supplied Origin must be an exact allowed HTTP(S) origin; null, malformed and duplicate origins are rejected. Native MCP/API clients may omit Origin but still require an allowed Host and valid authentication. The production Python entry disables proxy_headers; arbitrary X-Forwarded-* headers do not establish trust.

All HTTP bodies have an actual byte limit (default 65536; CB_MAX_REQUEST_BODY accepts 1024–65536) and one upload deadline (default 10 seconds; CB_BODY_READ_TIMEOUT is >0 and <=60). Excess size returns 413; upload expiry returns 408. These are not whole-provider execution deadlines.

The existing local LAN endpoint remains HTTP, with the old Tailscale endpoint retained; Tailscale is not required for LAN use. No production TLS migration is claimed. HTTP still exposes credentials, cookies, Bearer tokens and results to network interception or modification. Use SSH forwarding or plan trusted HTTPS using the [LAN HTTPS ADR](docs/adr/0001-lan-https.md). CB_PUBLIC_ORIGIN=https://... forces Secure cookies, so migration must account for existing HTTP browser login.

## MCP and outcome handling

Use Streamable HTTP at http://localhost:8765/mcp:

```text
Authorization: Bearer <SHORT_LIVED_GATEWAY_TOKEN>
```

The administrator token is separate and must never be used as an MCP token. Grants last at most 24 hours; tokens appear only once. Revocation rejects new admissions, while admitted operations may finish.

Local connection/grant mutations and their audit records commit in one SQLite transaction. A remote operation cannot commit atomically with local SQLite. If its result cannot be reliably recorded, outcome_unknown and retryable=false are returned; MCP marks the tool result isError. Do not retry automatically. Check upstream/local state first. If an issuance response is lost, refresh the grant list before revoking/reissuing the matching grant; issuing again can create duplicates.

Tests use synthetic providers and loopback HTTP, including the official MCP Python SDK. This does not verify real upstream accounts or every third-party client.

## Storage, backup and operations

Existing storage is checked before schema writes/listening: exact 32-byte binary key, service ownership, 0400/0600 modes, required tables/columns, and every legacy/scoped encrypted record and JSON structure. Missing/wrong keys, damaged ciphertext and partial installations fail closed without generating a replacement key. An empty database reports key_pairing=unproven, not verified. Readiness reports loaded-key, pairing, storage and schema-column states separately without decrypting the database on every request.

New storage is privately staged, fsynced and published without overwrite under a directory flock. Interrupted key-only/database-only states require matching-pair recovery. Ordinary read-only WAL preflight may create empty sidecars or update SHM reader metadata, but does not perform business writes or checkpoint WAL.

```bash
python -m cred_broker doctor
python -m cred_broker backup-check --db /secure/offline-backup/broker.db --key /secure/offline-backup/master.key
```

doctor inspects local configuration, ownership, permissions and standalone pairing without showing secrets or creating keys; it explicitly skips live-sidecar integrity/pairing checks. backup-check requires an independent snapshot with no WAL/SHM/journal sidecars and authenticates all encrypted records. It does not restore data or prove real provider access.

Back up the complete data state and matching master key before upgrade; keep the same project/volume and retain rollback artifacts. Browser sessions expire on restart. Run one worker only: revocation admission locks are process-local. RBAC, multi-tenancy and distributed coordination are not implemented. Encryption does not protect against a host administrator, and authorization does not prevent prompt injection.
