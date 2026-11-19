#!/bin/sh
# ============================================================================
# Serv00 (FreeBSD) 一键部署：Delta 解卡服务
# ----------------------------------------------------------------------------
# 全程邮箱注册，不绑卡、不绑手机号。装完 24 小时常驻，不休眠。
#
# 用法（SSH 登录后，在自家 home 目录执行）：
#   sh serv00.sh
#
# 它会做四件事：
#   1. 建 Python 虚拟环境、装依赖（自动选 Serv00 上可用的 python3）
#   2. 从 GitHub 拉代码（或直接用随包带来的源码）
#   3. 生成带 crontab 自动拉起的常驻启动脚本
#   4. 提示你去面板申请一个对外端口，然后启动并自检
#
# 注意：Serv00 是 FreeBSD，不是 Linux；没有 systemd、没有 Docker、不能用 apt。
#       本脚本只用 FreeBSD 自带命令（sh / cron / nohup / python3），不碰那些。
# ============================================================================

set -e

PORT="${DELTA_PORT:-2233}"
WORKERS="${DELTA_WORKERS:-3}"          # 512MB 内存，3 线程够用
REPO="${DELTA_REPO:-https://github.com/3661221335/Yuki.git}"
APP_DIR="${DELTA_APP_DIR:-$HOME/delta}"
PY=""

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. 环境检查
# ---------------------------------------------------------------------------
say "Serv00 / FreeBSD 部署开始"

[ -d "$HOME" ] || die "找不到 home 目录：$HOME"

# Serv00 上 python 常见名字：python3.11 / python3.10 / python3.9 / python3
for c in python3.11 python3.10 python3.9 python3; do
    if command -v "$c" >/dev/null 2>&1; then
        PY="$(command -v "$c")"
        break
    fi
done
[ -n "$PY" ] || die "没找到 python3。先在面板 Servers → 你的账号 → Software 里启用 Python。"
say "Python: $PY ($($PY --version 2>&1))"

# ---------------------------------------------------------------------------
# 1. 拿代码
# ---------------------------------------------------------------------------
if [ -d "$APP_DIR/.git" ]; then
    say "代码已存在，拉取最新：$APP_DIR"
    (cd "$APP_DIR" && git pull --ff-only) || warn "git pull 失败，用现有代码继续"
elif [ -f "$APP_DIR/delta.py" ]; then
    say "用随包源码：$APP_DIR"
else
    say "从 GitHub 克隆：$REPO"
    mkdir -p "$(dirname "$APP_DIR")"
    git clone --depth 1 "$REPO" "$APP_DIR" || die "克隆失败。检查仓库地址，或把源码用 FTP 传到 $APP_DIR"
fi
cd "$APP_DIR"
[ -f delta.py ] || die "$APP_DIR 里没有 delta.py，代码不完整"

# ---------------------------------------------------------------------------
# 2. venv + 依赖
# ---------------------------------------------------------------------------
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    say "创建虚拟环境"
    "$PY" -m venv "$APP_DIR/venv" || die "venv 创建失败。Serv00 可能需要先运行: $PY -m ensurepip"
fi
VPY="$APP_DIR/venv/bin/python"
say "安装依赖"
"$VPY" -m pip install --quiet --upgrade pip || warn "pip 升级失败，忽略"
# 必需依赖；可选加速（uvloop/httptools/orjson）在 FreeBSD 上编译慢，先不装
"$VPY" -m pip install --quiet 'requests>=2.28' 'urllib3>=1.26' 'pycryptodome>=3.15' \
    'fastapi>=0.100' 'uvicorn>=0.23' || die "依赖安装失败。把上面报错发给管理员。"

