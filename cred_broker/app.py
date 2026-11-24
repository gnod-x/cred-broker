"""FastAPI 应用：管理网页 + AI 侧 MCP 端点。

两条独立入口：
- /admin/*   管理页面（对外入口由精确来源配置控制）
- /mcp/*     AI 用 Bearer token 调用
"""

from __future__ import annotations

import inspect
import json
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, ssh_exec
from .admin_auth import AdminAuth, LOGIN_WINDOW, SESSION_TTL
from .broker import Broker, Denied
from .config import Config
from .control import Control
from .control_api import register_control_api
from .google_oauth import GoogleSettings, register_google_oauth
from .mcp import register_mcp
from .origin import canonical_public_origin as _canonical_public_origin
from .startup import prepare_store
from .security import HttpSecurityMiddleware, HttpSecurityPolicy

HERE = Path(__file__).parent


class GrantRequest(BaseModel):
    agent: str = Field("claude-code", max_length=64)
    credential: str = Field(..., max_length=128)
    actions: list[str] = Field(default_factory=list)
    ttl_seconds: int = Field(3600, ge=60, le=86400 * 30)
    note: str = Field("", max_length=256)


class TokenIssue(BaseModel):
    name: str = Field("default", max_length=64)
    grant_id: str


class CredentialRequest(BaseModel):
    name: str = Field(..., max_length=128)
    kind: str
    secret: dict
    meta: dict = Field(default_factory=dict)


def _as_dict(model: BaseModel) -> dict:
    """兼容 pydantic v1 / v2。"""
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


