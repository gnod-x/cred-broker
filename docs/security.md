# 安全模型与部署要求

## 能防什么

目标是让 AI 在日常调用中不接触明文凭证，并将每次使用限制到一个连接、明确的工具、明确的资源和短 TTL。新授权不支持通配范围。无效、到期、撤销或已断开连接的 token 均不能开始新调用。已接纳请求仍可完成，撤销不能收回已返回的数据，也不能取消已执行的远端操作。重签发是旧 grant 撤销加新 grant 创建的原子替换；新 token 只在响应中出现一次。

AES-256-GCM 加密连接 settings 和 secret，AAD 绑定连接 ID。Bearer 只存哈希，管理会话不能当作 MCP token。连接清单不返回 secret，工具发现只公开授权范围；结果对已知凭证值及敏感键脱敏，错误保持泛化。脱敏不是所有业务数据的匿名化，也不应把未知敏感正文视作已清理。

## 不构成硬隔离

本机 AI 以 dong 运行，而 dong 拥有 passwordless sudo 和 Docker 权限，可以提权读取 broker 的数据库和主密钥。独立系统用户、文件权限与 systemd 沙箱减少误访问，但不能抵御该管理员身份的恶意程序。

要实现相对于本机 AI 的凭证隔离，应将 broker 和凭证部署在独立云主机，并且不给本机 AI 云主机管理权限、SSH 密钥或可读主密钥的其他通道。仅把测试环境放到云端不改变生产本机的隔离能力。

源码默认回环监听；Compose 容器内部监听 0.0.0.0，但只将宿主机回环端口发布出去。本机既有 socket-proxyd 明确开放 LAN 地址与旧 Tailscale 地址，LAN 无需 Tailscale。此处 HTTP 没有 TLS，登录秘密、Cookie、MCP Bearer 和敏感结果仍受链路窃听/篡改风险影响。v0.3.1 第一批没有部署生产 TLS，不能把 Host/Origin 修复描述为传输加密。[LAN HTTPS ADR](adr/0001-lan-https.md) 提供未部署模板、证书信任与切换门禁。

所有 HTTP 路由统一校验精确 Host/Origin。允许来源由监听/回环维护地址、CB_ALLOWED_ORIGINS 精确 JSON 数组及可选 CB_PUBLIC_ORIGIN 构成，不支持通配。Host 不能反向生成白名单；缺失/重复 Host、重复/畸形/null Origin 拒绝。无 Origin 的原生客户端仍需通过 Host 与认证。HTTPS 的 CB_PUBLIC_ORIGIN 会强制 Secure Cookie，影响旧 HTTP 浏览器登录，迁移前需决定兼容入口。

正式 Python 入口关闭 Uvicorn proxy_headers，不信任客户端提供的 Forwarded/X-Forwarded-*。TCP 代理使应用仅看到代理 peer，因此不能声称逐设备登录限流。反向代理必须覆盖 Host 并清除客户端转发头；任何未来可信客户端 IP 支持需另行配置和验证。

最外层 ASGI 中间件对 JSON/form/chunked 统一累计实际字节，默认最多 65536，配置范围 1024–65536；整个请求体读取默认 10 秒，配置范围大于 0 且不超过 60 秒。Content-Length 不是唯一依据，超限 413、到期 408。它尚未解决整个 provider 动作 deadline、全局并发准入、子进程组回收及最终响应预算；这些是后续任务。

MCP 配置片段只应包含服务 URL 和 token 占位符。管理员令牌不能作为 MCP Bearer；实际网关 token 只能放入客户端的安全 secret 存储或短期环境变量。ready 诊断接口需要管理员认证，返回的 component 状态不应被当作外部 provider 健康或公网 readiness 证明。

## 不可信内容与 prompt injection

邮件、仓库内容、日志和远程输出均是不可信数据。凭证托管不会阻止 prompt injection，也不会替 AI 判断某条指令是否代表用户真实意图。攻击者仍可能诱导 AI 使用已授权工具读取或外传授权范围内的数据。按任务选择资源和工具、使用短 TTL，并把读出的内容当数据而非新指令；不得因为工具由 broker 提供就信任返回文本。

## 各 provider 的最小权限

- SSH：只提供预设诊断和配置范围内的服务状态查询，固定 host key，禁用交互认证和转发。远程账号应为低权限专用用户，不要配全局 sudo；可配合 authorized_keys 强制命令和服务端限制。固定命令仍可能读到敏感日志或系统信息。
- Instabox：读 key 与可选 diagnostic_key 分开。上游机器 write 权限可能包含广泛执行能力；broker 仅提供固定 uptime/disk/memory 诊断，未配置 diagnostic_key 不公开诊断工具。这是 broker 的软件收窄，不会降低原始 key 在上游的权限，因此原始 key 必须只由 broker 持有。
- GitHub：优先使用细粒度只读、限定仓库的凭证。broker 的仓库资源限制不能替代上游 key 自身的最小权限。
- Gmail：管理员使用自己持有的 Google OAuth 客户端完成授权，不接收普通 API 手填 refresh token。使用 gmail.readonly，不提供发信或修改邮件工具。gmail.readonly 是 Google restricted scope；对外分发或生产使用可能要求 Google 验证，服务端存储/传输受限数据也可能触发额外评估。测试模式的用户限制、refresh token 生命周期和 Google 政策需由部署者核对，不能把一次本地授权成功等同于完成验证。

