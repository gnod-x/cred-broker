"""Canonical HTTP(S) origin validation shared by the app and local tools."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


def canonical_origin(value: str) -> str:
    if not value or value != value.strip() or any(char.isspace() or ord(char) < 0x21 for char in value):
        raise ValueError("invalid origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise ValueError("invalid origin") from exc
    if (parsed.scheme.lower() not in {"http", "https"} or not hostname or not parsed.netloc
            or "@" in parsed.netloc or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment or "?" in value or "#" in value
            or parsed.netloc.endswith(":")):
        raise ValueError("invalid origin")
    if port is not None and port == 0:
        raise ValueError("invalid origin port")
    scheme = parsed.scheme.lower()
    if ":" in hostname:
        if "%" in hostname:
            raise ValueError("invalid IPv6 origin")
        try:
            host = f"[{ipaddress.IPv6Address(hostname).compressed}]"
        except ValueError as exc:
            raise ValueError("invalid IPv6 origin") from exc
    else:
        try:
            host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("invalid hostname") from exc
        labels = host[:-1].split(".") if host.endswith(".") else host.split(".")
        if len(host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        ):
            raise ValueError("invalid hostname")
    if port is not None and port not in ({80} if scheme == "http" else {443}):
        host += f":{port}"
    return f"{scheme}://{host}"


def canonical_public_origin(value: str) -> str:
    if not value:
        return ""
    try:
        return canonical_origin(value)
    except ValueError as exc:
        raise RuntimeError(
            "CB_PUBLIC_ORIGIN must be a valid HTTP(S) origin without path or credentials"
        ) from exc
