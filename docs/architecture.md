# 架构与运行边界

cred-broker 是单实例凭证代理。管理员在管理页建立连接并签发授权；AI 只获得一个授权的 Bearer token，通过 MCP 调用预设动作。系统没有返回凭证、通用 shell 或任意 URL 转发的新接口，也没有团队 RBAC、多租户或分布式控制面。

## 入口与数据流

管理页面使用管理员会话；`/admin/api/v1` 提供连接、授权与审计管理。管理员认证的 `GET /admin/api/v1/ready` 只检查本地版本、SQLite schema、主密钥可用性和已支持 provider 列表，不拨测外部 provider。`GET /admin/api/v1/providers/{provider}/schema` 提供不含秘密的连接字段、工具和资源约束，供自部署项目生成表单或接入检查。Gmail 连接只能由管理员浏览器完成 Google OAuth 回调后建立，普通连接创建接口拒绝 Gmail。管理员认证与 MCP Bearer 分离。

AI 使用标准 Streamable HTTP `POST /mcp`。传输无状态，不签发 MCP 会话 ID；支持 initialize、initialized 通知、ping、tools/list 和 tools/call。响应使用 JSON，通知返回 202，不提供 SSE 持久流，GET 返回 405。每次请求验证 Bearer、协议和 Origin；工具调用再次读取授权。发现结果只包含该授权的工具，resource 参数枚举被限制为该授权的资源。

执行路径为：哈希 token 查找授权 → 检查连接存在、有效期和撤销状态 → 精确核对工具与资源 → provider 执行固定操作并注入凭证 → 结果脱敏 → 记录调用结果。工具清单不会提供 secret。管理员触发连接测试时，只更新连接最近测试的时间和成功/失败状态，不持久化 provider 响应正文。

## 存储与加密

浏览器登录后签发独立的随机会话，只在进程内保存 SHA-256 和单调时钟到期时间，最长 8 小时。重复登录替换当前浏览器原会话，注销撤销其服务端哈希，重启清除全部会话；现有 `X-Admin-Token` 认证保留。登录尝试以 60 秒窗口限制单来源 8 次、实例合计 64 次，队列与会话最多分别保留 64/128 项。它们都只适用于单进程；反向代理后的来源可能共享额度。

新接口复用 Store 的 SQLite 数据库和 MasterKey，但使用独立表：

- `scoped_connections`：连接 ID、名称、provider、随机 nonce、密文和创建时间。
- `scoped_grants`：客户端标识、单个 connection_id、精确资源列表、工具列表、token 哈希、创建/到期/撤销时间。
- `scoped_audit`：时间、客户端、动作、连接、授权内资源和结果。

连接 settings 和 secret 一并使用 AES-256-GCM 加密，AAD 绑定连接 ID。主密钥与数据库分开保存；单独复制数据库不足以解密。管理员连接清单解密后仅返回经 provider 校验的 settings、资源和工具，不返回 secret。

每个新授权产生一个随机 Bearer，明文只在签发响应出现，数据库只存 SHA-256。TTL 为 1–86400 秒，不支持通配资源或通配工具。创建授权使用 SQLite `BEGIN IMMEDIATE` 事务检查连接与范围后写入。重签发沿用原 client、connection、resource 和 tool 范围，在同一事务中创建新 grant 并撤销旧 grant。

审计表最多保留最近 5000 条。管理 API 和页面默认读取最近 200 条；CSV 导出接受 `after`/`before` Unix 秒，查询区间最多 31 天，且只投影六个有限字段。导出值会进行 CSV 公式注入防护，响应固定文件名并禁止缓存。

## 撤销与并发

进程内 RLock 将调用接纳与撤销、断开同步。已接纳的在途请求可以完成，撤销不会取消远端已执行动作；撤销后新接纳的调用会被拒绝。断开连接同时撤销关联授权，随后所有关联 token 不再可解析。工具发现也复核撤销与到期状态。

必须只运行一个 worker。RLock 不跨进程，不能把该语义扩展到多 worker 或多个实例。SQLite WAL 适合个人或小团队的低并发单实例；分布式锁、集中数据库、任务取消和规模化管理属于后续工作。

## 审计与兼容

新审计覆盖新工具调用及管理员的连接创建、测试、断开、授权创建、重签发和撤销。管理员事件 client 固定为 admin，resource 为空；连接测试失败也记录 failed。这不是完整管理操作或 OAuth 审计。只记录有限字段和 ok/denied/failed，不记录 settings、secret、token、参数或响应正文。库内最多保留最近 5000 条，管理 API 返回最近 200 条。

旧凭证、grant/token 和旧审计表与新表隔离，不自动迁移或交叉认证。旧 API 默认关闭；只有明确设置 `CB_ENABLE_LEGACY=true` 才开放兼容入口。旧授权支持动作模式及更宽的 SSH/HTTP 行为，不能视为新接口的同等精确授权。迁移客户端后应关闭兼容入口。