## 启动与密钥配对

已有存储在 schema/journal 写入和监听之前，以正常只读 SQLite 事务验证。密钥必须是严格 32 字节二进制，不 strip；key/db 必须归服务用户所有且为 0400/0600。所有已有 legacy/scoped 记录须通过 AES-GCM 与 JSON 结构校验，未改变 nonce、AAD 或密文格式。缺 key/短 key 在打开数据库前失败；错 key、坏密文、缺表/列或部分安装均保留原文件并拒绝启动，不能生成替代 key、自动修权限或删除坏库“修复”。

仅 key/db 均不存在才初始化。文件私有暂存、fsync、无覆盖发布；两文件发布中断留下部分安装时明确失败并要求恢复配对。空库没有认证过的密文，key_pairing=unproven；已验证为 verified。它是启动时的配对快照，不是每次 readiness 都重新验证数据库，也不是正式 schema 版本证明。

live WAL 预检始终使用正常 mode=ro，不用 immutable 忽略最新 WAL。SQLite 读者可能创建空 sidecar 或触及 SHM 读者元数据；这不是业务写入或 WAL checkpoint。只有明确独立、无 sidecar 的备份才适合 immutable 校验。

## 审计和兼容风险

本地连接创建、断开、授权创建、重签发和撤销与对应审计在同一事务中提交；审计失败回滚本地业务变更。新审计还记录工具调用和连接测试的有限字段。远端动作与 SQLite 不具备原子提交：动作接纳后若结果处理或审计无法记录，使用 outcome_unknown、retryable=false，MCP 标记 isError；不得自动重试或宣称远端一定未执行，也不承诺 exactly-once。首次签发和重签发均保护一次性 token 窗口；网络失败后先核对授权清单，不能直接重复签发。管理员事件使用 admin 标识且 resource 为空，不记录 settings、secret、token、正文或参数，最多 5000 条，管理页面查询最多 200 条。CSV 导出同样只投影 `ts/client/action/connection_id/resource/outcome` 六列，查询窗口限制为 31 天，并对可能被表格软件执行的公式前缀加单引号；它不是不可篡改日志，也不覆盖所有管理、OAuth 和拒绝认证事件。

`python -m cred_broker doctor` 是只读本地检查。它不会打印管理员令牌、主密钥或数据库内容；它报告配置是否存在、权限是否符合 0400/0600、OAuth 三项配置是否齐全。仅对没有辅助文件的独立 SQLite 执行 immutable quick_check 及共享密文/JSON 校验，有 WAL/SHM/journal 时明确跳过完整性与配对检查，避免把在线库当不可变文件读取。doctor 不能证明上游账号可用，也不能替代连接测试。

浏览器会话不再携带长期管理令牌。服务端只在内存保留随机会话的哈希和最长 8 小时到期时间，注销后旧 cookie 不能重放，服务重启后全部失效。管理 API 的 `X-Admin-Token` 仍具有全权管理员能力。每个来源每分钟最多尝试登录 8 次、实例合计最多 64 次；超限返回 429，限流状态随重启清空。代理后的用户可能共享来源额度，需要在可信反向代理处自行设置适当的访问控制。

`backup-check` 只读核验独立数据库的完整性、业务表必需列和已有密文的 AES-GCM/JSON 结构。缺失配对主密钥、损坏密文或非独立 WAL 快照均拒绝；空数据库无法证明密钥配对。它不会执行数据恢复，也不等同于目标版本启动、迁移和上游账号验收。

旧 API 默认关闭。`CB_ENABLE_LEGACY=true` 仅用于临时兼容，旧动作通配及 SSH/HTTP 路径的授权边界比新工具资源模型宽。新旧 token 与存储相互隔离；启用旧入口并不使旧授权自动受到新资源清单约束。应明确迁移并在完成后关闭。

备份必须同时保护数据库和主密钥，不能提交 Git、发送给 AI 或上传到 staging。若凭证可能泄露，应在上游撤销/轮换，同时撤销 broker grant；仅删除本地连接无法撤销上游仍有效的凭证。

## 本批范围

v0.3.1 仅对应计划 T00–T03 及本次发布所需证据门禁。正式 schema/key 版本、provider 契约重整、全局执行预算、分页/保留机制等尚未实施，不把整份路线或真实外部账号验收标为完成。发布结论以 [验收记录](validation.md) 为准。
