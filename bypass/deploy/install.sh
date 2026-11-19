#!/usr/bin/env bash
# Delta 解卡服务 · Linux 一键安装
#
#   sudo bash deploy/install.sh
#
# 做的事：建用户 -> 拷代码 -> venv 装依赖 -> 自检 -> 装 systemd -> 启动
# 幂等：重复执行只会更新代码并重启服务。
set -euo pipefail

APP_DIR=/opt/delta
ENV_DIR=/etc/delta
SVC_USER=delta
PORT=2233
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 执行（sudo bash deploy/install.sh）"

# ---------- 1. 系统依赖 ----------
log "检查 Python"
if ! command -v python3 >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y && apt-get install -y python3 python3-venv python3-pip
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3 python3-pip
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3 python3-pip
    else
        die "找不到 python3，请先自行安装 3.9+"
    fi
fi
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
python3 -c 'import sys;sys.exit(0 if sys.version_info>=(3,8) else 1)' \
    || die "Python $PYV 太旧，需要 3.8+（建议 3.11）"
log "Python $PYV OK"

# ---------- 2. 用户与目录 ----------
if ! id "$SVC_USER" >/dev/null 2>&1; then
    log "创建系统用户 $SVC_USER"
    useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER" 2>/dev/null \
        || useradd --system --home "$APP_DIR" --shell /sbin/nologin "$SVC_USER"
fi

mkdir -p "$APP_DIR" "$ENV_DIR" /var/lib/delta

# ---------- 3. 拷代码（保留 venv 和缓存） ----------
log "同步代码到 $APP_DIR"
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
        --exclude 'venv' --exclude '__pycache__' --exclude '.git' \
        --exclude '.key_cache.json*' \
        --exclude '.link_source_state.json*' \
        --exclude '.delta_retryq.json*' \
        --exclude '.browser_profile' --exclude 'browser_debug' \
        --exclude '.profiles' --exclude '.discord_accounts.json' \
        "$SRC_DIR"/ "$APP_DIR"/
else
    for f in "$SRC_DIR"/*.py "$SRC_DIR"/*.md "$SRC_DIR"/*.txt; do
        [[ -e "$f" ]] && cp -f "$f" "$APP_DIR"/
    done
    mkdir -p "$APP_DIR/deploy"
    cp -f "$SRC_DIR"/deploy/* "$APP_DIR/deploy"/ 2>/dev/null || true
fi

# ---------- 4. 虚拟环境 ----------
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
    log "创建 venv"
    python3 -m venv "$APP_DIR/venv" || die "创建 venv 失败（缺 python3-venv？）"
fi
log "安装依赖"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# ---------- 5. 自检 ----------
log "运行自检（约 10~60s，会真解一条链接）"
set +e
(cd "$APP_DIR" && "$APP_DIR/venv/bin/python" preflight.py)
PF=$?
set -e
if [[ $PF -ne 0 ]]; then
    warn "自检未全绿（退出码 $PF）。常见原因："
    warn "  - 服务器不能直连上游：按 README 在 /etc/delta/delta.env 配 HTTPS_PROXY"
    warn "  - 已配全局代理但想直连：加 DELTA_NO_PROXY=1"
    warn "服务仍会安装并启动，但请先解决上面的失败项。"
fi

# ---------- 6. 环境文件 ----------
if [[ ! -f "$ENV_DIR/delta.env" ]]; then
    cat > "$ENV_DIR/delta.env" <<'EOF'
# Delta 解卡服务 环境配置
# 改完执行: systemctl restart delta
#
# 出网方式（三选一，都不写则按系统环境变量）
# DELTA_NO_PROXY=1                  # 强制直连，机房机器最常用
# DELTA_PROXY=http://127.0.0.1:1080 # 强制走指定代理
# HTTPS_PROXY=http://10.0.0.2:3128  # 标准变量，urllib3/requests 都认
#
# 接口鉴权（留空则不鉴权；设置后请求需带 X-Token 头或 ?token=）
# DELTA_TOKEN=change-me
#
# 缓存位置（默认 /var/lib/delta/keys.json）
# DELTA_CACHE_FILE=/var/lib/delta/keys.json
#
# 自愈 / 上游监控（内建在服务进程里，不需要另起脚本）
# DELTA_HEAL_INTERVAL=180          # 探测间隔秒数
# DELTA_HEAL=0                     # 只观测不自动修（/heal 仍可手动触发）
# DELTA_HEAL_AUTO=0                # 同上，粒度更细的开关
# DELTA_HEAL_COOLDOWN=60           # 同类失败的最小修复间隔
# DELTA_HEAL_HOURLY=30             # 每小时最多修复次数
# DELTA_ALERT_WEBHOOK=             # 状态变化时 POST 到这个地址
# DELTA_ALERT_LOG=/var/log/delta/gate.log
EOF
fi

chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR" /var/lib/delta
chmod 750 "$ENV_DIR" "$ENV_DIR/delta.env"

# ---------- 7. systemd ----------
log "安装 systemd 服务"
install -m 0644 "$APP_DIR/deploy/delta.service" /etc/systemd/system/delta.service
systemctl daemon-reload
systemctl enable delta >/dev/null 2>&1 || true
systemctl restart delta
sleep 2
systemctl --no-pager --lines=8 status delta || true

# ---------- 8. 收尾 ----------
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<EOF

$(printf '\033[1;32m')安装完成$(printf '\033[0m')

  健康检查   curl http://127.0.0.1:$PORT/healthz      # 含自愈状态与经验表
  解一条     curl -G "http://127.0.0.1:$PORT/delta" --data-urlencode "url=<链接>"
  手动自愈   curl -X POST http://127.0.0.1:$PORT/heal  # 探测+修复+复测
  看状态     sudo -u $SVC_USER $APP_DIR/venv/bin/python $APP_DIR/delta.py --status
  看日志     journalctl -u delta -f
  重启       systemctl restart delta
  配置       $ENV_DIR/delta.env

注意：服务和上游监控已经是同一个进程（delta.py），
不要再去单独起 probe_gate.py --watch，否则等于把探测翻倍。

$( [[ -n "${IP:-}" ]] && echo "  对外地址   http://$IP:$PORT/delta?url=..." )

如果开了防火墙，记得放行 $PORT：
  firewall-cmd --permanent --add-port=$PORT/tcp && firewall-cmd --reload   # CentOS/RHEL
  ufw allow $PORT/tcp                                                     # Ubuntu
EOF
