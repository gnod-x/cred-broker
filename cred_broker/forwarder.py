"""通用 HTTP 注入与 Google OAuth token 交换。

AI 只给 (service, method, path, body)，broker 负责：
- 校验 path 白名单；
- 注入密钥到配置指定的 header（或 query）；
- 转发并返回响应。

Google refresh token 永不外发，只在 broker 内部换 access token。
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass


class ForwardError(RuntimeError):
    pass


class PolicyError(ForwardError):
    """策略拒绝（不是上游故障），调用方应返回 403 并记 denied。"""


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str]
    body: str

    def as_dict(self) -> dict:
        return {"status": self.status, "headers": self.headers, "body": self.body}


ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}


def check_path(service_meta: dict, path: str) -> str | None:
    """path 必须以配置的前缀之一开头，且不能含 .. 或协议。"""
    if not path.startswith("/"):
        return "path 必须以 / 开头"
    if "://" in path or ".." in path:
        return "path 非法"
    # 拒绝编码后的穿越与绝对 URL（%2e%2e / %2f%2f）
    lowered = path.lower()
    if "%2e" in lowered or "%2f" in lowered or "%5c" in lowered:
        return "path 含编码字符，拒绝"
    prefixes = service_meta.get("allowed_paths") or ["/"]
    if not any(path == p or path.startswith(p.rstrip("*")) for p in prefixes):
        return f"path 不在白名单 {prefixes}"
    return None


def _build_request(service: dict, meta: dict, secret: dict, method: str, path: str, body: bytes | None, extra_headers: dict) -> urllib.request.Request:
    base = meta.get("base_url", "").rstrip("/")
    if not base:
        raise ForwardError("服务缺少 base_url")
    url = base + path

    headers = {"User-Agent": "cred-broker/0.2", **extra_headers}
    inject = meta.get("inject") or {}

    kind = inject.get("type", "header")
    if kind == "header":
        value = secret.get("value", "")
        prefix = inject.get("prefix", "")
        headers[inject.get("name", "Authorization")] = f"{prefix}{value}"
    elif kind == "query":
        q = urllib.parse.urlencode({inject.get("name", "key"): secret.get("value", "")})
        url = f"{url}{'&' if '?' in url else '?'}{q}"
    elif kind == "none":
        pass
    else:
        raise ForwardError(f"未知注入类型 {kind}")

    if body is not None:
        headers.setdefault("Content-Type", "application/json")

    return urllib.request.Request(url, data=body, headers=headers, method=method.upper())


def forward(service_row: dict, method: str, path: str, body: str | None, timeout: int = 30) -> HttpResult:
    method = method.upper()
    if method not in ALLOWED_METHODS:
        raise ForwardError(f"不支持的方法 {method}")

    kind, meta, secret = service_row["kind"], service_row["meta"], service_row["secret"]

    reason = check_path(meta, path)
    if reason:
        raise PolicyError(reason)

    if kind == "oauth_google":
        meta = dict(meta)
        secret = _google_access_token(meta, secret, timeout)

    payload = body.encode() if body else None
    req = _build_request(service_row, meta, secret, method, path, payload, {})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return HttpResult(
                status=resp.status,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=raw.decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return HttpResult(
            status=exc.code,
            headers={k.lower(): v for k, v in (exc.headers or {}).items()},
            body=raw.decode("utf-8", "replace"),
        )
    except Exception as exc:  # noqa: BLE001
        raise ForwardError(f"请求失败: {exc}") from exc


_token_cache: dict[str, tuple[str, int]] = {}


def _google_access_token(meta: dict, secret: dict, timeout: int) -> dict:
    """用 refresh token 换 access token，进程内缓存到过期前 60 秒。"""
    cache_key = meta.get("client_id", "") + "|" + secret.get("refresh_token", "")[:12]
    cached = _token_cache.get(cache_key)
    if cached and cached[1] > time.time() + 60:
        return {"value": cached[0]}

    data = urllib.parse.urlencode(
        {
            "client_id": meta.get("client_id", ""),
            "client_secret": meta.get("client_secret", ""),
            "refresh_token": secret.get("refresh_token", ""),
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())
    token = body.get("access_token")
    if not token:
        raise ForwardError(f"Google token 交换失败: {body}")
    _token_cache[cache_key] = (token, int(time.time()) + int(body.get("expires_in", 3600)))
    return {"value": token}
