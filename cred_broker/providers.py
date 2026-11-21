"""Narrow, credential-injecting provider adapters."""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

HEAD = "https://api.github.com"
UNIT = r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\.service"
MACHINE = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
REPOSITORY = r"[A-Za-z0-9][A-Za-z0-9-]{0,99}/[A-Za-z0-9_.-]{1,100}"
EMAIL = r"[^\s@]+@[^\s@]+"
HOST = r"[A-Za-z0-9][A-Za-z0-9.:-]*"
SSH_USER = r"[a-z_][a-z0-9_-]{0,31}"
HOST_KEY = r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]+={0,2}"
INSTABOX_KEY = r"ibk_[A-Za-z0-9_.-]+"
LIMIT = 65536

NAMES = {
    "gmail": ("Gmail", "搜索和读取已授权邮箱的邮件"),
    "ssh": ("SSH", "查看指定服务的状态与日志"),
    "github": ("GitHub", "只读访问选定仓库的文件、Issue 和 PR"),
    "instabox": ("Instabox", "查看机器状态，按需开放固定诊断"),
    "feishu": ("飞书", "暂不支持"),
    "instagram": ("Instagram", "暂不支持"),
}
FIELDS = {
    "gmail": (("email",), (), ("client_id", "client_secret", "refresh_token"), ()),
    "ssh": (("host", "port", "user", "host_key", "services"), (), ("private_key",), ()),
    "github": (("app_id", "installation_id", "repositories"), (), ("private_key",), ()),
    "instabox": (("machines",), (), ("read_key",), ("diagnostic_key",)),
}
STRING = {"type": "string"}
COUNT = {"type": "integer", "minimum": 1, "maximum": 100}
TOOLS = {
    "gmail": {"search": ("Search email", {"query": {"type": "string", "maxLength": 256}, "limit": {"type": "integer", "minimum": 1, "maximum": 25}}, ["query"]),
              "read": ("Read email", {"message_id": STRING}, ["message_id"])},
    "ssh": {"status": ("Read systemd service status", {"unit": STRING}, ["unit"]),
            "logs": ("Read recent service logs", {"unit": STRING, "lines": COUNT}, ["unit"])},
    "github": {"contents": ("Read repository contents", {"path": STRING}, []),
               "issues": ("List repository issues", {"limit": COUNT}, []),
               "pulls": ("List repository pull requests", {"limit": COUNT}, [])},
    "instabox": {"describe": ("Read sandbox state", {}, []),
                 "diagnostics": ("Run a fixed read-only sandbox diagnostic", {"action": {"type": "string", "enum": ["uptime", "disk", "memory"]}}, ["action"])},
}


def _text_schema(pattern=None, limit=256):
    result = {"type": "string", "minLength": 1, "maxLength": limit,
              "pattern": r"^[^\u0000]+(?![\s\S])", "not": {"pattern": r"\u0000"}}
    if pattern:
        result["pattern"] = "^(?:" + pattern + r")(?![\s\S])"
    return result


def _number_schema(maximum):
    # Numeric strings are accepted by the validator, including up to 19 digits.
    # JSON Schema cannot compare their numeric value; callers must keep server validation.
    return {"oneOf": [{"type": "integer", "minimum": 1, "maximum": maximum},
                      {"type": "string", "pattern": r"^[1-9][0-9]{0,18}(?![\s\S])",
                       "description": f"Decimal integer string; server enforces value <= {maximum}.",
                       "serverValidation": {"numericMaximum": maximum}}]}


def _resource_array(pattern):
    # Duplicate inputs are accepted and normalized by _list, not rejected.
    return {"type": "array", "minItems": 1, "maxItems": 100,
            "items": _text_schema(pattern)}


def _setting_properties(provider):
    if provider == "gmail":
        return {"email": _text_schema(EMAIL)}
    if provider == "ssh":
        return {
            "host": _text_schema(HOST),
            "port": _number_schema(65535),
            "user": {**_text_schema(SSH_USER), "not": {"const": "root"}},
            "host_key": _text_schema(HOST_KEY, 8192),
            "services": _resource_array(UNIT),
        }
    if provider == "github":
        repositories = _resource_array(REPOSITORY)
        repositories["items"]["allOf"] = [{"not": {"pattern": r"/\.{1,2}(?![\s\S])"}}]
        return {"app_id": _number_schema(2**63 - 1),
                "installation_id": _number_schema(2**63 - 1),
                "repositories": repositories}
    if provider == "instabox":
        return {"machines": _resource_array(MACHINE)}
    raise ValueError("Unsupported provider")


