"""Install a pinned official CLI after verifying the npm registry's SHA-512 digest."""
import base64
import hashlib
import hmac
import io
import os
import platform
import tarfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "0.8.320"
INTEGRITY = "+D9IsvtYrTeLz7qyGrkSCkUp7MaFVO9XQKq4PY7I5Fq4i91+Mbu/oZ515vlGtMmNtVS0NOwCfWJVRlx47J+pQg=="
MAX_DOWNLOAD = 128 * 1024 * 1024


def install(destination=Path("/usr/local/bin/instabox")):
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if platform.system() != "Linux" or arch is None:
        raise RuntimeError("The bundled installer supports Linux amd64/arm64")
    url = f"https://registry.npmjs.org/instabox/-/instabox-{VERSION}.tgz"
    with urlopen(url, timeout=30) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD or not hmac.compare_digest(
        hashlib.sha512(data).digest(), base64.b64decode(INTEGRITY)
    ):
        raise RuntimeError("Instabox package integrity check failed")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember(f"package/cli/instabox-linux-{arch}")
        if not member.isfile() or member.size > 96 * 1024 * 1024:
            raise RuntimeError("Invalid Instabox executable")
        binary = archive.extractfile(member)
        if binary is None:
            raise RuntimeError("Missing Instabox executable")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(binary.read())
        os.chmod(destination, 0o755)
    print(f"Installed official Instabox CLI {VERSION} ({arch})")


if __name__ == "__main__":
    install()
