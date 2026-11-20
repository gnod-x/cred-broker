"""Real OpenSSH transport against an isolated loopback Paramiko fixture.

Run only in a network-enabled test environment; no real host/account is used.
"""
from __future__ import annotations

import io
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import paramiko

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cred_broker import providers

STATUS = "/usr/bin/systemctl show --no-pager --property=LoadState,ActiveState,SubState,Result -- fixture.service"
LOGS = "/usr/bin/journalctl --no-pager --output=short-iso --lines=3 --unit=fixture.service"


class FixtureAuth(paramiko.ServerInterface):
    def __init__(self, client_key, commands):
        self.client_key = client_key
        self.commands = commands
        self.command = None
        self.received = threading.Event()

    def get_allowed_auths(self, username):
        return "publickey"

    def check_auth_publickey(self, username, key):
        if username == "fixture_diagnostics" and key == self.client_key:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.command = command.decode("utf-8", "strict")
        self.commands.append(self.command)
        self.received.set()
        return self.command in {STATUS, LOGS}


class LoopbackSSH:
    def __init__(self):
        self.server_key = paramiko.RSAKey.generate(2048)
        self.client_key = paramiko.RSAKey.generate(2048)
        self.commands = []
        self.stop = threading.Event()
        self.connections = 0
        self.errors = []
        self.active = None
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, name="fixture-ssh", daemon=True)

    def serve(self):
        deadline = time.monotonic() + 30
        while not self.stop.is_set() and time.monotonic() < deadline:
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections += 1
            connection.settimeout(5)
            transport = paramiko.Transport(connection)
            transport.banner_timeout = 5
            transport.auth_timeout = 5
            self.active = transport
            try:
                transport.add_server_key(self.server_key)
                auth = FixtureAuth(self.client_key, self.commands)
                transport.start_server(server=auth)
                channel = transport.accept(5)
                if channel is None:
                    continue  # Host-key rejection ends before a session is opened.
                try:
                    if not auth.received.wait(5):
                        raise RuntimeError("Fixture did not receive exec request")
                    if auth.command == STATUS:
                        body = b"LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n"
                    elif auth.command == LOGS:
                        body = b"2026-01-01T00:00:00 fixture log one\nfixture log two\nfixture log three\n"
                    else:
                        raise RuntimeError("Unexpected fixture command")
                    channel.sendall(body)
                    channel.send_exit_status(0)
                    channel.shutdown_write()
                    # Wait briefly for OpenSSH to consume exit status, bounded.
                    until = time.monotonic() + 2
                    while not channel.closed and time.monotonic() < until and not self.stop.wait(0.02):
                        pass
                finally:
                    channel.close()
            except (EOFError, paramiko.SSHException, OSError):
                # A deliberately wrong pinned key may close during negotiation.
                pass
            except Exception as exc:
                self.errors.append(exc)
            finally:
                transport.close()
                connection.close()
                self.active = None

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.listener.close()
        if self.active:
            self.active.close()
        self.thread.join(timeout=7)
        if self.thread.is_alive():
            raise AssertionError("Fixture SSH thread failed to stop")

    def settings(self, wrong_key=False):
        key = paramiko.RSAKey.generate(2048) if wrong_key else self.server_key
        return {"host": "127.0.0.1", "port": self.port, "user": "fixture_diagnostics",
                "host_key": key.get_name() + " " + key.get_base64(), "services": ["fixture.service"]}

    def secret(self):
        output = io.StringIO()
        self.client_key.write_private_key(output)
        return {"private_key": output.getvalue()}


class SSHTransportTests(unittest.TestCase):
    def test_real_openssh_fixed_commands_and_pinned_key(self):
        with LoopbackSSH() as fixture:
            settings = fixture.settings()
            resource = "127.0.0.1:" + str(fixture.port)
            secret = fixture.secret()
            status = providers.call("ssh", "ssh_status", resource, {"unit": "fixture.service"}, settings, secret)
            self.assertEqual(status["exit_status"], 0)
            self.assertIn("ActiveState=active", status["stdout"])
            logs = providers.call("ssh", "ssh_logs", resource, {"unit": "fixture.service", "lines": 3}, settings, secret)
            self.assertEqual(logs["exit_status"], 0)
            self.assertIn("fixture log three", logs["stdout"])
            self.assertEqual(fixture.commands, [STATUS, LOGS])

            with patch.object(providers, "_process", wraps=providers._process) as execute:
                with self.assertRaises(PermissionError):
                    providers.call("ssh", "ssh_logs", resource, {"unit": "unauthorized.service"}, settings, secret)
                execute.assert_not_called()

            with self.assertRaises(RuntimeError):
                providers.call("ssh", "ssh_status", resource, {"unit": "fixture.service"},
                               fixture.settings(wrong_key=True), secret)
            self.assertEqual(fixture.commands, [STATUS, LOGS])
            self.assertFalse(fixture.errors)


if __name__ == "__main__":
    unittest.main()
