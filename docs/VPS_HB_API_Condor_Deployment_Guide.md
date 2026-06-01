# AmethystFlame HB / API / Condor 双 VPS 部署说明

本文档用于把项目分开部署到两台 Ubuntu VPS：

- VPS-A：交易核心服务器，部署 `AmethystFlame_HB` 和 `AmethystFlame_hummingbot-api`。
- VPS-B：控制与 AI 代理服务器，部署 `AmethystFlame_condor`，连接 VPS-A 的 Hummingbot API。

建议先部署 VPS-A，确认 API 可用后，再部署 VPS-B。

## 文档位置与查看方式

为了方便在 VPS 上通过拉取仓库后直接查看部署方案，本说明文件会放在以下路径：

- `AmethystFlame_condor/docs/VPS_HB_API_Condor_Deployment_Guide.md`
- `AmethystFlame_HB/docs/VPS_HB_API_Condor_Deployment_Guide.md`
- `AmethystFlame_hummingbot-api/docs/VPS_HB_API_Condor_Deployment_Guide.md`

在 VPS 上查看文档的常用命令：

```bash
cd /opt/amethystflame/AmethystFlame_condor
cat docs/VPS_HB_API_Condor_Deployment_Guide.md
```

或：

```bash
less docs/VPS_HB_API_Condor_Deployment_Guide.md
```

如果你在 VPS-A 上：

```bash
cd /opt/amethystflame/AmethystFlame_hummingbot-api
less docs/VPS_HB_API_Condor_Deployment_Guide.md
```

如果你在 VPS-B 上：

```bash
cd /opt/amethystflame/AmethystFlame_condor
less docs/VPS_HB_API_Condor_Deployment_Guide.md
```

## 0. 安全与约定

不要把交易所 API Key、Telegram Token、VPS 密码写进 Git 仓库。

以下示例使用占位符：

```bash
API_VPS_IP="你的 API VPS 公网 IP"
CONDOR_VPS_IP="你的 Condor VPS 公网 IP"
API_USERNAME="admin"
API_PASSWORD="请改成强密码"
CONFIG_PASSWORD="请改成强密码"
TELEGRAM_TOKEN="你的 Telegram Bot Token"
ADMIN_USER_ID="你的 Telegram 数字用户 ID"
```

建议：

- 生产环境不要继续使用默认 `admin/admin`。
- 只开放必要端口：`22`、`8000`。数据库 `5432`、MQTT `1883` 不建议公网开放。
- API 的 `8000` 最好只允许 Condor VPS IP 访问。
- 完成部署后建议改为 SSH key 登录，并关闭 root 密码登录。

## 1. VPS-A：部署 HB + Hummingbot API

### 1.1 更新 Ubuntu、安装 Docker、配置时间同步

在 VPS-A 执行：

```bash
export DEBIAN_FRONTEND=noninteractive

dpkg --configure -a || true
apt-get -f install -y || true
apt-get update
apt-get upgrade -y

apt-get install -y ca-certificates curl gnupg git jq chrony docker.io python3

systemctl enable --now docker
systemctl enable --now chrony || true
systemctl restart chrony || true
timedatectl set-ntp true || true
```

安装 Docker Compose。如果系统仓库已有插件：

```bash
apt-get install -y docker-compose-plugin || true
```

如果 `docker compose version` 不可用，安装官方独立插件：

```bash
if ! docker compose version >/dev/null 2>&1; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64) COMPOSE_ARCH="x86_64" ;;
    aarch64|arm64) COMPOSE_ARCH="aarch64" ;;
    *) echo "Unsupported arch: $ARCH"; exit 1 ;;
  esac

  mkdir -p /usr/local/lib/docker/cli-plugins
  curl -fsSL "https://github.com/docker/compose/releases/download/v2.39.4/docker-compose-linux-${COMPOSE_ARCH}" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

docker --version
docker compose version
```

验证 VPS 与币安永续服务器时间差：

```bash
LOCAL_MS="$(date -u +%s%3N)"
BINANCE_MS="$(curl -fsS https://fapi.binance.com/fapi/v1/time | jq -r '.serverTime')"
DELTA_MS="$((LOCAL_MS - BINANCE_MS))"
ABS_DELTA_MS="${DELTA_MS#-}"
echo "local_ms=$LOCAL_MS binance_ms=$BINANCE_MS delta_ms=$DELTA_MS abs_delta_ms=$ABS_DELTA_MS"

if [ "$ABS_DELTA_MS" -gt 1000 ]; then
  echo "WARNING: time diff > 1000ms, check chrony/NTP"
else
  echo "OK: VPS time is within 1000ms of Binance futures server time"
fi
```

