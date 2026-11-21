# 集成与运维

Cred Broker 适合由个人或小团队自部署，再让 AI 通过短期网关授权读取必要资源。例如为代码助手开放一个仓库的文件和 Issue，为运维助手开放指定 systemd 服务状态与日志，为邮件助手开放自己的只读 Gmail。每项任务单独签发授权，任务结束即撤销。

## 第三方项目如何接入

1. 部署 broker，由管理员建立连接；上游凭证只进入 broker。
2. 管理员为 AI 客户端签发精确的资源、工具与 TTL，最长 24 小时。
3. 支持 HTTP MCP 的客户端连接 `/mcp`，在 `Authorization` 中发送该 grant 的 Bearer。管理令牌只用于管理 API，不交给 AI。
4. 用官方 SDK 的初始化、工具发现、调用和撤销流程验证集成，再记录实际测试过的客户端与版本。

管理 API 使用 `X-Admin-Token` 或已登录的浏览器会话；不能把管理 API 暴露给只应拥有工具权限的 AI。以下是非秘密能力描述响应的结构，具体字段以已认证的 `GET /admin/api/v1/providers/instabox/schema` 返回为准：

```json
{
  "provider": "instabox",
  "schema": {
    "auth_mode": "api_key",
    "settings": {"type": "object"},
    "secret": {"type": "object"},
    "tools": [],
    "resource_constraints": {"source": "connection.resources"}
  }
}
```

这份简化示例只说明结构。完整响应描述必填/可选字段、`writeOnly` secret、工具参数及范围约束；不带已保存的值。`enumFrom`、`serverValidation` 和 `requires_secret` 是 broker 扩展注释，通用 JSON Schema 校验器不会替你实施动态范围、数值字符串上限或诊断密钥条件。实际权限以服务端校验和 `/mcp` 的工具发现为准。飞书、Instagram 等未支持 provider 返回 404。

`POST /admin/api/v1/grants/{id}/reissue` 创建同 client、同连接、同资源/工具的新授权，并在同一事务中撤销原授权。新 token 只返回一次；新 token 复制到客户端后，后续请求使用新令牌。撤销不能取消已经接纳的在途操作。

## 排障与记录导出

`/healthz` 只证明进程响应；认证后的 `/admin/api/v1/ready` 报告本地 schema、已加载主密钥和存储状态。两者均不证明上游账号可用。连接测试须由管理员明确触发，只存最近成功/失败状态与时间。

在配置了同样 `CB_*` 的环境执行 `python -m cred_broker doctor`，或在容器内执行。doctor 不外联、不输出秘密。运行中的 SQLite 若存在辅助文件会跳过完整性检查；对备份运行下面的独立预检。

审计页面显示最近 200 条。CSV 最多导出现存 5000 条中的 31 天窗口，仅有 `ts/client/action/connection_id/resource/outcome`。`after` 与 `before` 是含端点的 Unix 秒，默认查询截至当前时间的最近 31 天。指定 `before` 而不指定 `after` 时，默认向前查询 31 天。导出日期筛选独立于页面的客户端搜索和结果筛选；行数受保留上限约束。CSV 会转义表格软件的公式前缀，不记录原始参数、工具结果或凭证。

## 备份与恢复演练

1. 在可信环境停止实例写入，备份整个数据卷及独立的部署配置到加密存储；记录代码 tag/镜像摘要。
2. 保留原始备份。在另一个隔离目录制作可供检查的独立数据库快照；若副本仍有 WAL，使用 SQLite backup API 导出新文件，让 SQLite 正确合并快照。
3. 对独立数据库及其原始主密钥执行 `python -m cred_broker backup-check --db /secure/backup/broker.db --key /secure/backup/master.key`。该命令不自动迁移、不恢复或创建文件；缺件、结构不完整、数据库损坏或密钥不匹配返回非零。
4. 在独立测试数据卷中放回数据库及配对的 32 字节主密钥，权限为 0600/0400，容器归属为 UID/GID 10001；使用目标版本单 worker 启动。新的代码可能添加兼容字段，因此保留升级前快照。
5. 检查连接能解密、未过期授权能发现正确工具、撤销仍生效；确认无真实秘密进入日志。验证后再决定恢复生产实例。

空数据库无法证明主密钥配对；完整性与解密预检也不能替代应用启动和账号联调。不要把数据库和主密钥一起提交仓库、上传公共测试环境或发送给 AI。

## 贡献与后续方向

优先贡献可复现的客户端接入示例、provider 契约测试、中文/英文文档和部署恢复经验。新增 provider 必须在原始凭证不外露、精确资源/工具与有限输出边界内工作，流程见 [贡献指南](../CONTRIBUTING.md)。多租户、RBAC、多 worker、写操作和自动外发 webhook 需要单独设计与验证，目前不提供这些能力。
