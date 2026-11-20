from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, StrictInt, ValidationError

from . import providers


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


def register_control_api(app, control, require_admin, google_oauth_enabled=False):
    prefix = "/admin/api/v1"
    admin = [Depends(require_admin)]

    @app.get(prefix + "/providers", dependencies=admin)
    def list_providers():
        return {"providers": providers.catalog(), "google_oauth_enabled": google_oauth_enabled}

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
        except Exception:
            raise HTTPException(400, "Invalid connection") from None

    @app.post(prefix + "/connections/{ident}/test", dependencies=admin)
    def test_connection(ident: str):
        try:
            return {"ok": True, "result": control.test_connection(ident)}
        except KeyError:
            raise HTTPException(404, "Connection unavailable") from None
        except Exception:
            return {"ok": False, "result": "Connection test failed"}

    @app.delete(prefix + "/connections/{ident}", dependencies=admin, status_code=204)
    def disconnect(ident: str):
        try:
            control.disconnect(ident)
        except KeyError:
            raise HTTPException(404, "Connection unavailable") from None
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

    @app.post(prefix + "/grants/{ident}/revoke", dependencies=admin)
    def revoke(ident: str):
        try:
            control.revoke(ident)
        except KeyError:
            raise HTTPException(404, "Grant unavailable") from None
        return {"ok": True}

    @app.get(prefix + "/audit", dependencies=admin)
    def audit():
        return {"audit": control.audit()}
