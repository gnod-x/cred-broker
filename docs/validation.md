# 验收记录


## v0.3.1 · 第一批 T00–T03 发布与部署验收

日期：2026-11-25。环境为云机器 three 的隔离目录 /data/workspace/cred-broker-refactor-20261125；只使用合成凭证、临时库、mock provider、独立回环 HTTP/TLS 和专用 Compose 项目。本机生产目录、真实数据库/主密钥、账号授权与既有 LAN/Tailscale 拓扑未作为测试对象。

本节记录隔离候选、公开制品与本机部署的分层证据；公开发布与本机升级已实际完成。以下为第一批范围，不代表整份 17 卡路线完成。

| 验证 | 候选结果 | 范围和限制 |
| --- | --- | --- |
| Python 回归 | 通过 | 14 个脚本以 .venv/bin/python -IB 运行；脚本清单见下，不累计或推测未提供的 unittest 总数 |
| T01 启动与配对 | 通过 | 新安装、缺/短/错 key、坏 legacy/scoped 密文与 JSON、部分安装、权限、并发无覆盖、中断发布、live WAL最新记录；空库unproven |
| T02 HTTP 入口与接收 | 通过 | 精确 Host/Origin、伪造转发头、实际请求体累计与读取期限；隔离真实回环/原样TCP代理，不修改生产入口 |
| T03 本地事务与未知结果 | 通过 | 本地状态/审计同事务、故障回滚、一次性token窗口、OutcomeUnknown与MCP结构化不重试提示；不保证远端exactly-once |
| Chromium | 通过 | 完整fixture流程，11张截图；fixture已清理，未使用真实账号 |
| 同一候选Compose镜像 | 通过 | verify_release_image.py验收下列准确image ID，含加固、doctor、加密、SQLite备份、原备份token经HTTP恢复/调用/撤销、重启持久性；合成provider |
| 隔离TLS fixture | 通过 | 13项检查，使用指定cafile及主机名校验，未全局跳过TLS验证；这不是生产TLS启用或ADR代理模板已部署的证据 |
| 公开tag / CI / GHCR | 通过 | v0.3.1 指向 5cda69c；主分支 CI 45489438195、标签发布 45489837995 成功；匿名拉取固定摘要后独立验收通过 |
| 本机 systemd / 面板 | 通过 | 已运行 v0.3.1，三地址登录/注销/拒绝重放及鉴权边界通过；面板原生检查 healthy/HTTP200，保留原账号、会话、4分组/23服务 |

候选镜像 image ID：

```text
sha256:95af0e9e67d1ba2c5cede9bdffcec288a49aef5ff49142e6c13a0dc4fb0548bc
```

它是本地候选镜像的配置/内容标识，不是可用于公开registry拉取的manifest digest。发布流水线应先验证该次构建的准确候选，推送后拉回固定digest并核对image ID，再执行隔离验收。镜像内不能包含其自身最终OCI digest。

公开镜像为 `ghcr.io/gnod-x/cred-broker:0.3.1`，固定摘要 `sha256:8bf8af58f34fd5e4afc178436ad29ca219db4fbb558961149cb5992ff7e5de7c`；OCI version 为 `0.3.1`、revision 为 `5cda69c73bbf4a2ce4f2fea5cbddb8ac05368844`。发布流水线先验证自己构建的同一候选再推送并拉回摘要复验；three 另用空 DOCKER_CONFIG 匿名拉取固定摘要，以唯一随机 Compose 项目再次通过加固、原快照 token HTTP 恢复、模拟调用、撤销和重启持久性验收。候选本地 image ID 与公开 registry digest 是不同阶段的标识，不要求相同。

