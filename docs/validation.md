# v0.1.0 验收记录

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

真实 Google OAuth consent、GitHub App installation 与外部服务账号联调尚需部署者配置自己的账号；这些测试不能证明账号已经建立连接。systemd 部署脚本完成语法检查，当前运行验收采用 Docker Compose。GitHub Actions 工作流已写入仓库，云端手动验收通过不代表远端 Actions 已运行成功。发布状态以 GitHub 仓库、CI 与镜像实际存在为准。
