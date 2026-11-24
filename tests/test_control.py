"""Exercise the scoped MCP transport with a local fake provider."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI

from cred_broker import control as control_module
from cred_broker.control_api import _csv_cell
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
    def test_ready_detects_incomplete_scoped_schema(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            control = control_module.Control(Store(MasterKey(path / "key"), path / "db"))
            self.assertEqual(control.ready()["migrations"], "current")
            with control.db() as db:
                db.execute("DROP TABLE scoped_grants")
            status = control.ready()
            self.assertEqual(status["storage"], "ready")
            self.assertEqual(status["migrations"], "incomplete")

    def test_audit_export_filters_and_limits_without_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            control = control_module.Control(Store(MasterKey(path / "key"), path / "db"))
            with patch("cred_broker.control.time.time", side_effect=[100, 200, 300, 400, 400, 400]):
                control._audit("=client", "+action", "connection", "@resource", "-failed")
                control._audit("normal", "read", "connection", None, "ok")
                control._audit("late", "read", "connection", "item", "ok")
            rows = control.audit_export(150, 250)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["client"], "normal")
            self.assertEqual(set(rows[0]), {"ts", "client", "action", "connection_id", "resource", "outcome"})
            with self.assertRaises(ValueError):
                control.audit_export(0, 31 * 86400 + 1)
            with self.assertRaises(ValueError):
                control.audit_export(10, 1)
            with self.assertRaises(ValueError):
                control.audit_export(0)
            self.assertEqual(_csv_cell("=cmd"), "'=cmd")
            self.assertEqual(_csv_cell("  @value"), "'  @value")
            self.assertEqual(_csv_cell("ordinary"), "ordinary")

    def test_reissue_is_atomic_and_serialized(self):
        with tempfile.TemporaryDirectory() as root, patch.object(control_module, "providers") as provider:
            path = Path(root)
            control = control_module.Control(Store(MasterKey(path / "key"), path / "db"))
            provider.validate_connection.return_value = {}, {"key": "fixture-secret"}
            provider.resource_list.return_value = [{"id": "one", "label": "One"}]
            provider.tool_specs.return_value = [{"name": "diagnostic", "inputSchema": {"type": "object"}}]
            connection = control.create_connection("fake", "Fixture", {}, {})
            grant, token = control.create_grant("test", connection["id"], ["one"], ["diagnostic"], 60)

            provider.resource_list.return_value = []
            with self.assertRaises(ValueError):
                control.reissue_grant(grant["id"], 60)
            self.assertIsNotNone(control.resolve(token), "failed reissue must preserve the old token")

            provider.resource_list.return_value = [{"id": "one", "label": "One"}]
            with patch.object(control, "_write_audit", side_effect=RuntimeError("fixture audit failure")):
                with self.assertRaisesRegex(RuntimeError, "fixture audit failure"):
                    control.reissue_grant(grant["id"], 60)
            self.assertIsNotNone(control.resolve(token), "audit failure must roll back token rotation")

            provider.resource_list.return_value = [{"id": "one", "label": "One"}]
            def reissue():
                try:
                    return control.reissue_grant(grant["id"], 60)
                except PermissionError:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _index: reissue(), range(2)))
            winners = [item for item in outcomes if item is not None]
            self.assertEqual(len(winners), 1)
            new_grant, new_token = winners[0]
            self.assertIsNone(control.resolve(token))
            self.assertEqual(control.resolve(new_token)["id"], new_grant["id"])
            self.assertEqual(new_grant["resources"], grant["resources"])
            self.assertEqual(new_grant["tools"], grant["tools"])
            control.revoke(new_grant["id"])
            with self.assertRaises(PermissionError):
                control.reissue_grant(new_grant["id"], 60)

            expiring, expiring_token = control.create_grant(
                "test", connection["id"], ["one"], ["diagnostic"], 1)
            with patch("cred_broker.control.time.time", return_value=expiring["expires_at"] + 1):
                self.assertIsNone(control.resolve(expiring_token))
                expired_reissue, expired_token = control.reissue_grant(expiring["id"], 60)
            self.assertIsNone(control.resolve(expiring_token))
            self.assertEqual(control.resolve(expired_token)["id"], expired_reissue["id"])

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
            self.assertEqual(connection["health"], {"checked_at": None, "ok": None})
            grant, token = control.create_grant("test", connection["id"], ["one"], ["diagnostic"], 90)
            provider.test_connection.return_value = {"ok": True}
            control.test_connection(connection["id"])
            healthy = control.connection(connection["id"])["health"]
            self.assertTrue(healthy["ok"])
            self.assertIsInstance(healthy["checked_at"], int)
            provider.test_connection.side_effect = RuntimeError("synthetic-fake-secret")
            with self.assertRaisesRegex(RuntimeError, "^Connection test failed$"):
                control.test_connection(connection["id"])
            provider.test_connection.side_effect = None
            failed = control.connection(connection["id"])["health"]
            self.assertFalse(failed["ok"])
            renewed, renewed_token = control.reissue_grant(grant["id"], 86400)
            self.assertNotEqual(renewed["id"], grant["id"])
            self.assertEqual(renewed["client"], grant["client"])
            self.assertEqual(renewed["resources"], grant["resources"])
            self.assertEqual(renewed["tools"], grant["tools"])
            self.assertNotIn(renewed_token, str(control.grants()))
            self.assertEqual(control.resolve(renewed_token)["id"], renewed["id"])
            self.assertIsNone(control.resolve(token))
            self.assertEqual(renewed["expires_at"] - renewed["created_at"], 86400)
            with self.assertRaises(ValueError):
                control.reissue_grant(renewed["id"], 86401)
            grant, token = renewed, renewed_token
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


class T03TransactionTests(unittest.TestCase):
    """Only synthetic providers and disposable SQLite databases are used."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.control = control_module.Control(Store(MasterKey(root / "key"), root / "db"))
        self.connection = self.control.create_connection(
            "instabox", "Fixture", {"machines": ["fixture-machine"]}, {"read_key": "ibk_fixture"})
        self.grant, self.token = self.control.create_grant(
            "fixture-ai", self.connection["id"], ["fixture-machine"], ["instabox_describe"], 300)

    def tearDown(self):
        self.directory.cleanup()

    def snapshot(self):
        with self.control.db() as db:
            return {
                table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
                for table in ("scoped_connections", "scoped_grants", "scoped_audit")
            }

    def deny(self, predicate):
        original = self.control.store._conn
        def connect():
            db = original()
            db.set_authorizer(lambda action, first, second, database, trigger:
                              sqlite3.SQLITE_DENY if predicate(action, first) else sqlite3.SQLITE_OK)
            return db
        return patch.object(self.control.store, "_conn", side_effect=connect)

    def test_local_mutations_and_audit_rollback_together_at_every_boundary(self):
        operations = {
            "create_connection": (
                lambda: self.control.create_connection("instabox", "Second", {"machines": ["second"]},
                                                       {"read_key": "ibk_second"}),
                sqlite3.SQLITE_INSERT, "scoped_connections"),
            "create_grant": (
                lambda: self.control.create_grant("second-ai", self.connection["id"],
                                                  ["fixture-machine"], ["instabox_describe"], 120),
                sqlite3.SQLITE_INSERT, "scoped_grants"),
            "revoke": (lambda: self.control.revoke(self.grant["id"]),
                       sqlite3.SQLITE_UPDATE, "scoped_grants"),
            "disconnect": (lambda: self.control.disconnect(self.connection["id"]),
                           sqlite3.SQLITE_DELETE, "scoped_connections"),
            "reissue": (lambda: self.control.reissue_grant(self.grant["id"], 120),
                        sqlite3.SQLITE_INSERT, "scoped_grants"),
        }
        for name, (operation, action, table) in operations.items():
            for failure in ("business", "audit_before", "audit_after", "audit_prune", "commit"):
                with self.subTest(operation=name, failure=failure):
                    before = self.snapshot()
                    if failure == "business":
                        fault = self.deny(lambda a, t: a == action and t == table)
                    elif failure == "commit":
                        fault = self.deny(lambda a, t: a == sqlite3.SQLITE_TRANSACTION and t == "COMMIT")
                    elif failure == "audit_prune":
                        fault = self.deny(lambda a, t: a == sqlite3.SQLITE_DELETE and t == "scoped_audit")
                    elif failure == "audit_before":
                        fault = patch.object(self.control, "_write_audit",
                                             side_effect=RuntimeError("synthetic audit failure"))
                    else:
                        original = self.control._write_audit
                        def write_then_fail(*args):
                            original(*args)
                            raise RuntimeError("synthetic failure after audit insert")
                        fault = patch.object(self.control, "_write_audit", side_effect=write_then_fail)
                    with fault, self.assertRaises(Exception):
                        operation()
                    self.assertEqual(self.snapshot(), before, "Failure must roll back business and audit")
                    self.assertIsNotNone(self.control.resolve(self.token))

    def test_connection_projection_failure_does_not_commit_a_hidden_connection(self):
        before = self.snapshot()
        with patch.object(self.control, "_public", side_effect=RuntimeError("synthetic projection failure")):
            with self.assertRaises(RuntimeError):
                self.control.create_connection("instabox", "Second", {"machines": ["second"]},
                                               {"read_key": "ibk_second"})
        self.assertEqual(self.snapshot(), before)

    def test_successful_provider_with_unrecorded_outcome_is_never_retried(self):
        for failure in ("audit", "commit", "projection"):
            with self.subTest(failure=failure):
                before = self.snapshot()
                if failure == "audit":
                    fault = patch.object(self.control, "_write_audit",
                                         side_effect=RuntimeError("synthetic-private-error"))
                elif failure == "projection":
                    fault = patch.object(self.control, "_redact",
                                         side_effect=RuntimeError("synthetic-private-error"))
                else:
                    fault = self.deny(lambda a, t: a == sqlite3.SQLITE_TRANSACTION and t == "COMMIT")
                with patch.object(control_module.providers, "call", return_value={"safe": True}) as execute:
                    with fault, self.assertRaises(control_module.OutcomeUnknown) as caught:
                        self.control.call(self.token, "instabox_describe", "fixture-machine", {})
                    execute.assert_called_once()
                self.assertEqual(caught.exception.outcome, "outcome_unknown")
                self.assertFalse(caught.exception.retryable)
                self.assertIn("Do not retry automatically", str(caught.exception))
                self.assertNotIn("synthetic-private-error", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(self.snapshot(), before)

    def test_provider_failure_and_audit_failure_are_reported_as_unknown(self):
        before = self.snapshot()
        with patch.object(control_module.providers, "call", side_effect=RuntimeError("synthetic-private-error")) as execute:
            with patch.object(self.control, "_write_audit", side_effect=RuntimeError("audit-private-error")):
                with self.assertRaises(control_module.OutcomeUnknown) as caught:
                    self.control.call(self.token, "instabox_describe", "fixture-machine", {})
            execute.assert_called_once()
        self.assertNotIn("private-error", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_connection_health_and_audit_rollback_after_external_test(self):
        for successful in (True, False):
            for failure in ("audit", "commit"):
                with self.subTest(successful=successful, failure=failure):
                    before = self.snapshot()
                    options = {"return_value": {"ok": True}} if successful else {
                        "side_effect": RuntimeError("synthetic-private-provider-error")}
                    fault = (patch.object(self.control, "_write_audit", side_effect=RuntimeError("audit failure"))
                             if failure == "audit" else
                             self.deny(lambda a, t: a == sqlite3.SQLITE_TRANSACTION and t == "COMMIT"))
                    with patch.object(control_module.providers, "test_connection", **options) as execute:
                        with fault, self.assertRaises(control_module.OutcomeUnknown):
                            self.control.test_connection(self.connection["id"])
                        execute.assert_called_once()
                    self.assertEqual(self.snapshot(), before)

    def test_long_provider_resources_round_trip_without_expanding_membership(self):
        settings = {"host": "a" * 256, "port": 65535, "user": "reader",
                    "host_key": "ssh-ed25519 ZmFrZQ==", "services": ["fixture.service"]}
        connection = self.control.create_connection("ssh", "Long host", settings, {"private_key": "fixture"})
        resource = connection["resources"][0]["id"]
        self.assertEqual(len(resource), 262)
        grant, token = self.control.create_grant("long-fixture", connection["id"], [resource], ["ssh_status"], 120)
        self.assertEqual(self.control.resolve(token)["resources"], [resource])
        self.assertEqual(self.control.discover(grant)[0]["inputSchema"]["properties"]["resource"]["enum"], [resource])
        with patch.object(control_module.providers, "_process", return_value=(0, b"LoadState=loaded")) as process:
            self.assertEqual(self.control.call(token, "ssh_status", resource, {"unit": "fixture.service"})["exit_status"], 0)
            process.assert_called_once()
        with self.assertRaises(ValueError):
            self.control.create_grant("invalid-fixture", connection["id"], [resource + "x"], ["ssh_status"], 120)
        with self.assertRaises(ValueError):
            self.control.create_grant("invalid-fixture", connection["id"], [resource], ["ssh_logs-extra"], 120)

if __name__ == "__main__":
    unittest.main()
