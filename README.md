# Cred Broker · 授权中心

一个可自部署的开源授权网关：连接 Gmail、SSH、GitHub 和 Instabox，按资源、工具和有效期把权限交给 AI。服务负责使用真实凭证，AI 获得的是可撤销的短期网关令牌。

首版面向个人和小团队的单实例部署。管理台为简体中文，前端无需构建、无需 CDN；提供标准 MCP Streamable HTTP，使用 MIT 许可证。不包含模型账号或模型 API 授权。

## 已实现

| 连接 | 身份方式 | 可授权工具 |
|---|---|---|
| Gmail | 自己的 Google OAuth 客户端、浏览器同意 | 搜索邮件、读取正文；仅 `gmail.readonly` |
| SSH | 专用私钥、固定主机公钥、非 root 账号 | 指定 systemd 服务的状态与最近日志 |
| GitHub | GitHub App 安装与 RSA 私钥 | 选定仓库的文件、Issue、PR，只读 |
| Instabox | 官方 API Key | 指定机器的状态；可选独立密钥开放运行时长、磁盘和内存诊断 |
| 飞书、Instagram | 尚未接入 | 目录中显示图标和「暂不支持」，无法建立连接 |

四个页面覆盖连接目录、我的连接、AI 授权和操作记录。一个授权绑定一个连接、具体资源、具体工具和有效期。令牌仅在签发时显示一次；撤销后拒绝新请求，断开连接会同时撤销关联授权。

## 用 Docker Compose 部署

需要 Docker Engine 和 Compose v2。首次构建需要访问 Docker Hub、PyPI 和 npm registry；运行时只访问所连接的上游服务。

```bash
git clone https://github.com/gnod-x/cred-broker.git
cd cred-broker
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
# 把生成的随机值填入 .env 的 CB_ADMIN_TOKEN。
docker compose up -d --build
docker compose ps
```

浏览器打开 **http://localhost:8765**，用刚设置的管理令牌登录。Compose 默认只把端口映射到宿主机回环地址。远程访问可使用 SSH 转发：

```bash
ssh -N -L 8765:127.0.0.1:8765 your-user@your-server
```

需要域名时，在可信 HTTPS 反向代理后部署，同时让代理访问内部端口。只信任实际代理的转发头；不要无条件信任公网客户端传来的 `X-Forwarded-*`。管理员页面与 MCP 共用服务端口，使用不同认证。

镜像以 UID/GID 10001 运行，根文件系统只读、不挂载宿主机 home、Docker socket 或 SSH agent，数据存在命名 volume `broker-data`。镜像附带校验 SHA-512 后安装的官方 Instabox CLI；不会导入宿主机现有 Instabox 登录。

Linux/systemd 部署可用 `sudo bash deploy.sh`，默认安装到 `/opt/cred-broker`。需要 Python 3.11+、venv、OpenSSH client 和 curl；脚本生成独立服务用户、首次管理令牌及服务单元。该脚本尚不替你配置域名或 HTTPS。

## 连接服务

### Gmail

1. 在自己的 Google Cloud 项目中启用 Gmail API，配置 OAuth consent screen，创建 Web application 类型的 OAuth client。
2. 测试状态下，把需要连接的邮箱加入测试用户。在客户端中登记精确回调地址，例如 `http://localhost:8765/admin/oauth/google/callback`。
3. 在 `.env` 中同时设置 `CB_GOOGLE_CLIENT_ID`、`CB_GOOGLE_CLIENT_SECRET`、`CB_GOOGLE_REDIRECT_URI`，然后运行 `docker compose up -d`。
4. 在连接目录选择 Gmail，通过 Google 页面同意只读权限。

采用随机 state、浏览器绑定 cookie、PKCE S256 和十分钟时限；回调只能使用一次。客户端 secret、refresh token 和连接配置在数据库中加密，access token 只用于当前调用。发信、删除邮件和附件下载未开放。

Gmail 正文只读权限属于 Google 的 restricted scopes。供自己部署与使用的测试应用仍受 Google 测试用户、令牌期限等规则约束；共用的公开 OAuth 应用需要按 Google 要求完成验证，可能涉及额外评估。请参考 [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes) 和 [OAuth verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)。测试状态通常会影响 refresh token 寿命，失效后重新连接。

### SSH 与普通云主机

先创建只能读取目标服务状态和必要日志的专用非 root 账号，安装它的公钥。管理台填写主机、端口、账号、私钥、目标主机公钥及允许查询的 `*.service` 名称。

主机公钥格式为 `ssh-ed25519 AAAA...` 等公钥本体；通过可信控制台或已有可信连接核对指纹，不能只因 `ssh-keyscan` 输出了一个 key 就信任它。运行中始终严格验证固定主机公钥，并禁用 SSH agent、交互认证及转发。

只执行固定的 `systemctl show` 和 `journalctl` 查询，不接收 shell 字符串。日志仍可能包含业务数据，应在目标机限制账号的读取权限。

### GitHub

创建自己的 GitHub App，设置需要的只读 repository permissions：Contents、Issues、Pull requests，以及必要的 Metadata。安装时只选择允许访问的仓库，再生成 App RSA 私钥。

在管理台填写 App ID、Installation ID、`owner/repository` 清单和 PEM 私钥。每次动作申请只限当前仓库和权限的短期 installation token，并核对仓库属于登记的 installation。无需配置 GitHub OAuth 回调或接收 webhook。

