"""Stateless MCP Streamable HTTP transport."""
import asyncio
import json

from fastapi import HTTPException, Request, Response

from fastapi.responses import JSONResponse
from . import __version__
from .control import OutcomeUnknown
from .security import HttpBodyError, HttpSecurityPolicy, read_http_body

VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")


def _error(code, message, ident=None, status=200):
    return JSONResponse({"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}},
                        status_code=status)


def register_mcp(app, control, security_policy=None):
    policy = security_policy or HttpSecurityPolicy()
    @app.get("/mcp")
    def mcp_stream():
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.post("/mcp")
    async def mcp(request: Request):
        denied = policy.validate(request.scope)
        if denied is not None:
            raise HTTPException(*denied)
        auth = request.headers.get("authorization", "").split()
        token = auth[1] if len(auth) == 2 and auth[0].lower() == "bearer" else ""
        grant = await asyncio.to_thread(control.resolve, token)
        unauthorized = {"WWW-Authenticate": "Bearer"}
        if grant is None:
            raise HTTPException(401, "Unauthorized", headers=unauthorized)
        version = request.headers.get("mcp-protocol-version")
        if version and version not in VERSIONS:
            return _error(-32600, "Unsupported protocol version", status=400)
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return _error(-32600, "JSON content required", status=415)
        try:
            body = await read_http_body(request.receive)
        except HttpBodyError as exc:
            return _error(-32600, exc.detail, status=exc.status_code)
        try:
            message = json.loads(body)
        except (ValueError, UnicodeError):
            return _error(-32700, "Parse error", status=400)
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)
                or ("id" in message and type(message["id"]) not in (str, int))):
            return _error(-32600, "Invalid request", status=400)
        method, ident = message["method"], message.get("id")
        params = message.get("params", {})
        if ident is None:
            return Response(status_code=202)
        if not isinstance(params, dict):
            return _error(-32602, "Invalid params", ident)
        if method == "initialize":
            version = params.get("protocolVersion")
            if not isinstance(version, str):
                return _error(-32602, "Invalid protocol version", ident)
            result = {"protocolVersion": version if version in VERSIONS else VERSIONS[0],
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "cred-broker", "version": __version__}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            try:
                result = {"tools": await asyncio.to_thread(control.discover, grant)}
            except (KeyError, PermissionError):
                raise HTTPException(401, "Unauthorized", headers=unauthorized) from None
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _error(-32602, "Invalid tool arguments", ident)
            arguments = dict(arguments)
            resource = arguments.pop("resource", None)
            if not isinstance(resource, str):
                return _error(-32602, "A resource is required", ident)
            try:
                output = await asyncio.to_thread(control.call, token, name, resource, arguments)
            except PermissionError:
                raise HTTPException(401, "Unauthorized", headers=unauthorized) from None
            except OutcomeUnknown:
                # Use a fixed public message, never the exception or provider output.
                details = {
                    "outcome": "outcome_unknown",
                    "retryable": False,
                    "message": (
                        "The operation may have completed, but its outcome could not be recorded. "
                        "Do not retry automatically. Verify the external operation before deciding what to do next."
                    ),
                }
                result = {"content": [{"type": "text", "text": json.dumps(details)}],
                          "structuredContent": details, "isError": True}
            except Exception:
                result = {"content": [{"type": "text", "text": "Tool request denied or failed"}], "isError": True}
            else:
                result = {"content": [{"type": "text", "text": json.dumps(output, ensure_ascii=False)}]}
        else:
            return _error(-32601, "Method not found", ident)
        return JSONResponse({"jsonrpc": "2.0", "id": ident, "result": result})
