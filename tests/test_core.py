"""不依赖网络与真实凭证的自测：加密存储、grant/token 生命周期、动作白名单、审计。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cred_broker import ssh_exec  # noqa: E402
from cred_broker.broker import Broker, Denied  # noqa: E402
from cred_broker.store import MasterKey, Store, matches  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        store = Store(MasterKey(tmp_path / "master.key"), tmp_path / "broker.db")

        # 1. 加密往返
        store.put_credential("ssh:demo", "ssh_password", {"password": "p@ss"}, {"port": 22})
        got = store.get_credential("ssh:demo")
        check("凭证加密往返", got is not None and got[2]["password"] == "p@ss")
        db_bytes = (tmp_path / "broker.db").read_bytes()
        check("数据库无反注明文", b"p@ss" not in db_bytes)

        # 2. 主密钥权限
        key_mode = (tmp_path / "master.key").stat().st_mode & 0o777
        check("主密钥权限 0400", key_mode == 0o400, oct(key_mode))

        # 3. grant 与 token
        gid = store.create_grant("claude-code", "broker:ssh", ["ssh.run"], 600)
        token, expires = store.issue_token("t1", gid)
        session = store.resolve_token(token)
        check("token 可解析", session is not None and session.agent == "claude-code")
        check("token 不落明文", token.encode() not in (tmp_path / "broker.db").read_bytes())

        # 4. 撤销立即失效
        store.revoke_grant(gid)
        check("撤销后 token 失效", store.resolve_token(token) is None)

        # 5. 错误 token
        check("伪造 token 拒绝", store.resolve_token("cb_fake") is None)

        # 6. 动作匹配
        check("动作通配", matches("ssh.run", ["ssh.*"]) and not matches("http.fetch", ["ssh.*"]))

        # 7. 命令白名单
        cfg = ssh_exec.HostConfig(host="127.0.0.1", allowed_commands=("uptime", "df"))
        check("白名单内放行", ssh_exec.check_argv(cfg, ["uptime"]) is None)
        check("白名单外拒绝", ssh_exec.check_argv(cfg, ["rm", "-rf", "/"]) is not None)
        check("路径形式命令拒绝", ssh_exec.check_argv(cfg, ["/bin/rm"]) is not None)
        check("空命令拒绝", ssh_exec.check_argv(cfg, []) is not None)
        check("未配置白名单全拒", ssh_exec.check_argv(ssh_exec.HostConfig(host="x"), ["uptime"]) is not None)

        # 8. broker 拒绝未授权动作并审计
        gid2 = store.create_grant("agent", "broker:ssh", ["http.fetch"], 600)
        token2, _ = store.issue_token("t2", gid2)
        sess2 = store.resolve_token(token2)
        broker = Broker(store, {"demo": cfg})
        try:
            broker.call(sess2, "ssh.run", {"host": "demo", "argv": ["uptime"]})
            check("越权动作被拒", False, "没有抛异常")
        except Denied:
            check("越权动作被拒", True)

        audit = store.list_audit(10)
        check("审计写入", any(a["action"] == "ssh.run" and a["outcome"] == "denied" for a in audit))

        # 9. 审计详情脱敏
        store.audit("agent", "test", "allowed", detail={"password": "hunter2", "other": "v"})
        row = store.list_audit(1)[0]
        check("审计脱敏", "hunter2" not in row["detail"] and "***" in row["detail"])

        # 10. 未知主机拒绝
        gid3 = store.create_grant("agent", "broker:ssh", ["ssh.run"], 600)
        token3, _ = store.issue_token("t3", gid3)
        try:
            broker.call(store.resolve_token(token3), "ssh.run", {"host": "nope", "argv": ["uptime"]})
            check("未登记主机被拒", False)
        except Denied:
            check("未登记主机被拒", True)

        # 11. grant 凭证必须覆盖目标主机（防止一个 token 操作全部主机）
        other = ssh_exec.HostConfig(host="other", allowed_commands=("uptime",))
        multi = Broker(store, {"demo": cfg, "other": other})
        gid4 = store.create_grant("agent", "ssh:demo", ["ssh.run"], 600)
        tok4, _ = store.issue_token("t4", gid4)
        sess4 = store.resolve_token(tok4)
        try:
            multi.call(sess4, "ssh.run", {"host": "other", "argv": ["uptime"]})
            check("跨主机越权被拒", False, "没有抛异常")
        except Denied:
            check("跨主机越权被拒", True)
        check(
            "越权审计为 denied",
            any(a["outcome"] == "denied" and a["target"] == "ssh:other" for a in store.list_audit(20)),
        )
        # 精确绑定本主机仍可通过策略层（未配置该主机凭证，应在取凭证处报缺少）
        gid5 = store.create_grant("agent", "ssh:other", ["ssh.run"], 600)
        tok5, _ = store.issue_token("t5", gid5)
        try:
            multi.call(store.resolve_token(tok5), "ssh.run", {"host": "other", "argv": ["uptime"]})
            check("缺凭证报错", False, "没有抛异常")
        except Denied as exc:
            check("缺凭证报错", "缺少主机 other 的登录凭证" in str(exc), str(exc))

        # 12. 通配凭证 ssh:* 覆盖所有已登记主机
        gid6 = store.create_grant("agent", "ssh:*", ["ssh.run"], 600)
        tok6, _ = store.issue_token("t6", gid6)
        sess6 = store.resolve_token(tok6)
        check("通配凭证覆盖", multi._covers(sess6, "ssh:demo") and multi._covers(sess6, "ssh:other"))
        check("错误凭证不覆盖", not multi._covers(sess6, "http:demo"))

        # 13. 工具清单只列出获授权的主机
        specs = {s["name"]: s for s in multi.tool_specs(sess4)}
        hosts = specs.get("ssh_run", {}).get("input_schema", {}).get("properties", {}).get("host", {}).get("enum", [])
        check("工具清单只含授权主机", hosts == ["demo"], str(hosts))

        # 14. 私钥类型自动识别（RSA / ECDSA / 非法输入）
        import io as _io

        import paramiko as _pk

        for name, key in (("rsa", _pk.RSAKey.generate(2048)), ("ecdsa", _pk.ECDSAKey.generate())):
            buf = _io.StringIO()
            key.write_private_key(buf)
            loaded = ssh_exec.load_private_key(buf.getvalue())
            check(f"{name} 私钥可加载", type(loaded).__name__ == type(key).__name__)
        try:
            ssh_exec.load_private_key("not a key")
            check("非法私钥拒绝", False)
        except ssh_exec.SSHError:
            check("非法私钥拒绝", True)
        try:
            ssh_exec.load_private_key("")
            check("空私钥拒绝", False)
        except ssh_exec.SSHError:
            check("空私钥拒绝", True)

    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