def connection_schema(provider):
    """Describe accepted input; enumFrom/serverValidation require server enforcement."""
    if provider not in FIELDS:
        raise ValueError("Unsupported provider")

    settings_required, _, secret_required, secret_optional = FIELDS[provider]
    settings = {"type": "object", "properties": _setting_properties(provider),
                "required": list(settings_required), "additionalProperties": False}

    # Gmail credentials are obtained through OAuth, never submitted as form fields.
    secret_properties = {}
    if provider != "gmail":
        for name in (*secret_required, *secret_optional):
            pattern = INSTABOX_KEY if provider == "instabox" else None
            # Instabox keys pass a second _string validation with the default 256 limit.
            limit = 256 if provider == "instabox" else 32768
            secret_properties[name] = {**_text_schema(pattern, limit), "writeOnly": True}
    secret_schema = {"type": "object", "properties": secret_properties,
                     "required": [] if provider == "gmail" else list(secret_required),
                     "additionalProperties": False}

    tools = []
    for name, (description, raw_properties, required) in TOOLS[provider].items():
        properties = copy.deepcopy(raw_properties)
        for key, field in properties.items():
            if field.get("type") == "string":
                properties[key] = {**_text_schema(limit=field.get("maxLength", 2048)), **field}
        if provider == "gmail" and name == "read":
            properties["message_id"] = _text_schema(r"[A-Za-z0-9_-]{1,128}", 128)
        if provider == "github" and name == "contents":
            properties["path"]["serverValidation"] = "Nonempty slash-separated segments; no dot segments or backslashes."
        if provider == "ssh":
            properties["unit"] = {**_text_schema(UNIT, 2048), "enumFrom": "settings.services"}
        properties["resource"] = {"type": "string", "enumFrom": "connection.resources",
                                  "description": "Must be one of this grant's authorized connection resources; enforced by server."}
        tool = {"name": f"{provider}_{name}", "description": description,
                "inputSchema": {"type": "object", "properties": properties,
                                "required": list(dict.fromkeys([*required, "resource"])),
                                "additionalProperties": False}}
        if provider == "instabox" and name == "diagnostics":
            tool["requires_secret"] = ["diagnostic_key"]
            tool["description"] += "; available only when diagnostic_key is configured"
        tools.append(tool)

    resource_item = {
        "gmail": _text_schema(EMAIL),
        "ssh": {**_text_schema(limit=262), "enumFrom": "connection.resources",
                "description": "Configured host (up to 256 characters) plus ':' and decimal port; server validated."},
        "github": copy.deepcopy(_setting_properties("github")["repositories"]["items"]),
        "instabox": _text_schema(MACHINE),
    }[provider]
    return {
        "auth_mode": {"gmail": "oauth", "ssh": "ssh_key", "github": "github_app", "instabox": "api_key"}[provider],
        "settings": settings,
        "secret": secret_schema,
        "tools": tools,
        "resource_constraints": {
            "type": "array", "items": resource_item,
            "minItems": 1, "maxItems": 1 if provider in {"gmail", "ssh"} else 100,
            "uniqueItems": True, "source": "connection.resources",
        },
    }


def catalog():
    return [{"id": name, "name": label, "description": description, "supported": name in FIELDS}
            for name, (label, description) in NAMES.items()]


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required + optional) or set(required) - set(value):
        raise ValueError("Unexpected or missing fields")
    return dict(value)


def _string(value, pattern=None, limit=256):
    if not isinstance(value, str) or not 0 < len(value) <= limit or "\x00" in value:
        raise ValueError("Invalid text")
    if pattern and not re.fullmatch(pattern, value):
        raise ValueError("Invalid text format")
    return value


def _list(values, pattern):
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise ValueError("Expected a nonempty resource list")
    return list(dict.fromkeys(_string(value, pattern) for value in values))


def _number(value, maximum):
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("Invalid numeric setting")
    return value