# ---------------------------------------------------------------------------
# 3. 状态文件放自家目录（Serv00 有持久盘，不用像 Render 那样丢在 /tmp）
#    注意：端口也写进 .env，start.sh 运行时从这里读，换端口脚本改这里才生效。
# ---------------------------------------------------------------------------
printf 'DELTA_PORT=%s\nDELTA_CACHE_FILE=%s/state/keys.json\nDELTA_RETRYQ_FILE=%s/state/retryq.json\nDELTA_LINK_STATE=%s/state/link_state.json\nDELTA_NO_PROXY=1\nDELTA_WORKERS=%s\nPYTHONUNBUFFERED=1\n' \
    "$PORT" "$APP_DIR" "$APP_DIR" "$APP_DIR" "$WORKERS" > "$APP_DIR/.env"
mkdir -p "$APP_DIR/state" "$APP_DIR/log"

# ---------------------------------------------------------------------------
# 4. 常驻启动脚本（FreeBSD 没有 systemd，用 nohup + cron 兜底）
# ---------------------------------------------------------------------------
cat > "$APP_DIR/start.sh" <<EOF
#!/bin/sh
cd "$APP_DIR" || exit 1
set -a; . "$APP_DIR/.env"; set +a
# 端口只从 .env 读，换端口脚本改 .env 即可生效（绝不在这里写死）
: "\${DELTA_PORT:=$PORT}"
# 已经在跑就不重复起
if [ -f "$APP_DIR/log/delta.pid" ] && kill -0 "\$(cat "$APP_DIR/log/delta.pid")" 2>/dev/null; then
    exit 0
fi
nohup "$VPY" -u delta.py --host 0.0.0.0 --port "\$DELTA_PORT" --workers "$WORKERS" \
    >> "$APP_DIR/log/delta.log" 2>&1 &
echo \$! > "$APP_DIR/log/delta.pid"
EOF
chmod +x "$APP_DIR/start.sh"

cat > "$APP_DIR/stop.sh" <<EOF
#!/bin/sh
if [ -f "$APP_DIR/log/delta.pid" ]; then
    kill "\$(cat "$APP_DIR/log/delta.pid")" 2>/dev/null || true
    rm -f "$APP_DIR/log/delta.pid"
fi
EOF
chmod +x "$APP_DIR/stop.sh"

# crontab：每 3 分钟拉起一次（进程在就秒退，挂了就自动重启）；登录时也会拉起
CRON_LINE="*/3 * * * * $APP_DIR/start.sh >/dev/null 2>&1"
( crontab -l 2>/dev/null | grep -v "$APP_DIR/start.sh" || true; echo "$CRON_LINE" ) | crontab - \
    || warn "crontab 写入失败，去面板 Crontab 里手动加一条：$CRON_LINE"

say "常驻配置完成（crontab 每 3 分钟自动拉起）"

# ---------------------------------------------------------------------------
# 5. 端口提示
# ---------------------------------------------------------------------------
echo ""
echo "=========================================================================="
echo "还差一步：去 Serv00 面板申请对外端口"
echo "  面板 → Ports → Add port → 随机给你的端口（例如 18234）"
echo "  拿到后执行：  DELTA_PORT=18234 sh $APP_DIR/serv00-restart.sh"
echo "  然后用这个端口对外访问：http://<你的用户名>.serv00.net:<端口>/delta?url=..."
echo "=========================================================================="
echo ""

# ---------------------------------------------------------------------------
# 6. 启动 + 自检
# ---------------------------------------------------------------------------
say "启动服务（端口 $PORT）"
"$APP_DIR/start.sh"
sleep 3

say "本地自检"
if command -v curl >/dev/null 2>&1; then
    curl -sS --max-time 8 "http://127.0.0.1:$PORT/healthz" | head -c 300
    echo ""
else
    "$VPY" -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:$PORT/healthz', timeout=8).read().decode()[:300])"
fi

echo ""
say "部署完成"
echo "  日志:   tail -f $APP_DIR/log/delta.log"
echo "  停止:   sh $APP_DIR/stop.sh"
echo "  重启:   sh $APP_DIR/start.sh"
