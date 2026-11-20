"""SSH 代理执行：AI 只提交 argv，broker 用它自己的凭证登录并执行。

设计约束：
- AI 不接触密码/私钥；
- 只接受 argv 列表，不接受 shell 拼接串，参数逐一 shlex 引用；
- 只允许 host 配置里的 argv[0] 白名单；
- 输出截断并记录摘要。
"""

from __future__ import annotations

import io
import shlex
import socket
from dataclasses import dataclass

import paramiko


class SSHError(RuntimeError):
    pass


@dataclass
class SSHResult:
    exit_status: int
    stdout: str
    stderr: str
    truncated: bool
    duration_ms: int


@dataclass
class HostConfig:
    host: str
    port: int = 22
    user: str = "broker"
    allowed_commands: tuple[str, ...] = ()
    timeout: int = 30
    max_output: int = 65536

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "HostConfig":
        missing = [k for k in ("host",) if not data.get(k)]
        if missing:
            raise ValueError(f"host 配置 {name} 缺少字段 {missing}")
        return cls(
            host=str(data["host"]),
            port=int(data.get("port", 22)),
            user=str(data.get("user", "broker")),
            allowed_commands=tuple(str(c) for c in data.get("allowed_commands", [])),
            timeout=int(data.get("timeout", 30)),
            max_output=int(data.get("max_output", 65536)),
        )


def _basename(cmd: str) -> str:
    return cmd.rsplit("/", 1)[-1]


def load_private_key(raw: str, passphrase: str | None = None) -> paramiko.PKey:
    """按密钥内容自动识别类型（ed25519 / RSA / ECDSA），不在调用处写死算法。

    paramiko 5.x 的 PKey 基类不能直接实例化，因此按具体类型依次尝试，
    只有全部失败才报错。失败信息不回显异常原文，避免密钥片段进入日志或调用方。
    """
    if not raw or not raw.strip():
        raise SSHError("私钥内容为空")

    key_types = (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey)
    last_error: Exception | None = None
    for cls in key_types:
        try:
            return cls.from_private_key(io.StringIO(raw), password=passphrase)
        except paramiko.PasswordRequiredException as exc:
            raise SSHError("私钥已加密但缺少 passphrase") from exc
        except (paramiko.SSHException, ValueError, OSError) as exc:
            last_error = exc
    raise SSHError("私钥无法解析，请确认格式或 passphrase") from last_error


def check_argv(cfg: HostConfig, argv: list[str]) -> str | None:
    """返回拒绝原因；None 表示允许。"""
    if not argv:
        return "空命令"
    if any("\x00" in a for a in argv):
        return "参数含 NUL"
    if not cfg.allowed_commands:
        return "该主机未配置命令白名单，拒绝执行"
    allowed = {_basename(a) for a in cfg.allowed_commands}
    if _basename(argv[0]) not in allowed:
        return f"命令 {_basename(argv[0])} 不在白名单 {sorted(allowed)}"
    return None


def run(
    host_cfg: HostConfig,
    auth: dict,
    argv: list[str],
    *,
    max_output: int = 65536,
    timeout: int = 30,
    known_hosts: str | None = None,
) -> SSHResult:
    if not argv:
        raise SSHError("空命令")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    if known_hosts:
        client.load_host_keys(known_hosts)
    else:
        client.load_system_host_keys()

    command = shlex.join(argv)
    connect_kwargs: dict = {
        "hostname": host_cfg.host,
        "port": host_cfg.port,
        "username": host_cfg.user,
        "timeout": min(timeout, 15),
        "allow_agent": False,
        "look_for_keys": False,
    }
    if "password" in auth:
        connect_kwargs["password"] = auth["password"]
    elif "private_key" in auth:
        connect_kwargs["pkey"] = load_private_key(auth["private_key"], auth.get("passphrase"))
    else:
        raise SSHError("凭证缺少 password 或 private_key")

    started = _monotonic_ms()
    try:
        client.connect(**connect_kwargs)
        transport = client.get_transport()
        if transport is None:
            raise SSHError("SSH 传输层未建立")
        channel = transport.open_session()
        channel.settimeout(timeout)
        channel.exec_command(command)

        out, err = b"", b""
        truncated = False
        buf = io.BytesIO()
        # stderr 只保留尾部：命令失败时有用信息通常在末尾，避免无限累积占内存
        err_limit = 65536
        try:
            # 退出码就绪不等于数据读完：远端可能在 exit-status 之后仍有缓冲输出，
            # 因此只有在退出码就绪且两个流都没有待读数据时才结束。
            while True:
                if channel.recv_ready():
                    chunk = channel.recv(32768)
                    if not chunk:
                        break
                    room = max_output - buf.tell()
                    if room <= 0:
                        truncated = True
                        break
                    if len(chunk) > room:
                        buf.write(chunk[:room])
                        truncated = True
                        break
                    buf.write(chunk)
                elif channel.recv_stderr_ready():
                    err = (err + channel.recv_stderr(32768))[-err_limit:]
                elif channel.exit_status_ready():
                    break
                else:
                    if channel.status_event.wait(0.2):
                        continue
        finally:
            exit_status = channel.recv_exit_status() if channel.exit_status_ready() else -1
            while channel.recv_stderr_ready():
                err = (err + channel.recv_stderr(32768))[-err_limit:]
            out = buf.getvalue()
            channel.close()
    except paramiko.AuthenticationException as exc:
        raise SSHError(f"认证失败: {exc}") from exc
    except (paramiko.SSHException, socket.error, OSError) as exc:
        raise SSHError(f"连接失败: {exc}") from exc
    finally:
        client.close()

    return SSHResult(
        exit_status=exit_status,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace")[-4096:],
        truncated=truncated,
        duration_ms=_monotonic_ms() - started,
    )


def _monotonic_ms() -> int:
    import time

    return int(time.monotonic() * 1000)
