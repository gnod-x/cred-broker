"""Separate loopback-only fixture app for browser tests; never use as a deployment."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if os.environ.get("CB_TEST_FIXTURE") != "1":
    raise RuntimeError("This server is only a browser test fixture")

from cred_broker import providers
from cred_broker.app import create_app
from cred_broker.config import load_config


def fake_test(provider, settings, secret):
    if provider != "instabox" or settings != {"machines": ["fixture-machine"]}:
        raise ValueError("Only the synthetic fixture is allowed")
    return {"ok": True}


providers.test_connection = fake_test
app = create_app(load_config())
