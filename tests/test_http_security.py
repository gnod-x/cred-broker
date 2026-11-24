"""Ingress tests: exact ASGI boundaries and real Uvicorn through a byte proxy.

All storage and credentials are temporary fixtures; no production endpoints.
"""
import asyncio
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from cred_broker.config import _http_settings
from cred_broker.security import HttpSecurityMiddleware, HttpSecurityPolicy


def scope(headers=(), method="POST", server=("127.0.0.1", 8765)):
    return {
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": "/", "raw_path": b"/", "query_string": b"",
        "root_path": "", "client": ("127.0.0.1", 1234), "server": server,
        "headers": list(headers),
    }


HOST = (b"host", b"localhost:8765")


class IngressBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def exchange(self, *, headers=None, messages=None, delay=0, timeout=.1,
                       maximum=65536, policy=None, method="POST"):
        entered, sent = [], []
        queue = list(messages if messages is not None else [
            {"type": "http.request", "body": b"", "more_body": False}])

        async def receive():
            if delay:
                await asyncio.sleep(delay)
            if queue:
                return queue.pop(0)
            await asyncio.sleep(5)

        async def downstream(request_scope, receive, send):
            entered.append(await receive())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def send(message):
            sent.append(message)

        app = HttpSecurityMiddleware(downstream, policy=policy or HttpSecurityPolicy(),
                                     max_body_bytes=maximum, read_timeout=timeout)
        await app(scope(headers if headers is not None else [HOST], method=method), receive, send)
        return sent[0]["status"], entered, dict(sent[0]["headers"])

    async def test_exact_size_and_content_length_are_accepted(self):
        for headers in ([HOST], [HOST, (b"content-length", b"65536")],
                        [HOST, (b"transfer-encoding", b"chunked")]):
            result, entered, _ = await self.exchange(headers=headers, messages=[
                {"type": "http.request", "body": b"x" * 32768, "more_body": True},
                {"type": "http.request", "body": b"x" * 32768, "more_body": False}])
            self.assertEqual(result, 200)
            self.assertEqual(len(entered[0]["body"]), 65536)

    async def test_actual_oversize_rejected_before_any_business_handler(self):
        for extra in ([], [(b"content-length", b"1")], [(b"transfer-encoding", b"chunked")]):
            result, entered, headers = await self.exchange(headers=[HOST, *extra], messages=[
                {"type": "http.request", "body": b"x" * 65536, "more_body": True},
                {"type": "http.request", "body": b"x", "more_body": False}])
            self.assertEqual(result, 413)
            self.assertEqual(entered, [])
            self.assertEqual(headers[b"cache-control"], b"no-store")
            self.assertEqual(headers[b"x-content-type-options"], b"nosniff")
            self.assertEqual(headers[b"connection"], b"close")

    async def test_declared_oversize_and_invalid_framing_rejected(self):
        for values, expected in (
            ([(b"content-length", b"65537")], 413),
            ([(b"content-length", b"-1")], 413),
            ([(b"content-length", b"not-a-length")], 413),
            ([(b"content-length", b"1"), (b"content-length", b"1")], 400),
            ([(b"content-length", b"1"), (b"transfer-encoding", b"chunked")], 400),
        ):
            status, entered, _ = await self.exchange(headers=[HOST, *values])
            self.assertEqual(status, expected)
            self.assertEqual(entered, [])

    async def test_absolute_deadline_includes_multiple_short_chunks(self):
        status, entered, _ = await self.exchange(timeout=.09, delay=.04, messages=[
            {"type": "http.request", "body": b"x", "more_body": True}
            for _ in range(4)])
        self.assertEqual(status, 408)
        self.assertEqual(entered, [])

    async def test_stalled_and_disconnected_uploads_fail_closed(self):
        for messages, expected in (([], 408), ([{"type": "http.disconnect"}], 400),
                                    ([{"type": "websocket.receive"}], 400)):
            status, entered, _ = await self.exchange(messages=messages, timeout=.02)
            self.assertEqual(status, expected)
            self.assertEqual(entered, [])

    async def test_host_and_origin_denials_precede_handlers(self):
        for headers, expected in (
            ([], 400), ([HOST, HOST], 400),
            ([(b"host", b"attacker.example")], 400),
            ([(b"host", b"localhost:9999")], 400),
            ([HOST, (b"origin", b"null")], 403),
            ([HOST, (b"origin", b"http://localhost:9999")], 403),
            ([HOST, (b"origin", b"http://[bad")], 403),
            ([HOST, (b"origin", b"http://localhost:8765"), (b"origin", b"http://localhost:8765")], 403),
            ([HOST, (b"sec-fetch-site", b"cross-site")], 403),
        ):
            status, entered, _ = await self.exchange(headers=headers)
            self.assertEqual(status, expected)
            self.assertEqual(entered, [])

    async def test_native_bearer_without_origin_and_valid_json_form(self):
        for kind, body in ((b"application/json", b'{"fixture":true}'),
                           (b"application/x-www-form-urlencoded", b"token=fake")):
            status, entered, _ = await self.exchange(headers=[
                HOST, (b"authorization", b"Bearer fake-fixture"), (b"content-type", kind)],
                messages=[{"type": "http.request", "body": body}])
            self.assertEqual(status, 200)
            self.assertEqual(entered[0]["body"], body)


