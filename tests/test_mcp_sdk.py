"""Official MCP SDK smoke test against a temporary real HTTP server.

Run with requirements-dev installed; uses fake credentials and local loopback only.
"""
import asyncio
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import uvicorn
from fastapi import FastAPI
from mcp import ClientSession
import httpx2
from mcp.client.streamable_http import streamable_http_client

from cred_broker import control as control_module
from cred_broker.mcp import register_mcp
from cred_broker.store import MasterKey, Store


class OfficialSdkSmoke(unittest.TestCase):
    def test_initialize_list_call_and_revoke(self):
        with tempfile.TemporaryDirectory() as root, patch.object(control_module, "providers") as provider:
            path = Path(root)
            control = control_module.Control(Store(MasterKey(path / "key"), path / "db"))
            provider.validate_connection.return_value = {}, {"key": "sdk-fixture-secret"}
            provider.resource_list.return_value = [{"id": "fixture", "label": "Fixture"}]
            provider.tool_specs.return_value = [{"name": "fixture_status", "description": "Read fake status",
                                                 "inputSchema": {"type": "object", "properties": {}}}]
            provider.call.return_value = {"status": "ok", "echo": "sdk-fixture-secret"}
            connection = control.create_connection("fake", "SDK fixture", {}, {})
            grant, token = control.create_grant("sdk-test", connection["id"], ["fixture"],
                                                ["fixture_status"], 120)
            app = FastAPI()
            register_mcp(app, control)
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not server.started and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(server.started, "Temporary MCP HTTP server did not start")
                asyncio.run(asyncio.wait_for(self._exchange(control, grant, token, port), timeout=20))
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                listener.close()
                self.assertFalse(thread.is_alive(), "Temporary HTTP server failed to stop")

    async def _exchange(self, control, grant, token, port):
        url = f"http://127.0.0.1:{port}/mcp"
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx2.AsyncClient(headers=headers, timeout=5) as transport_client, streamable_http_client(
                url, http_client=transport_client) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                self.assertEqual(initialized.server_info.name, "cred-broker")
                tools = await session.list_tools()
                self.assertEqual([tool.name for tool in tools.tools], ["fixture_status"])
                self.assertEqual(tools.tools[0].input_schema["properties"]["resource"]["enum"], ["fixture"])
                result = await session.call_tool("fixture_status", {"resource": "fixture"})
                self.assertFalse(result.is_error)
                self.assertIn("ok", result.content[0].text)
                self.assertNotIn("sdk-fixture-secret", str(result))
                denied = await session.call_tool("fixture_status", {"resource": "outside"})
                self.assertTrue(denied.is_error)
                control.revoke(grant["id"])
                # Verify the exact transport status after SDK interaction, rather
                # than depending on SDK-version-specific exception wrappers.
                async with httpx.AsyncClient(timeout=3) as client:
                    response = await client.post(url, headers={**headers, "Accept": "application/json, text/event-stream"},
                                                 json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers.get("www-authenticate"), "Bearer")


if __name__ == "__main__":
    unittest.main()
