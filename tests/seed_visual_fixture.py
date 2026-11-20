"""Create synthetic screenshot data in a new, explicit temporary fixture directory.

Never accepts an existing database or a production path. No upstream calls.
Usage: python -B -I tests/seed_visual_fixture.py /tmp/cb-visual-fixture-UNIQUE
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cred_broker.control import Control
from cred_broker.store import MasterKey, Store


def seed(path):
    path = Path(path).absolute()
    temporary = Path(tempfile.gettempdir()).resolve()
    if not path.name.startswith("cb-visual-fixture-") or path.parent.resolve() != temporary:
        raise ValueError("Use a new /tmp/cb-visual-fixture-UNIQUE directory")
    path.mkdir(mode=0o700, exist_ok=False)
    control = Control(Store(MasterKey(path / "master.key"), path / "broker.db"))
    definitions = [
        ("gmail", "工作邮箱", {"email": "reader@example.test"}, {"client_id": "fixture-client", "client_secret": "fixture-secret", "refresh_token": "fixture-refresh"}),
        ("github", "项目仓库", {"app_id": 123, "installation_id": 456, "repositories": ["fixture-org/sample-app"]}, {"private_key": "fixture-private-key-not-real"}),
        ("ssh", "测试主机", {"host": "fixture.example.test", "port": 22, "user": "diagnostics", "host_key": "ssh-ed25519 Zml4dHVyZQ==", "services": ["sample.service"]}, {"private_key": "fixture-private-key-not-real"}),
        ("instabox", "研发沙箱", {"machines": ["fixture-machine"]}, {"read_key": "ibk_fixture_read_not_a_real_key"}),
    ]
    connections = []
    for provider, label, settings, secret in definitions:
        connection = control.create_connection(provider, label, settings, secret)
        connections.append(connection)
        grant, _token = control.create_grant(
            "演示助手 · " + label, connection["id"],
            [connection["resources"][0]["id"]], [connection["tools"][0]["name"]], 3600)
        if provider == "ssh":
            control.revoke(grant["id"])
    print(json.dumps({"fixture": str(path), "database": str(path / "broker.db"),
                      "connections": len(connections), "synthetic": True}, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    seed(sys.argv[1])
