from __future__ import annotations

import os
import socket
import sys


def main() -> None:
    if sys.argv[1:] == ["doctor"]:
        from .doctor import run

        raise SystemExit(run())
    if sys.argv[1:2] == ["backup-check"]:
        from .backup_check import run

        raise SystemExit(run(sys.argv[2:]))
    if sys.argv[1:]:
        raise SystemExit("用法: python -m cred_broker [doctor | backup-check --db PATH --key PATH]")
    import uvicorn

    from .app import create_app
    from .config import load_config

    cfg = load_config()
    host, _, port = cfg.binding.rpartition(":")
    app = create_app(cfg)

    # 若以 root 启动（便于读取 0400 主密钥后降权），先绑定再切换用户
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host or "127.0.0.1", int(port)))
    sock.listen(128)
    sock.set_inheritable(True)

    config = uvicorn.Config(app, log_level=os.environ.get("CB_LOG_LEVEL", "info"), access_log=False)
    server = uvicorn.Server(config)
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