公开证据：[main CI](https://github.com/gnod-x/cred-broker/actions/runs/45489438195)、[tag test](https://github.com/gnod-x/cred-broker/actions/runs/45489838017)、[container release](https://github.com/gnod-x/cred-broker/actions/runs/45489837995)。

本机已部署至 `/opt/cred-broker-releases/v0.3.1`：84 个安装源文件逐一与云端 manifest 核对，源归档 SHA256 为 `4176c73f35fdad8f9e555b059283d5cb4d52ca38ef3584c9e07ccd1e27ecd0cc`；Python 3.12.3 运行时依赖检查、12项启动回归和随机回环端口/临时库的真实 HTTP 登录注销通过。生产 venv 未额外安装 httpx 等测试依赖；ASGI 开发测试仍以云端和 CI 为证。本机正式切换后回环/LAN/Tailscale 三个地址的健康、认证 ready、登录注销和旧会话401、未认证 MCP/管理401、legacy404、恶意 Host400/Origin403通过。程序维持 broker 身份、单进程、192 MiB 和原 systemd 隔离；doctor 以服务身份通过，live WAL 项按设计跳过。

停写备份与最终报告位于本机 root 私有 `/var/backups/cred-broker/pre-v0.3.1-20261125-arejnv47/`。首次备份门禁因导出副本继承 WAL 模式而停止并恢复旧程序；随后仅对导出目标用 SQLite 切为 DELETE 模式，独立预检通过后重新停写备份并完成切换，原始材料保留。生产环境文件与主密钥哈希未变，各表记录与停写快照一致；没有加密连接记录，配对为 `unproven`，不声称已用生产密文证明 key 配对。面板 ID27 的展示与健康 URL 保持 LAN，实际 API 与原生健康检查通过；临时维护会话已精确删除。验证覆盖同机三个地址和面板容器，未替代另一实体 LAN 设备或真实上游账号验收。

14个脚本：test_core.py、test_control.py、test_providers.py、test_google.py、test_app.py、test_ssh_transport.py、test_mcp_sdk.py、test_admin_auth.py、test_doctor.py、test_backup_check.py、test_release_notes.py、test_startup.py、test_http_security.py、test_release_regressions.py。浏览器和容器脚本另列，不重复计入。

本批保留的边界：

- 本机仍使用此前HTTP LAN入口，LAN设备无需Tailscale；链路TLS风险仍在，[HTTPS ADR](adr/0001-lan-https.md)未执行生产切换。
- 启动只读WAL检查可以触及SHM读者元数据或创建空辅助文件；独立backup-check才承诺无sidecar输入的immutable检查。
- key_pairing=unproven是空库无法证明配对，不应改写成verified；schema current不是已实现正式schema/key版本。
- 入口读取期限不等于整个provider动作deadline；全局准入、输出预算与其他T04–T14任务仍延期。
- 合成恢复与TLS回环不证明真实Gmail/GitHub/SSH/Instabox账号、DNS/CA生产信任或特定第三方客户端已验收。


## v0.2.1 · TLS 反向代理登录修复

日期：2026-11-21。修复 HTTPS 反向代理把请求转发为内部 HTTP 后，管理登录 POST 因外部 `Origin` 与应用内部来源不同而返回 403 的问题。新增 `CB_PUBLIC_ORIGIN` 精确配置；管理写请求规范化比较配置来源与内部来源，不依赖任意 `X-Forwarded-*`。外部 HTTPS 配置下登录与注销 Cookie 保持 Secure。

| 验证 | 本地结果 | 范围 |
|---|---|---|
| 管理 API | 通过 | 代理 Origin 登录、管理 POST 来源、恶意来源与畸形来源拒绝、Secure 登录/注销 Cookie、非法配置拒绝 |
| 核心与 provider 回归 | 通过 | `test_core.py`、`test_control.py`、`test_providers.py`、`test_google.py` |
| 官方 MCP SDK | 通过 | `test_mcp_sdk.py` 回环协议验证 |
| GitHub CI / 镜像流水线 | 通过 | [测试](https://github.com/gnod-x/cred-broker/actions/runs/44789721802)、[v0.2.1 镜像构建](https://github.com/gnod-x/cred-broker/actions/runs/44789721808)；本轮未改既有 staging |

`CB_PUBLIC_ORIGIN` 必须是无路径的 HTTP(S) origin，例如 `https://broker.example.com`。通过 HTTPS 反向代理部署时，在 Compose 环境文件设置该值。历史流水线状态已按公开运行记录核对；v0.3.0 使用下面的独立环境验收。

## v0.3.0 · 接入与运维验收

日期：2026-11-21。环境为 Instabox `three` 上的隔离源码副本、合成凭证、临时数据库和独立 Compose project `cred-broker-test-v030`；容器只映射 `127.0.0.1:18766`，浏览器 fixture 只监听 `127.0.0.1:18765`。未修改既有 `cred-broker-staging`，未读取或复制本机生产数据与登录。

| 验证 | 结果 | 范围 |
|---|---|---|
| Python 回归 | 通过 | 10 个脚本；44 个 unittest 场景及旧核心 27 个断言；覆盖控制层、provider、OAuth、管理 API、SSH、官方 MCP SDK、管理认证和本地预检 |
| 管理 ready | 通过 | 未认证 401；版本/存储/已加载 key/schema 状态；不返回路径与秘密；缺表 schema 标记 incomplete |
| 连接最近测试 | 通过 | 初始未测试、成功/失败时间与状态；provider 回应正文和凭证不进入状态存储 |
| 授权重签发 | 通过 | 同范围原子替换、旧 token 拒绝、新 token 一次返回、过期可重签、撤销拒绝、并发仅一个替代、失败事务回滚 |
| Provider schema | 通过 | 四个支持 provider 的字段与约束，未知/计划 provider 拒绝；密钥 writeOnly，动态范围由服务端实施 |
| 审计 CSV | 通过 | 管理员认证、日期与 31 天上限、固定六列、公式防护；Chromium 下载、今天的结束时间、超大日期窗口提示和假凭证排除 |
| 管理会话 | 通过 | cookie 使用独立随机值、哈希存储、轮换/注销/到期/重启失效，最多 128 会话；每来源与实例登录速率窗口 |
| 本地 doctor | 通过 | 新安装不创建目录；坏权限/密钥长度/SQLite/缺 key/配置错误；关闭的 WAL 库不产生辅助文件，运行中的 WAL 明确跳过完整性检查 |
| 备份预检 | 通过 | 独立快照完整性、必需表/列、legacy/scoped 密钥配对及 JSON 结构；缺件/坏 key/坏密文/旁路文件拒绝；不写输入文件 |
| Chromium 与响应式 | 通过 | 登录、诊断、四页导航、连接测试、MCP 配置、复制/关窗、重签发/撤销/删除、XSS；375/768/1024/1440px，无页面溢出或控制台错误 |
| 独立 Compose | 通过 | UID10001、只读根、CapDrop、no-new-privileges、256MiB、回环端口；容器内 doctor、SQLite backup+配对检查、恢复解密、重启后 grant 可用、撤销和断开拒绝 |
| GitHub CI、v0.3.0 tag 与 GHCR | 通过 | 标签指向 `c9568f2`；[主分支 CI](https://github.com/gnod-x/cred-broker/actions/runs/44850927046) 与[标签发布流水线](https://github.com/gnod-x/cred-broker/actions/runs/44869259039) 均成功，发布任务在功能、Chromium 和 Compose 检查后推送 amd64 镜像 |
| 公开镜像拉取与运行 | 通过 | `three` 匿名拉取固定摘要，在独立 `cred-broker-test-published-v030` 项目和 `127.0.0.1:18767` 运行 `test_container.py`；加固、doctor、备份配对及恢复解密、重启持久性、撤销和断开均通过，认证 ready 返回 `0.3.0` |

功能脚本：`test_core.py`、`test_control.py`、`test_providers.py`、`test_google.py`、`test_app.py`、`test_ssh_transport.py`、`test_mcp_sdk.py`、`test_admin_auth.py`、`test_doctor.py`、`test_backup_check.py`。浏览器与容器分别执行 `tests/test_browser.py`、`tests/test_container.py`。JavaScript `node --check` 与本地 Python 语法、`git diff --check` 通过。

公开镜像为 `ghcr.io/gnod-x/cred-broker:0.3.0`，索引摘要 `sha256:6ddf144f7a2580806498f0e46985f83616e70be6ac6649d2758374613b104963`，Linux amd64 manifest 摘要 `sha256:22b6b90ca5cecfd021bb156c494e2659980d3936a38fb5520583c5315fce4570`。拉取后核对 OCI version 为 `0.3.0`、revision 为 `c9568f2f8c122c9944beaf4f26d2f5e471c02845`，没有用本地构建镜像代替公开制品。验收结果留在 `three:/data/workspace/cred-broker-v030-release-evidence/`；公开镜像验收的临时容器、数据卷和源码已删除。既有 staging 的容器 ID、镜像、启动时刻和健康状态未变，本机历史生产服务没有升级。

这些结果覆盖合成 provider、真实回环协议、浏览器和本地构建容器，未验证用户的真实 Gmail/GitHub/SSH/Instabox 账号或特定第三方客户端兼容性。`ready` 不是上游可用性探针，`backup-check` 不是完整恢复；实际部署仍需自己的连接与恢复验收。

## v0.2.0 · 界面重构验收

本轮重做登录页与管理工作台的布局、信息层级和交互反馈，并保持连接、授权、MCP 和凭证安全边界。以下项目已在 three 的独立 staging 环境完成：

| 项目 | 当前记录 | 验收范围 |
|---|---|---|
| UI / 浏览器 | 通过 | 登录/退出、四页导航、搜索分类、连接创建/测试/删除、授权/复制/撤销、XSS 字符串安全、控制台无错误 |
| 键盘与无障碍 | 通过 | Escape 关闭、焦点恢复、状态提示、notice 自动关闭；浏览器流程无控制台错误 |
| 响应式 | 通过 | 375/768/1024/1440px，无页面横向溢出；本地 Manrope 字体加载 |
| 安全回归 | 通过 | token 不进入 localStorage/sessionStorage，XSS 字符串安全，登录/退出流程通过 |
| 功能与容器 | 通过 | 7 个核心 test 脚本、Compose hardening/health/auth/persistence/storage/revoke/disconnect |
| 发布 | 通过 | `v0.2.0` tag、main / release CI 和 GHCR `0.2.0` 公开镜像均已完成；镜像索引含 Linux amd64 |

使用隔离 fixture 和合成数据；未连接真实 Gmail、GitHub、SSH 或 Instabox 外部账号。测试环境为 three，容器先以本地构建镜像验收，发布后又切换到公开的 `ghcr.io/gnod-x/cred-broker:0.2.0` 做健康与容器最小回归，健康地址为 `127.0.0.1:8765`。浏览器测试在 `test-results/` 生成截图，目录为临时产物；代表性截图已纳入 `docs/images/console-v020.png`。GHCR manifest digest 为 `sha256:2614b4e5b37ac99d9a81ebcb0d4867e1b81551ec4b42ecfaf302b65c581b190b`。

发布证据：[main 测试](https://github.com/gnod-x/cred-broker/actions/runs/44774747022)、[v0.2.0 容器发布](https://github.com/gnod-x/cred-broker/actions/runs/44775054799)。仓库已推送 `v0.2.0` tag；当前 workflow 不自动创建 GitHub Release 页面。

## v0.1.0 · 历史验收


日期：2026-11-20。环境：独立 Instabox Linux 测试机，Python 3.13.5、Docker Engine 29.8.2、Compose 5.6.0。测试未访问用户的真实邮箱、GitHub 仓库、SSH 账号或 Instabox 机器授权。

| 验证 | 结果 | 范围 |
|---|---|---|
| 历史核心回归 | 27 个断言通过 | 加密、令牌、撤销、旧策略与 SSH key 加载 |
| 新授权控制 | 1 个组合场景通过 | 精确范围、撤销/断开、审计、协议输入与凭证反射 |
| Provider | 6 个场景通过 | 仓库/主机/机器范围、固定命令、两类 Instabox key 与参数/输出边界 |
| Gmail / OAuth | 8 个场景通过 | state、cookie 绑定、PKCE、只读 scope、过期/重放、邮箱核验与正文脱敏 |
| 管理 API | 2 个场景通过 | 页面、来源检查、连接与授权流程、存储无明文、二进制主密钥重载 |
| OpenSSH 真实传输 | 1 个场景通过 | 系统 SSH client 连接临时 Paramiko server，含错误主机密钥和未授权 unit 拒绝 |
| 官方 MCP SDK 2.3.0 | 1 个场景通过 | 真实回环 HTTP 握手、发现、调用、越权与撤销后 401 |
| Chromium 浏览器 | 完整流程通过 | 登录、四页、图标、创建/测试/授权/复制/撤销/删除，无控制台错误 |
| 手机布局 | 四页通过 | 375×812，无页面横向溢出 |
| Compose 容器 | 完整流程通过 | UID10001、只读根目录、无 capabilities、回环端口、限内存、健康与认证 |
| 持久化与恢复 | 通过 | 重启后连接和 token 可用；独立 SQLite backup + master key 可恢复解密 |
| 依赖检查 | 通过 | 安装依赖的 pip-audit 检查未发现已知漏洞；不等同于完整安全审计 |

脚本与复现命令见 README 和 `tests/`。浏览器和容器测试仅允许明确指定的隔离 fixture/staging；容器测试在任何重启前核验 Compose project，避免把生产实例当验收对象。

真实 Google OAuth consent、GitHub App installation 与外部服务账号联调尚需部署者配置自己的账号；这些测试不能证明账号已经建立连接。systemd 部署脚本完成语法检查，当前运行验收采用 Docker Compose。

GitHub 发布验收也已完成：原始版本代码通过 [功能、浏览器与容器 CI](https://github.com/gnod-x/cred-broker/actions/runs/44755615571)，[镜像发布流水线](https://github.com/gnod-x/cred-broker/actions/runs/44756325668) 按 `v0.1.0` 标签重新验收并构建成功。`ghcr.io/gnod-x/cred-broker:0.1.0` 支持 Linux amd64，匿名 registry manifest 请求已返回 200，索引摘要为 `sha256:5d79dc280e4b03255e4fff2691223109317c138149be01b43afab17793f1f182`。
