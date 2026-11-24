# 架构与运行边界

cred-broker 是单实例凭证代理。管理员建立连接并签发授权；AI 只获得一个授权的 Bearer token，通过 MCP 调用预设动作。系统没有返回凭证、通用 shell 或任意 URL 转发的新接口，也没有团队 RBAC、多租户或分布式控制面。

v0.3.1 对应 2026-11-25 计划的第一批 T00–T03：基线、启动保护、入口和请求读取边界、本地事务与结果语义。它不是整份改造路线的完成版本。发布与测试状态以 [验收记录](validation.md) 为准。

## 入口与数据流

管理页面使用管理员会话；`/admin/api/v1` 提供连接、授权与审计管理。管理员认证的 `GET /admin/api/v1/ready` 返回版本、storage、key、key_pairing、migrations 和支持的 provider 列表。key 表示密钥已加载，key_pairing 是启动时扫描得到的 verified/unproven；空库没有密文可供证明，因此为 unproven。该接口不在每次请求时遍历解密全库，也不拨测外部 provider。schema current 只是当前必需表/列检查，不是正式 schema 版本机制。

`GET /admin/api/v1/providers/{provider}/schema` 提供不含秘密的字段、工具和资源约束。Gmail 只能由管理员浏览器完成 Google OAuth 回调后建立。管理会话、管理 API 令牌与 MCP Bearer 是不同的认证边界。

AI 使用无状态 Streamable HTTP `POST /mcp`，支持 initialize、initialized 通知、ping、tools/list 和 tools/call。响应使用 JSON，通知返回 202，不提供 SSE 持久流，GET 返回 405。每次请求验证 Bearer、协议及入口策略；工具调用再次读取授权。发现结果只包含授权内工具与资源。

执行路径为：哈希 token 查找授权 → 复核连接、有效期与撤销状态 → 精确核对工具和资源 → provider 使用凭证执行固定操作 → 结果脱敏 → 记录有限结果。清单不会返回 secret。

## HTTP 接收边界

web 和 MCP 共用 `HttpSecurityPolicy` 与最外层 ASGI 中间件。Host 必须匹配允许的精确 origin 的 authority；允许来源来自维护用回环地址/监听端口、`CB_ALLOWED_ORIGINS` JSON 数组和可选 `CB_PUBLIC_ORIGIN`。不能从请求 Host 反推白名单。非默认的 Compose 宿主机映射端口必须登记对应外部 origin。

请求若带 Origin，只接受规范化后的精确白名单；拒绝 null、畸形或重复 Origin，拒绝缺失/重复或未登记的 Host。没有 Origin 的原生 API/MCP 客户端仍须通过 Host 和各自认证。修改请求的 `Sec-Fetch-Site: cross-site` 会被拒绝。

中间件对所有 HTTP 路由累计实际接收字节，默认上限 65536 字节，`CB_MAX_REQUEST_BODY` 可在 1024–65536 之间配置；默认整个请求体读取期限 10 秒，`CB_BODY_READ_TIMEOUT` 必须大于 0 且不超过 60 秒。它覆盖 JSON、form 与 chunked 读取，不依赖 Content-Length 声明。超限返回 413，读取到期返回 408；这些是入口读取预算，不是整个 provider 动作 deadline 或最终响应预算，后两者仍在后续 T04。

正式 Python 入口显式关闭 Uvicorn 的 proxy_headers，并清空 forwarded_allow_ips。原样 TCP 代理下不信任客户端伪造的转发头；后端只能看到代理 peer，登录限流可能由所有 LAN 用户共享。TLS 与名称/信任选择见 [LAN HTTPS ADR](adr/0001-lan-https.md)。

## 启动、存储与加密

正式启动调用 `prepare_store(cfg)`。已有库在任何 schema/journal 写操作前检查密钥、0400/0600 权限及服务属主，并以正常 `mode=ro` 事务检查 SQLite、必需表/列及全部 legacy/scoped 密文的 AES-GCM 和 JSON 结构。已有库缺 key、短 key、错 key、坏密文或结构不完整均失败关闭，不造替代 key、不修权限、不删除坏库。缺 key 和短 key 在打开 SQLite 前失败。

新安装仅在 key/db 均不存在时初始化：父目录 flock 串行化初始化，私有临时文件完整写入并 fsync 后，以不覆盖目标的 hard-link 发布。两个独立文件不能作为一个文件系统操作提交；中断留下 key-only/db-only 时，下次启动明确拒绝，需恢复原配对，不猜测“空安装”。不新增 sentinel、密钥 ID、正式 schema 版本或密文格式。

启动读取 live WAL 一律使用普通只读连接，不能使用 immutable 忽略 WAL。SQLite 可以创建空辅助文件或更新 SHM 读者元数据；这不同于业务写入或 checkpoint。独立 `backup-check` 才要求无 sidecar，并使用 immutable 核验输入。

新接口沿用 SQLite 和 MasterKey，使用独立 scoped 表：

- scoped_connections：连接身份、provider、nonce、密文、创建时间及最近连接测试状态。
- scoped_grants：client、单个 connection_id、精确资源/工具列表、token 哈希及时间状态。
- scoped_audit：时间、client、动作、连接、授权内资源与结果。

settings 与 secret 一并使用 AES-256-GCM 加密，AAD 绑定连接 ID；legacy AAD 保持凭证名。主密钥是严格 32 字节二进制文件，不 strip。新授权明文 token 只在签发响应出现，数据库存 SHA-256，TTL 为 1–86400 秒。

## 本地事务、外部结果与并发

创建连接、创建授权、撤销、断开、重签发的本地业务修改与有限审计在同一 SQLite 事务提交；审计失败会回滚本地业务修改。首次签发和重签发均保护一次性 token 窗口。收到响应后需先保存 token，再关闭窗口；网络失败时先核对授权清单，不能直接重复签发。

远端动作与 SQLite 不能原子提交。已接纳的 provider 操作可能完成，但结果处理或本地审计失败，此时控制层使用 OutcomeUnknown。管理连接测试返回 outcome_unknown 与 retryable=false；MCP 在工具结果中返回 isError、outcome_unknown 和不自动重试提示。它不是“远端一定失败”或 exactly-once 保证。结果未知时先核对上游及本地状态。

进程内 RLock 将调用接纳与撤销、断开同步。已接纳请求可能完成；撤销后新接纳调用拒绝。必须只运行一个 worker，锁不跨进程。SQLite WAL 面向低并发单实例；全局/provider 准入、完整动作 deadline 与集中存储尚未实施。

## 会话、审计与兼容

浏览器会话是随机值，内存仅存哈希和最长 8 小时到期时间；注销和重启使旧会话失效。管理 API 的 X-Admin-Token 保留全权能力。登录按 60 秒窗口限制每来源 8 次、全实例 64 次；会话库存最多 128。代理后的用户可能共享额度。

审计最多保留 5000 条，页面默认读取最近 200 条；CSV 限 31 天窗口，只有 ts/client/action/connection_id/resource/outcome，并防 CSV 公式注入。记录有限结果，不记录 secret、token、settings、参数、正文；它不是不可篡改日志，也未覆盖所有 OAuth/认证拒绝事件。

旧凭证、grant/token 和审计表与 scoped 表隔离，不迁移或交叉认证。旧 API 默认关闭，只有 CB_ENABLE_LEGACY=true 才开放兼容入口。旧动作通配和更宽 SSH/HTTP 行为不具备 scoped 模型的同等精确授权边界。
