"""Opt-in destructive staging-only Compose smoke test. Never targets production."""
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

PROJECT = os.environ.get("CB_TEST_PROJECT", "cred-broker-staging")
CONTAINER = os.environ.get("CB_TEST_CONTAINER", PROJECT + "-cred-broker-1")
BASE_URL = os.environ.get("CB_TEST_BASE_URL", "http://127.0.0.1:8765")
FAKE_SECRET = "ibk_fixture_container_key"


def docker(*args, timeout=10):
    result = subprocess.run(["docker", *args], capture_output=True, timeout=timeout)
    if result.returncode:
        raise AssertionError("Docker operation failed (output suppressed)")
    return result.stdout


def inspect():
    rows = json.loads(docker("inspect", CONTAINER))
    if len(rows) != 1 or rows[0].get("Config", {}).get("Labels", {}).get(
            "com.docker.compose.project") != PROJECT:
        raise AssertionError("Refusing container outside exact staging Compose project")
    return rows[0]


def load_env(path):
    values = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key.removeprefix("export ").strip()] = value.strip().strip("\"'")
    return values


def main():
    if os.environ.get("CB_TEST_STAGING") != "1":
        raise SystemExit("Refusing: set CB_TEST_STAGING=1 for staging only")
    if PROJECT != "cred-broker-staging" and not PROJECT.startswith("cred-broker-test-"):
        raise SystemExit("Refusing: use a dedicated cred-broker-test-* Compose project")
    endpoint = urlsplit(BASE_URL)
    if (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1"
            or not endpoint.port or endpoint.username or endpoint.password
            or endpoint.path or endpoint.query or endpoint.fragment):
        raise SystemExit("Refusing: test endpoint must be a loopback HTTP origin")
    env_path = os.environ.get("CB_TEST_ENV_FILE")
    if not env_path or not Path(env_path).is_file():
        raise SystemExit("Refusing: CB_TEST_ENV_FILE must reference external staging env")
    env_file = Path(env_path).resolve()
    if env_file.is_relative_to(Path(__file__).resolve().parent.parent):
        raise SystemExit("Refusing: staging env must live outside source tree")
    admin = load_env(env_file).get("CB_ADMIN_TOKEN")
    if not admin:
        raise SystemExit("Refusing: staging env missing admin authentication")
    metadata = inspect()
    config, host = metadata["Config"], metadata["HostConfig"]
    assert config["User"].split(":")[0] == "10001", "Container must use UID10001"
    assert host["ReadonlyRootfs"], "Root filesystem must be read-only"
    assert "ALL" in [value.upper() for value in host.get("CapDrop", [])], "Drop all capabilities"
    assert any(value.split(":")[0] == "no-new-privileges" for value in host.get("SecurityOpt", []))
    assert host["Memory"] == 256 * 1024 * 1024, "Memory must be 256 MiB"
    ports = host.get("PortBindings") or {}
    assert set(ports) == {"8765/tcp"}, "Unexpected published port"
    bindings = ports["8765/tcp"]
    assert bindings and all(item["HostIp"] == "127.0.0.1" and item["HostPort"] == str(endpoint.port)
                            for item in bindings), "Publish only the expected loopback port"
    base = BASE_URL

    def request(path, payload=None, bearer=None, admin_auth=False, method=None):
        headers = {}
        if admin_auth:
            headers["X-Admin-Token"] = admin
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = Request(base + path, body, headers, method=method)
        try:
            with urlopen(req, timeout=4) as response:
                raw = response.read(1024 * 1024)
                return response.status, json.loads(raw) if raw else None
        except HTTPError as error:
            return error.code, None

    def wait_healthy(deadline):
        while time.monotonic() < deadline:
            try:
                if request("/healthz")[0] == 200:
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.25)
        raise AssertionError("Staging health deadline exceeded")

    def rpc(token, method, params=None):
        return request("/mcp", {"jsonrpc": "2.0", "id": 1, "method": method,
                                "params": params or {}}, bearer=token)

    wait_healthy(time.monotonic() + 10)
    assert request("/admin/api/v1/connections")[0] == 401
    assert request("/admin/api/v1/ready")[0] == 401
    code, status = request("/admin/api/v1/ready", admin_auth=True)
    assert code == 200 and status["storage"] == "ready" and status["migrations"] == "current"
    code, schema = request("/admin/api/v1/providers/instabox/schema", admin_auth=True)
    assert code == 200 and schema["schema"]["secret"]["properties"]["read_key"]["writeOnly"]
    docker("exec", CONTAINER, "python", "-B", "-m", "cred_broker", "doctor", timeout=10)
    assert rpc("fixture-invalid", "tools/list")[0] == 401
    connection_id = grant_id = None
    try:
        code, result = request("/admin/api/v1/connections", {
            "provider": "instabox", "label": "Container test fixture", "settings": {
                "machines": ["fixture-container"]}, "secret": {"read_key": FAKE_SECRET}},
                admin_auth=True)
        assert code == 201, "Synthetic connection creation failed"
        assert FAKE_SECRET not in json.dumps(result), "Connection response leaked secret"
        connection_id = result["connection"]["id"]
        code, result = request("/admin/api/v1/grants", {
            "client": "container-test", "connection_id": connection_id,
            "resources": ["fixture-container"], "tools": ["instabox_describe"], "ttl_seconds": 300},
                admin_auth=True)
        assert code == 201, "Grant creation failed"
        grant_id, token = result["grant"]["id"], result["token"]
        code, result = rpc(token, "tools/list")
        assert code == 200 and [item["name"] for item in result["result"]["tools"]] == ["instabox_describe"]
        assert FAKE_SECRET not in json.dumps(result)
        # Check storage and exercise an isolated SQLite backup without altering live data.
        validation = """
import os, shutil, sqlite3, tempfile
from pathlib import Path
from cred_broker.store import Store, MasterKey
from cred_broker.control import Control
from cred_broker.backup_check import run as check_backup
root = Path(os.environ.get('CB_DATA_DIR','/var/lib/cred-broker'))
db = Path(os.environ.get('CB_DB_PATH',str(root/'broker.db')))
key = Path(os.environ.get('CB_MASTER_KEY_PATH',str(root/'master.key')))
assert db.stat().st_mode & 0o777 == 0o600
assert key.stat().st_mode & 0o777 == 0o400
assert db.stat().st_uid == key.stat().st_uid == 10001
for path in (db, Path(str(db)+'-wal')):
    if path.exists():
        assert b'ibk_fixture_container_key' not in path.read_bytes()
with tempfile.TemporaryDirectory(dir='/tmp') as temp:
    backup = Path(temp)/'backup.db'
    source = sqlite3.connect(db)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    copied_key = Path(temp)/'master.key'
    shutil.copyfile(key,copied_key)
    copied_key.chmod(0o400)
    assert check_backup(['--db',str(backup),'--key',str(copied_key)]) == 0
    restored = Control(Store(MasterKey(copied_key),backup))
    assert any(row['label']=='Container test fixture' for row in restored.connections())
print('storage checks passed')
"""
        inspect()
        docker("exec", CONTAINER, "python", "-B", "-c", validation, timeout=10)
        # Recheck exact ownership immediately before the only restart in this test.
        inspect()
        restart_deadline = time.monotonic() + 30
        docker("restart", "--time", "5", CONTAINER, timeout=20)
        wait_healthy(restart_deadline)
        code, result = request("/admin/api/v1/connections", admin_auth=True)
        assert code == 200 and connection_id in {item["id"] for item in result["connections"]}
        assert rpc(token, "tools/list")[0] == 200, "Token not persisted across restart"
        assert request(f"/admin/api/v1/grants/{grant_id}/revoke", {}, admin_auth=True)[0] == 200
        assert rpc(token, "tools/list")[0] == 401, "Revocation failed"
        assert request(f"/admin/api/v1/connections/{connection_id}", admin_auth=True,
                       method="DELETE")[0] in (200, 204)
        connection_id = None
        assert rpc(token, "tools/list")[0] == 401
        print("PASS staging container hardening, health, auth, persistence, storage, revoke, disconnect")
    finally:
        if connection_id:
            request(f"/admin/api/v1/connections/{connection_id}", admin_auth=True, method="DELETE")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("FAIL staging test; details suppressed to protect credentials")