def _settings(provider, settings):
    if provider not in FIELDS:
        raise ValueError("Unsupported provider")
    required, optional, _, _ = FIELDS[provider]
    clean = _fields(settings, required, optional)
    if provider == "gmail":
        clean["email"] = _string(clean["email"], EMAIL).lower()
    if provider == "ssh":
        clean["host"] = _string(clean["host"], HOST)
        clean["port"] = _number(clean["port"], 65535)
        clean["user"] = _string(clean["user"], SSH_USER)
        if clean["user"] == "root":
            raise ValueError("Use a dedicated non-root SSH account")
        clean["host_key"] = _string(clean["host_key"], HOST_KEY, 8192)
        clean["services"] = _list(clean["services"], UNIT)
    if provider == "github":
        clean["app_id"] = _number(clean["app_id"], 2**63-1)
        clean["installation_id"] = _number(clean["installation_id"], 2**63-1)
        clean["repositories"] = [value.lower() for value in _list(clean["repositories"], REPOSITORY)]
        if any(repo.split("/")[1] in {".", ".."} for repo in clean["repositories"]):
            raise ValueError("Invalid repository")
        clean["repositories"] = list(dict.fromkeys(clean["repositories"]))
    if provider == "instabox":
        clean["machines"] = _list(clean["machines"], MACHINE)
    return clean


def validate_connection(provider, settings, secret):
    clean = _settings(provider, settings)
    _, _, required, optional = FIELDS[provider]
    secrets = _fields(secret, required, optional)
    for name, value in secrets.items():
        secrets[name] = _string(value, None if name == "private_key" else r"[!-~]+", 32768 if name == "private_key" else 4096)
    if provider == "instabox":
        for key in secrets.values():
            _string(key, INSTABOX_KEY)
    return clean, secrets


def resource_list(provider, settings):
    settings = _settings(provider, settings)
    if provider == "gmail":
        values = [settings["email"]]
    elif provider == "ssh":
        values = [f'{settings["host"]}:{settings["port"]}']
    else:
        values = settings["repositories" if provider == "github" else "machines"]
    return [{"id": value, "label": value} for value in values]


def tool_specs(provider, settings, secret=None):
    _settings(provider, settings)
    specs = []
    for action, (description, properties, required) in TOOLS[provider].items():
        if provider == "instabox" and action == "diagnostics" and not (secret or {}).get("diagnostic_key"):
            continue
        properties = dict(properties)
        if provider == "ssh":
            properties["unit"] = {"type": "string", "enum": list(settings["services"])}
        specs.append({"name": f"{provider}_{action}", "description": description,
                      "inputSchema": {"type": "object", "properties": properties,
                                      "required": required, "additionalProperties": False}})
    return specs


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _json(url, headers=None, data=None):
    try:
        request = Request(url, headers=headers or {}, data=data)
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
            result = response.read(2 * 1024 * 1024 + 1)
        if len(result) > 2 * 1024 * 1024:
            raise ValueError
        return json.loads(result)
    except (OSError, ValueError):
        raise RuntimeError("Upstream request failed") from None


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _github_token(settings, secret, resource, action):
    try:
        key = serialization.load_pem_private_key(secret["private_key"].encode(), password=None)
    except (TypeError, ValueError):
        raise ValueError("Invalid GitHub App private key") from None
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError("GitHub App requires an RSA private key of at least 2048 bits")
    now = int(time.time())
    payload = _b64(json.dumps({"iat": now - 30, "exp": now + 480, "iss": str(settings["app_id"])}).encode())
    signing_input = f'{_b64(b"""{"alg":"RS256","typ":"JWT"}""")}.{payload}'
    jwt = f'{signing_input}.{_b64(key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()))}'
    headers = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json",
               "Accept": "application/vnd.github+json", "User-Agent": "cred-broker",
               "X-GitHub-Api-Version": "2022-11-28"}
    owner, repo = resource.split("/")
    installation = _json(f"{HEAD}/repos/{owner}/{repo}/installation", headers)
    if not isinstance(installation, dict) or installation.get("id") != settings["installation_id"]:
        raise RuntimeError("Repository is outside the configured GitHub App installation")
    permissions = {"contents": "contents", "issues": "issues", "pulls": "pull_requests", "test": "metadata"}
    data = json.dumps({"repositories": [repo], "permissions": {permissions[action]: "read"}}).encode()
    result = _json(f"{HEAD}/app/installations/{settings['installation_id']}/access_tokens", headers, data)
    if not isinstance(result, dict) or not isinstance(result.get("token"), str):
        raise RuntimeError("GitHub App token request failed")
    return {**headers, "Authorization": f"Bearer {result['token']}"}