### 1.2 拉取项目代码

```bash
mkdir -p /opt/amethystflame
cd /opt/amethystflame

git clone https://github.com/fm0668/AmethystFlame_HB.git
git clone https://github.com/fm0668/AmethystFlame_hummingbot-api.git
```

后续更新代码用：

```bash
cd /opt/amethystflame/AmethystFlame_HB
git pull --ff-only origin master

cd /opt/amethystflame/AmethystFlame_hummingbot-api
git pull --ff-only origin main
```

### 1.3 配置并启动 Hummingbot API

进入 API 项目：

```bash
cd /opt/amethystflame/AmethystFlame_hummingbot-api
```

创建 `.env`：

```bash
cat > .env <<'EOF'
USERNAME=admin
PASSWORD=请改成强密码
CONFIG_PASSWORD=请改成强密码
DEBUG_MODE=false
BROKER_USERNAME=admin
BROKER_PASSWORD=password
LOGFIRE_ENVIRONMENT=prod
EOF
```

创建 `docker-compose.override.yml`，确保使用我们仓库源码构建 API 镜像，并且后台持续运行：

```bash
cat > docker-compose.override.yml <<'EOF'
services:
  hummingbot-api:
    build:
      context: .
      dockerfile: Dockerfile
    image: amethystflame/hummingbot-api:local
    restart: unless-stopped
  emqx:
    restart: unless-stopped
  postgres:
    restart: unless-stopped
EOF
```

启动：

```bash
docker compose up -d --build
```

验证：

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
curl -u admin:请改成强密码 http://127.0.0.1:8000/
curl -u admin:请改成强密码 -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/docs
```

预期：

- `hummingbot-api`、`hummingbot-postgres`、`hummingbot-broker` 都是 `Up`。
- API root 返回 `{"name":"Hummingbot API", ...}`。
- `/docs` 返回 HTTP `200`。

### 1.4 配置并启动 Hummingbot Client 容器

进入 HB 项目：

```bash
cd /opt/amethystflame/AmethystFlame_HB
```

不启用 Gateway 时：

```bash
cat > .compose.env <<'EOF'
COMPOSE_PROFILES=
EOF
```

启用后台持续运行：

```bash
cat > docker-compose.override.yml <<'EOF'
services:
  hummingbot:
    restart: unless-stopped
    environment:
      - CONFIG_PASSWORD=admin
EOF
```

启动：

```bash
docker compose up -d
```

验证：

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker logs --tail=80 hummingbot
```

需要进入 Hummingbot 控制台时：

```bash
docker attach hummingbot
```

退出 attach 但不停止容器：按 `Ctrl+P` 再按 `Ctrl+Q`。

## 2. VPS-A 防火墙建议

如果使用 `ufw`：

```bash
apt-get install -y ufw
ufw allow 22/tcp
ufw allow from CONDOR_VPS_IP to any port 8000 proto tcp
ufw --force enable
ufw status verbose
```

如果需要临时从本机浏览器访问 API 文档，可以临时放开你的固定 IP 到 `8000`，不要长期全网开放。

## 3. VPS-B：部署 Condor / AI 控制层

### 3.1 安装基础依赖

在 VPS-B 执行：

```bash
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get upgrade -y
apt-get install -y ca-certificates curl git jq tmux build-essential python3
```

安装 `uv`：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

安装 Node.js 22：

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs
node --version
npm --version
```

### 3.2 拉取 Condor 项目

```bash
mkdir -p /opt/amethystflame
cd /opt/amethystflame
git clone https://github.com/fm0668/AmethystFlame_condor.git
cd AmethystFlame_condor
```

后续更新代码用：

```bash
cd /opt/amethystflame/AmethystFlame_condor
git pull --ff-only origin main
```

### 3.3 安装 Condor 依赖

```bash
cd /opt/amethystflame/AmethystFlame_condor
export PATH="$HOME/.local/bin:$PATH"

