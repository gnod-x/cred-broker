"""受控动作路由。

所有 AI 可触发的动作都从这里过：
1. 解析 token -> Session
2. 动作是否在该 grant 的 actions 白名单
3. 执行（注入真实凭证）
4. 审计

AI 拿不到任何"把凭证交出来"的通道。
"""

from __future__ import annotations

import json
import time

from . import forwarder, ssh_exec
from .store import Session, Store, matches


class Denied(Exception):
    pass


class Broker:
    def __init__(self, store: Store, hosts: dict[str, ssh_exec.HostConfig], timeout: int = 30,
                 max_output: int = 65536, known_hosts: str | None = None):
        self.store = store
        self.hosts = hosts
        self.timeout = timeout
        self.max_output = max_output
        self.known_hosts = known_hosts

    # ---------- 工具清单：告诉 AI 能做什么，不含任何秘密 ----------

    def tool_specs(self, session: Session) -> list[dict]:
        specs = []
        if any(a.startswith("ssh.") for a in session.actions):
            allowed_hosts = sorted(h for h in self.hosts if self._covers(session, f"ssh:{h}"))
            if allowed_hosts:
                specs.append(
                    {
                        "name": "ssh_run",
                        "description": "在允许的远程主机上以 argv 方式执行白名单命令，返回退出码和输出。",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "host": {"type": "string", "enum": allowed_hosts},
                                "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                            },
                            "required": ["host", "argv"],
                        },
                    }
                )
        if any(a.startswith("http.") for a in session.actions):
            services = [
                c["name"]
                for c in self.store.list_credentials()
                if c["kind"] in ("api_key", "oauth_google") and self._covers(session, c["name"])
            ]
            if services:
                specs.append(
                    {
                        "name": "http_fetch",
                        "description": "以受控凭证向已授权的服务发起 HTTP 请求；不能指定任意主机。",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "service": {"type": "string", "enum": services},
                                "method": {"type": "string", "enum": sorted(forwarder.ALLOWED_METHODS)},
                                "path": {"type": "string"},
                                "body": {"type": "string"},
                            },
                            "required": ["service", "method", "path"],
                        },
                    }
                )
        return specs

    # ---------- 动作执行 ----------

    def _require_credential(self, session: Session, target: str, *, action: str) -> None:
        """校验 grant 绑定的凭证覆盖本次目标。不匹配时抛 Denied 并审计，不静默降级。"""
        if self._covers(session, target):
            return
        self.store.audit(
            session.agent, action, "denied", target=target,
            detail={"error": "grant 凭证不覆盖目标", "grant_credential": session.credential},
            grant_id=session.grant_id,
        )
        raise Denied(f"grant 凭证 {session.credential} 不覆盖 {target}")

    @staticmethod
    def _covers(session: Session, target: str) -> bool:
        """grant 凭证是否覆盖目标。

        - 凭证名精确相等视为覆盖；
        - grant 凭证以 `*` 结尾时按前缀覆盖，如 `ssh:*`；
        - `broker:ssh` 是早期内置名，等价于 `ssh:*`，保留兼容已签发 token。
        """
        bound = session.credential or ""
        if bound == target or bound == "*":
            return True
        if bound.endswith("*") and target.startswith(bound[:-1]):
            return True
        return bound == "broker:ssh" and target.startswith("ssh:")

    def call(self, session: Session, action: str, args: dict) -> dict:
        if not matches(action, session.actions):
            self.store.audit(session.agent, action, "denied", detail="action 不在 grant 白名单", grant_id=session.grant_id)
            raise Denied(f"动作 {action} 未授权；允许: {session.actions}")
        started = time.time()
        try:
            if action == "ssh.run":
                result = self._ssh_run(session, args)
            elif action == "http.fetch":
                result = self._http_fetch(session, args)
            elif action == "tools.list":
                result = {"tools": self.tool_specs(session)}
            else:
                raise Denied(f"未知动作 {action}")
        except Denied:
            raise
        except forwarder.PolicyError as exc:
            self.store.audit(
                session.agent, action, "denied", target=str(args.get("host") or args.get("service") or ""),
                detail={"error": str(exc)}, grant_id=session.grant_id,
            )
            raise Denied(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            self.store.audit(
                session.agent, action, "error", target=str(args.get("host") or args.get("service") or ""),
                detail={"error": str(exc)}, grant_id=session.grant_id,
            )
            raise
        self.store.audit(
            session.agent,
            action,
            "allowed",
            target=str(args.get("host") or args.get("service") or ""),
            detail={**args, "duration_ms": int((time.time() - started) * 1000)},
            grant_id=session.grant_id,
            output=result.get("stdout", "").encode() if isinstance(result.get("stdout"), str) else None,
        )
        return result | {"duration_ms": int((time.time() - started) * 1000)}

    def _ssh_run(self, session: Session, args: dict) -> dict:
        host = args.get("host", "")
        argv = args.get("argv")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise Denied("argv 必须是字符串数组")
        cfg = self.hosts.get(host)
        if cfg is None:
            raise Denied(f"主机 {host} 未登记")
        # grant 绑定的凭证必须覆盖目标主机，否则同 token 可越权访问其他已登记主机
        self._require_credential(session, f"ssh:{host}", action="ssh.run")
        reason = ssh_exec.check_argv(cfg, argv)
        if reason:
            raise Denied(reason)

        cred = self.store.get_credential(f"ssh:{host}")
        if cred is None:
            raise Denied(f"缺少主机 {host} 的登录凭证")
        _kind, _meta, secret = cred

        result = ssh_exec.run(
            cfg, secret, argv, max_output=min(self.max_output, cfg.max_output),
            timeout=min(self.timeout, cfg.timeout), known_hosts=self.known_hosts,
        )
        return {
            "exit_status": result.exit_status,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "truncated": result.truncated,
        }

    def _http_fetch(self, session: Session, args: dict) -> dict:
        service = args.get("service", "")
        # 与 ssh.run 同一规则：grant 绑定的凭证必须就是被调用的服务
        self._require_credential(session, service, action="http.fetch")
        cred = self.store.get_credential(service)
        if cred is None:
            raise Denied(f"服务 {service} 未登记")
        kind, meta, secret = cred
        row = {"kind": kind, "meta": meta, "secret": secret}
        result = forwarder.forward(
            row,
            args.get("method", "GET"),
            args.get("path", "/"),
            args.get("body"),
            timeout=self.timeout,
        )
        return result.as_dict()