def _github_call(action, resource, args, settings, secret):
    path = args.get("path", "")
    if action == "contents" and path:
        segments = path.split("/")
        if any(not value or value in {".", ".."} or "\\" in value for value in segments):
            raise ValueError("Invalid repository path")
        path = "/" + "/".join(quote(value, safe="") for value in segments)
    headers = _github_token(settings, secret, resource, action)
    url = f"{HEAD}/repos/{resource}"
    if action == "test":
        result = _json(url, headers)
        if not isinstance(result, dict) or str(result.get("full_name", "")).lower() != resource:
            raise RuntimeError("Repository check failed")
        return {"ok": True, "repository": resource}
    ephemeral = {"installation_token": headers["Authorization"].removeprefix("Bearer ")}
    if action == "contents":
        raw = _json(f"{url}/contents{path}", headers)
        if isinstance(raw, list):
            result = {"entries": [{key: row.get(key) for key in ("name", "path", "type", "size")}
                                  for row in raw[:100] if isinstance(row, dict)]}
            return _redact(result, ephemeral)
        if not isinstance(raw, dict):
            raise RuntimeError("Unexpected GitHub response")
        result = {key: raw.get(key) for key in ("name", "path", "type", "size")}
        if raw.get("type") == "file" and raw.get("encoding") == "base64" and isinstance(raw.get("content"), str):
            try:
                data = base64.b64decode("".join(raw["content"].split()), validate=True)
                result.update(content=data[:LIMIT].decode("utf-8", "replace"), truncated=len(data) > LIMIT)
            except ValueError:
                raise RuntimeError("Unexpected GitHub content encoding") from None
        return _redact(result, ephemeral)
    raw = _json(f"{url}/{action}?" + urlencode({"per_page": args.get("limit", 20)}), headers)
    if not isinstance(raw, list):
        raise RuntimeError("Unexpected GitHub response")
    result = {"items": [
        {"number": row.get("number"), "title": str(row.get("title", ""))[:512], "state": row.get("state"),
         "body": str(row.get("body") or "")[:4096], "updated_at": row.get("updated_at")}
        for row in raw[:args.get("limit", 20)] if isinstance(row, dict)
        and (action != "issues" or "pull_request" not in row)]}
    return _redact(result, ephemeral)


def _process(argv, env, timeout=15):
    deadline = time.monotonic() + timeout
    with subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, start_new_session=True) as process:
        try:
            with selectors.DefaultSelector() as event:
                event.register(process.stdout, selectors.EVENT_READ)
                output = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not event.select(remaining):
                        raise RuntimeError("Provider process timed out")
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > LIMIT:
                        raise RuntimeError("Provider output exceeded the limit")
                return process.wait(timeout=max(0, deadline - time.monotonic())), bytes(output)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("Provider command failed") from None
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)


