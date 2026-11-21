# cred-broker 项目契约

## 来源与入口

开源源码位于 `/home/dong/projects/cred-broker`，是新的单实例授权中心。历史生产服务 `/opt/cred-broker` 与 `cred-broker.service` 独立存在；只修改源码仓库不会升级生产服务。未经部署任务授权，不把开发源码覆盖到历史生产目录，不复制其数据库、主密钥、环境文件或现有登录到云端。

部署默认使用 `compose.yaml`；Python 入口为 `python -m cred_broker`。管理台 `/admin`、管理 API `/admin/api/v1`、标准 MCP `POST /mcp`、存活检查 `/healthz`。契约详细行为见 [README](README.md)、[架构](docs/architecture.md)、[安全边界](docs/security.md)。

## 必须保留

- 只集成外部工具服务，不做模型账号授权。Gmail、SSH、GitHub、Instabox 可建立连接；飞书和 Instagram 仅显示未支持图标，不能伪装为可调用。
- 不存在向 AI 返回原始凭证的接口。连接 settings 与 secret 用 AES-256-GCM 加密，AAD 绑定 connection ID；主密钥是严格 32 字节的二进制文件，不能 strip。主密钥文件 0400、数据库 0600。
- 一个新授权绑定单个连接、精确资源与工具、最长一天 TTL。令牌只返回一次，库存 SHA-256；管理清单、工具清单和审计不含令牌或 secret。新的 scoped 表与 legacy 表隔离，不交叉认证。
- 撤销或断开后拒绝所有新接纳调用。已经接纳的在途动作可能完成；不要声称能撤回远端动作。只运行一个 worker；锁不跨进程。
- 管理页 cookie HttpOnly + SameSite=Strict，HTTPS 时 Secure。管理写请求检查浏览器来源；MCP Bearer 与管理认证分开。页面禁止外部脚本、CDN 和不可信 HTML 注入。Google callback 使用浏览器绑定的一次性 state、PKCE、TTL、固定配置回调地址与独立 Lax cookie。
- 管理 cookie 只能使用随机短期会话；只在单进程内存存哈希，注销后拒绝重放、重启失效，不能再直接保存长期管理令牌。保留管理 API 的独立 `X-Admin-Token`，登录尝试限制和会话库存必须有界。
- Gmail 只请求 `gmail.readonly`，每次操作核对 profile 邮箱，不开放发信或附件下载。GitHub App 每次签仓库和动作权限受限的 installation token，只读。
- SSH 固定主机公钥与专用非 root 身份，只开放预定义 systemctl/journalctl 查询，service unit 白名单，不接任意 argv 或 shell。禁用 agent、用户配置、跳板和端口转发。
- Instabox 每次使用隔离 HOME、固定官方服务器、明确配置的 API key。查询与诊断分开用 key；diagnostic key 缺失时不发现诊断工具；仅允许登记机器及 uptime/disk/memory 三种固定诊断，不开放生命周期或公网端口操作。
- provider 网络与子进程有限时、输出上限，HTTP 不跟随重定向、不继承环境代理，子进程不继承现有账号凭证。上游异常不给客户端回显，已知凭证不得出现在输出。
- 管理事件与工具调用结果进入 scoped audit，最多保留最近 5000 条、界面最多取 200 条；不记录原始工具参数、邮件/文件/日志正文或凭证。
- Provider schema 不返回已保存值；动态范围和条件工具以服务端为准。CSV 导出仅限有限审计字段、有界日期窗口并处理公式注入。本地 doctor/backup-check 不得创建主密钥或修改输入备份，预检不等同于完整恢复验收。
- legacy 默认关闭，开启需 `CB_ENABLE_LEGACY=true`。旧接口允许更宽的 SSH/HTTP，不能把其保证等同于新 scoped 接口；改动不得静默复用旧授权。
- 容器以 UID/GID 10001 运行，根文件系统只读、独立数据卷、回环端口映射、无 capabilities。systemd 保持 ProtectSystem/ProtectHome/NoNewPrivileges 等限制。主机 root、Docker 管理员与有全权 sudo 的 AI 无法被同机软件可靠隔离。

## 验证与文档

逻辑改动运行 `tests/test_core.py`、`test_control.py`、`test_providers.py`、`test_google.py`、`test_app.py`；认证与运维检查另跑 `test_admin_auth.py`、`test_doctor.py`、`test_backup_check.py`；SSH 变更加真实回环传输 `test_ssh_transport.py`，MCP 变更加官方 `test_mcp_sdk.py`，网页变更加 fixture 专用 `test_browser.py`。依赖在 requirements[-dev].txt。完整命令见 README。

测试只用临时数据库、假凭证和独立回环服务器；不连接用户真实 Gmail/仓库/SSH/Instabox 资源。真实账号验收需用户自行建立连接，报告必须区分 mock、协议联调和真实上游验证。

发布前在隔离云测试环境完成完整测试、Compose 容器启动与重启后状态恢复、secret 不落明文及回滚材料核验；文档须按最终实现重写。不要发布未通过的版本或把本机生产服务当测试对象。构建产物、临时归档和废弃测试数据需清理，保留仍需运行的测试环境与验收材料。
