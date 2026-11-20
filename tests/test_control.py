"""Exercise the scoped MCP transport with a local fake provider."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI

from cred_broker import control as control_module
from cred_broker.mcp import register_mcp
from cred_broker.store import MasterKey, Store


def post(app, headers, payload):
    async def request():
        events = []
        body = json.dumps(payload).encode()
        received = False
        async def receive():
            nonlocal received
            if received:
                await asyncio.Future()
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        async def send(event):
            events.append(event)
        pairs = [(name.lower().encode(), value.encode())
                 for name, value in {"Content-Type": "application/json", "Host": "127.0.0.1", **headers}.items()]
        task = asyncio.create_task(app({"type": "http", "method": "POST", "path": "/mcp", "headers": pairs,
                   "query_string": b"", "scheme": "http"}, receive, send))
        # Keep bounded event-loop progress in restricted runners where cross-thread
        # self-pipe wakeups can be delayed. Never let an ASGI fixture wait forever.
        deadline = asyncio.get_running_loop().time() + 5
        while not task.done() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.01)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise AssertionError("ASGI request exceeded five seconds")
        await task
        response_body = b"".join(event.get("body", b"") for event in events)
        return events[0]["status"], json.loads(response_body) if response_body else None
    return asyncio.run(request())


class ScopedMcpTest(unittest.TestCase):
    def test_protocol_scope_revoke_and_disconnect(self):
        with tempfile.TemporaryDirectory() as root, patch.object(control_module, "providers") as provider:
            path = Path(root)
            store = Store(MasterKey(path / "key"), path / "db")
            control = control_module.Control(store)
            provider.validate_connection.return_value = {}, {"key": "synthetic-fake-secret"}
            provider.resource_list.return_value = [{"id": "one", "label": "One"}, {"id": "two", "label": "Two"}]
            provider.tool_specs.return_value = [{"name": "diagnostic", "inputSchema": {"type": "object"}}]
            provider.call.return_value = {"ok": True}
            connection = control.create_connection("fake", "Fixture", {}, {})
            grant, token = control.create_grant("test", connection["id"], ["one"], ["diagnostic"], 90)
            provider.test_connection.return_value = {"ok": True}
            control.test_connection(connection["id"])
            provider.test_connection.side_effect = RuntimeError("synthetic-fake-secret")
            with self.assertRaisesRegex(RuntimeError, "^Connection test failed$"):
                control.test_connection(connection["id"])
            provider.test_connection.side_effect = None
            app = FastAPI()
            register_mcp(app, control)

            def rpc(method, params=None, **options):
                headers = {"Authorization": f"Bearer {token}", **options.get("headers", {})}
                return post(app, headers,{"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

            self.assertEqual(rpc("initialize", {"protocolVersion": "2025-03-26"})[1]["result"][
                "protocolVersion"], "2025-03-26")
            self.assertEqual(rpc("tools/list")[1]["result"]["tools"][0]["inputSchema"][
                "properties"]["resource"]["enum"], ["one"])
            self.assertEqual(rpc("ping", headers={"Origin": "http://other.invalid"})[0], 403)

            def check(resource):
                return rpc("tools/call", {"name": "diagnostic", "arguments": {"resource": resource}})

            self.assertTrue(check("two")[1]["result"]["isError"])
            provider.call.assert_not_called()
            self.assertIn("ok", check("one")[1]["result"]["content"][0]["text"])
            # The provider returning a credential must not leak it to the client.
            provider.call.return_value = {"echo": "synthetic-fake-secret", "access_token": "opaque"}
            redacted = check("one")[1]["result"]
            self.assertNotIn("synthetic-fake-secret", str(redacted))
            self.assertNotIn("opaque", str(redacted))
            # A separate connection cannot expand the token's resource scope.
            other = control.create_connection("fake", "Other", {}, {})
            self.assertNotEqual(other["id"], connection["id"])
            before = provider.call.call_count
            self.assertTrue(check(other["id"])[1]["result"]["isError"])
            self.assertEqual(before, provider.call.call_count)
            # Malformed JSON-RPC values are errors rather than server exceptions.
            self.assertEqual(post(app, {"Authorization": f"Bearer {token}"}, [1, 2])[0], 400)
            self.assertEqual(rpc("tools/call", {"name": "diagnostic", "arguments": []})[1]["error"]["code"], -32602)
            provider.call.side_effect = RuntimeError("synthetic-fake-secret")
            self.assertNotIn("synthetic-fake-secret", str(check("one")[1]))
            provider.call.side_effect = None
            control.revoke(grant["id"])
            self.assertEqual(check("one")[0], 401)
            self.assertNotIn("synthetic-fake-secret", str(control.audit()))
            _, token = control.create_grant("test", connection["id"], ["one"], ["diagnostic"], 90)
            control.disconnect(connection["id"])
            self.assertEqual(check("one")[0], 401)
            admin_events = [row for row in control.audit() if row["client"] == "admin"]
            self.assertTrue({"connection.create", "connection.test", "connection.disconnect",
                             "grant.create", "grant.revoke"} <= {row["action"] for row in admin_events})
            self.assertEqual({row["outcome"] for row in admin_events if row["action"] == "connection.test"},
                             {"ok", "failed"})
            self.assertTrue(all(row["resource"] is None for row in admin_events))
            self.assertNotIn(token, str(control.audit()))
            self.assertNotIn("synthetic-fake-secret", str(control.audit()))
            self.assertTrue(all(set(row) == {"ts", "client", "action", "connection_id", "resource", "outcome"}
                                for row in admin_events))

if __name__ == "__main__":
    unittest.main()