首版不提供 push、建仓库、提交 Issue/PR 或合并权限。你的 GitHub SSH 登录也不自动成为网关的 GitHub App 连接。

### Instabox

使用官方 CLI 在自己的登录环境中创建查询专用 key，避免使用 CLI 默认的宽权限：

```bash
instabox keys create --name cred-broker-read --scope machines:read --expires-in-days 30
```

把 key 和允许的机器 ID 填入管理台。若需要固定诊断，再单独创建 `machines:write` key，填入可选的诊断密钥：

```bash
instabox keys create --name cred-broker-diagnostics --scope machines:write --expires-in-days 30
```

Instabox 把远程执行包含在 `machines:write` 中；网关进一步限制到已登记的机器和 `uptime/disk/memory` 三种预设操作。查询始终用 read key，诊断只用 diagnostic key。首版不开放启停、删除、创建机器、文件传输或发布公网端口。

## 让 AI 调用

在「AI 授权」选择连接、资源和工具，设置有效期后签发。默认界面提供 15 分钟到一天，服务端最长一天。不支持通配资源，也不把管理员令牌发给 AI。

MCP 地址为 `http://localhost:8765/mcp`，认证头为 `Authorization: Bearer <本次签发的网关令牌>`。MCP 客户端的配置方式因产品而异，可在支持 HTTP MCP 和自定义 Bearer 的客户端中填入这两项。客户端若仅支持 stdio，需要另配可信的 HTTP bridge；内置的是标准 HTTP 入口。

下面是协议层示例，网关令牌由环境变量提供：

```bash
curl -sS http://localhost:8765/mcp \
  -H "Authorization: Bearer $CRED_BROKER_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

工具的 `resource` 参数枚举只包含该授权的资源。例如已授权 Instabox 状态查询时：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "instabox_describe",
    "arguments": {"resource": "your-authorized-machine-id"}
  }
}
```

撤销不能撤回已经发到远端的动作；已接纳的在途请求可能完成。后续请求会立即拒绝。首版单实例、单 worker，不声称提供跨进程或分布式撤销。

## 配置、备份与升级

| 配置 | 默认值 / 用途 |
|---|---|
| `CB_ADMIN_TOKEN` | 必填，独立随机管理令牌 |
| `CB_DATA_DIR` | `/var/lib/cred-broker`，数据库和主密钥 |
| `CB_BIND` | 源码运行 `127.0.0.1:8765`；容器内部 `0.0.0.0:8765` |
| `CB_GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI` | 三项同时配置才开启 Gmail OAuth |
| `CB_INSTABOX_CLI` | `/usr/local/bin/instabox`，由管理员指定的可执行文件 |
| `CB_ENABLE_LEGACY` | 默认 false，仅迁移时按需启用旧管理和旧 HTTP 调用 |

备份必须同时包含数据卷里的 `broker.db`、相关 WAL 状态和 `master.key`。最简单的方式是先 `docker compose stop`，再完整备份数据卷到加密存储，完成后 `docker compose start`。主密钥丢失后不能恢复凭证；数据库与主密钥一起泄露则可以解密，文件权限与主机隔离仍然必要。另行妥善保存 `.env`，不要提交到 Git。

升级前备份，记录现用 Git tag 或镜像版本，拉取目标版本后重新构建并启动。回滚同时考虑代码与数据格式，不能假定降级总兼容。新表与原有 cred-broker 的凭证、授权和 token 表隔离，不自动迁移旧授权；开启兼容模式的范围与边界见 [架构说明](docs/architecture.md)。

## 开发与测试

```bash
python3 -m venv .venv
.venv/bin/pip install --no-cache-dir -r requirements-dev.txt
.venv/bin/python -IB tests/test_core.py
.venv/bin/python -IB tests/test_control.py
.venv/bin/python -IB tests/test_providers.py
.venv/bin/python -IB tests/test_google.py
.venv/bin/python -IB tests/test_app.py
.venv/bin/python -IB tests/test_ssh_transport.py
.venv/bin/python -IB tests/test_mcp_sdk.py
```

测试使用隔离数据库、假凭证与 mock upstream。SDK 测试启动临时回环 HTTP 服务器，通过官方 MCP Python 客户端完成握手、发现、调用和撤销验证。真实 Gmail、GitHub App 与 Instabox 账号授权需要部署者提供；mock 通过不代表那些账号已经连接。

浏览器测试需要 `.venv/bin/python -m playwright install --with-deps chromium`，并启动专用 fixture：

```bash
export CB_TEST_FIXTURE=1 CB_TEST_ADMIN_TOKEN=browser-fixture-only CB_ADMIN_TOKEN=browser-fixture-only
export CB_DATA_DIR=/tmp/cred-broker-browser-fixture
.venv/bin/python -m uvicorn tests.browser_fixture:app --host 127.0.0.1 --port 18765
# 在另一终端保留同样的 CB_TEST_FIXTURE / CB_TEST_ADMIN_TOKEN 设置：
.venv/bin/python -B tests/test_browser.py
```

浏览器 fixture 只能用作测试。CI 会执行功能、官方 SDK 与浏览器测试；推送版本 tag 后的容器发布任务依赖这些检查通过，再向当前仓库对应的 GHCR 地址发布 amd64 镜像。

凭证加密不能替代主机隔离，授权也不能自动消除 prompt injection。完整边界与限制见 [安全说明](docs/security.md)。

首版的测试范围和限制见 [验收记录](docs/validation.md)。
