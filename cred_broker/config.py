"""运行时配置，全部来自环境变量，由 systemd unit 注入。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .origin import canonical_origin


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    data_dir: Path
    db_path: Path
    master_key_path: Path
    binding: str
    web_token_path: Path
    admin_token: str
    mcp_token: str
    hosts_json: str
    session_cookie: str
    request_timeout: int
    max_output_bytes: int
    ssh_known_hosts: Path | None
    legacy_enabled: bool = False
    public_origin: str = ""
    allowed_origins: tuple[str, ...] = ()
    body_read_timeout: float = 10.0
    max_request_body_bytes: int = 65536

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


def _http_settings():
    from .security import validate_read_limits

    try:
        values = json.loads(os.environ.get("CB_ALLOWED_ORIGINS", "[]"))
        if not isinstance(values, list) or len(values) > 32 or not all(isinstance(value, str) for value in values):
            raise ValueError
        origins = tuple(dict.fromkeys(canonical_origin(value) for value in values))
        timeout = float(os.environ.get("CB_BODY_READ_TIMEOUT", "10"))
        maximum = int(os.environ.get("CB_MAX_REQUEST_BODY", "65536"))
    except (TypeError, ValueError):
        raise RuntimeError("Invalid HTTP ingress/read configuration") from None
    validate_read_limits(maximum, timeout)
    return origins, timeout, maximum


def load_config() -> Config:
    origins, body_read_timeout, max_request_body_bytes = _http_settings()
    data_dir = Path(_env("CB_DATA_DIR", "/var/lib/cred-broker"))
    kh = os.environ.get("CB_SSH_KNOWN_HOSTS")
    return Config(
        data_dir=data_dir,
        db_path=Path(_env("CB_DB_PATH", str(data_dir / "broker.db"))),
        master_key_path=Path(_env("CB_MASTER_KEY_PATH", str(data_dir / "master.key"))),
        binding=_env("CB_BIND", "127.0.0.1:8765"),
        web_token_path=Path(_env("CB_WEB_TOKEN_FILE", str(data_dir / "web.token"))),
        admin_token=_env("CB_ADMIN_TOKEN"),
        # Legacy setting retained for old deployments; grants provide the actual AI authentication.
        mcp_token=os.environ.get("CB_MCP_TOKEN", ""),
        hosts_json=_env("CB_HOSTS", "{}"),
        session_cookie="cb_session",
        request_timeout=_env_int("CB_REQUEST_TIMEOUT", 30),
        max_output_bytes=_env_int("CB_MAX_OUTPUT", 65536),
        ssh_known_hosts=Path(kh) if kh else None,
        legacy_enabled=os.environ.get("CB_ENABLE_LEGACY", "").lower() == "true",
        public_origin=os.environ.get("CB_PUBLIC_ORIGIN", ""),
        allowed_origins=origins,
        body_read_timeout=body_read_timeout,
        max_request_body_bytes=max_request_body_bytes,
    )
