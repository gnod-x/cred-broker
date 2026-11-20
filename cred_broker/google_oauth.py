"""Administrator consent flow for an individual Gmail connection."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import provider_gmail as gmail

CALLBACK = "/admin/oauth/google/callback"
STATE_COOKIE = "cb_google_flow"
FLOW_TTL = 600


@dataclass(frozen=True)
class GoogleSettings:
    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def load(cls) -> GoogleSettings | None:
        client_id = os.environ.get("CB_GOOGLE_CLIENT_ID", "")
        client_secret = os.environ.get("CB_GOOGLE_CLIENT_SECRET", "")
        redirect_uri = os.environ.get("CB_GOOGLE_REDIRECT_URI", "")
        if not any((client_id, client_secret, redirect_uri)):
            return None
        url = urlsplit(redirect_uri)
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        transport_ok = url.scheme == "https" or (url.scheme == "http" and local)
        if not (client_id and client_secret and transport_ok and url.hostname and url.path == CALLBACK
                and not (url.username or url.password or url.query or url.fragment)):
            raise RuntimeError("Google OAuth 配置无效，请检查三项 CB_GOOGLE_* 设置及回调地址")
        return cls(client_id, client_secret, redirect_uri)


@dataclass(frozen=True)
class Flow:
    browser: str
    verifier: str
    label: str
    expires: float


def register_google_oauth(app, control, require_browser_admin, settings: GoogleSettings | None):
    if settings is None:
        return
    pending: dict[str, Flow] = {}
    guard = threading.Lock()
    secure = urlsplit(settings.redirect_uri).scheme == "https"

    @app.get("/admin/oauth/google/start", dependencies=[Depends(require_browser_admin)])
    def start(request: Request, label: str = ""):
        if request.headers.get("sec-fetch-site") not in {None, "none", "same-origin"}:
            raise HTTPException(403, "请从管理界面发起 Google 授权")
        label = label.strip()
        if len(label) > 100 or any(ord(ch) < 32 for ch in label):
            raise HTTPException(400, "连接名称无效")
        state = secrets.token_urlsafe(32)
        browser = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        now = time.monotonic()
        with guard:
            for key in [key for key, flow in pending.items() if flow.expires <= now]:
                del pending[key]
            if len(pending) >= 128:
                raise HTTPException(429, "待处理的 Google 授权过多")
            pending[state] = Flow(browser, verifier, label, now + FLOW_TTL)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        response = RedirectResponse(gmail.AUTHORIZE_URL + "?" + urlencode({
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "response_type": "code",
            "scope": gmail.SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }), status_code=303)
        response.set_cookie(STATE_COOKIE, browser, max_age=FLOW_TTL, path=CALLBACK,
                            httponly=True, samesite="lax", secure=secure)
        return response

    @app.get(CALLBACK)
    def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        browser = request.cookies.get(STATE_COOKIE, "")
        with guard:
            flow = pending.get(state)
            if (flow is None or flow.expires <= time.monotonic()
                    or not secrets.compare_digest(browser.encode(), flow.browser.encode())):
                raise HTTPException(400, "Google 授权请求无效或已过期")
            del pending[state]
        status = "cancelled" if error else "failed"
        if not error and code and len(code) <= 4096:
            try:
                result = gmail.google_json(gmail.TOKEN_URL, forms={
                    "client_id": settings.client_id,
                    "client_secret": settings.client_secret,
                    "redirect_uri": settings.redirect_uri,
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": flow.verifier,
                })
                token = gmail.access_from(result)
                refresh = result.get("refresh_token")
                if not isinstance(refresh, str) or not refresh or len(refresh) > 4096:
                    raise ValueError("missing Google refresh token")
                email = gmail.mailbox(token)
                control.create_connection("gmail", flow.label or email, {"email": email}, {
                    "client_id": settings.client_id,
                    "client_secret": settings.client_secret,
                    "refresh_token": refresh,
                })
                status = "connected"
            except (RuntimeError, ValueError):
                pass
        # The local landing page allows the browser to send the Strict admin cookie again.
        response = RedirectResponse("/google/complete?" + urlencode({"google": status}), status_code=303)
        response.delete_cookie(STATE_COOKIE, path=CALLBACK, httponly=True, samesite="lax", secure=secure)
        return response

    @app.get("/google/complete")
    def complete(google: str = ""):
        if google not in {"connected", "failed", "cancelled"}:
            google = "failed"
        # Refresh navigates from a same-origin document after a cross-site OAuth callback.
        from fastapi.responses import HTMLResponse

        target = "/admin?google=" + google
        return HTMLResponse(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>返回授权中心</title>'
            f'<meta http-equiv="refresh" content="0;url={target}">'
            f'<a href="{target}">返回授权中心</a></html>'
        )