class ExactPolicyTests(unittest.TestCase):
    def test_explicit_lan_https_and_no_forwarded_header_trust(self):
        policy = HttpSecurityPolicy(("http://192.0.2.10:8765", "https://broker.example"))
        for host, origin in ((b"192.0.2.10:8765", b"http://192.0.2.10:8765"),
                             (b"broker.example", b"https://broker.example")):
            self.assertIsNone(policy.validate(scope([(b"host", host), (b"origin", origin)])))
        self.assertEqual(policy.validate(scope([
            (b"host", b"attacker.example"), (b"x-forwarded-host", b"localhost:8765"),
            (b"x-forwarded-proto", b"https")]))[0], 400)

    def test_only_binding_or_real_server_loopback_ports_are_automatic(self):
        policy = HttpSecurityPolicy()
        for host in (b"localhost:8765", b"127.0.0.1:8765", b"[::1]:8765"):
            self.assertIsNone(policy.validate(scope([(b"host", host)])))
        self.assertIsNone(policy.validate(scope([(b"host", b"localhost:18765")],
                                                server=("127.0.0.1", 18765))))
        self.assertEqual(policy.validate(scope([(b"host", b"localhost:18766")]))[0], 400)
        explicit = HttpSecurityPolicy(("http://localhost:18766",))
        self.assertIsNone(explicit.validate(scope([(b"host", b"localhost:18766")])))
        # httpx fixtures have no real port; default 80 is allowed, arbitrary ports are not.
        self.assertIsNone(policy.validate(scope([(b"host", b"localhost")], server=("localhost", None))))
        self.assertEqual(policy.validate(scope([(b"host", b"localhost:18766")],
                                               server=("localhost", None)))[0], 400)

    def test_invalid_allowed_origin_config_and_body_limits_fail(self):
        for env in (
            {"CB_ALLOWED_ORIGINS": "http://localhost"},
            {"CB_ALLOWED_ORIGINS": '["http://localhost/path"]'},
            {"CB_ALLOWED_ORIGINS": '["http://user@localhost"]'},
            {"CB_ALLOWED_ORIGINS": "[null]"},
            {"CB_ALLOWED_ORIGINS": json.dumps(["http://localhost"] * 33)},
            {"CB_MAX_REQUEST_BODY": "65537"}, {"CB_MAX_REQUEST_BODY": "0"},
            {"CB_BODY_READ_TIMEOUT": "nan"}, {"CB_BODY_READ_TIMEOUT": "inf"},
            {"CB_BODY_READ_TIMEOUT": "0"}, {"CB_BODY_READ_TIMEOUT": "61"},
        ):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                with self.assertRaises(RuntimeError):
                    _http_settings()
        with patch.dict(os.environ, {"CB_ALLOWED_ORIGINS": '["HTTPS://Broker.Example:443"]'}, clear=True):
            self.assertEqual(_http_settings(), (("https://broker.example",), 10.0, 65536))


