import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cred_broker import providers


class ProviderTests(unittest.TestCase):
    def test_gmail_schema_and_argument_bounds_match_execution(self):
        settings = {"email": "reader@example.test"}
        secret = {"client_id": "fake-client", "client_secret": "fake-secret", "refresh_token": "fake-refresh"}
        schema = providers.tool_specs("gmail", settings)[0]["inputSchema"]["properties"]
        self.assertEqual(schema["limit"]["maximum"], 25)
        self.assertEqual(schema["query"]["maxLength"], 256)
        with patch("cred_broker.provider_gmail.call") as execute:
            for args in ({"query": "x", "limit": 26}, {"query": "x" * 257}):
                with self.assertRaises(ValueError):
                    providers.call("gmail", "gmail_search", settings["email"], args, settings, secret)
            execute.assert_not_called()

    def test_instabox_uses_only_read_key_and_fixed_query(self):
        settings = {"machines": ["sample"]}
        secret = {"read_key": "ibk_fake123", "diagnostic_key": "ibk_fake456"}
        response = b'{"machine":{"id":"abc","name":"sample","state":"running","metadata":{"key":"ibk_fake123"}}}'
        with patch("cred_broker.providers._process", return_value=(0, response)) as process:
            result = providers.call("instabox", "instabox_describe", "sample", {}, settings, secret)
            argv, env = process.call_args.args
            self.assertEqual(argv[1:], ["--server", "https://api.instabox.sh", "get", "sample", "--json"])
            self.assertEqual(env["INSTABOX_API_KEY"], secret["read_key"])
            self.assertFalse(Path(env["HOME"]).exists())
            self.assertEqual(result, {"id": "abc", "name": "sample", "state": "running"})
            with self.assertRaises(PermissionError):
                providers.call("instabox", "describe", "other", {}, settings, secret)
            with self.assertRaises(PermissionError):
                providers.call("instabox", "exec", "sample", {}, settings, secret)
            process.assert_called_once()


    def test_instabox_diagnostics_has_separate_key_and_fixed_commands(self):
        settings = {"machines": ["sample"]}
        secret = {"read_key": "ibk_fake123", "diagnostic_key": "ibk_fake456"}
        self.assertNotIn("instabox_diagnostics", [s["name"] for s in providers.tool_specs("instabox", settings)])
        self.assertIn("instabox_diagnostics", [s["name"] for s in providers.tool_specs("instabox", settings, secret)])
        response = b'{"stdout":"ibk_fake456","stderr":"","exitCode":0,"truncated":false}'
        with patch("cred_broker.providers._process", return_value=(0, response)) as process:
            for action, argv_end in [("uptime", ["/usr/bin/uptime"]), ("disk", ["/usr/bin/df", "-h", "/data"]), ("memory", ["/usr/bin/free", "-m"])]:
                result = providers.call("instabox", "instabox_diagnostics", "sample", {"action": action}, settings, secret)
                argv, env = process.call_args.args
                self.assertEqual(argv[1:], ["--server", "https://api.instabox.sh", "exec", "sample", "--json", "--timeout", "8", "--", *argv_end])
                self.assertEqual(env["INSTABOX_API_KEY"], secret["diagnostic_key"])
                self.assertEqual(result["stdout"], "***")
            for arguments in ({"action": "uptime; id"}, {"action": "disk", "argv": ["id"]}):
                with self.assertRaises(ValueError):
                    providers.call("instabox", "diagnostics", "sample", arguments, settings, secret)
            with self.assertRaises(PermissionError):
                providers.call("instabox", "diagnostics", "other", {"action": "disk"}, settings, secret)
            with self.assertRaises(PermissionError):
                providers.call("instabox", "diagnostics", "sample", {"action": "disk"}, settings, {"read_key": secret["read_key"]})
            self.assertEqual(process.call_count, 3)

    def test_redaction_replaces_only_exact_credentials(self):
        secret = {"private_key": "BEGIN\nprivate material\nEND"}
        result = providers._redact({"token": "ordinary text", "body": "BEGIN a story END", "privateKey": "unrelated", "actual": secret["private_key"]}, secret)
        self.assertEqual(result, {"token": "ordinary text", "body": "BEGIN a story END", "privateKey": "unrelated", "actual": "***"})

    def test_ssh_checks_exact_unit_and_sanitizes_response(self):
        settings = {"host": "ssh.example.test", "port": 2222, "user": "diagnostics",
                    "host_key": "ssh-ed25519 ZmFrZQ==", "services": ["sample.service"]}
        secret = {"private_key": "fake-key-for-test"}
        resource = "ssh.example.test:2222"
        with patch("cred_broker.providers._process", return_value=(0, b"fake-key-for-test")) as process:
            result = providers.call("ssh", "ssh_logs", resource, {"unit": "sample.service", "lines": 4}, settings, secret)
            argv, _ = process.call_args.args
            self.assertIn("-oStrictHostKeyChecking=yes", argv)
            self.assertIn("-oIdentitiesOnly=yes", argv)
            self.assertIn("--lines=4 --unit=sample.service", argv[-1])
            self.assertEqual(result["stdout"], "***")
            for unit in ("other.service", "sample.service; id"):
                with self.assertRaises((ValueError, PermissionError)):
                    providers.call("ssh", "logs", resource, {"unit": unit}, settings, secret)
            with self.assertRaises(ValueError):
                providers.call("ssh", "logs", resource, {"unit": "sample.service", "lines": True}, settings, secret)
            process.assert_called_once()

    def test_github_app_signing_repository_permissions_and_projection(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        secret = {"private_key": private_key.private_bytes(serialization.Encoding.PEM,
                  serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()}
        settings = {"app_id": 11, "installation_id": 22, "repositories": ["Example/Repo"]}
        responses = [{"id": 22}, {"token": "fake-installation-token"}, [{"name": "file", "path": "file",
                      "type": "file", "size": 10, "download_url": "https://example.test/private"}]]
        with patch("cred_broker.providers._json", side_effect=responses) as request:
            result = providers.call("github", "github_contents", "example/repo", {}, settings, secret)
            installation, token, contents = request.call_args_list
            self.assertTrue(installation.args[0].endswith("/repos/example/repo/installation"))
            jwt = installation.args[1]["Authorization"].split()[1]
            header, payload, signature = jwt.split(".")
            private_key.public_key().verify(base64.urlsafe_b64decode(signature + "=="), f"{header}.{payload}".encode(),
                                            padding.PKCS1v15(), hashes.SHA256())
            self.assertEqual(json.loads(token.args[2]), {"repositories": ["repo"], "permissions": {"contents": "read"}})
            self.assertEqual(contents.args[1]["Authorization"], "Bearer fake-installation-token")
            self.assertEqual(result, {"entries": [{"name": "file", "path": "file", "type": "file", "size": 10}]})
        with patch("cred_broker.providers._json", return_value={"id": 44}) as request:
            with self.assertRaises(RuntimeError):
                providers.call("github", "issues", "example/repo", {}, settings, secret)
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