def _ssh_call(action, args, settings, secret):
    unit = args["unit"]
    if unit not in settings["services"]:
        raise PermissionError("Service unit is outside this connection")
    if action == "logs":
        remote = f"/usr/bin/journalctl --no-pager --output=short-iso --lines={args.get('lines', 30)} --unit={unit}"
    else:
        remote = f"/usr/bin/systemctl show --no-pager --property=LoadState,ActiveState,SubState,Result -- {unit}"
    with tempfile.TemporaryDirectory(prefix="cred-broker-ssh-") as directory:
        key = Path(directory) / "identity"
        key.touch(mode=0o600)
        key.write_text(secret["private_key"].rstrip() + "\n")
        hosts = Path(directory) / "known_hosts"
        hosts.write_text("cred-broker-target " + settings["host_key"] + "\n")
        code, output = _process([
            "/usr/bin/ssh", "-F", "/dev/null", "-nT", "-oBatchMode=yes", "-oIdentitiesOnly=yes",
            "-oIdentityAgent=none", "-oStrictHostKeyChecking=yes", "-oGlobalKnownHostsFile=/dev/null",
            f"-oUserKnownHostsFile={hosts}", "-oHostKeyAlias=cred-broker-target",
            "-oConnectTimeout=5", "-oClearAllForwardings=yes", "-oPermitLocalCommand=no",
            "-oPasswordAuthentication=no", "-oKbdInteractiveAuthentication=no",
            "-i", str(key), "-p", str(settings["port"]), "-l", settings["user"], "--",
            settings["host"], remote
        ], {"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    if code == 255:
        raise RuntimeError("SSH connection failed")
    return {"exit_status": code, "stdout": output.decode("utf-8", "replace")}


def _instabox_call(resource, secret, action="describe", arguments=None):
    config = os.environ.get("CB_INSTABOX_CLI", "/usr/local/bin/instabox")
    binary = config if os.path.isabs(config) else shutil.which(config, path="/usr/local/bin:/usr/bin:/bin")
    if not binary:
        raise RuntimeError("Instabox CLI unavailable")
    diagnostic = action == "diagnostics"
    if diagnostic and not secret.get("diagnostic_key"):
        raise PermissionError("Diagnostic access is not configured")
    commands = {"uptime": ["/usr/bin/uptime"], "disk": ["/usr/bin/df", "-h", "/data"],
                "memory": ["/usr/bin/free", "-m"]}
    if diagnostic and (arguments or {}).get("action") not in commands:
        raise ValueError("Unsupported diagnostic")
    argv = [binary, "--server", "https://api.instabox.sh"]
    argv += (["exec", resource, "--json", "--timeout", "8", "--"] + commands[arguments["action"]]
             if diagnostic else ["get", resource, "--json"])
    with tempfile.TemporaryDirectory(prefix="cred-broker-instabox-") as directory:
        env = {"HOME": directory, "XDG_CONFIG_HOME": directory, "PATH": "/usr/local/bin:/usr/bin:/bin",
               "INSTABOX_API_KEY": secret["diagnostic_key" if diagnostic else "read_key"], "INSTABOX_NO_TELEMETRY": "1", "LC_ALL": "C"}
        code, output = _process(argv, env)
    try:
        row = json.loads(output) if code == 0 else None
    except ValueError:
        row = None
    if not isinstance(row, dict) or "error" in row:
        raise RuntimeError("Instabox state query failed")
    if diagnostic:
        if row.get("timedOut") or row.get("exitCode") != 0:
            raise RuntimeError("Instabox diagnostic failed")
        return {"action": arguments["action"], "stdout": str(row.get("stdout", ""))[:16384],
                "stderr": str(row.get("stderr", ""))[:4096], "truncated": bool(row.get("truncated"))}
    if isinstance(row.get("machine"), dict):
        row = row["machine"]
    if resource not in (row.get("id"), row.get("name")):
        raise RuntimeError("Instabox response did not match selected machine")
    return {key: row[key] for key in ("id", "name", "state", "status", "template", "createdAt", "updatedAt")
            if type(row.get(key)) in (str, bool, int)}


def _redact(result, secrets):
    if isinstance(result, str):
        for secret in secrets.values():
            for part in {secret}:
                if part:
                    result = result.replace(part, "***")
        return result
    if isinstance(result, list):
        return [_redact(item, secrets) for item in result]
    if isinstance(result, dict):
        return {key: _redact(value, secrets) for key, value in result.items()}
    return result


def call(provider, action, resource, arguments, settings, secret):
    settings, secret = validate_connection(provider, settings, secret)
    if resource not in {row["id"] for row in resource_list(provider, settings)}:
        raise PermissionError("Resource is outside this connection")
    prefix = provider + "_"
    if action.startswith(prefix):
        action = action[len(prefix):]
    if action not in TOOLS[provider]:
        raise PermissionError("Unsupported provider action")
    _, properties, required = TOOLS[provider][action]
    arguments = _fields(arguments, tuple(required), tuple(properties))
    for key, value in arguments.items():
        if properties[key]["type"] == "integer":
            if type(value) is not int:
                raise ValueError("Invalid integer argument")
            _number(value, properties[key].get("maximum", 100))
        else:
            _string(value, UNIT if key == "unit" else None, properties[key].get("maxLength", 2048))
            if "enum" in properties[key] and value not in properties[key]["enum"]:
                raise ValueError("Unsupported argument value")
    if provider == "ssh":
        result = _ssh_call(action, arguments, settings, secret)
    elif provider == "github":
        result = _github_call(action, resource, arguments, settings, secret)
    elif provider == "instabox":
        result = _instabox_call(resource, secret, action, arguments)
    else:
        from . import provider_gmail
        result = provider_gmail.call(action, resource, arguments, settings, secret)
    return _redact(result, secret)


def test_connection(provider, settings, secret):
    settings, secret = validate_connection(provider, settings, secret)
    if provider == "gmail":
        from . import provider_gmail
        return _redact(provider_gmail.test_connection(settings, secret), secret)
    for row in resource_list(provider, settings):
        resource = row["id"]
        if provider == "github":
            _github_call("test", resource, {}, settings, secret)
        elif provider == "instabox":
            _instabox_call(resource, secret)
        else:
            result = _ssh_call("status", {"unit": settings["services"][0]}, settings, secret)
            if result["exit_status"] != 0 or "LoadState=loaded" not in result["stdout"].splitlines():
                raise RuntimeError("SSH unit check failed")
    return {"ok": True}