class RawProxy(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.create_connection(("127.0.0.1", self.server.upstream_port), timeout=3)
        try:
            peers = (self.request, upstream)
            while True:
                ready, _, _ = select.select(peers, [], [], 5)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    target = upstream if source is self.request else self.request
                    target.sendall(data)
        except (OSError, TimeoutError):
            return
        finally:
            upstream.close()


class ThreadedProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class RealUvicornIngressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="broker-http-security-")
        cls.root = Path(cls.temporary.name)
        cls.process = None
        cls.proxy = None
        cls.log = (cls.root / "server.log").open("w+")
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                cls.backend_port = listener.getsockname()[1]
            cls.proxy = ThreadedProxy(("127.0.0.1", 0), RawProxy)
            cls.proxy.upstream_port = cls.backend_port
            cls.proxy_port = cls.proxy.server_address[1]
            cls.proxy_thread = threading.Thread(target=cls.proxy.serve_forever, daemon=True)
            cls.proxy_thread.start()
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("CB_", "FORWARDED_", "UVICORN_"))}
            env.update(CB_DATA_DIR=str(cls.root / "data"),
                       CB_ADMIN_TOKEN="http-ingress-fake-admin",
                       CB_BIND=f"127.0.0.1:{cls.backend_port}",
                       CB_ALLOWED_ORIGINS=json.dumps([f"http://127.0.0.1:{cls.proxy_port}"]),
                       CB_BODY_READ_TIMEOUT="0.35", FORWARDED_ALLOW_IPS="*",
                       PYTHONDONTWRITEBYTECODE="1")
            cls.process = subprocess.Popen([sys.executable, "-B", "-m", "cred_broker"],
                                            cwd=Path(__file__).resolve().parents[1],
                                            env=env, stdout=cls.log, stderr=cls.log)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if cls.process.poll() is not None:
                    cls.log.seek(0)
                    raise AssertionError("Fixture Uvicorn exited: " + cls.log.read())
                try:
                    with httpx.Client(trust_env=False, timeout=.3) as client:
                        if client.get(f"http://127.0.0.1:{cls.backend_port}/healthz").status_code == 200:
                            return
                except httpx.TransportError:
                    pass
                time.sleep(.03)
            raise AssertionError("Fixture Uvicorn startup timed out")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if cls.process is not None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.process.kill()
                cls.process.wait(timeout=5)
        if cls.proxy is not None:
            cls.proxy.shutdown()
            cls.proxy.server_close()
            cls.proxy_thread.join(timeout=5)
        cls.log.close()
        cls.temporary.cleanup()

    def raw(self, data, *, delay_chunks=()):
        with socket.create_connection(("127.0.0.1", self.proxy_port), timeout=3) as peer:
            peer.settimeout(3)
            peer.sendall(data)
            for pause, chunk in delay_chunks:
                time.sleep(pause)
                try:
                    peer.sendall(chunk)
                except OSError:
                    break
            chunks = []
            while True:
                data = peer.recv(65536)
                if not data:
                    break
                chunks.append(data)
            return b"".join(chunks)

    def test_real_proxy_forwarded_headers_do_not_change_scheme_or_peer(self):
        # Raw TCP proxy does not sanitize anything: ingress itself ignores forwarded headers.
        with httpx.Client(base_url=f"http://127.0.0.1:{self.proxy_port}",
                          timeout=3, trust_env=False) as client:
            login = client.post("/login", headers={
                "X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.1"},
                data={"token": "http-ingress-fake-admin"})
            self.assertEqual(login.status_code, 303, login.text)
            self.assertNotIn("Secure", login.headers["set-cookie"])
            for index in range(7):
                bad = client.post("/login", headers={"X-Forwarded-For": f"198.51.100.{index + 2}"},
                                  data={"token": "wrong-fixture"})
                self.assertEqual(bad.status_code, 401)
            blocked = client.post("/login", headers={"X-Forwarded-For": "203.0.113.250"},
                                  data={"token": "wrong-fixture"})
            self.assertEqual(blocked.status_code, 429)

    def test_real_proxy_wrong_host_and_origin_rejected(self):
        with httpx.Client(base_url=f"http://127.0.0.1:{self.proxy_port}",
                          timeout=3, trust_env=False) as client:
            self.assertEqual(client.get("/healthz", headers={"Host": "attacker.example"}).status_code, 400)
            for path in ("/admin/api/v1/connections", "/mcp"):
                response = client.post(path, headers={"Origin": "http://attacker.example"}, json={})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.headers["connection"], "close")
            self.assertEqual(client.post("/mcp", headers={"Authorization": "Bearer fake"},
                                         json={}).status_code, 401)

    def test_real_chunked_overflow_returns_413_and_closes(self):
        head = (f"POST /login HTTP/1.1\r\nHost: 127.0.0.1:{self.proxy_port}\r\n"
                "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n").encode()
        response = self.raw(head + b"10000\r\n" + b"x" * 65536 + b"\r\n1\r\nx\r\n0\r\n\r\n")
        self.assertTrue(response.startswith(b"HTTP/1.1 413"), response[:100])
        self.assertIn(b"connection: close", response.lower())

    def test_real_partial_content_length_deadline_returns_408(self):
        head = (f"POST /login HTTP/1.1\r\nHost: 127.0.0.1:{self.proxy_port}\r\n"
                "Content-Length: 100\r\nConnection: close\r\n\r\nx").encode()
        started = time.monotonic()
        response = self.raw(head)
        self.assertTrue(response.startswith(b"HTTP/1.1 408"), response[:100])
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn(b"connection: close", response.lower())

    def test_real_slow_chunked_absolute_deadline_returns_408(self):
        head = (f"POST /login HTTP/1.1\r\nHost: 127.0.0.1:{self.proxy_port}\r\n"
                "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n1\r\nx\r\n").encode()
        response = self.raw(head, delay_chunks=[
            (.13, b"1\r\nx\r\n"), (.13, b"1\r\nx\r\n"), (.13, b"1\r\nx\r\n")])
        self.assertTrue(response.startswith(b"HTTP/1.1 408"), response[:100])


if __name__ == "__main__":
    unittest.main()
