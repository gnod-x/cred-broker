# Cred Broker

[完整中文指南](README.md) · [Security model](docs/security.md) · [Contributing](CONTRIBUTING.md)

A self-hosted authorization gateway for AI tools. Keep upstream credentials in the broker and give AI a revocable, short-lived Bearer token scoped to one connection, explicit resources and tools. Credentials are encrypted at rest; there is no API for returning them to AI.

The console currently uses Simplified Chinese. This is a single-instance project for individuals and small teams, licensed under MIT. **v0.3.0 has passed isolated cloud acceptance; publication is being verified.** See the [validation record](docs/validation.md) for actual publication status.

| Provider | Supported operations |
| --- | --- |
| Gmail | Search and read mail through your Google OAuth client; `gmail.readonly` only |
| SSH | Read selected systemd service status and recent logs using a dedicated non-root identity and pinned host key |
| GitHub | Read files, issues and pull requests in selected repositories using a GitHub App |
| Instabox | Read selected machine state; optional separate key enables fixed uptime, disk and memory diagnostics |

Feishu and Instagram are future catalog entries, not working integrations. Model accounts and model API authorization are outside this project.

## Try the current source

Requires Docker Engine and Compose v2. Build locally while v0.3.0 is pending release; the Compose image tag alone does not prove a public image exists.

```bash
git clone https://github.com/gnod-x/cred-broker.git
cd cred-broker
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
# Put the generated value in .env as CB_ADMIN_TOKEN.
docker compose up -d --build
docker compose ps
```

Open http://localhost:8765 and sign in with that administrator token. Compose binds the host port to loopback. For remote access, use SSH forwarding or a trusted HTTPS reverse proxy; set `CB_PUBLIC_ORIGIN` to the exact external origin. See the Chinese guide for provider setup, deployment and backup instructions.

## Connect an MCP client

Use Streamable HTTP at `http://localhost:8765/mcp` with:

```text
Authorization: Bearer <SHORT_LIVED_GATEWAY_TOKEN>
```

The administrator token is separate and must never be used as the MCP token. Grants last at most 24 hours. A gateway token appears only at issuance; store it in the client's secure secret storage, never in a repository. Revocation rejects new calls; an already admitted operation may finish.

The console provides generic HTTP configuration and smoke-test examples. Tests cover the official MCP Python SDK against a local fake provider, including handshake, discovery, calls and revocation. This verifies protocol behavior, not real upstream accounts or compatibility with every third-party client.

## Local checks

With Python dependencies installed and the same runtime environment configured:

```bash
python -m cred_broker doctor
python -m cred_broker backup-check --db /secure/offline-backup/broker.db --key /secure/offline-backup/master.key
```

`doctor` reports local configuration, permissions and SQLite checks without showing secret values. `backup-check` reads an independent offline database and matching 32-byte key, checks SQLite integrity and authenticates legacy/scoped encrypted records. It refuses WAL/SHM/journal sidecars; use a stopped-service backup or SQLite backup API. An empty backup cannot prove key pairing. It does not restore data or create a key.

Run **one worker only**: revocation admission locks are process-local. Team RBAC, multi-tenancy and distributed coordination are not implemented. Encryption cannot protect credentials from a host administrator, and tool authorization does not prevent prompt injection. Read the [security model](docs/security.md) before deploying.
