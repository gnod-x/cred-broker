"""Integration checks for the real management app; no live upstream accounts."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from cred_broker.app import create_app
from cred_broker.config import Config
from cred_broker.store import MasterKey

ADMIN = "test-admin-not-a-real-secret"


def config(root, public_origin=""):
    root = Path(root)
    return Config(root, root / "db", root / "key", "127.0.0.1:8765", root / "web.token",
                  ADMIN, "", "{}", "cb_session", 10, 65536, None, public_origin=public_origin)


class ManagementTest(unittest.TestCase):
    def test_browser_sessions_and_login_throttling(self):
        with tempfile.TemporaryDirectory() as root:
            asyncio.run(self.session_flow(create_app(config(root))))

    async def session_flow(self, app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            old_cookie = {"Cookie": "cb_session=" + ADMIN}
            self.assertEqual((await client.get("/admin/api/v1/connections", headers=old_cookie)).status_code, 401)
            self.assertEqual((await client.get("/admin/api/v1/connections", headers={
                **old_cookie, "X-Admin-Token": ADMIN})).status_code, 200)
            logged = await client.post("/login", data={"token": ADMIN})
            first = logged.cookies["cb_session"]
            self.assertNotEqual(first, ADMIN)
            self.assertNotIn(ADMIN, logged.headers["set-cookie"])
            logged = await client.post("/login", data={"token": ADMIN})
            second = logged.cookies["cb_session"]
            self.assertNotEqual(first, second)
            self.assertEqual((await client.get("/admin/api/v1/connections", headers={
                "Cookie": "cb_session=" + first})).status_code, 401)
            await client.post("/logout")
            self.assertEqual((await client.get("/admin/api/v1/connections", headers={
                "Cookie": "cb_session=" + second})).status_code, 401)
            for _ in range(6):
                bad = await client.post("/login", data={"token": "synthetic-wrong-token"})
                self.assertEqual(bad.status_code, 401)
            blocked = await client.post("/login", data={"token": ADMIN})
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked.headers["retry-after"], "60")
            self.assertNotIn(ADMIN, blocked.text)

    def test_configured_https_origin_is_exact_and_keeps_secure_cookie(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_app(config(root, "https://broker.example"))
            asyncio.run(self.public_origin_flow(app))

    async def public_origin_flow(self, app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            origin = "https://broker.example"
            logged = await client.post("/login", headers={"Origin": origin}, data={"token": ADMIN})
            self.assertEqual(logged.status_code, 303)
            self.assertIn("Secure", logged.headers["set-cookie"])
            session_cookie = "cb_session=" + logged.cookies["cb_session"]
            self.assertNotIn(ADMIN, logged.headers["set-cookie"])
            authenticated = await client.get("/admin/api/v1/connections", headers={
                "Origin": origin, "Cookie": session_cookie,
            })
            self.assertEqual(authenticated.status_code, 200)
            managed = await client.post("/admin/api/v1/connections", headers={
                "Origin": origin, "Cookie": session_cookie,
            }, json={})
            self.assertEqual(managed.status_code, 400)
            cross = await client.post("/admin/api/v1/connections", headers={
                "Origin": "https://attacker.example", "Cookie": session_cookie,
            }, json={})
            self.assertEqual(cross.status_code, 403)
            malformed = await client.post("/logout", headers={"Origin": "http://[invalid"})
            self.assertEqual(malformed.status_code, 403)
            empty = await client.post("/logout", headers={"Origin": ""})
            self.assertEqual(empty.status_code, 403)
            logout = await client.post("/logout", headers={"Origin": origin})
            self.assertEqual(logout.status_code, 303)
            self.assertIn("Secure", logout.headers["set-cookie"])

    def test_public_origin_rejects_path_and_credentials(self):
        for origin in (
            "https://broker.example/admin", "https://user@broker.example", "https://broker.example?",
            "https://broker.example#", "https://broker.example:", "https://broker.example:0",
        ):
            with tempfile.TemporaryDirectory() as root:
                with self.assertRaisesRegex(RuntimeError, "CB_PUBLIC_ORIGIN"):
                    create_app(config(root, origin))

    def test_public_origin_normalizes_scheme_host_and_default_port(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_app(config(root, "HTTPS://Broker.Example:443"))
            asyncio.run(self.public_origin_flow(app))

    def test_full_flow_and_input_boundaries(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "CB_GOOGLE_CLIENT_ID": "", "CB_GOOGLE_CLIENT_SECRET": "", "CB_GOOGLE_REDIRECT_URI": "",
        }):
            app = create_app(config(root))
            asyncio.run(asyncio.wait_for(self.flow(app, Path(root)), 20))

    async def flow(self, app, root):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            self.assertEqual((await client.get("/admin/api/v1/connections")).status_code, 401)
            self.assertEqual((await client.get("/admin/api/v1/ready")).status_code, 401)
            self.assertEqual((await client.get("/admin/api/v1/audit/export.csv")).status_code, 401)
            self.assertEqual((await client.get("/admin/api/v1/providers/ssh/schema")).status_code, 401)
            health = await client.get("/healthz")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(set(health.json()), {"ok", "ts"})
            page = await client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(ADMIN, page.text)
            self.assertIn("script-src 'self'", page.headers["content-security-policy"])
            bad = await client.post("/login", data={"token": "错误令牌"})
            self.assertEqual(bad.status_code, 401)
            logged = await client.post("/login", data={"token": ADMIN})
            self.assertEqual(logged.status_code, 303)
            cookie = logged.headers["set-cookie"]
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=strict", cookie)
            ready = await client.get("/admin/api/v1/ready")
            self.assertEqual(ready.status_code, 200)
            self.assertEqual(ready.json()["version"], "0.3.0")
            self.assertEqual(ready.json()["storage"], "ready")
            self.assertEqual(ready.json()["key"], "ready")
            self.assertEqual(ready.json()["migrations"], "current")
            self.assertIn("instabox", ready.json()["providers"])
            self.assertNotIn(str(root), ready.text)
            schema = await client.get("/admin/api/v1/providers/instabox/schema")
            self.assertEqual(schema.status_code, 200)
            self.assertEqual(schema.json()["provider"], "instabox")
            self.assertEqual(schema.json()["schema"]["auth_mode"], "api_key")
            self.assertTrue(schema.json()["schema"]["secret"]["properties"]["read_key"]["writeOnly"])
            self.assertNotIn("ibk_only_a_fixture", schema.text)
            self.assertEqual((await client.get("/admin/api/v1/providers/feishu/schema")).status_code, 404)
            exported = await client.get("/admin/api/v1/audit/export.csv")
            self.assertEqual(exported.status_code, 200)
            self.assertEqual(exported.headers["content-type"], "text/csv; charset=utf-8")
            self.assertIn('attachment; filename="cred-broker-audit.csv"', exported.headers["content-disposition"])
            self.assertIn("Cache-Control", exported.headers)
            self.assertEqual(exported.text.splitlines()[0], "ts,client,action,connection_id,resource,outcome")
            self.assertNotIn("token", exported.text.lower())
            self.assertEqual((await client.get("/admin/api/v1/audit/export.csv?after=2&before=1")).status_code, 400)
            self.assertEqual((await client.get("/admin")).status_code, 200)
            self.assertEqual((await client.get("/static/console.js")).status_code, 200)
            self.assertEqual((await client.get("/admin/api/overview")).status_code, 404)
            self.assertEqual((await client.get("/mcp/tools")).status_code, 404)
            cross = await client.post("/admin/api/v1/connections", headers={"Origin": "http://outside.invalid"}, json={})
            self.assertEqual(cross.status_code, 403)
            invalid = await client.post("/admin/api/v1/connections", json={"secret": {"key": "do-not-echo"}})
            self.assertEqual(invalid.status_code, 400)
            self.assertNotIn("do-not-echo", invalid.text)
            sent = {"provider": "instabox", "label": "<script>alert(1)</script>",
                    "settings": {"machines": ["fixture-machine"]}, "secret": {"read_key": "ibk_only_a_fixture"}}
            created = await client.post("/admin/api/v1/connections", json=sent)
            self.assertEqual(created.status_code, 201, created.text)
            self.assertNotIn("ibk_only_a_fixture", created.text)
            connection = created.json()["connection"]
            self.assertEqual(connection["health"], {"checked_at": None, "ok": None})
            with patch("cred_broker.providers.test_connection", return_value={
                    "ok": True, "read_key": "ibk_only_a_fixture"}):
                tested = await client.post("/admin/api/v1/connections/" + connection["id"] + "/test")
            self.assertEqual(tested.status_code, 200)
            self.assertNotIn("ibk_only_a_fixture", tested.text)
            listed_connection = (await client.get("/admin/api/v1/connections")).json()["connections"][0]
            self.assertEqual(listed_connection["health"]["ok"], True)
            self.assertIsInstance(listed_connection["health"]["checked_at"], int)
            result = await client.post("/admin/api/v1/grants", json={
                "client": "integration", "connection_id": connection["id"], "resources": ["fixture-machine"],
                "tools": ["instabox_describe"], "ttl_seconds": 120,
            })
            self.assertEqual(result.status_code, 201, result.text)
            grant, token = result.json()["grant"], result.json()["token"]
            self.assertGreater(grant["expires_at"], grant["created_at"])
            for endpoint in ("/connections", "/grants", "/audit"):
                listing = await client.get("/admin/api/v1" + endpoint)
                self.assertNotIn(token, listing.text)
                self.assertNotIn("ibk_only_a_fixture", listing.text)
            headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
            rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            tools = await client.post("/mcp", json=rpc, headers=headers)
            self.assertEqual(tools.status_code, 200, tools.text)
            self.assertEqual([row["name"] for row in tools.json()["result"]["tools"]], ["instabox_describe"])
            self.assertNotIn("ibk_only_a_fixture", tools.text)
            bad_reissue = await client.post("/admin/api/v1/grants/" + grant["id"] + "/reissue",
                                            json={"ttl_seconds": 86401})
            self.assertEqual(bad_reissue.status_code, 400)
            malformed_reissue = await client.post(
                "/admin/api/v1/grants/" + grant["id"] + "/reissue",
                content=b"[]", headers={"Content-Type": "application/json"})
            self.assertEqual(malformed_reissue.status_code, 400)
            self.assertEqual((await client.post("/mcp", json=rpc, headers=headers)).status_code, 200)
            reissued = await client.post("/admin/api/v1/grants/" + grant["id"] + "/reissue",
                                         json={"ttl_seconds": 120})
            self.assertEqual(reissued.status_code, 201, reissued.text)
            next_grant, next_token = reissued.json()["grant"], reissued.json()["token"]
            self.assertNotEqual(token, next_token)
            self.assertEqual(next_grant["client"], grant["client"])
            self.assertEqual(next_grant["connection_id"], grant["connection_id"])
            self.assertEqual(next_grant["resources"], grant["resources"])
            self.assertEqual(next_grant["tools"], grant["tools"])
            self.assertEqual(next_grant["expires_at"] - next_grant["created_at"], 120)
            for endpoint in ("/connections", "/grants", "/audit"):
                listing = await client.get("/admin/api/v1" + endpoint)
                self.assertNotIn(next_token, listing.text)
            self.assertEqual((await client.post("/mcp", json=rpc, headers=headers)).status_code, 401)
            next_headers = {"Authorization": "Bearer " + next_token,
                            "Accept": "application/json, text/event-stream"}
            self.assertEqual((await client.post("/mcp", json=rpc, headers=next_headers)).status_code, 200)
            await client.post("/admin/api/v1/grants/" + next_grant["id"] + "/revoke")
            self.assertEqual((await client.post("/mcp", json=rpc, headers=next_headers)).status_code, 401)
            self.assertEqual((await client.post("/mcp", json=rpc, headers=headers)).status_code, 401)
            await client.delete("/admin/api/v1/connections/" + connection["id"])
            self.assertEqual((await client.get("/admin/api/v1/connections")).json()["connections"], [])
            await client.post("/logout")
            self.assertEqual((await client.get("/admin/api/v1/connections")).status_code, 401)
            for path in root.iterdir():
                if path.is_file() and path.name != "key":
                    self.assertNotIn(b"ibk_only_a_fixture", path.read_bytes())
                    self.assertNotIn(token.encode(), path.read_bytes())

    def test_binary_master_key_reload(self):
        with tempfile.TemporaryDirectory() as root:
            key = Path(root) / "key"
            key.write_bytes(b"\n" + b"k" * 30 + b"\r")
            loaded = MasterKey(key)
            nonce = b"x" * 12
            ciphertext = loaded.aead().encrypt(nonce, b"fixture", b"fixture")
            self.assertEqual(MasterKey(key).aead().decrypt(nonce, ciphertext, b"fixture"), b"fixture")


if __name__ == "__main__":
    unittest.main()
