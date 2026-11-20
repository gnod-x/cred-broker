#!/usr/bin/env bash
# Optional Linux/systemd installation. Docker Compose is the documented default.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then echo "请用 sudo 运行此脚本" >&2; exit 1; fi

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${CB_INSTALL_DIR:-/opt/cred-broker}"
DATA_DIR=/var/lib/cred-broker
ETC_DIR=/etc/cred-broker
ENV_FILE="$ETC_DIR/cred-broker.env"
SERVICE_USER=broker

for executable in python3 ssh curl install systemctl; do
  command -v "$executable" >/dev/null || { echo "缺少 $executable" >&2; exit 1; }
done
id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$DATA_DIR"
install -d -o root -g root -m 0755 "$APP_DIR" "$ETC_DIR"
if [[ "$SOURCE_DIR" != "$APP_DIR" ]]; then
  install -d "$APP_DIR/cred_broker" "$APP_DIR/tools"
  cp -a "$SOURCE_DIR/cred_broker/." "$APP_DIR/cred_broker/"
  install -m 0644 "$SOURCE_DIR/requirements.txt" "$APP_DIR/requirements.txt"
  install -m 0644 "$SOURCE_DIR/tools/install_instabox.py" "$APP_DIR/tools/install_instabox.py"
fi
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then python3 -m venv "$APP_DIR/.venv"; fi
"$APP_DIR/.venv/bin/python" -m pip install --no-cache-dir --upgrade pip==26.3
"$APP_DIR/.venv/bin/python" -m pip install --no-cache-dir -r "$APP_DIR/requirements.txt"
if [[ ! -x /usr/local/bin/instabox ]]; then
  "$APP_DIR/.venv/bin/python" "$APP_DIR/tools/install_instabox.py"
fi

NEW_ADMIN=0
if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  NEW_ADMIN=1
  cat >"$ENV_FILE" <<EOF
CB_ADMIN_TOKEN=$ADMIN_TOKEN
CB_BIND=127.0.0.1:8765
CB_DATA_DIR=$DATA_DIR
CB_ENABLE_LEGACY=false
EOF
  chown root:root "$ENV_FILE"
  chmod 0400 "$ENV_FILE"
fi

cat >/etc/systemd/system/cred-broker.service <<EOF
[Unit]
Description=Credential Broker authorization gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python -m cred_broker
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$DATA_DIR
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryMax=256M
TasksMax=96

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now cred-broker.service >/dev/null
systemctl restart cred-broker.service
READY=0
for attempt in {1..20}; do
  if curl -fsS http://127.0.0.1:8765/healthz >/dev/null; then READY=1; break; fi
  sleep .5
done
if [[ $READY != 1 ]]; then echo "服务未就绪，请查看 journalctl -u cred-broker" >&2; exit 1; fi
echo "服务已就绪：http://127.0.0.1:8765"
if [[ $NEW_ADMIN == 1 ]]; then
  echo "首次管理令牌（请妥善保存）：$ADMIN_TOKEN"
fi
