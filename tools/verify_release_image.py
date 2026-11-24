"""Verify one already-built image in a disposable Compose project; never production."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--project", default="cred-broker-test-release")
    parser.add_argument("--port", type=int, default=18766)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9./:@_-]{0,255}", args.image):
        parser.error("Invalid image reference")
    if not re.fullmatch(r"cred-broker-test-[a-z0-9-]{1,48}", args.project):
        parser.error("A dedicated cred-broker-test-* project is required")
    if not 1024 <= args.port <= 65535:
        parser.error("Invalid loopback test port")
    root = Path(__file__).resolve().parents[1]
    project = args.project + "-" + secrets.token_hex(4)
    metadata = subprocess.run(["docker", "ps", "-aq", "--filter",
                               "label=com.docker.compose.project=" + project],
                              check=True, capture_output=True, text=True)
    if metadata.stdout.strip():
        raise RuntimeError("Refusing to reuse an existing project")
    for resource in ("volume", "network"):
        existing = subprocess.check_output(["docker", resource, "ls", "-q", "--filter",
                     "label=com.docker.compose.project=" + project], text=True)
        if existing.strip():
            raise RuntimeError("Refusing to reuse existing project resources")
    volumes = subprocess.check_output(["docker", "volume", "ls", "--format", "{{.Name}}"], text=True).splitlines()
    if project + "_broker-data" in volumes:
        raise RuntimeError("Refusing an existing data volume")
    env = dict(os.environ)
    env.update(CB_ADMIN_TOKEN=secrets.token_urlsafe(32), CB_HOST_PORT=str(args.port),
               CB_ALLOWED_ORIGINS=json.dumps(["http://127.0.0.1:" + str(args.port)]),
               CB_GOOGLE_CLIENT_ID="", CB_GOOGLE_CLIENT_SECRET="", CB_GOOGLE_REDIRECT_URI="",
               CB_PUBLIC_ORIGIN="", CB_BODY_READ_TIMEOUT="10", CB_MAX_REQUEST_BODY="65536")
    image_id = subprocess.check_output(["docker", "image", "inspect", args.image,
                                        "--format", "{{.Id}}"], text=True).strip()
    with tempfile.TemporaryDirectory(prefix="cb-release-") as temporary:
        private = Path(temporary)
        config = private / "fixture.env"
        config.write_text("CB_ADMIN_TOKEN=" + env["CB_ADMIN_TOKEN"] + "\n")
        config.chmod(0o600)
        override = private / "image.yaml"
        override.write_text("services:\n  cred-broker:\n    image: " + args.image + "\n")
        compose = ["docker", "compose", "-f", str(root / "compose.yaml"), "-f", str(override),
                   "--env-file", str(config), "-p", project]
        def run(command, timeout=180):
            result = subprocess.run(command, cwd=root, env=env, capture_output=True,
                                    text=True, timeout=timeout)
            if result.returncode:
                raise RuntimeError("Isolated image verification command failed (output suppressed)")
            return result.stdout
        try:
            run(compose + ["up", "-d", "--no-build", "--pull", "never", "--wait"])
            env.update(CB_TEST_STAGING="1", CB_TEST_PROJECT=project,
                       CB_TEST_CONTAINER=project + "-cred-broker-1",
                       CB_TEST_BASE_URL="http://127.0.0.1:" + str(args.port),
                       CB_TEST_ENV_FILE=str(config))
            run(["python3", "-B", str(root / "tests/test_container.py")])
            actual = json.loads(run(["docker", "inspect", project + "-cred-broker-1"]))[0]
            if actual["Image"] != image_id:
                raise RuntimeError("Container did not run the exact candidate image")
            print(json.dumps({"project": project, "image": args.image, "image_id": image_id,
                              "container_acceptance": "PASS"}))
        finally:
            run(compose + ["down", "--volumes", "--remove-orphans"])
if __name__ == "__main__":
    main()
