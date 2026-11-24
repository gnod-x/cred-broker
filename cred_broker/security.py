"""Exact ingress policy and bounded HTTP reads shared by web and MCP."""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from .origin import canonical_origin

MAX_REQUEST_BODY = 65536
DEFAULT_BODY_READ_TIMEOUT = 10.0
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")


class HttpBodyError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def validate_read_limits(max_body_bytes, read_timeout):
    if type(max_body_bytes) is not int or not 1024 <= max_body_bytes <= MAX_REQUEST_BODY:
        raise RuntimeError("CB_MAX_REQUEST_BODY must be an integer from 1024 to 65536")
    if (isinstance(read_timeout, bool) or not isinstance(read_timeout, (int, float))
            or not math.isfinite(read_timeout) or not 0 < read_timeout <= 60):
        raise RuntimeError("CB_BODY_READ_TIMEOUT must be greater than 0 and at most 60")


@dataclass(frozen=True)
class HttpSecurityPolicy:
    allowed_origins: tuple[str, ...] = ()
    binding: str = "127.0.0.1:8765"

    def __post_init__(self):
        try:
            origins = tuple(dict.fromkeys(canonical_origin(value) for value in self.allowed_origins))
        except (TypeError, ValueError):
            raise RuntimeError("Allowed HTTP origins must be exact HTTP(S) origins") from None
        if len(origins) > 32:
            raise RuntimeError("At most 32 HTTP origins may be configured")
        object.__setattr__(self, "allowed_origins", origins)
        host, separator, raw_port = self.binding.rpartition(":")
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            port = 0
        if not separator or not host or not 1 <= port <= 65535:
            raise RuntimeError("CB_BIND must contain an explicit valid port")

    @classmethod
    def from_config(cls, cfg):
        origins = tuple(cfg.allowed_origins)
        if cfg.public_origin:
            origins += (cfg.public_origin,)
        return cls(origins, cfg.binding)

    def origins_for(self, scope):
        # The ASGI server address is the listening socket, never a client header.
        # A port-mapped external loopback URL still needs an explicit origin.
        scheme = scope.get("scheme", "http")
        ports = {int(self.binding.rpartition(":")[2])}
        server = scope.get("server")
        if server is not None and len(server) == 2 and type(server[1]) is int and 1 <= server[1] <= 65535:
            ports.add(server[1])
        elif server is None or (len(server) == 2 and server[1] is None):
            # Minimal ASGI fixtures have no listening socket; do not infer a port from Host.
            ports.add(443 if scheme == "https" else 80)
        origins = set(self.allowed_origins)
        for port in ports:
            for host in LOOPBACK_HOSTS:
                origins.add(canonical_origin(f"{scheme}://{host}:{port}"))
        host, _, raw_port = self.binding.rpartition(":")
        if host not in {"0.0.0.0", "::", "[::]"}:
            try:
                origins.add(canonical_origin(f"{scheme}://{host}:{raw_port}"))
            except ValueError:
                raise RuntimeError("CB_BIND contains an invalid HTTP host") from None
        return origins

    def validate(self, scope):
        headers = scope.get("headers", ())
        hosts = [value for key, value in headers if key.lower() == b"host"]
        origin_headers = [value for key, value in headers if key.lower() == b"origin"]
        if len(hosts) != 1:
            return 400, "Host denied"
        if len(origin_headers) > 1:
            return 403, "Origin denied"
        try:
            host = hosts[0].decode("ascii")
            # Match using each explicitly permitted external scheme. This supports
            # a trusted TLS terminator without trusting X-Forwarded-Proto.
            origins = self.origins_for(scope)
            if not any(canonical_origin(urlsplit(origin).scheme + "://" + host) == origin
                       for origin in origins):
                return 400, "Host denied"
        except (UnicodeError, ValueError):
            return 400, "Host denied"
        if origin_headers:
            try:
                origin = canonical_origin(origin_headers[0].decode("ascii"))
            except (UnicodeError, ValueError):
                return 403, "Origin denied"
            if origin not in origins:
                return 403, "Origin denied"
        if scope.get("method") not in {"GET", "HEAD", "OPTIONS"}:
            sites = [value.lower() for key, value in headers if key.lower() == b"sec-fetch-site"]
            if b"cross-site" in sites:
                return 403, "Origin denied"
        return None


async def read_http_body(receive, *, max_body_bytes=MAX_REQUEST_BODY,
                         read_timeout=DEFAULT_BODY_READ_TIMEOUT):
    """Read at most the fixed byte budget within one absolute upload deadline."""
    validate_read_limits(max_body_bytes, read_timeout)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + read_timeout
    body = bytearray()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise HttpBodyError(408, "Request body timed out")
        try:
            message = await asyncio.wait_for(receive(), timeout=remaining)
        except asyncio.TimeoutError:
            raise HttpBodyError(408, "Request body timed out") from None
        if message["type"] == "http.disconnect":
            raise HttpBodyError(400, "Request disconnected")
        if message["type"] != "http.request":
            raise HttpBodyError(400, "Invalid request body")
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes):
            raise HttpBodyError(400, "Invalid request body")
        if len(chunk) > max_body_bytes - len(body):
            raise HttpBodyError(413, "Request too large")
        body.extend(chunk)
        if not message.get("more_body", False):
            return bytes(body)


class HttpSecurityMiddleware:
    """Reject before routing or mutations; buffer only a bounded request body."""
    def __init__(self, app, *, policy, max_body_bytes=MAX_REQUEST_BODY,
                 read_timeout=DEFAULT_BODY_READ_TIMEOUT):
        validate_read_limits(max_body_bytes, read_timeout)
        self.app = app
        self.policy = policy
        self.max_body_bytes = max_body_bytes
        self.read_timeout = read_timeout

    async def reject(self, scope, receive, send, status, detail):
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if scope.get("http_version", "1.1") in {"1.0", "1.1"}:
            # Never reuse a connection whose rejected upload has not been drained.
            headers["Connection"] = "close"
        await JSONResponse({"detail": detail}, status_code=status, headers=headers)(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        denied = self.policy.validate(scope)
        if denied is not None:
            await self.reject(scope, receive, send, *denied)
            return
        headers = scope.get("headers", ())
        lengths = [value for key, value in headers if key.lower() == b"content-length"]
        transfer = any(key.lower() == b"transfer-encoding" for key, _ in headers)
        if len(lengths) > 1 or (lengths and transfer):
            await self.reject(scope, receive, send, 400, "Invalid request length")
            return
        if lengths:
            value = lengths[0]
            if (not value or not value.isdigit() or len(value) > 20
                    or int(value) > self.max_body_bytes):
                await self.reject(scope, receive, send, 413, "Request too large")
                return
        try:
            body = await read_http_body(receive, max_body_bytes=self.max_body_bytes,
                                        read_timeout=self.read_timeout)
        except HttpBodyError as exc:
            await self.reject(scope, receive, send, exc.status_code, exc.detail)
            return
        replayed = False

        async def bounded_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