uv sync
cd frontend
npm install
npm run build
cd ..
```

### 3.4 配置 Condor

创建 `.env`：

```bash
cat > .env <<'EOF'
TELEGRAM_TOKEN=你的 Telegram Bot Token
ADMIN_USER_ID=你的 Telegram 数字用户 ID
OPENAI_API_KEY=
OPENROUTER_API_KEY=
EOF
```

创建 `config.yml`，把 `API_VPS_IP`、用户名、密码改成 VPS-A 的真实配置：

```bash
cat > config.yml <<'EOF'
servers:
  main:
    host: API_VPS_IP
    port: 8000
    username: admin
    password: 请改成强密码
default_server: main
admin_id: TELEGRAM数字用户ID
users: {}
server_access: {}
chat_defaults: {}
audit_log: []
EOF
```

为 Condor MCP 的 Hummingbot API 工具写入默认连接配置：

```bash
mkdir -p ~/.hummingbot_mcp
cat > ~/.hummingbot_mcp/server.yml <<'EOF'
name: main
url: http://API_VPS_IP:8000
username: admin
password: 请改成强密码
EOF
```

### 3.5 使用 systemd 后台持续运行 Condor

创建服务：

```bash
cat > /etc/systemd/system/amethystflame-condor.service <<'EOF'
[Unit]
Description=AmethystFlame Condor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/amethystflame/AmethystFlame_condor
Environment=PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/root/.local/bin/uv run python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

启动：

```bash
systemctl daemon-reload
systemctl enable --now amethystflame-condor
systemctl status amethystflame-condor --no-pager
```

查看日志：

```bash
journalctl -u amethystflame-condor -f
```

## 4. VPS-B 到 VPS-A 的连通性验证

在 VPS-B 执行：

```bash
curl -u admin:请改成强密码 http://API_VPS_IP:8000/
curl -u admin:请改成强密码 -o /dev/null -w '%{http_code}\n' http://API_VPS_IP:8000/docs
```

预期：

- API root 能返回 JSON。
- `/docs` 返回 `200`。

## 5. USDC AI 网格功能验证

在 VPS-B 的 Condor 项目目录执行本地扫描帮助命令：

```bash
cd /opt/amethystflame/AmethystFlame_condor
uv run python trading_agents/usdc_perp_ai_grid/routines/binance_usdc_universe.py --help
uv run python trading_agents/usdc_perp_ai_grid/routines/scan_usdc_universe.py --help
```

在 VPS-A 验证 API 新增路由：

```bash
curl -u admin:请改成强密码 "http://127.0.0.1:8000/usdc-perp-market/universe?max_pairs=5" | jq
```

如果 Hummingbot API 已经能加载 `binance_perpetual` 数据源，可进一步验证候选池：

```bash
curl -u admin:请改成强密码 \
  -H 'Content-Type: application/json' \
  -d '{"connector_name":"binance_perpetual","trading_pairs":["BTC-USDC"],"interval":"1h","max_records":72,"order_book_depth":50}' \
  http://127.0.0.1:8000/usdc-perp-market/candidates | jq
```

## 6. 更新项目代码

VPS-A：

```bash
cd /opt/amethystflame/AmethystFlame_hummingbot-api
git pull --ff-only origin main
docker compose up -d --build

cd /opt/amethystflame/AmethystFlame_HB
git pull --ff-only origin master
docker compose up -d
```

VPS-B：

```bash
cd /opt/amethystflame/AmethystFlame_condor
git pull --ff-only origin main
export PATH="$HOME/.local/bin:$PATH"
uv sync
cd frontend && npm install && npm run build && cd ..
systemctl restart amethystflame-condor
```

## 7. 常用排障命令

VPS-A：

```bash
docker ps
docker compose -f /opt/amethystflame/AmethystFlame_hummingbot-api/docker-compose.yml \
  -f /opt/amethystflame/AmethystFlame_hummingbot-api/docker-compose.override.yml logs --tail=120 hummingbot-api
docker logs --tail=120 hummingbot
```

VPS-B：

```bash
systemctl status amethystflame-condor --no-pager
journalctl -u amethystflame-condor --tail=120 --no-pager
```

时间同步：

```bash
timedatectl
chronyc tracking
LOCAL_MS="$(date -u +%s%3N)"
BINANCE_MS="$(curl -fsS https://fapi.binance.com/fapi/v1/time | jq -r '.serverTime')"
echo "delta_ms=$((LOCAL_MS - BINANCE_MS))"
```