def create_app(cfg: Config) -> FastAPI:
    public_origin = _canonical_public_origin(cfg.public_origin)
    policy = HttpSecurityPolicy.from_config(cfg)
    store = prepare_store(cfg)
    raw_hosts = json.loads(cfg.hosts_json)
    if not isinstance(raw_hosts, dict):
        raise RuntimeError("CB_HOSTS 必须是 JSON 对象")
    hosts = {name: ssh_exec.HostConfig.from_dict(name, data) for name, data in raw_hosts.items()}
    broker = Broker(store, hosts, timeout=cfg.request_timeout, max_output=cfg.max_output_bytes,
                    known_hosts=str(cfg.ssh_known_hosts) if cfg.ssh_known_hosts else None)

    app = FastAPI(title="cred-broker", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def page(request, name, context, status_code=200):
        parameters = inspect.signature(templates.TemplateResponse).parameters
        if "request" not in parameters and "args" not in parameters:
            return templates.TemplateResponse(
                name=name, context={"request": request, "version": __version__, **context}, status_code=status_code
            )
        return templates.TemplateResponse(request=request, name=name, context={"version": __version__, **context}, status_code=status_code)

    @app.middleware("http")
    async def browser_security(request: Request, call_next):
        path = request.url.path
        legacy = (path == "/admin/legacy" or path.startswith("/mcp/")
                  or (path.startswith("/admin/api/") and not path.startswith("/admin/api/v1/")))
        if legacy and not cfg.legacy_enabled:
            return JSONResponse({"detail": "旧版接口未启用"}, status_code=404)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # no-referrer turns Chromium form POST Origin into "null", breaking same-origin CSRF checks.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # The old admin view has inline scripts and styles; new UI uses local static assets.
        if not request.url.path.startswith("/admin/legacy"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                "form-action 'self'; object-src 'none'"
            )
        return response

    # ASGI boundary runs outside the response-header/legacy middleware.
    app.add_middleware(HttpSecurityMiddleware, policy=policy,
                       max_body_bytes=cfg.max_request_body_bytes,
                       read_timeout=cfg.body_read_timeout)

    # ---------- 鉴权 ----------

    sessions = AdminAuth()

    def admin_ok(request: Request) -> bool:
        if sessions.valid(request.cookies.get(cfg.session_cookie)):
            return True
        tok = request.headers.get("x-admin-token", "")
        return bool(tok) and secrets.compare_digest(tok.encode(), cfg.admin_token.encode())

    def require_admin(request: Request) -> None:
        if not admin_ok(request):
            raise HTTPException(401, "需要管理员登录")

    def require_browser_admin(request: Request) -> None:
        if not sessions.valid(request.cookies.get(cfg.session_cookie)):
            raise HTTPException(401, "请先登录管理界面")

    control = Control(store)
    google_settings = GoogleSettings.load()
    register_control_api(app, control, require_admin, google_oauth_enabled=google_settings is not None)
    register_google_oauth(app, control, require_browser_admin, google_settings)
    register_mcp(app, control, security_policy=policy)

    def resolve_ai(authorization: str | None) -> tuple[dict, object]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "缺少 Bearer token")
        raw = authorization.split(" ", 1)[1].strip()
        session = store.resolve_token(raw)
        if session is None:
            raise HTTPException(401, "token 无效、过期或已撤销")
        return {"agent": session.agent}, session

    # ---------- 管理页面 ----------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        if not admin_ok(request):
            return page(request, "login.html", {"error": ""})
        return RedirectResponse("/admin", status_code=303)

    @app.post("/login")
    async def login(request: Request):
        peer = request.client.host if request.client else "unknown"
        if not sessions.admit_login(peer):
            response = page(request, "login.html", {"error": "登录尝试过多，请一分钟后重试"}, status_code=429)
            response.headers["Retry-After"] = str(LOGIN_WINDOW)
            return response
        form = await request.form()
        token = str(form.get("token", ""))
        if not secrets.compare_digest(token.encode(), cfg.admin_token.encode()):
            return page(request, "login.html", {"error": "令牌不正确"}, status_code=401)
        resp = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(
            cfg.session_cookie, sessions.issue(request.cookies.get(cfg.session_cookie, "")),
            httponly=True, samesite="strict",
            secure=request.url.scheme == "https" or public_origin.startswith("https://"), max_age=SESSION_TTL
        )
        return resp

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request):
        if not admin_ok(request):
            return RedirectResponse("/", status_code=303)
        return page(request, "admin.html", {"legacy_enabled": cfg.legacy_enabled})

    @app.post("/logout")
    def logout(request: Request):
        sessions.revoke(request.cookies.get(cfg.session_cookie, ""))
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(
            cfg.session_cookie, httponly=True, secure=request.url.scheme == "https"
            or public_origin.startswith("https://"), samesite="strict"
        )
        return response

    @app.get("/admin/legacy", response_class=HTMLResponse)
    def legacy(request: Request):
        if not admin_ok(request):
            return RedirectResponse("/", status_code=303)
        return page(request, "legacy.html", {
                "credentials": store.list_credentials(),
                "grants": store.list_grants(),
                "tokens": store.list_tokens(),
                "audit": store.list_audit(150),
                "hosts": sorted(hosts),
            })

    # ---------- 管理 API ----------

    @app.get("/admin/api/overview", dependencies=[Depends(require_admin)])
    def overview():
        return {
            "credentials": store.list_credentials(),
            "grants": store.list_grants(),
            "tokens": store.list_tokens(),
            "hosts": {k: {"host": v.host, "user": v.user, "allowed_commands": list(v.allowed_commands)} for k, v in hosts.items()},
        }

    @app.get("/admin/api/audit", dependencies=[Depends(require_admin)])
    def audit(limit: int = 200):
        return {"audit": store.list_audit(min(limit, 1000))}

    @app.post("/admin/api/credentials", dependencies=[Depends(require_admin)])
    def put_credential(body: CredentialRequest):
        if body.kind not in ("api_key", "oauth_google", "ssh_password", "ssh_key"):
            raise HTTPException(400, "未知凭证类型")
        if not body.secret:
            raise HTTPException(400, "secret 不能为空")
        store.put_credential(body.name, body.kind, body.secret, body.meta)
        store.audit("admin", "credential.put", "allowed", target=body.name, detail={"kind": body.kind, "meta": body.meta})
        return {"ok": True, "name": body.name}

    @app.delete("/admin/api/credentials/{name}", dependencies=[Depends(require_admin)])
    def delete_credential(name: str):
        ok = store.delete_credential(name)
        store.audit("admin", "credential.delete", "allowed" if ok else "error", target=name)
        return {"ok": ok}

    @app.post("/admin/api/grants", dependencies=[Depends(require_admin)])
    def create_grant(body: GrantRequest):
        if not body.actions:
            raise HTTPException(400, "actions 不能为空")
        gid = store.create_grant(body.agent, body.credential, body.actions, body.ttl_seconds, body.note)
        store.audit("admin", "grant.create", "allowed", target=gid, detail=_as_dict(body))
        return {"ok": True, "grant_id": gid}

    @app.post("/admin/api/grants/{grant_id}/revoke", dependencies=[Depends(require_admin)])
    def revoke_grant(grant_id: str):
        store.revoke_grant(grant_id)
        store.audit("admin", "grant.revoke", "allowed", target=grant_id)
        return {"ok": True}

    @app.post("/admin/api/tokens", dependencies=[Depends(require_admin)])
    def issue_token(body: TokenIssue):
        try:
            token, expires = store.issue_token(body.name, body.grant_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        store.audit("admin", "token.issue", "allowed", target=body.grant_id, detail={"name": body.name})
        # 明文 token 只在这一次响应里出现
        return {"ok": True, "token": token, "expires_at": expires}

    # ---------- AI 侧 ----------

    @app.get("/mcp/tools")
    def mcp_tools(authorization: str | None = Header(None)):
        _who, session = resolve_ai(authorization)
        return {"tools": broker.tool_specs(session)}

    @app.post("/mcp/call")
    async def mcp_call(request: Request, authorization: str | None = Header(None)):
        who, session = resolve_ai(authorization)
        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, "请求体必须是 JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "请求体必须是 JSON 对象")
        action = payload.get("action")
        args = payload.get("args") or {}
        if not isinstance(action, str) or not isinstance(args, dict):
            raise HTTPException(400, "需要 {action: string, args: object}")
        try:
            result = broker.call(session, action, args)
        except Denied as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        return {"ok": True, "result": result}

    @app.get("/mcp/whoami")
    def whoami(authorization: str | None = Header(None)):
        who, session = resolve_ai(authorization)
        return {
            "agent": who["agent"],
            "grant_id": session.grant_id,
            "credential": session.credential,
            "actions": session.actions,
            "expires_at": session.expires_at,
        }

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "ts": int(time.time())}

    return app
