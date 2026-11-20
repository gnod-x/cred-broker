# 验收记录

## v0.2.0 · 界面重构验收

本轮重做登录页与管理工作台的布局、信息层级和交互反馈，并保持连接、授权、MCP 和凭证安全边界。以下项目已在 three 的独立 staging 环境完成：

| 项目 | 当前记录 | 验收范围 |
|---|---|---|
| UI / 浏览器 | 通过 | 登录/退出、四页导航、搜索分类、连接创建/测试/删除、授权/复制/撤销、XSS 字符串安全、控制台无错误 |
| 键盘与无障碍 | 通过 | Escape 关闭、焦点恢复、状态提示、notice 自动关闭；浏览器流程无控制台错误 |
| 响应式 | 通过 | 375/768/1024/1440px，无页面横向溢出；本地 Manrope 字体加载 |
| 安全回归 | 通过 | token 不进入 localStorage/sessionStorage，XSS 字符串安全，登录/退出流程通过 |
| 功能与容器 | 通过 | 7 个核心 test 脚本、Compose hardening/health/auth/persistence/storage/revoke/disconnect |
| 发布 | 未完成 | v0.2.0 本地 staging 镜像已验证；GitHub tag、CI 发布记录和公开镜像仍待完成 |

使用隔离 fixture 和合成数据；未连接真实 Gmail、GitHub、SSH 或 Instabox 外部账号。测试环境为 three，容器使用本地构建的 `ghcr.io/gnod-x/cred-broker:0.2.0`，健康地址为 `127.0.0.1:8765`。浏览器测试在 `test-results/` 生成截图，目录为临时产物；代表性截图已纳入 `docs/images/console-v020.png`。不要把本地 staging 验收描述为 GitHub 或公开镜像发布成功。

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
