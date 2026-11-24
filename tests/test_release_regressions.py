"""MCP release regressions using temporary encrypted storage and a fake provider."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
import httpx

from cred_broker import control as control_module
from cred_broker.mcp import register_mcp
from cred_broker.store import MasterKey, Store

SECRET = "release-fixture-private-provider-key"
PRIVATE_ERROR = "release-fixture-private-stack-and-path"


class McpOutcomeRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="broker-release-regressions-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.provider_patch = patch.object(control_module, "providers")
        self.provider = self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)
        self.provider.validate_connection.return_value = {}, {"key": SECRET}
        self.provider.resource_list.return_value = [{"id": "fixture", "label": "Fixture"}]
        self.provider.tool_specs.return_value = [
            {"name": "fixture_action", "inputSchema": {"type": "object", "properties": {}}}]
        self.control = control_module.Control(Store(MasterKey(root / "key"), root / "db"))
        connection = self.control.create_connection("fake", "Release fixture", {}, {})
        self.grant, self.token = self.control.create_grant(
            "release-fixture", connection["id"], ["fixture"], ["fixture_action"], 120)
        app = FastAPI()
        register_mcp(app, self.control)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765")
        self.addAsyncCleanup(self.client.aclose)

    async def call(self):
        response = await self.client.post("/mcp", headers={
            "Authorization": "Bearer " + self.token,
            "MCP-Protocol-Version": "2025-11-25",
        }, json={"jsonrpc": "2.0", "id": 41, "method": "tools/call",
                 "params": {"name": "fixture_action", "arguments": {"resource": "fixture"}}})
        self.assertNotIn(SECRET, response.text)
        self.assertNotIn(PRIVATE_ERROR, response.text)
        self.assertNotIn(self.token, response.text)
        return response

    def assert_unknown(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        envelope = response.json()
        self.assertEqual(envelope["jsonrpc"], "2.0")
        self.assertEqual(envelope["id"], 41)
        self.assertNotIn("error", envelope)
        result = envelope["result"]
        self.assertIs(result["isError"], True)
        structured = result["structuredContent"]
        self.assertEqual(structured["outcome"], "outcome_unknown")
        self.assertIs(structured["retryable"], False)
        self.assertIn("Do not retry automatically", structured["message"])
        self.assertIn("may have completed", structured["message"])
        self.assertEqual(result["content"], [{"type": "text", "text": json.dumps(structured)}])

    async def test_provider_success_then_audit_failure_has_explicit_nonretryable_outcome(self):
        self.provider.call.return_value = {"ok": True, "private": SECRET}
        with patch.object(self.control, "_write_audit", side_effect=RuntimeError(PRIVATE_ERROR)):
            response = await self.call()
        self.assert_unknown(response)
        self.provider.call.assert_called_once()

    async def test_provider_failure_then_audit_failure_remains_distinct_and_never_retries(self):
        self.provider.call.side_effect = RuntimeError(PRIVATE_ERROR + SECRET)
        with patch.object(self.control, "_write_audit", side_effect=RuntimeError(PRIVATE_ERROR)):
            response = await self.call()
        self.assert_unknown(response)
        self.provider.call.assert_called_once()

    async def test_postexecution_redaction_failure_is_unknown_and_never_retries(self):
        self.provider.call.return_value = {"private": SECRET}
        with patch.object(self.control, "_redact", side_effect=RuntimeError(PRIVATE_ERROR)):
            response = await self.call()
        self.assert_unknown(response)
        self.provider.call.assert_called_once()

    async def test_ordinary_provider_failure_is_not_misclassified_as_unknown(self):
        self.provider.call.side_effect = RuntimeError(PRIVATE_ERROR + SECRET)
        response = await self.call()
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertIs(result["isError"], True)
        self.assertNotIn("outcome_unknown", response.text)
        self.assertNotIn("structuredContent", result)
        self.provider.call.assert_called_once()

    async def test_success_preserves_normal_result_and_redaction(self):
        self.provider.call.return_value = {"ok": True, "detail": "fixture " + SECRET}
        response = await self.call()
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertFalse(result.get("isError", False))
        self.assertNotIn("outcome_unknown", response.text)
        self.assertEqual(json.loads(result["content"][0]["text"]), {"ok": True, "detail": "fixture ***"})
        self.provider.call.assert_called_once()

    async def test_revoked_grant_is_unauthorized_without_provider_execution(self):
        self.control.revoke(self.grant["id"])
        response = await self.call()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.provider.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
