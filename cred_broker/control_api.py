import csv
import io

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, StrictInt, ValidationError

from . import providers
from .control import OutcomeUnknown


class ConnectionInput(BaseModel):
    provider: str
    label: str
    settings: dict
    secret: dict


class GrantInput(BaseModel):
    client: str
    connection_id: str
    resources: list[str]
    tools: list[str]
    ttl_seconds: StrictInt


def _csv_cell(value):
    text = "" if value is None else str(value)
    if text.startswith(("\t", "\r", "\n")) or text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def register_control_api(app, control, require_admin, google_oauth_enabled=False):
    prefix = "/admin/api/v1"
    admin = [Depends(require_admin)]

    @app.get(prefix + "/ready", dependencies=admin)
    def ready():
        return control.ready()

    @app.get(prefix + "/providers", dependencies=admin)
    def list_providers():
        return {"providers": providers.catalog(), "google_oauth_enabled": google_oauth_enabled}

    @app.get(prefix + "/providers/{provider}/schema", dependencies=admin)
    def provider_schema(provider: str):
        try:
            return {"provider": provider, "schema": providers.connection_schema(provider)}
        except ValueError:
            raise HTTPException(404, "Provider unavailable") from None

    @app.get(prefix + "/connections", dependencies=admin)
    def list_connections():
        return {"connections": control.connections()}

    @app.post(prefix + "/connections", dependencies=admin, status_code=201)
    async def create_connection(request: Request):
        try:
            data = ConnectionInput(**(await request.json()))
        except (ValidationError, ValueError, TypeError):
            raise HTTPException(400, "Invalid connection") from None
        if data.provider in {"gmail", "google"}:
            raise HTTPException(400, "OAuth required")
        try:
            return {"connection": control.create_connection(**dict(data))}
        except ValueError:
            raise HTTPException(400, "Invalid connection") from None
        except Exception:
            raise HTTPException(503, "Local change could not be confirmed; refresh state before retrying") from None

    @app.post(prefix + "/connections/{ident}/test", dependencies=admin)
    def test_connection(ident: str):
        try:
            return {"ok": True, "result": control.test_connection(ident)}
        except KeyError:
            raise HTTPException(404, "Connection unavailable") from None
        except OutcomeUnknown:
            return {"ok": False, "outcome": "outcome_unknown", "retryable": False,
                    "result": "The connection test may have completed; do not retry automatically"}
        except Exception:
            return {"ok": False, "result": "Connection test failed"}

    @app.delete(prefix + "/connections/{ident}", dependencies=admin, status_code=204)
    def disconnect(ident: str):
        try:
            control.disconnect(ident)
        except KeyError:
            raise HTTPException(404, "Connection unavailable") from None
        except Exception:
            raise HTTPException(503, "Local change could not be confirmed; refresh state before retrying") from None
        return Response(status_code=204)

    @app.get(prefix + "/grants", dependencies=admin)
    def list_grants():
        return {"grants": control.grants()}

    @app.post(prefix + "/grants", dependencies=admin, status_code=201)
    async def create_grant(request: Request):
        try:
            data = GrantInput(**(await request.json()))
        except (ValidationError, ValueError, TypeError):
            raise HTTPException(400, "Invalid grant") from None
        try:
            grant, token = control.create_grant(**dict(data))
            return {"grant": grant, "token": token}
        except KeyError:
            raise HTTPException(404, "Connection unavailable") from None
        except ValueError:
            raise HTTPException(400, "Invalid scope") from None
        except Exception:
            raise HTTPException(503, "Local change could not be confirmed; refresh state before retrying") from None

    @app.post(prefix + "/grants/{ident}/revoke", dependencies=admin)
    def revoke(ident: str):
        try:
            control.revoke(ident)
        except KeyError:
            raise HTTPException(404, "Grant unavailable") from None
        except Exception:
            raise HTTPException(503, "Local change could not be confirmed; refresh state before retrying") from None
        return {"ok": True}

    @app.post(prefix + "/grants/{ident}/reissue", dependencies=admin, status_code=201)
    async def reissue(ident: str, request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Invalid reissue payload")
            ttl_seconds = body.get("ttl_seconds", 86400)
            grant, token = control.reissue_grant(ident, ttl_seconds)
            return {"grant": grant, "token": token}
        except KeyError:
            raise HTTPException(404, "Grant unavailable") from None
        except PermissionError:
            raise HTTPException(409, "Grant revoked") from None
        except ValueError:
            raise HTTPException(400, "Invalid expiry or scope") from None
        except Exception:
            raise HTTPException(503, "Local change could not be confirmed; refresh state before retrying") from None

    @app.get(prefix + "/audit", dependencies=admin)
    def audit():
        return {"audit": control.audit()}

    @app.get(prefix + "/audit/export.csv", dependencies=admin)
    def export_audit(after: int | None = None, before: int | None = None):
        try:
            rows = control.audit_export(after, before)
        except ValueError:
            raise HTTPException(400, "Invalid audit range") from None
        fields = ("ts", "client", "action", "connection_id", "resource", "outcome")

        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(fields)
        for row in rows:
            writer.writerow([_csv_cell(row[field]) for field in fields])
        return Response(
            content=stream.getvalue(), media_type="text/csv", headers={
                "Content-Disposition": 'attachment; filename="cred-broker-audit.csv"',
                "Cache-Control": "no-store",
            },
        )
