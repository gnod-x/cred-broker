"""Read a single authorized mailbox with the Gmail read-only scope."""

from __future__ import annotations

import base64
import binascii
import json
import re
from html.parser import HTMLParser
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_RESPONSE = 4 * 1024 * 1024
MAX_BODY = 20000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def google_json(url: str, *, access_token: str | None = None, forms: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = "Bearer " + access_token
    data = None
    if forms is not None:
        data = urlencode(forms).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(Request(url, data=data, headers=headers), timeout=10) as response:
            payload = response.read(MAX_RESPONSE + 1)
        if len(payload) > MAX_RESPONSE:
            raise ValueError("response too large")
        result = json.loads(payload)
        if not isinstance(result, dict):
            raise ValueError("expected JSON object")
        return result
    except (URLError, OSError, ValueError) as exc:
        raise RuntimeError("Google 连接失败或返回了无效响应") from None


def access_from(response: dict) -> str:
    if "scope" in response:
        scope = response["scope"]
        if not isinstance(scope, str) or set(scope.split()) != {SCOPE}:
            raise RuntimeError("Google 授权范围与 Gmail 只读权限不匹配")
    token = response.get("access_token")
    if not isinstance(token, str) or not token or len(token) > 4096 or any(ch.isspace() for ch in token):
        raise RuntimeError("Google 未返回有效的访问令牌")
    return token


def mailbox(access_token: str) -> str:
    result = google_json(API + "/profile", access_token=access_token)
    email = result.get("emailAddress")
    if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+", email):
        raise RuntimeError("Google 未返回有效的邮箱地址")
    return email.lower()


def _refresh(secret: dict) -> str:
    return access_from(google_json(TOKEN_URL, forms={
        "client_id": secret["client_id"],
        "client_secret": secret["client_secret"],
        "refresh_token": secret["refresh_token"],
        "grant_type": "refresh_token",
    }))


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head", "template", "noscript"}:
            self.hidden.append(tag)
        if tag in {"br", "p", "div", "li", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _message_body(root: dict) -> str:
    todo = [root]
    html_text = ""
    seen = 0
    while todo and seen < 128:
        part = todo.pop()
        seen += 1
        if not isinstance(part, dict):
            continue
        if part.get("filename"):
            continue
        parts = part.get("parts")
        if isinstance(parts, list):
            todo.extend(reversed(parts[:128]))
        mime = part.get("mimeType")
        data = part.get("body")
        encoded = data.get("data") if isinstance(data, dict) else None
        if mime not in {"text/plain", "text/html"} or not isinstance(encoded, str):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_\-=]*", encoded):
            continue
        encoded = encoded[:MAX_BODY * 8]
        try:
            decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            continue
        if mime == "text/plain" and decoded.strip():
            return decoded[:MAX_BODY]
        if not html_text:
            text_parser = _PlainText()
            text_parser.feed(decoded)
            text_parser.close()
            html_text = "".join(text_parser.parts).strip()[:MAX_BODY]
    return html_text


def _scrub(value, token):
    if isinstance(value, str):
        return value.replace(token, "***")
    if isinstance(value, list):
        return [_scrub(item, token) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item, token) for key, item in value.items()}
    return value


def test_connection(settings: dict, secret: dict) -> dict:
    token = _refresh(secret)
    email = mailbox(token)
    if email != settings["email"].lower():
        raise RuntimeError("授权邮箱与连接登记的邮箱不一致")
    return _scrub({"email": email}, token)


def call(action: str, resource: str, arguments: dict, settings: dict, secret: dict) -> dict:
    if resource.lower() != settings["email"].lower():
        raise ValueError("邮箱资源不匹配")
    if action not in {"search", "read"}:
        raise ValueError("未知 Gmail 动作")
    if action == "search":
        query, limit = arguments.get("query", ""), arguments.get("limit", 10)
        if set(arguments) - {"query", "limit"} or not isinstance(query, str) or len(query) > 256:
            raise ValueError("搜索条件无效")
        if type(limit) is not int or not 1 <= limit <= 25:
            raise ValueError("搜索条数应为 1 至 25")
        token = _refresh(secret)
        if mailbox(token) != settings["email"].lower():
            raise RuntimeError("授权邮箱与连接登记的邮箱不一致")
        data = google_json(API + "/messages?" + urlencode({
            "q": query, "maxResults": limit, "fields": "messages(id,threadId),resultSizeEstimate",
        }), access_token=token)
        matches = data.get("messages", [])
        if not isinstance(matches, list):
            raise RuntimeError("Gmail 搜索结果格式无效")
        messages = [
            {"id": entry["id"], "thread_id": str(entry.get("threadId", ""))[:128]}
            for entry in matches[:limit]
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and len(entry["id"]) <= 128
        ]
        return _scrub({"messages": messages, "count": len(messages)}, token)
    if action == "read":
        mid = arguments.get("message_id")
        if set(arguments) != {"message_id"} or not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", mid):
            raise ValueError("邮件 ID 无效")
        token = _refresh(secret)
        if mailbox(token) != settings["email"].lower():
            raise RuntimeError("授权邮箱与连接登记的邮箱不一致")
        data = google_json(API + f"/messages/{mid}?format=full", access_token=token)
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("Gmail 邮件格式无效")
        headers = {}
        raw_headers = payload.get("headers", [])
        if not isinstance(raw_headers, list):
            raw_headers = []
        for field in raw_headers[:250]:
            if not isinstance(field, dict):
                continue
            name = str(field.get("name", "")).lower()
            if name in {"subject", "from", "to", "date"}:
                headers.setdefault(name, str(field.get("value", ""))[:4096])
        return _scrub({"id": mid, "headers": headers, "body": _message_body(payload),
                       "snippet": str(data.get("snippet", ""))[:1000]}, token)
    raise ValueError("未知 Gmail 动作")
