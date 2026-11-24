# ADR 0001：LAN HTTPS 的部署与信任边界

日期：2026-11-25。状态：方案与模板，未选择生产名称/CA，未在生产启用。

## 背景与约束

既有管理入口为 http://192.168.101.240:8765/admin，同时保留旧 Tailscale 地址。LAN 设备无需连接 Tailscale。不能擅自关闭现有入口、开放公网、申请新公网域名或轮换真实凭证。

HTTP 会让登录秘密、会话 Cookie、MCP Bearer 和结果在 LAN 链路中缺少 TLS 保护。Host/Origin、短 TTL 和磁盘加密都不能替代传输加密。此次第一批先修入口信任与读取边界，不将生产 HTTP 描述为已经安全迁移。

## 决策方向

保留 broker 回环监听，以专用 TLS 反向代理提供精确名称；所有浏览器和 MCP 客户端必须信任签发 CA 并验证主机名。证书可以来自部署者选择的内部 CA 或合法持有域名的受信 CA，最终名称、DNS 和客户端信任方案仍需部署者确认。禁止以关闭证书验证或 ignore_https_errors 作为验收。

broker 继续关闭 proxy_headers，不依赖外部 X-Forwarded-* 决定来源。代理必须覆盖 Host 并清除客户端传入的转发头；保留 Origin 让应用校验。后端看到代理 peer，因此没有真实客户端 IP 级限流保证。需要恢复可信客户端 IP 时，另行定义并测试代理信任链。

## 隔离环境的配置示例

下面的名称、证书路径与地址只是模板，不代表当前存在 DNS 或证书。先在独立测试实例验证，再安排生产切换。示例 Nginx server：

```nginx
server {
    listen 192.168.101.240:443 ssl;
    server_name broker.home.arpa;
    ssl_certificate /etc/cred-broker/tls/server.fullchain.pem;
    ssl_certificate_key /etc/cred-broker/tls/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 64k;
    client_body_timeout 10s;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host broker.home.arpa;
        proxy_set_header Forwarded "";
        proxy_set_header X-Forwarded-For "";
        proxy_set_header X-Forwarded-Host "";
        proxy_set_header X-Forwarded-Proto "";
        proxy_http_version 1.1;
    }
}
```

对应隔离实例设置：

```dotenv
CB_PUBLIC_ORIGIN=https://broker.home.arpa
CB_ALLOWED_ORIGINS=["https://broker.home.arpa"]
CB_MAX_REQUEST_BODY=65536
CB_BODY_READ_TIMEOUT=10
```

代理的 client_body_timeout 是另一层读取约束，不能代替应用的总请求体读取期限。模板未执行代理语法/证书握手验收，不能直接称为已验证部署。

CB_PUBLIC_ORIGIN 为 HTTPS 时浏览器会话 Cookie 强制 Secure，因此不能假定同一实例仍可通过旧 HTTP URL 正常进行浏览器登录。生产切换前必须明确 HTTP 入口是保留仅供受控维护、迁移重定向还是关闭；当前没有授权擅自变更它。

## 切换门禁与回滚

上线前核对 DNS、证书链、每类客户端信任、精确 Host/Origin、Secure Cookie、登录/退出、MCP 鉴权/撤销及无浏览器 Origin 的原生客户端。证书主机名错误、未信任 CA、伪造转发头、恶意 Host/Origin 必须拒绝；测试不能降级证书校验。

更新环境变量后使用 compose up -d --force-recreate 或对应受控重建流程，restart 不会重新加载 Compose 的环境配置。同步面板显示 URL、健康 URL、MCP 配置和运维说明，并核对实际面板 API；不得只更新文档。

本 ADR 不执行任何生产切换。将来切换失败时回到变更前的代理与运行环境配置并重建服务，保留原数据卷、配对主密钥及回滚制品；不能生成新 key 作为恢复方式。
